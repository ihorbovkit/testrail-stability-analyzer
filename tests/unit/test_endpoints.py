import unittest
from datetime import UTC, datetime, timedelta, timezone

from testrail_stability_analyzer.api import TestRailResponseError
from testrail_stability_analyzer.models import (
    Case,
    CustomField,
    Plan,
    Project,
    Result,
    Run,
    Section,
    Status,
    Suite,
    Test,
)

from . import payloads
from .fakes import make_client, ok

BASE = "https://example.testrail.io/index.php?/api/v2/"


def paths(transport):
    return [call["url"].removeprefix(BASE) for call in transport.calls]


class GetOnlyTests(unittest.TestCase):
    def test_every_endpoint_method_issues_only_get_requests_to_get_endpoints(self):
        calls = {
            "get_project": (lambda c: c.get_project(1), payloads.project()),
            "get_suites": (lambda c: c.get_suites(1), []),
            "get_sections": (lambda c: list(c.get_sections(1)), []),
            "get_cases": (lambda c: list(c.get_cases(1)), []),
            "get_case_fields": (lambda c: c.get_case_fields(), []),
            "get_case_types": (lambda c: c.get_case_types(), []),
            "get_statuses": (lambda c: c.get_statuses(), []),
            "get_result_fields": (lambda c: c.get_result_fields(), []),
            "get_plans": (lambda c: list(c.get_plans(1)), []),
            "get_plan": (lambda c: c.get_plan(1), payloads.plan()),
            "get_runs": (lambda c: list(c.get_runs(1)), []),
            "get_run": (lambda c: c.get_run(1), payloads.run()),
            "get_tests": (lambda c: list(c.get_tests(1)), []),
            "get_results_for_run": (lambda c: list(c.get_results_for_run(1)), []),
            "get_results_for_case": (lambda c: list(c.get_results_for_case(1, 2)), []),
        }
        for name, (call, payload) in calls.items():
            client, transport, _ = make_client(ok(payload))
            with self.subTest(endpoint=name):
                call(client)
                self.assertEqual(len(transport.calls), 1)
                self.assertTrue(paths(transport)[0].startswith(name + "/") or paths(transport)[0] == name)
        self.assertFalse(any(hasattr(transport, method) for method in ("post", "put", "patch", "delete")))


