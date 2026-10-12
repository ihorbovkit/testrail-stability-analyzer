import unittest
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

from testrail_stability_analyzer.api import (
    HttpResponse,
    TestRailAuthenticationError,
    TestRailAuthorizationError,
    TestRailHTTPError,
    TestRailNetworkError,
    TestRailRateLimitError,
    TestRailRequestBudgetExceeded,
    TestRailTimeoutError,
    TransportError,
    TransportTimeout,
)

from .fakes import FAKE_KEY, make_client, ok


def rate_limited(retry_after=None):
    headers = {} if retry_after is None else {"retry-after": retry_after}
    return ok({"error": "API rate limit exceeded"}, status=429, headers=headers)


class RetryClient:
    """Client with retries enabled and a recording, non-waiting sleep."""

    def __init__(self, *responses, **http):
        self.sleeps = []
        settings = {"max_retries": 3, "retry_backoff_seconds": 1.0, "max_retry_delay_seconds": 10.0, **http}
        self.client, self.transport, _ = make_client(*responses, http=settings, sleep=self.sleeps.append)


class RateLimitRetryTests(unittest.TestCase):
    def test_retry_after_header_is_respected(self):
        retry = RetryClient(rate_limited("2"), rate_limited("0"), ok({"id": 1}))
        self.assertEqual(retry.client.get("get_case/1"), {"id": 1})
        self.assertEqual(retry.sleeps, [2.0, 0.0])
        self.assertEqual(len(retry.transport.calls), 3)
        self.assertEqual(retry.client.requests_made, 3)

    def test_exponential_backoff_without_retry_after(self):
        retry = RetryClient(rate_limited(), rate_limited(), rate_limited(), ok({}), max_retry_delay_seconds=3.0)
        retry.client.get("get_case/1")
        self.assertEqual(retry.sleeps, [1.0, 2.0, 3.0])  # 4.0 capped at max_retry_delay_seconds

    def test_malformed_or_negative_retry_after_falls_back_to_backoff(self):
        for value in ("soon", "-5"):
            retry = RetryClient(rate_limited(value), ok({}))
            with self.subTest(value=value):
                retry.client.get("get_case/1")
                self.assertEqual(retry.sleeps, [1.0])

    def test_http_date_retry_after_beyond_maximum_is_not_waited_for(self):
        retry = RetryClient(rate_limited(format_datetime(datetime.now(UTC) + timedelta(hours=1), usegmt=True)), ok({}))
        with self.assertRaises(TestRailRateLimitError):
            retry.client.get("get_case/1")
        self.assertEqual(retry.sleeps, [])

    def test_retry_exhaustion_raises_last_error_with_context(self):
        retry = RetryClient(*[rate_limited("1")] * 4)
        with self.assertRaises(TestRailRateLimitError) as ctx:
            retry.client.get("get_case/1")
        error = ctx.exception
        self.assertEqual(error.attempts, 4)
        self.assertEqual(error.retry_after_seconds, 1.0)
        self.assertIn("rate limit", str(error))
        self.assertIn("gave up after 4 attempts", " ".join(error.__notes__))
        self.assertEqual(len(retry.transport.calls), 4)
        self.assertEqual(retry.sleeps, [1.0, 1.0, 1.0])

    def test_retry_after_above_maximum_is_not_waited_for(self):
        retry = RetryClient(rate_limited("3600"), ok({}))
        with self.assertRaises(TestRailRateLimitError) as ctx:
            retry.client.get("get_case/1")
        self.assertEqual(retry.sleeps, [])
        self.assertIn("exceeds http.max_retry_delay_seconds", " ".join(ctx.exception.__notes__))

    def test_retries_disabled(self):
        retry = RetryClient(rate_limited("1"), ok({}), max_retries=0)
        with self.assertRaises(TestRailRateLimitError) as ctx:
            retry.client.get("get_case/1")
        self.assertEqual(ctx.exception.attempts, 1)
        self.assertEqual(retry.sleeps, [])

    def test_retries_count_against_request_budget(self):
        retry = RetryClient(rate_limited("1"), rate_limited("1"), ok({}), max_requests_per_execution=2)
        with self.assertRaises(TestRailRequestBudgetExceeded) as ctx:
            retry.client.get("get_case/1")
        self.assertEqual(len(retry.transport.calls), 2)
        self.assertEqual(retry.client.requests_made, 2)
        self.assertIn("while retrying", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, TestRailRateLimitError)
        self.assertEqual(retry.sleeps, [1.0])  # no wait before the attempt that cannot be sent

    def test_no_wait_when_last_budgeted_request_fails(self):
        for response in (rate_limited("60"), rate_limited(), TransportError("connection reset")):
            retry = RetryClient(response, ok({}), max_requests_per_execution=1, max_retry_delay_seconds=60)
            with self.subTest(response=response), self.assertRaises(TestRailRequestBudgetExceeded):
                retry.client.get("get_case/1")
            self.assertEqual(retry.sleeps, [])
            self.assertEqual(len(retry.transport.calls), 1)

    def test_retry_during_pagination(self):
        retry = RetryClient(
            ok({"cases": [{"id": 1}], "_links": {"next": "/api/v2/get_cases/1&offset=1"}}),
            rate_limited("1"),
            ok({"cases": [{"id": 2}], "_links": {"next": None}}),
        )
        items = list(retry.client.get_paginated("get_cases/1", "cases"))
        self.assertEqual([item["id"] for item in items], [1, 2])
        self.assertEqual(retry.transport.calls[1]["url"], retry.transport.calls[2]["url"])


