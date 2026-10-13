import unittest

from testrail_stability_analyzer.api import TestRailAuthenticationError
from testrail_stability_analyzer.collection import (
    Coverage,
    HistoricalCollector,
    RunDecision,
    RunDiscovery,
    SelectionKind,
)

from . import fake_testrail as fx
from .fake_testrail import FAILED, NOW, PASSED, FakeTestRail, days_ago

PROJECT_WIDE_LISTINGS = ("get_runs/1", "get_plans/1")


def settings(plan_ids=(), run_ids=(), **sections):
    collection = {"plan_ids": list(plan_ids), "run_ids": list(run_ids), **sections.pop("collection", {})}
    return fx.settings(collection=collection, **sections)


def collect(server, settings_obj, plans=None):
    client = fx.make_client(server, settings_obj)
    return HistoricalCollector(client, settings_obj, fx.resolver(), clock=lambda: NOW, plans=plans).collect()


def build_server():
    """Two plans and two standalone runs, all synthetic."""
    server = FakeTestRail()
    t = days_ago(3)
    server.add_plan(900, "App-A Regression", created=days_ago(4))
    server.add_plan_run(
        900,
        fx.run(501, "Regression", created=days_ago(4), config="Plat-X"),
        tests=[fx.test(1, 100, 501), fx.test(2, 101, 501)],
        results=[fx.result(11, 1, FAILED, t), fx.result(12, 1, PASSED, t + 60), fx.result(13, 2, PASSED, t)],
    )
    server.add_plan_run(
        900,
        fx.run(502, "Regression", created=days_ago(4), config="Plat-Y"),
        tests=[fx.test(3, 100, 502)],
        results=[fx.result(14, 3, PASSED, t)],
    )
    server.add_plan(901, "App-B Nightly", created=days_ago(2))
    server.add_plan_run(
        901,
        fx.run(503, "Nightly", created=days_ago(2), config="Plat-X"),
        tests=[fx.test(4, 200, 503)],
        results=[fx.result(15, 4, FAILED, t)],
    )
    server.add_run(
        fx.run(601, "App-A smoke Plat-X", created=days_ago(1)),
        tests=[fx.test(5, 100, 601)],
        results=[fx.result(16, 5, PASSED, days_ago(1))],
    )
    server.add_run(
        fx.run(602, "App-B smoke Plat-Y", created=days_ago(1)),
        tests=[fx.test(6, 200, 602)],
        results=[fx.result(17, 6, FAILED, days_ago(1))],
    )
    return server


def run_ids(report):
    return [run.run_id for run in report.discovery.runs]


