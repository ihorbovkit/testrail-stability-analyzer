import dataclasses
import unittest

from testrail_stability_analyzer.collection import (
    Applicability,
    AvailabilityState,
    Coverage,
    DimensionState,
    HistoricalCollector,
)
from testrail_stability_analyzer.config import HistorySettings, StatusCategory

from . import fake_testrail as fx
from .fake_testrail import (
    AUTOMATION_ISSUE,
    BLOCKED,
    FAILED,
    NOT_APPLICABLE,
    NOW,
    PASSED,
    UNTESTED,
    FakeTestRail,
    days_ago,
)


def collect(server, settings=None):
    settings = settings or fx.settings()
    client = fx.make_client(server, settings)
    return HistoricalCollector(client, settings, fx.resolver(), clock=lambda: NOW).collect()


def history(report, case_id, application, platform):
    matches = [
        h
        for h in report.histories
        if h.key.case_id == case_id and h.key.application.name == application and h.key.platform.name == platform
    ]
    assert len(matches) == 1, matches
    return matches[0]


class WindowCollectionTests(unittest.TestCase):
    def setUp(self):
        server = FakeTestRail()
        server.add_plan(900, "App-A Regression", created=days_ago(10))
        t = days_ago(9)
        server.add_plan_run(
            900,
            fx.run(501, "Regression", created=days_ago(10), config="Plat-X"),
            tests=[fx.test(1, 100, 501, status_id=AUTOMATION_ISSUE), fx.test(2, 101, 501, status_id=PASSED)],
            results=[
                fx.result(11, 1, BLOCKED, t, comment="retry 1"),
                fx.result(12, 1, FAILED, t + 60),
                fx.result(13, 1, AUTOMATION_ISSUE, t + 3600, comment="selector changed"),
                fx.result(14, 1, None, t + 7200, comment="ticket filed"),
                fx.result(15, 2, FAILED, t),
                fx.result(16, 2, PASSED, t + 60, version="3.1.0"),
            ],
        )
        server.add_plan_run(
            900,
            fx.run(502, "Regression", created=days_ago(10), config="Plat-Y"),
            tests=[fx.test(3, 100, 502, status_id=PASSED)],
            results=[fx.result(17, 3, PASSED, t)],
        )
        self.server = server
        self.report = collect(server)

    def test_retries_collapse_to_one_outcome_per_run_with_evidence(self):
        h = history(self.report, 100, "App A", "Platform X")
        self.assertEqual(len(h.outcomes), 1)
        outcome = h.outcomes[0]
        self.assertEqual(outcome.category, StatusCategory.AUTOMATION_ISSUE)
        self.assertEqual([r.result.id for r in outcome.results], [11, 12, 13, 14])
        self.assertEqual(outcome.final.result.id, 13)
        self.assertEqual((outcome.run_id, outcome.plan_id, outcome.test_id), (501, 900, 1))
        self.assertEqual(h.state, AvailabilityState.RESULT_AVAILABLE)
        self.assertEqual(h.window, (outcome,))

    def test_app_platform_combinations_are_separate_histories(self):
        x = history(self.report, 100, "App A", "Platform X")
        y = history(self.report, 100, "App A", "Platform Y")
        self.assertEqual(y.latest_known.category, StatusCategory.PASSED)
        self.assertNotEqual(x.latest_known.run_id, y.latest_known.run_id)

    def test_subset_run_reports_not_included(self):
        h = history(self.report, 101, "App A", "Platform Y")
        self.assertEqual(h.state, AvailabilityState.NOT_INCLUDED_IN_RUN)
        self.assertEqual(h.outcomes, ())
        self.assertIsNone(h.latest_known)
        self.assertEqual(history(self.report, 101, "App A", "Platform X").latest_known.application_version, "3.1.0")

    def test_no_per_case_requests_and_comments_not_in_repr(self):
        self.assertFalse(any(call.startswith(("get_case/", "get_results_for_case/", "get_cases/")) for call in self.server.calls))
        self.assertNotIn("selector changed", repr(self.report))
        self.assertNotIn("ticket filed", repr(self.report))

    def test_lookback_limited_coverage_is_never_reported_as_verified(self):
        h = history(self.report, 100, "App A", "Platform X")
        self.assertEqual(h.coverage, Coverage.LOOKBACK_LIMITED)
        self.assertFalse(h.is_verified_latest)


