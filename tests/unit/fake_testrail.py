"""An in-memory, read-only fake of the TestRail endpoints used by collection.

Applies the documented server-side filters (``created_after``/``created_before``,
``is_completed``, ``suite_id``) and the ``limit`` page size so discovery and
capped retrieval are exercised realistically. All data is synthetic.
"""

import json
from datetime import UTC, datetime
from urllib.parse import parse_qsl

from testrail_stability_analyzer.api import HttpResponse, TestRailClient
from testrail_stability_analyzer.config import settings_from_mapping
from testrail_stability_analyzer.models import Status, StatusResolver

from . import payloads
from .fakes import FakeCredentialProvider

NOW = datetime(2026, 6, 1, tzinfo=UTC)
NOW_TS = int(NOW.timestamp())
DAY = 86_400
API = "https://example.testrail.io/index.php?/api/v2/"

# Synthetic status set: system statuses plus custom ones mapped in ``settings()``.
STATUSES = [
    *payloads.STATUSES,
    payloads.status(8, "custom_status3", "Mystery", is_system=False),
]
STATUS_MAPPINGS = {"automation_issue": ["custom_status1"], "not_applicable": ["custom_status2"]}
PASSED, BLOCKED, UNTESTED, RETEST, FAILED, AUTOMATION_ISSUE, NOT_APPLICABLE, MYSTERY = 1, 2, 3, 4, 5, 6, 7, 8


def days_ago(days, seconds=0):
    return NOW_TS - int(days * DAY) + seconds


def settings(**sections):
    data = {
        "testrail": {"base_url": "https://example.testrail.io", "project_id": 1},
        "http": {"max_requests_per_execution": 500, "max_retries": 0},
        "mappings": {
            "applications": [
                {"name": "App A", "patterns": ["(?i)app-a"], "sources": ["plan_name", "run_name"]},
                {"name": "App B", "patterns": ["(?i)app-b"], "sources": ["plan_name", "run_name"]},
            ],
            "platforms": [
                {"name": "Platform X", "patterns": ["(?i)plat-x"], "sources": ["configuration", "run_name"]},
                {"name": "Platform Y", "patterns": ["(?i)plat-y"], "sources": ["configuration", "run_name"]},
            ],
            "statuses": STATUS_MAPPINGS,
        },
    }
    for key, value in sections.items():
        if key == "mappings":
            data["mappings"] = {**data["mappings"], **value}
        elif key == "http":
            data["http"] = {**data["http"], **value}
        else:
            data[key] = value
    return settings_from_mapping(data)


def resolver():
    return StatusResolver([Status.from_api(s) for s in STATUSES], settings().mappings.statuses)


def run(id, name, *, created=None, completed=None, config=None, plan_id=None, suite_id=10):
    return payloads.run(
        id,
        name=name,
        created_on=days_ago(5) if created is None else created,
        is_completed=completed is not None,
        completed_on=completed,
        config=config,
        plan_id=plan_id,
        suite_id=suite_id,
    )


def test(id, case_id, run_id, *, status_id=UNTESTED, **custom):
    return payloads.test(id, case_id=case_id, run_id=run_id, status_id=status_id, title=f"Case {case_id}", **custom)


def result(id, test_id, status_id, created, *, comment=None, version=None, **custom):
    return payloads.result(
        id, test_id=test_id, status_id=status_id, created_on=created, comment=comment, version=version, **custom
    )