class PlanSelectionTests(unittest.TestCase):
    def test_single_plan_resolves_child_runs_without_project_wide_discovery(self):
        server = build_server()
        report = collect(server, settings(plan_ids=[900]))

        self.assertEqual(run_ids(report), [501, 502])
        contexts = {(run.plan_id, run.plan_name) for run in report.discovery.runs}
        self.assertEqual(contexts, {(900, "App-A Regression")})
        self.assertTrue(report.discovery.targeted)
        self.assertEqual(report.discovery.selected_plan_ids, (900,))
        for listing in PROJECT_WIDE_LISTINGS:
            self.assertNotIn(listing, server.calls)
        self.assertEqual(server.calls.count("get_plan/900"), 1)
        self.assertEqual({o.case_id for o in report.outcomes}, {100, 101})

    def test_multiple_plans(self):
        server = build_server()
        report = collect(server, settings(plan_ids=[900, 901]))

        self.assertEqual(run_ids(report), [501, 502, 503])
        self.assertEqual({r.run_id: r.plan_id for r in report.discovery.runs}, {501: 900, 502: 900, 503: 901})

    def test_application_and_platform_classification_is_preserved(self):
        report = collect(build_server(), settings(plan_ids=[900, 901]))
        by_run = {r.run_id: r.classification for r in report.discovery.runs}

        self.assertEqual((by_run[501].application.name, by_run[501].platform.name), ("App A", "Platform X"))
        self.assertEqual((by_run[502].application.name, by_run[502].platform.name), ("App A", "Platform Y"))
        self.assertEqual((by_run[503].application.name, by_run[503].platform.name), ("App B", "Platform X"))

    def test_prefetched_plan_is_not_requested_again(self):
        server = build_server()
        client = fx.make_client(server, settings())
        plan = client.get_plan(900)
        server.calls.clear()

        report = collect(server, settings(plan_ids=[900]), plans={900: plan})

        self.assertNotIn("get_plan/900", server.calls)
        self.assertEqual(run_ids(report), [501, 502])

    def test_old_completed_plan_runs_are_included_when_selected(self):
        server = FakeTestRail()
        server.add_plan(900, "App-A Regression", created=days_ago(400), completed=days_ago(399))
        server.add_plan_run(
            900,
            fx.run(501, "Regression", created=days_ago(400), completed=days_ago(399), config="Plat-X"),
            tests=[fx.test(1, 100, 501)],
            results=[fx.result(11, 1, PASSED, days_ago(10))],
        )
        report = collect(server, settings(plan_ids=[900]))

        self.assertEqual(report.discovery.runs[0].decision, RunDecision.INCLUDED)
        self.assertEqual(report.outcomes[0].final.result.id, 11)

    def test_naming_and_filter_decisions_still_apply(self):
        report = collect(
            build_server(), settings(plan_ids=[900], filters={"platforms": ["Platform X"]})
        )
        decisions = {r.run_id: r.decision for r in report.discovery.runs}

        self.assertEqual(decisions, {501: RunDecision.INCLUDED, 502: RunDecision.EXCLUDED_BY_FILTER})


class RunSelectionTests(unittest.TestCase):
    def test_single_standalone_run(self):
        server = build_server()
        report = collect(server, settings(run_ids=[601]))

        self.assertEqual(run_ids(report), [601])
        self.assertIsNone(report.discovery.runs[0].plan_id)
        self.assertEqual(server.calls[:1], ["get_run/601"])
        for listing in PROJECT_WIDE_LISTINGS:
            self.assertNotIn(listing, server.calls)

    def test_multiple_runs(self):
        report = collect(build_server(), settings(run_ids=[601, 602]))

        self.assertEqual(run_ids(report), [601, 602])

    def test_plan_run_selected_directly_keeps_parent_plan_context(self):
        server = build_server()
        report = collect(server, settings(run_ids=[503]))

        run = report.discovery.runs[0]
        self.assertEqual((run.run_id, run.plan_id, run.plan_name), (503, 901, "App-B Nightly"))
        self.assertEqual(run.classification.application.name, "App B")  # matched on the plan name
        self.assertIn("get_plan/901", server.calls)

    def test_parent_plan_shared_by_selected_runs_is_fetched_once(self):
        server = build_server()
        collect(server, settings(run_ids=[501, 502]))

        self.assertEqual(server.calls.count("get_plan/900"), 1)

    def test_inaccessible_parent_plan_is_reported_but_run_is_kept(self):
        server = build_server()
        server.errors["get_plan/901"] = (403, "forbidden")
        report = collect(server, settings(run_ids=[503]))

        self.assertEqual(run_ids(report), [503])
        self.assertIsNone(report.discovery.runs[0].plan_name)
        self.assertTrue(any("parent plan 901" in note for note in report.limitations))


