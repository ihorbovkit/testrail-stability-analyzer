"""TestRail API exception hierarchy. Messages never include credentials or headers."""

from __future__ import annotations


class TestRailError(Exception):
    """Base class for all TestRail API errors."""

    # Number of attempts made before the error was raised (> 1 after retries).
    attempts: int = 1


class TestRailNetworkError(TestRailError):
    """The request could not be completed (DNS, connection, TLS, ...)."""


class TestRailTimeoutError(TestRailNetworkError):
    """The connection or read timed out."""


class TestRailHTTPError(TestRailError):
    """TestRail returned a non-success HTTP status."""

    def __init__(self, status: int, message: str, retry_after_seconds: float | None = None) -> None:
        self.status = status
        # Parsed ``Retry-After`` header, if the server sent a valid one.
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"HTTP {status}: {message}")


class TestRailAuthenticationError(TestRailHTTPError):
    """HTTP 401: credentials missing or rejected."""


class TestRailAuthorizationError(TestRailHTTPError):
    """HTTP 403: authenticated, but not permitted (or the API is disabled)."""


class TestRailRateLimitError(TestRailHTTPError):
    """HTTP 429: too many requests."""


class TestRailResponseError(TestRailError):
    """The response body was not valid JSON or had an unexpected shape."""


class TestRailRequestBudgetExceeded(TestRailError):
    """The configured maximum number of API requests per execution was reached."""
