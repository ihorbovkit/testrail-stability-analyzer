"""Read-only TestRail API v2 access."""

from .client import TestRailClient
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

__all__ = [
    "HttpResponse",
    "HttpTransport",
    "StdlibHttpTransport",
    "TestRailAuthenticationError",
    "TestRailAuthorizationError",
    "TestRailClient",
    "TestRailError",
    "TestRailHTTPError",
    "TestRailNetworkError",
    "TestRailRateLimitError",
    "TestRailRequestBudgetExceeded",
    "TestRailResponseError",
    "TestRailTimeoutError",
    "TransportError",
    "TransportTimeout",
]
