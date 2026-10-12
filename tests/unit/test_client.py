import base64
import json
import unittest

from testrail_stability_analyzer.api import (
    HttpResponse,
    TestRailAuthenticationError,
    TestRailAuthorizationError,
    TestRailClient,
    TestRailError,
    TestRailHTTPError,
    TestRailNetworkError,
    TestRailRateLimitError,
    TestRailRequestBudgetExceeded,
    TestRailResponseError,
    TestRailTimeoutError,
    TransportError,
    TransportTimeout,
)
from testrail_stability_analyzer.config import Credentials, settings_from_mapping

FAKE_USER = "user@example.invalid"
FAKE_KEY = "fake-api-key-0000"


class FakeTransport:
    """Records requests and replays queued responses or exceptions."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers, *, connect_timeout, read_timeout):
        self.calls.append({"url": url, "headers": dict(headers), "connect": connect_timeout, "read": read_timeout})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeCredentialProvider:
    def __init__(self):
        self.calls = 0

    def get_credentials(self):
        self.calls += 1
        return Credentials(username=FAKE_USER, api_key=FAKE_KEY)


def ok(payload, status=200, headers=None):
    return HttpResponse(status=status, body=json.dumps(payload).encode(), headers=headers or {})


def make_client(*responses, base_url="https://example.testrail.io", http=None):
    settings = settings_from_mapping(
        {"testrail": {"base_url": base_url, "project_id": 1}, "http": http or {"max_requests_per_execution": 50}}
    )
    transport = FakeTransport(*responses)
    credentials = FakeCredentialProvider()
    return TestRailClient(settings, credentials, transport), transport, credentials


class UrlConstructionTests(unittest.TestCase):
    def test_build_url(self):
        client, _, _ = make_client()
        self.assertEqual(client.build_url("get_case/42"), "https://example.testrail.io/index.php?/api/v2/get_case/42")
        self.assertEqual(
            client.build_url("get_cases/1", {"suite_id": 2, "status_id": "1,5", "is_completed": False}),
            "https://example.testrail.io/index.php?/api/v2/get_cases/1&suite_id=2&status_id=1,5&is_completed=0",
        )
        self.assertEqual(client.build_url("get_statuses"), "https://example.testrail.io/index.php?/api/v2/get_statuses")

    def test_base_url_with_subpath(self):
        client, _, _ = make_client(base_url="https://host.example/testrail/")
        self.assertEqual(client.build_url("get_projects"), "https://host.example/testrail/index.php?/api/v2/get_projects")

    def test_param_values_are_encoded(self):
        client, _, _ = make_client()
        self.assertTrue(client.build_url("get_runs/1", {"filter": "a&b c"}).endswith("&filter=a%26b+c"))

    def test_invalid_params_rejected(self):
        client, _, _ = make_client()
        for params in ({"Bad-Key": 1}, {"limit": 1.5}, {"limit": None}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                client.build_url("get_cases/1", params)


class ReadOnlyTests(unittest.TestCase):
    def test_client_exposes_no_write_methods(self):
        client, _, _ = make_client()
        public = {name for name in dir(client) if not name.startswith("_")}
        self.assertEqual(public, {"build_url", "get", "get_paginated", "requests_made"})
        for prefix in ("post", "put", "patch", "delete", "add", "update", "close_", "send"):
            self.assertFalse(any(name.startswith(prefix) for name in public), prefix)

    def test_non_get_endpoints_rejected_before_any_request(self):
        client, transport, credentials = make_client()
        endpoints = [
            "add_result/1",
            "add_results_for_cases/1",
            "update_case/1",
            "delete_run/1",
            "close_run/1",
            "get_case/../../add_result/1",
            "get_case/abc",
            "get_cases/1&suite_id=2",
            "get_cases?x=1",
            "/get_case/1",
            "GET_CASE/1",
            "",
        ]
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                client.get(endpoint)
        self.assertEqual(transport.calls, [])
        self.assertEqual(credentials.calls, 0)

    def test_get_uses_transport_get_only(self):
        client, transport, _ = make_client(ok({"id": 42}))
        self.assertEqual(client.get("get_case/42"), {"id": 42})
        self.assertEqual(len(transport.calls), 1)
        self.assertFalse(hasattr(transport, "post"))


class RequestTests(unittest.TestCase):
    def test_auth_header_and_timeouts(self):
        client, transport, credentials = make_client(
            ok({}), ok({}), http={"connect_timeout_seconds": 3, "read_timeout_seconds": 7}
        )
        client.get("get_project/1")
        client.get("get_project/1")

        call = transport.calls[0]
        expected = base64.b64encode(f"{FAKE_USER}:{FAKE_KEY}".encode()).decode()
        self.assertEqual(call["headers"]["Authorization"], f"Basic {expected}")
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertEqual((call["connect"], call["read"]), (3.0, 7.0))
        self.assertEqual(credentials.calls, 1)  # fetched lazily, once
        self.assertEqual(client.requests_made, 2)

    def test_credentials_not_fetched_until_first_request(self):
        _, _, credentials = make_client()
        self.assertEqual(credentials.calls, 0)

    def test_request_budget(self):
        client, transport, _ = make_client(ok({}), ok({}), ok({}), http={"max_requests_per_execution": 2})
        client.get("get_case/1")
        client.get("get_case/2")
        with self.assertRaises(TestRailRequestBudgetExceeded):
            client.get("get_case/3")
        self.assertEqual(len(transport.calls), 2)


class ErrorHandlingTests(unittest.TestCase):
    def assert_raises_from(self, response, error_type):
        client, _, _ = make_client(response)
        with self.assertRaises(error_type) as ctx:
            client.get("get_case/1")
        message = str(ctx.exception)
        self.assertNotIn(FAKE_KEY, message)
        self.assertNotIn("Basic ", message)
        return ctx.exception

    def test_authentication_error(self):
        error = self.assert_raises_from(ok({"error": "Authentication failed"}, status=401), TestRailAuthenticationError)
        self.assertEqual(error.status, 401)
        self.assertIn("Authentication failed", str(error))

    def test_authorization_error(self):
        error = self.assert_raises_from(ok({"error": "No access"}, status=403), TestRailAuthorizationError)
        self.assertEqual(error.status, 403)

    def test_rate_limit_error_with_retry_after(self):
        error = self.assert_raises_from(ok({}, status=429, headers={"retry-after": "30"}), TestRailRateLimitError)
        self.assertEqual(error.retry_after_seconds, 30.0)

    def test_rate_limit_error_without_retry_after(self):
        error = self.assert_raises_from(ok({}, status=429, headers={"retry-after": "soon"}), TestRailRateLimitError)
        self.assertIsNone(error.retry_after_seconds)

    def test_other_http_errors(self):
        error = self.assert_raises_from(ok({"error": "Field :case_id is not valid"}, status=400), TestRailHTTPError)
        self.assertEqual(error.status, 400)
        self.assertIn("case_id", str(error))
        error = self.assert_raises_from(HttpResponse(status=500, body=b"<html>oops</html>"), TestRailHTTPError)
        self.assertIn("no error details", str(error))
        error = self.assert_raises_from(HttpResponse(status=302, body=b""), TestRailHTTPError)
        self.assertIn("redirect not followed", str(error))

    def test_long_error_messages_truncated(self):
        error = self.assert_raises_from(ok({"error": "x" * 5000}, status=400), TestRailHTTPError)
        self.assertLess(len(str(error)), 600)

    def test_network_and_timeout_errors(self):
        self.assert_raises_from(TransportError("connection refused"), TestRailNetworkError)
        error = self.assert_raises_from(TransportTimeout("timed out"), TestRailTimeoutError)
        self.assertIsInstance(error, TestRailNetworkError)

    def test_invalid_json(self):
        self.assert_raises_from(HttpResponse(status=200, body=b"not json"), TestRailResponseError)

    def test_all_errors_share_base_class(self):
        for error_type in (
            TestRailAuthenticationError,
            TestRailAuthorizationError,
            TestRailRateLimitError,
            TestRailNetworkError,
            TestRailResponseError,
            TestRailRequestBudgetExceeded,
        ):
            self.assertTrue(issubclass(error_type, TestRailError))


class PaginationTests(unittest.TestCase):
    def test_follows_next_links(self):
        client, transport, _ = make_client(
            ok({"offset": 0, "limit": 2, "size": 2, "_links": {"next": "/api/v2/get_cases/1&limit=2&offset=2"},
                "cases": [{"id": 1}, {"id": 2}]}),
            ok({"offset": 2, "limit": 2, "size": 1, "_links": {"next": None}, "cases": [{"id": 3}]}),
        )
        items = list(client.get_paginated("get_cases/1", "cases", {"limit": 2}))
        self.assertEqual([item["id"] for item in items], [1, 2, 3])
        self.assertEqual(
            transport.calls[1]["url"], "https://example.testrail.io/index.php?/api/v2/get_cases/1&limit=2&offset=2"
        )

    def test_legacy_list_response(self):
        client, transport, _ = make_client(ok([{"id": 1}, {"id": 2}]))
        self.assertEqual(len(list(client.get_paginated("get_cases/1", "cases"))), 2)
        self.assertEqual(len(transport.calls), 1)

    def test_missing_collection_key(self):
        client, _, _ = make_client(ok({"runs": []}))
        with self.assertRaises(TestRailResponseError):
            list(client.get_paginated("get_cases/1", "cases"))

    def test_unsafe_next_links_rejected(self):
        for link in ("https://evil.example/api/v2/get_cases/1", "/api/v2/add_result/1", "/api/v2/get_cases/1&x=1#y"):
            client, transport, _ = make_client(ok({"cases": [], "_links": {"next": link}}))
            with self.subTest(link=link), self.assertRaises(TestRailResponseError):
                list(client.get_paginated("get_cases/1", "cases"))
            self.assertEqual(len(transport.calls), 1)

    def test_pagination_respects_request_budget(self):
        page = ok({"cases": [{"id": 1}], "_links": {"next": "/api/v2/get_cases/1&offset=1"}})
        client, _, _ = make_client(page, page, page, http={"max_requests_per_execution": 2})
        with self.assertRaises(TestRailRequestBudgetExceeded):
            list(client.get_paginated("get_cases/1", "cases"))


if __name__ == "__main__":
    unittest.main()
