"""Read-only TestRail API v2 client.

Only ``get_*`` endpoints are accepted and only HTTP GET is ever issued. The
client deliberately has no methods for adding, updating, or deleting data.
"""

from __future__ import annotations

import base64
import json
import math
import re
from collections.abc import Iterator, Mapping
from typing import Any, Final
from urllib.parse import urlencode

from .. import __version__
from ..config.auth import CredentialProvider
from ..config.settings import Settings
from .errors import (
    TestRailAuthenticationError,
    TestRailAuthorizationError,
    TestRailHTTPError,
    TestRailNetworkError,
    TestRailRateLimitError,
    TestRailRequestBudgetExceeded,
    TestRailResponseError,
    TestRailTimeoutError,
)
from .transport import HttpResponse, HttpTransport, StdlibHttpTransport, TransportError, TransportTimeout

API_PREFIX: Final = "/api/v2/"
_ENDPOINT_RE: Final = re.compile(r"^get_[a-z0-9_]+(?:/[0-9]+)*$")
_PARAM_RE: Final = re.compile(r"^[a-z][a-z0-9_]*$")
_MAX_ERROR_MESSAGE_LENGTH: Final = 500

type QueryValue = str | int | bool


class TestRailClient:
    def __init__(
        self,
        settings: Settings,
        credential_provider: CredentialProvider,
        transport: HttpTransport | None = None,
    ) -> None:
        self._base_url = settings.testrail.base_url
        self._http = settings.http
        self._credential_provider = credential_provider
        self._transport = transport or StdlibHttpTransport()
        self._authorization: str | None = None
        self._requests_made = 0

    @property
    def requests_made(self) -> int:
        return self._requests_made

    def build_url(self, endpoint: str, params: Mapping[str, QueryValue] | None = None) -> str:
        """Build ``<base>/index.php?/api/v2/<endpoint>&<params>`` for a read-only endpoint."""
        _validate_endpoint(endpoint)
        query = API_PREFIX + endpoint
        if params:
            query += "&" + _encode_params(params)
        return f"{self._base_url}/index.php?{query}"

    def get(self, endpoint: str, params: Mapping[str, QueryValue] | None = None) -> Any:
        """GET a single endpoint and return the decoded JSON payload."""
        return self._request(self.build_url(endpoint, params))

    def get_paginated(
        self, endpoint: str, collection_key: str, params: Mapping[str, QueryValue] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Yield items across all pages of a bulk endpoint (e.g. ``get_cases`` / ``"cases"``).

        Supports both paginated responses (TestRail 6.7+: ``{"<key>": [...],
        "_links": {"next": ...}}``) and legacy plain-list responses.
        """
        url: str | None = self.build_url(endpoint, params)
        while url is not None:
            payload = self._request(url)
            if isinstance(payload, list):
                yield from _validated_items(payload, endpoint)
                return
            if not isinstance(payload, dict) or not isinstance(payload.get(collection_key), list):
                raise TestRailResponseError(f"{endpoint}: response has no '{collection_key}' list")
            yield from _validated_items(payload[collection_key], endpoint)
            url = self._next_page_url(_next_link(payload, endpoint))

    def _next_page_url(self, link: object) -> str | None:
        if link is None:
            return None
        if not isinstance(link, str) or not link.startswith(API_PREFIX):
            raise TestRailResponseError("unexpected pagination link format")
        endpoint, _, query = link.removeprefix(API_PREFIX).partition("&")
        try:
            _validate_endpoint(endpoint)
        except ValueError:
            raise TestRailResponseError("pagination link points to a non read-only endpoint") from None
        if any(char in query for char in "#? \r\n"):
            raise TestRailResponseError("unexpected pagination link format")
        return f"{self._base_url}/index.php?{link}"

    def _headers(self) -> dict[str, str]:
        if self._authorization is None:
            credentials = self._credential_provider.get_credentials()
            token = base64.b64encode(f"{credentials.username}:{credentials.api_key}".encode()).decode("ascii")
            self._authorization = f"Basic {token}"
        return {
            "Authorization": self._authorization,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"testrail-stability-analyzer/{__version__}",
        }

    def _request(self, url: str) -> Any:
        if self._requests_made >= self._http.max_requests_per_execution:
            raise TestRailRequestBudgetExceeded(
                f"maximum of {self._http.max_requests_per_execution} API requests per execution reached"
            )
        headers = self._headers()
        self._requests_made += 1
        endpoint = url.partition("?")[2]
        try:
            response = self._transport.get(
                url,
                headers,
                connect_timeout=self._http.connect_timeout_seconds,
                read_timeout=self._http.read_timeout_seconds,
            )
        except TransportTimeout as exc:
            raise TestRailTimeoutError(f"{endpoint}: {exc}") from exc
        except TransportError as exc:
            raise TestRailNetworkError(f"{endpoint}: {exc}") from exc

        _raise_for_status(response)
        try:
            return json.loads(response.body)
        except ValueError:
            raise TestRailResponseError(f"{endpoint}: response is not valid JSON") from None


def _validated_items(items: list[Any], endpoint: str) -> list[dict[str, Any]]:
    if not all(isinstance(item, dict) for item in items):
        raise TestRailResponseError(f"{endpoint}: response contains non-object items")
    return items


def _next_link(payload: dict[str, Any], endpoint: str) -> object:
    """Return ``_links.next``; absent or null means last page, any other shape is an error."""
    links = payload.get("_links")
    if links is None:
        return None
    if not isinstance(links, dict):
        raise TestRailResponseError(f"{endpoint}: unexpected '_links' format")
    return links.get("next")


def _validate_endpoint(endpoint: str) -> None:
    if not isinstance(endpoint, str) or not _ENDPOINT_RE.match(endpoint):
        raise ValueError(f"not a read-only TestRail endpoint: {endpoint!r} (expected 'get_<name>[/<id>...]')")


def _encode_params(params: Mapping[str, QueryValue]) -> str:
    encoded: dict[str, str] = {}
    for key, value in params.items():
        if not isinstance(key, str) or not _PARAM_RE.match(key):
            raise ValueError(f"invalid query parameter name: {key!r}")
        if isinstance(value, bool):
            encoded[key] = "1" if value else "0"
        elif isinstance(value, int | str):
            encoded[key] = str(value)
        else:
            raise ValueError(f"invalid value type for query parameter {key!r}: {type(value).__name__}")
    # TestRail uses comma-separated lists (e.g. status_id=1,5).
    return urlencode(encoded, safe=",")


def _raise_for_status(response: HttpResponse) -> None:
    status = response.status
    if 200 <= status < 300:
        return
    message = _error_message(response)
    if status == 401:
        raise TestRailAuthenticationError(status, message)
    if status == 403:
        raise TestRailAuthorizationError(status, message)
    if status == 429:
        raise TestRailRateLimitError(status, message, _retry_after(response))
    if 300 <= status < 400:
        message = f"redirect not followed ({message})"
    raise TestRailHTTPError(status, message)


def _error_message(response: HttpResponse) -> str:
    try:
        payload = json.loads(response.body)
    except ValueError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        message = payload["error"]
    else:
        message = "no error details returned"
    return message[:_MAX_ERROR_MESSAGE_LENGTH]


def _retry_after(response: HttpResponse) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return max(seconds, 0.0) if math.isfinite(seconds) else None