class OtherRetryTests(unittest.TestCase):
    def test_temporary_server_errors_are_retried(self):
        for status in (502, 503, 504):
            with self.subTest(status=status):
                retry = RetryClient(HttpResponse(status=status, body=b""), ok({"id": 1}))
                self.assertEqual(retry.client.get("get_case/1"), {"id": 1})
                self.assertEqual(retry.sleeps, [1.0])

    def test_service_unavailable_respects_retry_after(self):
        retry = RetryClient(HttpResponse(status=503, body=b"", headers={"retry-after": "4"}), ok({}))
        retry.client.get("get_case/1")
        self.assertEqual(retry.sleeps, [4.0])

    def test_network_failures_are_retried(self):
        retry = RetryClient(TransportError("connection reset"), TransportTimeout("timed out"), ok({"id": 1}))
        self.assertEqual(retry.client.get("get_case/1"), {"id": 1})
        self.assertEqual(retry.sleeps, [1.0, 2.0])

    def test_network_failure_exhaustion(self):
        retry = RetryClient(*[TransportTimeout("timed out")] * 3, max_retries=2)
        with self.assertRaises(TestRailTimeoutError) as ctx:
            retry.client.get("get_case/1")
        self.assertIsInstance(ctx.exception, TestRailNetworkError)
        self.assertEqual(ctx.exception.attempts, 3)

    def test_non_retryable_errors_fail_immediately(self):
        cases = [
            (ok({"error": "Authentication failed"}, status=401), TestRailAuthenticationError),
            (ok({"error": "No access"}, status=403), TestRailAuthorizationError),
            (ok({"error": "Field :run_id is not valid"}, status=400), TestRailHTTPError),
            (ok({"error": "Not found"}, status=404), TestRailHTTPError),
            (HttpResponse(status=500, body=b""), TestRailHTTPError),
        ]
        for response, error_type in cases:
            retry = RetryClient(response, ok({}))
            with self.subTest(status=response.status), self.assertRaises(error_type) as ctx:
                retry.client.get("get_case/1")
            self.assertEqual(len(retry.transport.calls), 1)
            self.assertEqual(retry.sleeps, [])
            self.assertNotIn(FAKE_KEY, str(ctx.exception))

    def test_auth_failure_after_retry_is_not_retried_further(self):
        retry = RetryClient(rate_limited("1"), ok({}, status=401), ok({}))
        with self.assertRaises(TestRailAuthenticationError) as ctx:
            retry.client.get("get_case/1")
        self.assertEqual(ctx.exception.attempts, 2)
        self.assertEqual(len(retry.transport.calls), 2)


if __name__ == "__main__":
    unittest.main()