class SingleObjectEndpointTests(unittest.TestCase):
    def test_get_project(self):
        client, transport, _ = make_client(ok(payloads.project()))
        self.assertIsInstance(client.get_project(1), Project)
        self.assertEqual(paths(transport), ["get_project/1"])

    def test_get_plan_with_child_runs(self):
        entry = payloads.plan_entry("e1", 900, [(501, "Config One", 11), (502, "Config Two", 12)])
        client, transport, _ = make_client(ok(payloads.plan(entries=[entry])))
        plan = client.get_plan(900)
        self.assertIsInstance(plan, Plan)
        self.assertEqual([run.id for run in plan.runs], [501, 502])
        self.assertEqual(paths(transport), ["get_plan/900"])

    def test_get_run(self):
        client, transport, _ = make_client(ok(payloads.run()))
        self.assertTrue(client.get_run(500).is_standalone)
        self.assertEqual(paths(transport), ["get_run/500"])

    def test_malformed_object_raises_response_error_with_endpoint(self):
        client, _, _ = make_client(ok({"name": "no id"}))
        with self.assertRaises(TestRailResponseError) as ctx:
            client.get_project(1)
        self.assertIn("get_project/1", str(ctx.exception))
        self.assertIn("project.id is required", str(ctx.exception))

    def test_invalid_ids_rejected_before_any_request(self):
        client, transport, _ = make_client()
        for value in (0, -1, True, "1", 1.0, None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    client.get_project(value)
                with self.assertRaises(ValueError):
                    client.get_results_for_case(1, value)
        self.assertEqual(transport.calls, [])


class MetadataEndpointTests(unittest.TestCase):
    def test_legacy_array_responses(self):
        client, transport, _ = make_client(
            ok([payloads.suite()]),
            ok(payloads.STATUSES),
            ok([{"id": 1, "name": "Automated", "is_default": True}]),
            ok([payloads.case_field()]),
            ok([payloads.case_field(system_name="custom_environment")]),
        )
        self.assertIsInstance(client.get_suites(1)[0], Suite)
        statuses = client.get_statuses()
        self.assertTrue(all(isinstance(status, Status) for status in statuses))
        self.assertEqual(len(statuses), 7)
        self.assertEqual(client.get_case_types()[0].name, "Automated")
        self.assertIsInstance(client.get_case_fields()[0], CustomField)
        self.assertEqual(client.get_result_fields()[0].system_name, "custom_environment")
        self.assertEqual(
            paths(transport), ["get_suites/1", "get_statuses", "get_case_types", "get_case_fields", "get_result_fields"]
        )

    def test_empty_collections(self):
        client, _, _ = make_client(ok([]), ok([]))
        self.assertEqual(client.get_statuses(), [])
        self.assertEqual(client.get_suites(1), [])

    def test_malformed_collection(self):
        client, _, _ = make_client(ok({"unexpected": True}))
        with self.assertRaises(TestRailResponseError):
            client.get_statuses()


class CollectionEndpointTests(unittest.TestCase):
    def test_get_cases_paginates_and_parses(self):
        client, transport, _ = make_client(
            ok({"cases": [payloads.case(1), payloads.case(2)], "_links": {"next": "/api/v2/get_cases/1&suite_id=10&offset=2"}}),
            ok({"cases": [payloads.case(3)], "_links": {"next": None}}),
        )
        cases = list(client.get_cases(1, suite_id=10))
        self.assertTrue(all(isinstance(case, Case) for case in cases))
        self.assertEqual([case.id for case in cases], [1, 2, 3])
        self.assertEqual(paths(transport), ["get_cases/1&suite_id=10", "get_cases/1&suite_id=10&offset=2"])

    def test_get_sections_paginates_and_parses(self):
        client, transport, _ = make_client(
            ok({"sections": [payloads.section(1, "Top")], "_links": {"next": "/api/v2/get_sections/1&suite_id=10&offset=1"}}),
            ok({"sections": [payloads.section(2, "Child", parent_id=1)], "_links": {"next": None}}),
        )
        sections = list(client.get_sections(1, suite_id=10))
        self.assertTrue(all(isinstance(section, Section) for section in sections))
        self.assertEqual([(s.id, s.parent_id) for s in sections], [(1, None), (2, 1)])
        self.assertEqual(paths(transport), ["get_sections/1&suite_id=10", "get_sections/1&suite_id=10&offset=1"])

    def test_get_sections_legacy_list_without_suite(self):
        client, transport, _ = make_client(ok([payloads.section(1, "Top")]))
        self.assertEqual([s.name for s in client.get_sections(1)], ["Top"])
        self.assertEqual(paths(transport), ["get_sections/1"])

    def test_collection_methods_are_lazy_but_validate_eagerly(self):
        client, transport, _ = make_client(ok({"cases": []}))
        iterator = client.get_cases(1)
        self.assertEqual(transport.calls, [])
        self.assertEqual(list(iterator), [])
        with self.assertRaises(ValueError):
            client.get_cases(1, created_after="2024-01-01")
        self.assertEqual(len(transport.calls), 1)

    def test_filter_parameters(self):
        after = datetime(2024, 1, 1, tzinfo=UTC)
        before = datetime(2024, 1, 2, 2, 0, tzinfo=timezone(timedelta(hours=2)))  # 2024-01-02T00:00Z
        client, transport, _ = make_client(ok({"plans": []}), ok({"tests": []}), ok({"results": []}), ok({"cases": []}))
        list(client.get_plans(1, created_after=after, created_before=before, created_by=[5, 6], is_completed=False))
        list(client.get_tests(500, status_id=(4, 5)))
        list(client.get_results_for_run(500, created_after=1704067200, status_id=5))
        list(client.get_cases(1, updated_after=after, type_id=3, priority_id=[1, 2]))
        self.assertEqual(
            paths(transport),
            [
                "get_plans/1&created_after=1704067200&created_before=1704153600&created_by=5,6&is_completed=0",
                "get_tests/500&status_id=4,5",
                "get_results_for_run/500&created_after=1704067200&status_id=5",
                "get_cases/1&updated_after=1704067200&priority_id=1,2&type_id=3",
            ],
        )

    def test_invalid_filters_rejected(self):
        client, transport, _ = make_client()
        invalid = [
            {"created_after": datetime(2024, 1, 1)},  # naive datetime
            {"created_after": datetime(1969, 12, 31, tzinfo=UTC)},  # negative timestamp
            {"created_after": -1},
            {"created_after": True},
            {"created_by": []},
            {"created_by": "5"},
            {"created_by": [5, 0]},
            {"is_completed": 1},
        ]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                client.get_plans(1, **kwargs)
        self.assertEqual(transport.calls, [])

    def test_multiple_results_for_one_test_are_all_preserved(self):
        results = [
            payloads.result(80003, status_id=1, created_on=payloads.T0 + 300),
            payloads.result(80002, status_id=None, created_on=payloads.T0 + 200, comment="triage"),
            payloads.result(80001, status_id=5, created_on=payloads.T0 + 100),
        ]
        client, transport, _ = make_client(ok({"results": results, "_links": {"next": None}}))
        parsed = list(client.get_results_for_run(500))
        self.assertTrue(all(isinstance(result, Result) for result in parsed))
        self.assertEqual([(r.id, r.test_id, r.status_id, r.run_id) for r in parsed],
                         [(80003, 7000, 1, 500), (80002, 7000, None, 500), (80001, 7000, 5, 500)])  # fmt: skip

    def test_get_results_for_case_sets_context(self):
        client, transport, _ = make_client(ok([payloads.result()]))
        (result,) = client.get_results_for_case(500, 100, status_id=[1, 5])
        self.assertEqual((result.run_id, result.case_id), (500, 100))
        self.assertEqual(paths(transport), ["get_results_for_case/500/100&status_id=1,5"])

    def test_get_tests(self):
        client, _, _ = make_client(ok({"tests": [payloads.test(), payloads.test(7001, case_id=101)]}))
        tests = list(client.get_tests(500))
        self.assertTrue(all(isinstance(test, Test) for test in tests))
        self.assertEqual([test.case_id for test in tests], [100, 101])

    def test_malformed_item_in_collection(self):
        client, _, _ = make_client(ok({"tests": [payloads.test(), {"id": 1}]}))
        with self.assertRaises(TestRailResponseError) as ctx:
            list(client.get_tests(500))
        self.assertIn("get_tests/500", str(ctx.exception))


class GetRunsTests(unittest.TestCase):
    def test_standalone_runs_only_by_default(self):
        client, transport, _ = make_client(ok({"runs": [payloads.run(500)], "_links": {"next": None}}))
        runs = list(client.get_runs(1, is_completed=True, suite_id=10))
        self.assertEqual([run.id for run in runs], [500])
        self.assertEqual(paths(transport), ["get_runs/1&is_completed=1&suite_id=10"])

    def test_include_plan_runs(self):
        plan_a = payloads.plan(900, entries=[payloads.plan_entry("e1", 900, [(501, "Config One", 11), (502, "Config Two", 12)])])
        plan_b = payloads.plan(901, entries=[payloads.plan_entry("e2", 901, [(503, None, 13)])])
        client, transport, _ = make_client(
            ok({"runs": [payloads.run(500)]}),
            ok({"plans": [payloads.plan(900), payloads.plan(901)]}),
            ok(plan_a),
            ok(plan_b),
        )
        runs = list(client.get_runs(1, include_plan_runs=True))
        self.assertEqual([(run.id, run.plan_id) for run in runs], [(500, None), (501, 900), (502, 900), (503, 901)])
        self.assertEqual(paths(transport), ["get_runs/1", "get_plans/1", "get_plan/900", "get_plan/901"])

    def test_include_plan_runs_applies_filters_to_plan_runs(self):
        entry = payloads.plan_entry("e1", 900, [(501, "Config One", 11), (502, "Config Two", 12)])
        entry["runs"][1]["suite_id"] = 99
        entry["runs"][0]["created_on"] = payloads.T0 - 10  # before created_after
        old_runs_entry = payloads.plan_entry("e2", 900, [(503, None, 13)])
        client, transport, _ = make_client(
            ok({"runs": []}),
            ok({"plans": [payloads.plan(900)]}),
            ok(payloads.plan(900, entries=[entry, old_runs_entry])),
        )
        runs = list(
            client.get_runs(1, created_after=payloads.T0, created_before=payloads.T0 + 100, suite_id=[10], is_completed=False, include_plan_runs=True)
        )
        self.assertEqual([run.id for run in runs], [503])
        # Only filters that cannot exclude matching runs are sent to get_plans.
        self.assertEqual(paths(transport)[1], f"get_plans/1&created_before={payloads.T0 + 100}")

    def test_completed_run_in_active_plan_is_found(self):
        entry = payloads.plan_entry("e1", 900, [(501, None, 11), (502, None, 12)])
        entry["runs"][0]["is_completed"] = True
        client, transport, _ = make_client(
            ok({"runs": []}), ok({"plans": [payloads.plan(900)]}), ok(payloads.plan(900, entries=[entry]))
        )
        runs = list(client.get_runs(1, is_completed=True, include_plan_runs=True))
        self.assertEqual([run.id for run in runs], [501])
        self.assertEqual(paths(transport)[:2], ["get_runs/1&is_completed=1", "get_plans/1"])


if __name__ == "__main__":
    unittest.main()