class MixedSelectionTests(unittest.TestCase):
    def test_plans_and_runs_are_combined_and_deduplicated(self):
        server = build_server()
        report = collect(server, settings(plan_ids=[900], run_ids=[501, 601]))

        self.assertEqual(run_ids(report), [501, 502, 601])
        self.assertNotIn("get_run/501", server.calls)  # already selected through plan 900
        self.assertEqual(report.discovery.runs[0].plan_id, 900)
        self.assertEqual(len([o for o in report.outcomes if o.run_id == 501]), 2)

    def test_duplicate_ids_are_collected_once(self):
        server = build_server()
        report = collect(server, settings(plan_ids=[900, 900], run_ids=[601, 601]))

        self.assertEqual(run_ids(report), [501, 502, 601])
        self.assertEqual(server.calls.count("get_plan/900"), 1)
        self.assertEqual(server.calls.count("get_run/601"), 1)
        self.assertEqual(server.calls.count("get_results_for_run/601"), 1)


class InvalidSelectionTests(unittest.TestCase):
    def test_invalid_plan_id_is_reported_and_others_collected(self):
        server = build_server()
        server.errors["get_plan/999"] = (400, "Field :plan_id is not a valid test plan.")
        report = collect(server, settings(plan_ids=[999, 901]))

        (invalid,) = report.discovery.invalid_selections
        self.assertEqual((invalid.kind, invalid.id, invalid.http_status), (SelectionKind.PLAN, 999, 400))
        self.assertIn("does not exist", invalid.reason)
        self.assertEqual(run_ids(report), [503])
        self.assertFalse(report.discovery.complete)
        self.assertTrue(report.is_partial)
        self.assertTrue(all(h.coverage is Coverage.PARTIAL for h in report.histories))

    def test_invalid_run_id(self):
        server = build_server()
        server.errors["get_run/999"] = (400, "Field :run_id is not a valid test run.")
        report = collect(server, settings(run_ids=[999]))

        (invalid,) = report.discovery.invalid_selections
        self.assertEqual((invalid.kind, invalid.id), (SelectionKind.RUN, 999))
        self.assertEqual(report.discovery.runs, ())

    def test_inaccessible_id(self):
        server = build_server()
        server.errors["get_plan/900"] = (403, "no access")
        report = collect(server, settings(plan_ids=[900]))

        self.assertIn("not accessible", report.discovery.invalid_selections[0].reason)

    def test_plan_of_another_project_is_rejected(self):
        server = build_server()
        server.plans[900][0]["project_id"] = 2
        report = collect(server, settings(plan_ids=[900]))

        (invalid,) = report.discovery.invalid_selections
        self.assertEqual(invalid.reason, "plan belongs to another project")
        self.assertEqual(report.discovery.runs, ())
        self.assertFalse(any(call.startswith("get_results_for_run") for call in server.calls))

    def test_run_of_another_project_is_rejected(self):
        server = build_server()
        server.standalone_runs[0]["project_id"] = 2
        report = collect(server, settings(run_ids=[601, 602]))

        self.assertEqual([i.id for i in report.discovery.invalid_selections], [601])
        self.assertEqual(run_ids(report), [602])

    def test_authentication_errors_are_not_treated_as_invalid_ids(self):
        server = build_server()
        server.errors["get_plan/900"] = (401, "Authentication failed")

        with self.assertRaises(TestRailAuthenticationError):
            collect(server, settings(plan_ids=[900]))


class SelectionCoverageTests(unittest.TestCase):
    def test_complete_selection_is_selection_limited_not_complete(self):
        report = collect(build_server(), settings(plan_ids=[900], history={"lookback_limit_days": 0}))

        self.assertTrue(report.histories)
        self.assertEqual({h.coverage for h in report.histories}, {Coverage.SELECTION_LIMITED})
        self.assertFalse(any(h.is_verified_latest for h in report.histories))
        self.assertFalse(report.is_partial)
        self.assertTrue(any("targeted collection" in note for note in report.limitations))

    def test_selection_with_lookback_mentions_both_limits(self):
        report = collect(build_server(), settings(plan_ids=[900]))
        history = report.histories[0]

        self.assertIs(history.coverage, Coverage.SELECTION_LIMITED)
        self.assertIn("results before the lookback start were not searched", history.coverage_notes)

    def test_request_budget_during_selection_is_partial(self):
        server = build_server()
        report = collect(server, settings(plan_ids=[900, 901], http={"max_requests_per_execution": 1}))

        self.assertTrue(report.discovery.budget_exhausted)
        self.assertEqual(run_ids(report), [501, 502])
        self.assertTrue(report.is_partial)


