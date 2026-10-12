"""GET-only HTTP transport abstraction and a standard-library implementation."""

from __future__ import annotations

import http.client
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes
    # Header names are lower-cased.
    headers: Mapping[str, str] = field(default_factory=dict)


class TransportError(Exception):
    """Network-level failure raised by a transport."""


class TransportTimeout(TransportError):
    """Connection or read timeout raised by a transport."""


class HttpTransport(Protocol):
    """Performs a single HTTP GET. Implementations must not follow redirects."""

    def get(
        self, url: str, headers: Mapping[str, str], *, connect_timeout: float, read_timeout: float
    ) -> HttpResponse: ...


class StdlibHttpTransport:
    """``http.client`` transport with separate connect and read timeouts.

    Redirects are not followed, so the Authorization header is never sent to
    another location.
    """

    def __init__(self, ssl_context: ssl.SSLContext | None = None) -> None:
        self._ssl_context = ssl_context or ssl.create_default_context()

    def get(
        self, url: str, headers: Mapping[str, str], *, connect_timeout: float, read_timeout: float
    ) -> HttpResponse:
        parts = urlsplit(url)
        connection: http.client.HTTPConnection
        if parts.scheme == "https":
            connection = http.client.HTTPSConnection(
                parts.hostname, parts.port, timeout=connect_timeout, context=self._ssl_context
            )
        elif parts.scheme == "http":
            connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=connect_timeout)
        else:
            raise ValueError(f"unsupported URL scheme: {parts.scheme!r}")

        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        try:
            connection.connect()
            if connection.sock is not None:
                connection.sock.settimeout(read_timeout)
            connection.request("GET", target, headers=dict(headers))
            response = connection.getresponse()
            body = response.read()
            return HttpResponse(
                status=response.status,
                body=body,
                headers={name.lower(): value for name, value in response.getheaders()},
            )
        except TimeoutError as exc:
            raise TransportTimeout(f"request to {parts.hostname} timed out") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise TransportError(f"request to {parts.hostname} failed: {type(exc).__name__}: {exc}") from exc
        finally:
            connection.close()
