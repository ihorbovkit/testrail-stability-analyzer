"""Read-only TestRail API v2 client.

Only ``get_*`` endpoints are accepted and only HTTP GET is ever issued. The
client deliberately has no methods for adding, updating, or deleting data.

Typed endpoint methods return models from :mod:`..models`. Collection methods
return lazy iterators that follow pagination; requests are made while
iterating, but arguments are validated immediately.
"""

from __future__ import annotations

import base64
import email.utils
import itertools
import json
import math
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlencode

from .. import __version__
from ..config.auth import CredentialProvider
from ..config.settings import Settings
from ..models import (
    Case,
    CaseType,
    CustomField,
    ModelError,
    Plan,
    Project,
    Result,
    Run,
    Section,
    Status,
    Suite,
    Test,
)
from ..progress import (
    BudgetExhausted,
    ProgressListener,
    RequestFinished,
    RequestStarted,
    RetryScheduled,
    ignore_progress,
)
from .errors import (
    TestRailAuthenticationError,
    TestRailAuthorizationError,
    TestRailError,
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
# Largest page size TestRail accepts for the ``limit`` filter of bulk endpoints.
MAX_PAGE_LIMIT: Final = 250
# Temporary server-side conditions worth retrying (in addition to 429).
_RETRYABLE_STATUSES: Final = frozenset({502, 503, 504})

type QueryValue = str | int | bool
# A single ID or several IDs (sent comma-separated).
type IdFilter = int | Iterable[int]
# An aware datetime or a Unix timestamp in seconds.
type DateFilter = datetime | int


class TestRailClient:
    def __init__(
        self,
        settings: Settings,
        credential_provider: CredentialProvider,
        transport: HttpTransport | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        progress: ProgressListener = ignore_progress,
    ) -> None:
        """``progress`` receives request, retry, and budget events (endpoint paths and counts only)."""
        self._base_url = settings.testrail.base_url
        self._http = settings.http
        self._credential_provider = credential_provider
        self._transport = transport or StdlibHttpTransport()
        self._sleep = sleep
        self._authorization: str | None = None
        self._requests_made = 0
        self._progress = progress

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

    # -- typed endpoints: project metadata -----------------------------------

    def get_project(self, project_id: int) -> Project:
        return self._get_model(f"get_project/{_id(project_id, 'project_id')}", Project.from_api)

    def get_suites(self, project_id: int) -> list[Suite]:
        return self._list_models(f"get_suites/{_id(project_id, 'project_id')}", "suites", Suite.from_api)

    def get_sections(self, project_id: int, *, suite_id: int | None = None) -> Iterator[Section]:
        """Sections (case hierarchy) of a project. ``suite_id`` is required by TestRail for multi-suite projects."""
        params = _filters(suite_id=suite_id)
        return self._iter_models(f"get_sections/{_id(project_id, 'project_id')}", "sections", Section.from_api, params)

    def get_case_fields(self) -> list[CustomField]:
        """Custom case field definitions (all projects; see ``CustomField.applies_to_project``)."""
        return self._list_models("get_case_fields", "case_fields", CustomField.from_api)

    def get_result_fields(self) -> list[CustomField]:
        """Custom result field definitions (all projects; see ``CustomField.applies_to_project``)."""
        return self._list_models("get_result_fields", "result_fields", CustomField.from_api)

    def get_case_types(self) -> list[CaseType]:
        return self._list_models("get_case_types", "case_types", CaseType.from_api)

    def get_statuses(self) -> list[Status]:
        """System and custom execution statuses defined on the instance."""
        return self._list_models("get_statuses", "statuses", Status.from_api)

    # -- typed endpoints: cases, plans, runs, tests, results ------------------

    def get_cases(
        self,
        project_id: int,
        *,
        suite_id: int | None = None,
        section_id: int | None = None,
        created_after: DateFilter | None = None,
        created_before: DateFilter | None = None,
        created_by: IdFilter | None = None,
        updated_after: DateFilter | None = None,
        updated_before: DateFilter | None = None,
        updated_by: IdFilter | None = None,
        milestone_id: IdFilter | None = None,
        priority_id: IdFilter | None = None,
        template_id: IdFilter | None = None,
        type_id: IdFilter | None = None,
    ) -> Iterator[Case]:
        """Cases of a project. ``suite_id`` is required by TestRail for multi-suite projects."""
        params = _filters(
            suite_id=suite_id,
            section_id=section_id,
            created_after=created_after,
            created_before=created_before,
            created_by=created_by,
            updated_after=updated_after,
            updated_before=updated_before,
            updated_by=updated_by,
            milestone_id=milestone_id,
            priority_id=priority_id,
            template_id=template_id,
            type_id=type_id,
        )
        return self._iter_models(f"get_cases/{_id(project_id, 'project_id')}", "cases", Case.from_api, params)

    def get_plans(
        self,
        project_id: int,
        *,
        created_after: DateFilter | None = None,
        created_before: DateFilter | None = None,
        created_by: IdFilter | None = None,
        is_completed: bool | None = None,
        milestone_id: IdFilter | None = None,
    ) -> Iterator[Plan]:
        """Plans of a project, without entries (use :meth:`get_plan` for child runs)."""
        params = _filters(
            created_after=created_after,
            created_before=created_before,
            created_by=created_by,
            is_completed=is_completed,
            milestone_id=milestone_id,
        )
        return self._iter_models(f"get_plans/{_id(project_id, 'project_id')}", "plans", Plan.from_api, params)

    def get_plan(self, plan_id: int) -> Plan:
        """One plan with its entries and child runs."""
        return self._get_model(f"get_plan/{_id(plan_id, 'plan_id')}", Plan.from_api)

    def get_runs(
        self,
        project_id: int,
        *,
        created_after: DateFilter | None = None,
        created_before: DateFilter | None = None,
        created_by: IdFilter | None = None,
        is_completed: bool | None = None,
        milestone_id: IdFilter | None = None,
        suite_id: IdFilter | None = None,
        include_plan_runs: bool = False,
    ) -> Iterator[Run]:
        """Runs of a project.

        TestRail's ``get_runs`` only returns standalone runs. With
        ``include_plan_runs=True`` the client additionally lists plans
        (``get_plans``) and fetches each one (``get_plan``) to yield its runs,
        applying the same filters to each run client-side. This costs one extra
        request per plan. Standalone runs are yielded first.
        """
        params = _filters(
            created_after=created_after,
            created_before=created_before,
            created_by=created_by,
            is_completed=is_completed,
            milestone_id=milestone_id,
            suite_id=suite_id,
        )
        endpoint = f"get_runs/{_id(project_id, 'project_id')}"
        standalone = self._iter_models(endpoint, "runs", Run.from_api, params)
        if not include_plan_runs:
            return standalone
        return itertools.chain(standalone, self._iter_plan_runs(project_id, params))

    def get_run(self, run_id: int) -> Run:
        return self._get_model(f"get_run/{_id(run_id, 'run_id')}", Run.from_api)

    def get_tests(self, run_id: int, *, status_id: IdFilter | None = None) -> Iterator[Test]:
        """Test instances (one per case) of a run, with the run's current status."""
        params = _filters(status_id=status_id)
        return self._iter_models(f"get_tests/{_id(run_id, 'run_id')}", "tests", Test.from_api, params)

    def get_results_for_run(
        self,
        run_id: int,
        *,
        created_after: DateFilter | None = None,
        created_before: DateFilter | None = None,
        created_by: IdFilter | None = None,
        status_id: IdFilter | None = None,
        limit: int | None = None,
    ) -> Iterator[Result]:
        """All results of all tests in a run (newest first), including retries and triage updates.

        ``limit`` (1-250) is the page size requested from TestRail; pagination
        still continues while the iterator is consumed. Stop iterating early
        (e.g. ``itertools.islice``) to bound the number of records retrieved.
        """
        run_id = _id(run_id, "run_id")
        params = _filters(
            created_after=created_after, created_before=created_before, created_by=created_by, status_id=status_id
        )
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_LIMIT:
                raise ValueError(f"limit must be an integer between 1 and {MAX_PAGE_LIMIT}")
            params["limit"] = limit
        return self._iter_models(
            f"get_results_for_run/{run_id}", "results", lambda item: Result.from_api(item, run_id=run_id), params
        )

    def get_results_for_case(
        self, run_id: int, case_id: int, *, status_id: IdFilter | None = None
    ) -> Iterator[Result]:
        """Results for one case within one run (newest first). Prefer :meth:`get_results_for_run` in bulk."""
        run_id, case_id = _id(run_id, "run_id"), _id(case_id, "case_id")
        params = _filters(status_id=status_id)
        return self._iter_models(
            f"get_results_for_case/{run_id}/{case_id}",
            "results",
            lambda item: Result.from_api(item, run_id=run_id, case_id=case_id),
            params,
        )

    # -- helpers ---------------------------------------------------------------

    def _iter_plan_runs(self, project_id: int, params: Mapping[str, QueryValue]) -> Iterator[Run]:
        # Pre-filter plans only where that cannot exclude matching runs: a plan's
        # runs are not created before the plan. Completion is checked per run,
        # since runs in an active plan can be closed independently.
        plan_params = {key: params[key] for key in ("created_before",) if key in params}
        for summary in self._iter_models(f"get_plans/{project_id}", "plans", Plan.from_api, plan_params):
            for run in self.get_plan(summary.id).runs:
                if _run_matches(run, params):
                    yield run

    def _get_model[M](self, endpoint: str, parse: Callable[[object], M]) -> M:
        return _parse_model(parse, self.get(endpoint), endpoint)

    def _list_models[M](self, endpoint: str, collection_key: str, parse: Callable[[object], M]) -> list[M]:
        return list(self._iter_models(endpoint, collection_key, parse))

    def _iter_models[M](
        self,
        endpoint: str,
        collection_key: str,
        parse: Callable[[object], M],
        params: Mapping[str, QueryValue] | None = None,
    ) -> Iterator[M]:
        for item in self.get_paginated(endpoint, collection_key, params):
            yield _parse_model(parse, item, endpoint)

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
        """Send one logical request, retrying temporary failures within the configured bounds."""
        endpoint = url.partition("?")[2]
        # Path only (no query string) for progress events.
        path = endpoint.removeprefix(API_PREFIX).partition("&")[0]
        attempt = 0
        last_error: TestRailError | None = None
        while True:
            attempt += 1
            if self._requests_made >= self._http.max_requests_per_execution:
                self._progress(BudgetExhausted(self._requests_made, self._http.max_requests_per_execution))
                message = f"maximum of {self._http.max_requests_per_execution} API requests per execution reached"
                if last_error is not None:
                    message += f" while retrying {endpoint} after: {last_error}"
                raise TestRailRequestBudgetExceeded(message) from last_error
            try:
                response = self._send(url, endpoint, path, attempt)
                _raise_for_status(response)
            except (TestRailNetworkError, TestRailHTTPError) as exc:
                exc.attempts = attempt
                if not _is_retryable(exc):
                    raise
                delay = self._retry_delay(exc, attempt)
                if delay is None:
                    raise
                last_error = exc
                # Without budget for another attempt, the check above raises; don't wait first.
                if self._requests_made < self._http.max_requests_per_execution:
                    self._progress(
                        RetryScheduled(path, _retry_reason(exc), delay, attempt + 1, self._http.max_retries + 1)
                    )
                    self._sleep(delay)
                continue
            try:
                return json.loads(response.body)
            except ValueError:
                raise TestRailResponseError(f"{endpoint}: response is not valid JSON") from None

    def _retry_delay(self, error: TestRailError, attempt: int) -> float | None:
        """Seconds to wait before the next attempt, or ``None`` to give up (with a note on the error)."""
        if attempt > self._http.max_retries:
            if self._http.max_retries:
                error.add_note(f"gave up after {attempt} attempts (http.max_retries = {self._http.max_retries})")
            return None
        retry_after = getattr(error, "retry_after_seconds", None)
        if retry_after is not None:
            if retry_after > self._http.max_retry_delay_seconds:
                error.add_note(
                    f"not retried: Retry-After of {retry_after:g}s exceeds "
                    f"http.max_retry_delay_seconds = {self._http.max_retry_delay_seconds:g}"
                )
                return None
            return retry_after
        backoff = self._http.retry_backoff_seconds * 2 ** (attempt - 1)
        return min(backoff, self._http.max_retry_delay_seconds)

    def _send(self, url: str, endpoint: str, path: str, attempt: int) -> HttpResponse:
        headers = self._headers()
        self._requests_made += 1
        self._progress(RequestStarted(path, attempt, self._requests_made, self._http.max_requests_per_execution))
        try:
            response = self._transport.get(
                url,
                headers,
                connect_timeout=self._http.connect_timeout_seconds,
                read_timeout=self._http.read_timeout_seconds,
            )
        except TransportTimeout as exc:
            self._progress(RequestFinished(path, None))
            raise TestRailTimeoutError(f"{endpoint}: {exc}") from exc
        except TransportError as exc:
            self._progress(RequestFinished(path, None))
            raise TestRailNetworkError(f"{endpoint}: {exc}") from exc
        self._progress(RequestFinished(path, response.status))
        return response


def _parse_model[M](parse: Callable[[object], M], item: object, endpoint: str) -> M:
    try:
        return parse(item)
    except ModelError as exc:
        raise TestRailResponseError(f"{endpoint}: {exc}") from None


def _id(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _filters(**values: object) -> dict[str, QueryValue]:
    """Convert documented filter arguments to query parameters, skipping ``None``."""
    params: dict[str, QueryValue] = {}
    for key, value in values.items():
        if value is None:
            continue
        if key.endswith(("_after", "_before")):
            params[key] = _timestamp(value, key)
        elif key.startswith("is_"):
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be a boolean")
            params[key] = value
        else:
            params[key] = ",".join(str(item) for item in _ids(value, key))
    return params


def _ids(value: object, name: str) -> tuple[int, ...]:
    if isinstance(value, int):
        values: list[Any] = [value]
    elif isinstance(value, Iterable) and not isinstance(value, str | bytes):
        values = list(value)
    else:
        values = []
    if not values:
        raise ValueError(f"{name} must be a positive integer or a non-empty collection of them")
    return tuple(_id(item, name) for item in values)


def _timestamp(value: object, name: str) -> int:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be a timezone-aware datetime")
        value = math.floor(value.timestamp())
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be an aware datetime or a non-negative Unix timestamp")
    return value


def _run_matches(run: Run, params: Mapping[str, QueryValue]) -> bool:
    """Apply normalized ``get_runs`` filters to a run client-side (used for plan runs)."""
    created_on = math.floor(run.created_on.timestamp()) if run.created_on else None
    if "created_after" in params and (created_on is None or created_on < int(params["created_after"])):
        return False
    if "created_before" in params and (created_on is None or created_on > int(params["created_before"])):
        return False
    if "is_completed" in params and bool(run.is_completed) != params["is_completed"]:
        return False
    for key, actual in (("created_by", run.created_by), ("milestone_id", run.milestone_id), ("suite_id", run.suite_id)):
        if key in params and str(actual) not in str(params[key]).split(","):
            return False
    return True


def _retry_reason(error: TestRailError) -> str:
    """A fixed description of a retryable error, never the (untrusted) message text."""
    if isinstance(error, TestRailRateLimitError):
        return "rate limited (HTTP 429)"
    if isinstance(error, TestRailTimeoutError):
        return "timeout"
    if isinstance(error, TestRailHTTPError):
        return f"HTTP {error.status}"
    return "network error"


def _is_retryable(error: TestRailError) -> bool:
    if isinstance(error, TestRailNetworkError | TestRailRateLimitError):
        return True
    return isinstance(error, TestRailHTTPError) and error.status in _RETRYABLE_STATUSES


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
    if status == 503:
        raise TestRailHTTPError(status, message, _retry_after(response))
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


def _retry_after(response: HttpResponse, now: datetime | None = None) -> float | None:
    """Parse ``Retry-After`` as delay-seconds or an HTTP-date; ``None`` if absent or invalid."""
    value = response.headers.get("retry-after")
    if value is None:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        return _retry_after_date(value, now)
    # Negative delay-seconds are invalid, not "retry immediately".
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _retry_after_date(value: str, now: datetime | None) -> float | None:
    try:
        retry_at = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if retry_at.tzinfo is None:  # "-0000" zone: UTC per RFC 5322
        retry_at = retry_at.replace(tzinfo=UTC)
    # A date in the past means the server already permits a retry.
    return max((retry_at - (now or datetime.now(UTC))).total_seconds(), 0.0)