class FakeTestRail:
    """Serves synthetic data. ``page_size`` > 0 splits list endpoints into pages."""

    def __init__(self, page_size=0):
        self.standalone_runs = []
        self.plans = {}  # plan id -> (plan payload without entries, [run payloads])
        self.tests = {}  # run id -> [test payloads]
        self.results = {}  # run id -> [result payloads]
        self.cases = {}  # suite id -> [case payloads]
        self.sections = {}  # suite id -> [section payloads]
        self.suites = [payloads.suite()]
        self.case_types = [{"id": 1, "name": "Automated", "is_default": False}, {"id": 3, "name": "Functional"}]
        self.page_size = page_size
        self.calls = []  # endpoint paths, e.g. "get_tests/501"
        self.params = []  # query parameters per call
        self.errors = {}  # endpoint path -> (HTTP status, error message)

    # -- data setup --------------------------------------------------------

    def add_run(self, run_payload, tests=(), results=()):
        self.standalone_runs.append(run_payload)
        self._add_content(run_payload, tests, results)
        return run_payload

    def add_plan(self, plan_id, name, *, created=None, completed=None):
        plan = payloads.plan(
            plan_id,
            name=name,
            created_on=days_ago(5) if created is None else created,
            is_completed=completed is not None,
            completed_on=completed,
        )
        self.plans[plan_id] = (plan, [])
        return plan

    def add_plan_run(self, plan_id, run_payload, tests=(), results=()):
        run_payload = {**run_payload, "plan_id": plan_id, "entry_id": f"entry-{plan_id}"}
        self.plans[plan_id][1].append(run_payload)
        self._add_content(run_payload, tests, results)
        return run_payload

    def _add_content(self, run_payload, tests, results):
        self.tests[run_payload["id"]] = list(tests)
        self.results[run_payload["id"]] = list(results)

    # -- transport ---------------------------------------------------------

    def get(self, url, headers, *, connect_timeout, read_timeout):
        assert url.startswith(API), url
        path, _, query = url.removeprefix(API).partition("&")
        params = dict(parse_qsl(query))
        self.calls.append(path)
        self.params.append(params)
        if path in self.errors:
            status, message = self.errors[path]
            return HttpResponse(status, json.dumps({"error": message}).encode(), {})
        name, *ids = path.split("/")
        handler = getattr(self, "_" + name)
        payload = handler(*map(int, ids), params)
        return HttpResponse(200, json.dumps(payload).encode(), {})

    def _page(self, key, items, params):
        offset = int(params.get("offset", 0))
        size = int(params["limit"]) if "limit" in params else self.page_size
        if not size:
            return {key: items, "_links": {"next": None}}
        page = items[offset : offset + size]
        more = offset + size < len(items)
        query = "&".join(f"{k}={v}" for k, v in params.items() if k != "offset")
        next_link = f"/api/v2/{self.calls[-1]}&{query}&offset={offset + size}" if more else None
        return {key: page, "_links": {"next": next_link.replace("&&", "&") if next_link else None}}

    def _get_runs(self, project_id, params):
        runs = [r for r in self.standalone_runs if _matches(r, params)]
        if "suite_id" in params:
            runs = [r for r in runs if str(r["suite_id"]) in params["suite_id"].split(",")]
        return self._page("runs", runs, params)

    def _get_plans(self, project_id, params):
        plans = [plan for plan, _ in self.plans.values() if _matches(plan, params)]
        return self._page("plans", plans, params)

    def _get_plan(self, plan_id, params):
        plan, runs = self.plans[plan_id]
        entries = [{"id": f"entry-{plan_id}", "suite_id": 10, "name": plan["name"], "runs": runs}] if runs else []
        return {**plan, "entries": entries}

    def _get_run(self, run_id, params):
        for run_payload in self.standalone_runs:
            if run_payload["id"] == run_id:
                return run_payload
        for _, runs in self.plans.values():
            for run_payload in runs:
                if run_payload["id"] == run_id:
                    return run_payload
        return None  # unreachable in tests: unknown IDs are configured in ``errors``

    def _get_statuses(self, params):
        return STATUSES

    def _get_tests(self, run_id, params):
        return self._page("tests", self.tests[run_id], params)

    def _get_results_for_run(self, run_id, params):
        results = sorted(self.results[run_id], key=lambda r: (r["created_on"] or 0, r["id"]), reverse=True)
        if "created_after" in params:
            results = [r for r in results if r["created_on"] is None or r["created_on"] > int(params["created_after"])]
        return self._page("results", results, params)

    def _get_cases(self, project_id, params):
        return self._page("cases", self.cases.get(int(params.get("suite_id", 0)), []), params)

    def _get_sections(self, project_id, params):
        return self._page("sections", self.sections.get(int(params.get("suite_id", 0)), []), params)

    def _get_suites(self, project_id, params):
        return self.suites

    def _get_case_types(self, params):
        return self.case_types


def _matches(item, params):
    created = item["created_on"]
    if "created_after" in params and created <= int(params["created_after"]):
        return False
    if "created_before" in params and created >= int(params["created_before"]):
        return False
    if "is_completed" in params and bool(item["is_completed"]) != (params["is_completed"] == "1"):
        return False
    return True


def make_client(server, settings_obj):
    return TestRailClient(settings_obj, FakeCredentialProvider(), server, sleep=lambda seconds: None)