class HistoryOrderingTests(unittest.TestCase):
    def test_sorted_by_final_result_time_and_capped(self):
        server = FakeTestRail()
        # Creation order deliberately differs from execution order.
        for index in range(12):
            run_id = 600 + index
            server.add_run(
                fx.run(run_id, "App-A Plat-X nightly", created=days_ago(40), completed=days_ago(1)),
                tests=[fx.test(run_id, 100, run_id, status_id=PASSED)],
                results=[fx.result(run_id, run_id, PASSED, days_ago(30 - index * 2))],
            )
        report = collect(server)
        h = history(report, 100, "App A", "Platform X")
        self.assertEqual(h.window_total, 12)
        self.assertEqual(len(h.window), 10)
        self.assertEqual(len(h.outcomes), 12)
        self.assertEqual([o.run_id for o in h.window], list(range(611, 601, -1)))
        executed = [o.executed_on for o in h.outcomes]
        self.assertEqual(executed, sorted(executed, reverse=True))
        self.assertEqual(h.latest_known.run_id, 611)

    def test_results_outside_window_stay_in_history_but_not_in_window(self):
        server = FakeTestRail()
        server.add_run(
            fx.run(700, "App-A Plat-X", created=days_ago(10)),
            tests=[fx.test(1, 100, 700, status_id=FAILED)],
            results=[fx.result(1, 1, FAILED, days_ago(9))],
        )
        server.add_run(
            fx.run(701, "App-A Plat-X", created=days_ago(100), completed=days_ago(99)),
            tests=[fx.test(2, 100, 701, status_id=PASSED)],
            results=[fx.result(2, 2, PASSED, days_ago(100))],
        )
        server.add_run(
            fx.run(702, "App-A Plat-X", created=days_ago(9)),
            tests=[fx.test(3, 100, 702, status_id=PASSED)],
            results=[fx.result(3, 3, PASSED, days_ago(8))],
        )
        h = history(collect(server), 100, "App A", "Platform X")
        self.assertEqual([o.run_id for o in h.window], [702, 700])
        self.assertEqual(h.window_total, 2)
        # The older execution within the lookback stays in the full history.
        self.assertEqual([o.run_id for o in h.outcomes], [702, 700, 701])