class BackwardCompatibilityTests(unittest.TestCase):
    def test_without_selection_discovery_is_project_wide(self):
        server = build_server()
        report = collect(server, fx.settings())

        self.assertFalse(report.discovery.targeted)
        self.assertIn("get_runs/1", server.calls)
        self.assertIn("get_plans/1", server.calls)
        self.assertEqual(run_ids(report), [501, 502, 503, 601, 602])
        self.assertEqual({h.coverage for h in report.histories}, {Coverage.LOOKBACK_LIMITED})
        self.assertEqual(report.max_results, None)
        self.assertFalse(report.results_capped)
        self.assertEqual(report.result_records_fetched, 7)
        self.assertFalse(any("limit" in params for params in server.params))


class SamplePlanTests(unittest.TestCase):
    def find(self, server, settings_obj=None, **kwargs):
        settings_obj = settings_obj or fx.settings()
        client = fx.make_client(server, settings_obj)
        lookback_start = NOW.replace(year=NOW.year - 1)
        return RunDiscovery(client, settings_obj).find_sample_plan(lookback_start, **kwargs), server

    def test_newest_plan_with_an_eligible_run_is_selected(self):
        selection, server = self.find(build_server())

        self.assertEqual(selection.plan.id, 901)
        self.assertEqual(selection.inspected_plan_ids, (901,))
        self.assertEqual(server.calls, ["get_plans/1", "get_plan/901"])
        self.assertIn("created_after", server.params[0])

    def test_plans_not_matching_naming_patterns_are_skipped_without_requests(self):
        selection, server = self.find(build_server(), fx.settings(naming={"plan_name_patterns": ["Regression"]}))

        self.assertEqual(selection.plan.id, 900)
        self.assertNotIn("get_plan/901", server.calls)
        self.assertIn("1 plan(s) do not match naming.plan_name_patterns", selection.notes)

    def test_plans_without_eligible_runs_are_skipped(self):
        selection, _ = self.find(build_server(), fx.settings(filters={"applications": ["App A"]}))

        self.assertEqual(selection.plan.id, 900)
        self.assertEqual(selection.inspected_plan_ids, (901, 900))
        self.assertTrue(any("plan 901 has no run eligible" in note for note in selection.notes))

    def test_no_unmatched_plan_is_selected(self):
        selection, _ = self.find(build_server(), fx.settings(naming={"plan_name_patterns": ["^Nothing$"]}))

        self.assertIsNone(selection.plan)
        self.assertEqual(selection.inspected_plan_ids, ())

    def test_candidate_limit(self):
        selection, server = self.find(
            build_server(), fx.settings(filters={"applications": ["App A"]}), max_candidates=1
        )

        self.assertIsNone(selection.plan)
        self.assertNotIn("get_plan/900", server.calls)
        self.assertIn("stopped after inspecting 1 candidate plan(s)", selection.notes)

    def test_plan_listing_is_paginated(self):
        server = build_server()
        server.page_size = 1
        selection, _ = self.find(server)

        self.assertEqual(server.calls[:2], ["get_plans/1", "get_plans/1"])
        self.assertEqual(selection.plans_listed, 2)
        self.assertEqual(selection.plan.id, 901)

    def test_request_budget_is_respected(self):
        selection, server = self.find(build_server(), fx.settings(http={"max_requests_per_execution": 1}))

        self.assertIsNone(selection.plan)
        self.assertTrue(selection.budget_exhausted)
        self.assertEqual(server.calls, ["get_plans/1"])


if __name__ == "__main__":
    unittest.main()
