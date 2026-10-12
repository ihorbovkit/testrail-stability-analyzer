"""Shared test doubles. No network access; credentials are fake placeholders."""

import json

from testrail_stability_analyzer.api import HttpResponse, TestRailClient
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


def make_client(*responses, base_url="https://example.testrail.io", http=None, sleep=None):
    """Retries are disabled unless ``http`` enables them; ``sleep`` never waits for real."""
    settings = settings_from_mapping(
        {
            "testrail": {"base_url": base_url, "project_id": 1},
            "http": {"max_requests_per_execution": 50, "max_retries": 0, **(http or {})},
        }
    )
    transport = FakeTransport(*responses)
    credentials = FakeCredentialProvider()
    client = TestRailClient(settings, credentials, transport, sleep=sleep or (lambda seconds: None))
    return client, transport, credentials