class LatestKnownTests(unittest.TestCase):
    def build(self, settings=None):
        server = FakeTestRail()
        server.add_run(
            fx.run(800, "App-A Plat-X current", created=days_ago(5)),
            tests=[fx.test(1, 100, 800, status_id=UNTESTED), fx.test(2, 101, 800, status_id=PASSED)],
            results=[fx.result(1, 2, PASSED, days_ago(4))],
        )
        for run_id, age in ((801, 100), (802, 150), (803, 300)):
            server.add_run(
                fx.run(run_id, "App-A Plat-X old", created=days_ago(age), completed=days_ago(age - 1)),
                tests=[fx.test(run_id, 100, run_id, status_id=FAILED)],
                results=[fx.result(run_id, run_id, FAILED, days_ago(age))],
            )
        return server, collect(server, settings)

    def test_latest_result_outside_window_found_from_actual_result_times(self):
        server, report = self.build()
        h = history(report, 100, "App A", "Platform X")
        self.assertEqual(h.latest_known.run_id, 801)
        self.assertEqual(h.state, AvailabilityState.RESULT_AVAILABLE)
        self.assertEqual(h.window, ())
        self.assertEqual(h.coverage, Coverage.LOOKBACK_LIMITED)
        # Every result-bearing run is scanned, newest actual result first.
        old_tests = [call for call in server.calls if call in ("get_tests/801", "get_tests/802", "get_tests/803")]
        self.assertEqual(old_tests, ["get_tests/801", "get_tests/802", "get_tests/803"])
        # The untested instance in the current run is kept alongside the older results.
        self.assertEqual([o.run_id for o in h.outcomes], [801, 802, 803, 800])

    def test_untimed_results_do_not_outrank_newer_dated_results(self):
        server = FakeTestRail()
        server.add_run(
            fx.run(820, "App-A Plat-X untimed", created=days_ago(300), completed=days_ago(299)),
            tests=[fx.test(1, 100, 820, status_id=FAILED)],
            results=[fx.result(1, 1, FAILED, days_ago(300)), fx.result(2, 1, None, None, comment="note")],
        )
        server.add_run(
            fx.run(821, "App-A Plat-X dated", created=days_ago(101), completed=days_ago(100)),
            tests=[fx.test(3, 100, 821, status_id=PASSED)],
            results=[fx.result(3, 3, PASSED, days_ago(100))],
        )
        # 4 discovery + 2 result requests + 1 get_tests.
        report = collect(server, fx.settings(http={"max_requests_per_execution": 7}))
        self.assertIn("get_tests/821", server.calls)
        self.assertNotIn("get_tests/820", server.calls)
        h = history(report, 100, "App A", "Platform X")
        self.assertEqual(h.latest_known.run_id, 821)
        # The unscanned run has an untimed result, so the latest stays uncertain.
        self.assertEqual(h.coverage, Coverage.PARTIAL)

    def test_run_with_unknown_completion_flag_is_treated_as_possibly_open(self):
        server = FakeTestRail()
        server.add_run(
            {**fx.run(830, "App-A Plat-X", created=days_ago(100)), "is_completed": None},
            tests=[fx.test(1, 100, 830, status_id=UNTESTED)],
            results=[],
        )
        report = collect(server)
        self.assertIn("get_tests/830", server.calls)
        self.assertEqual(history(report, 100, "App A", "Platform X").state, AvailabilityState.UNTESTED)

    def test_case_executed_only_outside_the_window_has_a_history(self):
        server = FakeTestRail()
        server.add_run(
            fx.run(810, "App-A Plat-X", created=days_ago(101), completed=days_ago(100)),
            tests=[fx.test(1, 300, 810, status_id=FAILED)],
            results=[fx.result(1, 1, FAILED, days_ago(100))],
        )
        h = history(collect(server), 300, "App A", "Platform X")
        self.assertEqual(h.latest_known.run_id, 810)
        self.assertEqual(h.latest_known.category, StatusCategory.FAILED)
        self.assertEqual(h.window, ())
        self.assertEqual(h.state, AvailabilityState.RESULT_AVAILABLE)

    def test_budget_exhaustion_loses_the_oldest_runs_first(self):
        # 9 requests as below, plus one get_tests: only the run with the newest result.
        server, report = self.build(fx.settings(http={"max_requests_per_execution": 10}))
        self.assertTrue(report.budget_exhausted)
        self.assertIn("get_tests/801", server.calls)
        self.assertNotIn("get_tests/802", server.calls)
        h = history(report, 100, "App A", "Platform X")
        self.assertEqual(h.latest_known.run_id, 801)
        # Unscanned runs only hold older results, so the latest is still backed.
        self.assertEqual(h.coverage, Coverage.LOOKBACK_LIMITED)
        self.assertTrue(any("request budget" in note for note in report.limitations))

    def test_scan_continues_past_runs_that_do_not_contain_the_case(self):
        server = FakeTestRail()
        server.add_run(
            fx.run(800, "App-A Plat-X current", created=days_ago(5)),
            tests=[fx.test(1, 100, 800)],
            results=[],
        )
        server.add_run(
            fx.run(801, "App-A Plat-X subset", created=days_ago(100), completed=days_ago(99)),
            tests=[fx.test(2, 999, 801, status_id=PASSED)],
            results=[fx.result(2, 2, PASSED, days_ago(100))],
        )
        server.add_run(
            fx.run(802, "App-A Plat-X older", created=days_ago(150), completed=days_ago(149)),
            tests=[fx.test(3, 100, 802, status_id=BLOCKED)],
            results=[fx.result(3, 3, BLOCKED, days_ago(150))],
        )
        h = history(collect(server), 100, "App A", "Platform X")
        self.assertEqual(h.latest_known.run_id, 802)
        self.assertEqual(h.latest_known.category, StatusCategory.BLOCKED)
        self.assertEqual(h.coverage, Coverage.LOOKBACK_LIMITED)

    def test_case_absent_from_a_platform_is_not_included(self):
        server = FakeTestRail()
        server.add_run(
            fx.run(800, "App-A Plat-X", created=days_ago(5)),
            tests=[fx.test(1, 100, 800, status_id=PASSED)],
            results=[fx.result(1, 1, PASSED, days_ago(4))],
        )
        server.add_run(
            fx.run(801, "App-A Plat-Y old", created=days_ago(100), completed=days_ago(99)),
            tests=[fx.test(2, 555, 801, status_id=PASSED)],
            results=[fx.result(2, 2, PASSED, days_ago(100))],
        )
        report = collect(server)
        h = history(report, 100, "App A", "Platform Y")
        self.assertEqual(h.state, AvailabilityState.NOT_INCLUDED_IN_RUN)
        self.assertEqual(h.coverage, Coverage.LOOKBACK_LIMITED)
        self.assertEqual(history(report, 555, "App A", "Platform Y").latest_known.run_id, 801)
        self.assertEqual(history(report, 555, "App A", "Platform X").state, AvailabilityState.NOT_INCLUDED_IN_RUN)

    def test_unlimited_lookback_with_complete_scan_is_verified(self):
        _, report = self.build(fx.settings(history={"lookback_limit_days": 0}))
        h = history(report, 100, "App A", "Platform X")
        self.assertEqual(h.coverage, Coverage.COMPLETE)
        self.assertTrue(h.is_verified_latest)
        self.assertIsNone(report.lookback_start)

    def test_request_budget_exhaustion_marks_coverage_partial(self):
        # 4 discovery requests + 2 for the current run + 3 result requests = 9.
        _, report = self.build(fx.settings(http={"max_requests_per_execution": 9}))
        self.assertTrue(report.budget_exhausted)
        h = history(report, 100, "App A", "Platform X")
        self.assertIsNone(h.latest_known)
        self.assertEqual(h.state, AvailabilityState.UNTESTED)
        self.assertEqual(h.coverage, Coverage.PARTIAL)
        self.assertFalse(h.is_verified_latest)
        self.assertTrue(any("request budget" in note for note in report.limitations))
        # Data collected before exhaustion is kept.
        self.assertEqual(history(report, 101, "App A", "Platform X").latest_known.category, StatusCategory.PASSED)

    def test_old_closed_plan_with_recent_triage_is_in_window(self):
        server = FakeTestRail()
        server.add_plan(900, "App-A old", created=days_ago(200), completed=days_ago(190))
        server.add_plan_run(
            900,
            fx.run(901, "Plat-X", created=days_ago(200), completed=days_ago(190)),
            tests=[fx.test(1, 100, 901, status_id=AUTOMATION_ISSUE)],
            results=[fx.result(1, 1, FAILED, days_ago(200)), fx.result(2, 1, AUTOMATION_ISSUE, days_ago(2))],
        )
        report = collect(server)
        h = history(report, 100, "App A", "Platform X")
        self.assertEqual(h.latest_known.final.result.id, 2)
        self.assertEqual(h.window, (h.latest_known,))
        self.assertEqual(h.latest_known.executed_on.timestamp(), days_ago(2))
        self.assertEqual([r.result.id for r in h.latest_known.results], [1, 2])

    def test_old_open_run_results_in_window_are_collected(self):
        server = FakeTestRail()
        server.add_run(
            fx.run(950, "App-A Plat-X long-running", created=days_ago(500)),
            tests=[fx.test(1, 100, 950, status_id=PASSED)],
            results=[fx.result(1, 1, FAILED, days_ago(400)), fx.result(2, 1, PASSED, days_ago(1))],
        )
        report = collect(server)
        h = history(report, 100, "App A", "Platform X")
        self.assertEqual([o.run_id for o in h.window], [950])
        self.assertTrue(h.latest_known.evidence_truncated)
        self.assertEqual([r.result.id for r in h.latest_known.results], [2])


class MissingStateTests(unittest.TestCase):
    def test_states_are_not_conflated(self):
        server = FakeTestRail()
        server.add_run(
            fx.run(1, "App-A Plat-X", created=days_ago(5)),
            tests=[
                fx.test(1, 100, 1, status_id=PASSED),
                fx.test(2, 101, 1, status_id=UNTESTED),
                fx.test(3, 102, 1, status_id=NOT_APPLICABLE),
            ],
            results=[fx.result(1, 1, PASSED, days_ago(4)), fx.result(2, 3, NOT_APPLICABLE, days_ago(4))],
        )
        server.add_run(fx.run(2, "App-A Plat-Y", created=days_ago(5)), tests=[fx.test(4, 103, 2)], results=[])
        server.add_run(
            fx.run(3, "App-B Plat-X", created=days_ago(5)),
            tests=[fx.test(5, 200, 3, status_id=PASSED)],
            results=[fx.result(3, 5, PASSED, days_ago(4))],
        )
        # Executed long ago: current status is Failed, but no result record within the lookback.
        server.add_run(
            fx.run(4, "App-B Plat-X ancient open", created=days_ago(500)),
            tests=[fx.test(6, 201, 4, status_id=FAILED)],
            results=[fx.result(4, 6, FAILED, days_ago(450))],
        )
        report = collect(server)
        states = {
            (h.key.case_id, h.key.application.name, h.key.platform.name): h.state for h in report.histories
        }
        self.assertEqual(states[(100, "App A", "Platform X")], AvailabilityState.RESULT_AVAILABLE)
        self.assertEqual(states[(101, "App A", "Platform X")], AvailabilityState.UNTESTED)
        self.assertEqual(states[(102, "App A", "Platform X")], AvailabilityState.NOT_APPLICABLE)
        self.assertEqual(states[(100, "App A", "Platform Y")], AvailabilityState.NOT_INCLUDED_IN_RUN)
        self.assertEqual(states[(103, "App A", "Platform Y")], AvailabilityState.UNTESTED)
        self.assertEqual(states[(200, "App B", "Platform Y")], AvailabilityState.NO_RUN_FOR_PLATFORM)
        self.assertEqual(states[(201, "App B", "Platform X")], AvailabilityState.NO_RESULT_WITHIN_LOOKBACK)
        self.assertTrue(all(h.applicability is Applicability.NOT_CONFIGURED for h in report.histories))
        # No execution records are fabricated for missing tests.
        self.assertEqual(len(report.outcomes), 6)

    def applicability_settings(self):
        return fx.settings(
            mappings={
                "applications": [
                    {"name": "App A", "patterns": ["(?i)app-a"], "applicability_values": ["A"]},
                    {"name": "App B", "patterns": ["(?i)app-b"]},
                ],
                "custom_fields": {"applicability": "custom_apps"},
            }
        )

    def test_applicability_metadata_states(self):
        server = FakeTestRail()
        server.add_run(
            fx.run(1, "App-A Plat-X", created=days_ago(5)),
            tests=[
                fx.test(1, 100, 1, status_id=PASSED, custom_apps=["A"]),
                fx.test(2, 101, 1, status_id=PASSED, custom_apps=["B"]),
                fx.test(3, 102, 1, status_id=PASSED, custom_apps=None),
            ],
            results=[fx.result(1, 1, PASSED, days_ago(4)), fx.result(2, 2, PASSED, days_ago(4)), fx.result(3, 3, PASSED, days_ago(4))],
        )
        server.add_run(fx.run(2, "App-A Plat-Y", created=days_ago(5)), tests=[], results=[])
        report = collect(server, self.applicability_settings())
        self.assertEqual(history(report, 100, "App A", "Platform Y").state, AvailabilityState.NOT_INCLUDED_IN_RUN)
        self.assertEqual(history(report, 101, "App A", "Platform Y").state, AvailabilityState.NOT_APPLICABLE)
        self.assertEqual(history(report, 102, "App A", "Platform Y").state, AvailabilityState.UNKNOWN_APPLICABILITY)
        # A real result is kept even when metadata says not applicable.
        h = history(report, 101, "App A", "Platform X")
        self.assertEqual((h.state, h.applicability), (AvailabilityState.RESULT_AVAILABLE, Applicability.NOT_APPLICABLE))
        self.assertFalse(any(call.startswith("get_cases/") for call in server.calls))

    def test_unfetched_applicability_metadata_marks_coverage_partial(self):
        server = FakeTestRail()
        server.cases[10] = [fx.payloads.case(100, custom_apps=["A"])]
        server.add_run(
            fx.run(1, "App-A Plat-X", created=days_ago(5)),
            tests=[fx.test(1, 100, 1, status_id=PASSED)],
            results=[fx.result(1, 1, PASSED, days_ago(4))],
        )
        server.add_run(fx.run(2, "App-A Plat-Y", created=days_ago(5)), tests=[], results=[])
        # 4 discovery + 2 per run; the get_cases fallback does not fit.
        settings = self.applicability_settings()
        settings = dataclasses.replace(settings, http=dataclasses.replace(settings.http, max_requests_per_execution=8))
        report = collect(server, settings)
        self.assertTrue(report.budget_exhausted)
        self.assertNotIn("get_cases/1", server.calls)
        for platform in ("Platform X", "Platform Y"):
            h = history(report, 100, "App A", platform)
            self.assertEqual(h.coverage, Coverage.PARTIAL)
            self.assertIn("applicability metadata could not be fetched (request budget)", h.coverage_notes)
            self.assertEqual(h.applicability, Applicability.UNKNOWN)
        self.assertEqual(history(report, 100, "App A", "Platform Y").state, AvailabilityState.UNKNOWN_APPLICABILITY)

    def test_applicability_falls_back_to_bulk_cases_once_per_suite(self):
        server = FakeTestRail()
        server.cases[10] = [fx.payloads.case(100, custom_apps=["B"]), fx.payloads.case(101, custom_apps=["A"])]
        server.add_run(
            fx.run(1, "App-A Plat-X", created=days_ago(5)),
            tests=[fx.test(1, 100, 1), fx.test(2, 101, 1)],
            results=[],
        )
        server.add_run(fx.run(2, "App-A Plat-Y", created=days_ago(5)), tests=[], results=[])
        report = collect(server, self.applicability_settings())
        self.assertEqual(sum(call.startswith("get_cases/") for call in server.calls), 1)
        self.assertEqual(history(report, 100, "App A", "Platform Y").state, AvailabilityState.NOT_APPLICABLE)
        self.assertEqual(history(report, 101, "App A", "Platform Y").applicability, Applicability.APPLICABLE)


class UnidentifiedAndFilteredRunTests(unittest.TestCase):
    def test_unidentified_runs_form_separate_groups(self):
        server = FakeTestRail()
        for run_id in (1, 2):
            server.add_run(
                fx.run(run_id, "Weekly sign-off", created=days_ago(5)),
                tests=[fx.test(run_id, 100, run_id, status_id=FAILED)],
                results=[fx.result(run_id, run_id, FAILED, days_ago(4))],
            )
        report = collect(server)
        keys = [h.key for h in report.histories]
        self.assertEqual([(k.case_id, k.run_id) for k in keys], [(100, 1), (100, 2)])
        self.assertTrue(all(k.application.state is DimensionState.UNRESOLVED for k in keys))
        self.assertTrue(any("grouped per run" in note for note in report.limitations))

    def two_runs_of_case_100(self):
        server = FakeTestRail()
        for run_id in (1, 2):
            server.add_run(
                fx.run(run_id, f"Nightly {run_id}", created=days_ago(5)),
                tests=[fx.test(run_id, 100, run_id, status_id=PASSED)],
                results=[fx.result(run_id, run_id, PASSED, days_ago(4))],
            )
        return server

    def test_unconfigured_application_and_platform_are_not_grouped(self):
        settings = fx.settings(mappings={"applications": [], "platforms": []})
        report = collect(self.two_runs_of_case_100(), settings)
        keys = [h.key for h in report.histories]
        self.assertEqual([(k.case_id, k.run_id) for k in keys], [(100, 1), (100, 2)])
        self.assertTrue(all(k.platform.state is DimensionState.NOT_CONFIGURED for k in keys))
        self.assertTrue(all(len(h.window) == 1 for h in report.histories))

    def test_catch_all_mappings_group_runs_explicitly(self):
        catch_all = {"applications": [{"name": "Product", "patterns": [".*"]}], "platforms": [{"name": "Any", "patterns": [".*"]}]}
        report = collect(self.two_runs_of_case_100(), fx.settings(mappings=catch_all))
        (h,) = report.histories
        self.assertTrue(h.key.is_identified)
        self.assertEqual([o.run_id for o in h.window], [2, 1])

    def test_filtered_unclassified_runs_are_reported_but_not_downloaded(self):
        server = FakeTestRail()
        server.add_run(fx.run(1, "App-A Plat-X", created=days_ago(5)), tests=[], results=[])
        server.add_run(fx.run(2, "Unknown naming", created=days_ago(5)), tests=[], results=[])
        report = collect(server, fx.settings(filters={"applications": ["App A"]}))
        self.assertIn("get_results_for_run/1", server.calls)
        self.assertNotIn("get_results_for_run/2", server.calls)
        self.assertEqual([r.run_id for r in report.discovery.unclassified], [2])
        self.assertTrue(any("not collected" in note for note in report.limitations))


class EdgeCaseTests(unittest.TestCase):
    def test_empty_project_plan_and_run(self):
        self.assertEqual(collect(FakeTestRail()).histories, ())
        server = FakeTestRail()
        server.add_plan(900, "App-A empty plan")
        server.add_run(fx.run(1, "App-A Plat-X empty", created=days_ago(5)), tests=[], results=[])
        report = collect(server)
        self.assertEqual(report.histories, ())
        self.assertEqual([(s.run_id, s.results_fetched, s.tests_fetched) for s in report.scans], [(1, True, True)])

    def test_pagination_orphans_and_conflicting_duplicates(self):
        server = FakeTestRail(page_size=2)
        server.add_run(
            fx.run(1, "App-A Plat-X", created=days_ago(5)),
            tests=[fx.test(1, 100, 1, status_id=PASSED), fx.test(2, 101, 1), fx.test(3, 102, 1)],
            results=[
                fx.result(1, 1, FAILED, days_ago(4)),
                fx.result(2, 1, PASSED, days_ago(3)),
                fx.result(3, 99, FAILED, days_ago(3)),
                fx.result(2, 1, FAILED, days_ago(3)),
            ],
        )
        report = collect(server)
        self.assertEqual(history(report, 100, "App A", "Platform X").latest_known.final.result.id, 2)
        self.assertEqual([o.result.id for o in report.orphan_results], [3])
        self.assertEqual([(c.run_id, c.result_id) for c in report.conflicts], [(1, 2)])
        self.assertGreater(sum(call.startswith("get_results_for_run/1") for call in server.calls), 1)

    def test_zero_lookback_in_constructed_settings_means_no_limit(self):
        server = FakeTestRail()
        server.add_run(fx.run(1, "App-A Plat-X", created=days_ago(5)), tests=[], results=[])
        settings = dataclasses.replace(fx.settings(), history=HistorySettings(lookback_limit_days=0))
        report = collect(server, settings)
        self.assertIsNone(report.lookback_start)
        self.assertEqual([p for c, p in zip(server.calls, server.params) if c == "get_results_for_run/1"], [{}])

    def test_results_are_requested_from_lookback_start(self):
        server = FakeTestRail()
        server.add_run(fx.run(1, "App-A Plat-X", created=days_ago(5)), tests=[], results=[])
        report = collect(server)
        lookback = int(report.lookback_start.timestamp())
        self.assertEqual(lookback, days_ago(365))
        params = [p for c, p in zip(server.calls, server.params) if c == "get_results_for_run/1"]
        self.assertEqual(params, [{"created_after": str(lookback - 1)}])

    def test_result_exactly_at_the_lookback_start_is_collected(self):
        server = FakeTestRail()
        server.add_run(
            fx.run(1, "App-A Plat-X long-running", created=days_ago(400)),
            tests=[fx.test(1, 100, 1, status_id=FAILED)],
            results=[fx.result(1, 1, FAILED, days_ago(365)), fx.result(2, 1, PASSED, days_ago(365, seconds=-1))],
        )
        h = history(collect(server), 100, "App A", "Platform X")
        self.assertEqual([r.result.id for r in h.latest_known.results], [1])

    def test_budget_exhausted_during_discovery_is_reported(self):
        report = collect(FakeTestRail(), fx.settings(http={"max_requests_per_execution": 1}))
        self.assertFalse(report.discovery.complete)
        self.assertTrue(report.discovery.budget_exhausted)
        self.assertTrue(report.budget_exhausted)
        self.assertEqual(report.histories, ())
