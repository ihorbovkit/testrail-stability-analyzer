import unittest

from testrail_stability_analyzer.collection import AvailabilityState, Coverage, HistoricalCollector
from testrail_stability_analyzer.config import StatusCategory

from . import fake_testrail as fx
from .fake_testrail import AUTOMATION_ISSUE, FAILED, NOW, PASSED, FakeTestRail, days_ago


def collect(server, max_results, *, plan_ids=(900,), page_size=0, **sections):
    server.page_size = page_size
    collection = {"plan_ids": list(plan_ids), "max_results": max_results}
    settings = fx.settings(collection=collection, **sections)
    client = fx.make_client(server, settings)
    return HistoricalCollector(client, settings, fx.resolver(), clock=lambda: NOW).collect()


def plan_with_runs(*runs):
    """``runs``: (run_id, platform, number_of_tests, results_per_test). Results are synthetic."""
    server = FakeTestRail()
    server.add_plan(900, "App-A Regression", created=days_ago(10))
    result_id = 1000
    test_id = 1
    for offset, (run_id, platform, tests, per_test) in enumerate(runs):
        test_payloads, results = [], []
        for index in range(tests):
            test_payloads.append(fx.test(test_id, 100 + index, run_id))
            for attempt in range(per_test):
                result_id += 1
                status = PASSED if attempt == per_test - 1 else FAILED
                created = days_ago(5 - offset, seconds=index * 100 + attempt)
                results.append(fx.result(result_id, test_id, status, created))
            test_id += 1
        server.add_plan_run(
            900,
            fx.run(run_id, "Regression", created=days_ago(10 - offset), config=platform),
            tests=test_payloads,
            results=results,
        )
    return server


def result_calls(server):
    calls = zip(server.calls, server.params, strict=True)
    return [(call, params) for call, params in calls if call.startswith("get_results_for_run")]


class ResultCapTests(unittest.TestCase):
    def test_cap_of_15_in_one_run(self):
        server = plan_with_runs((501, "Plat-X", 10, 2))  # 20 records
        report = collect(server, 15)

        self.assertEqual(report.result_records_fetched, 15)
        self.assertTrue(report.results_capped)
        self.assertEqual(report.max_results, 15)
        self.assertEqual(sum(len(o.results) for o in report.outcomes), 15)
        (calls,) = result_calls(server)
        self.assertEqual(calls[1]["limit"], "15")
        self.assertTrue(report.is_partial)
        self.assertTrue(all(h.coverage is Coverage.PARTIAL for h in report.histories))
        self.assertTrue(any("result-record cap of 15 reached" in note for note in report.limitations))

    def test_records_and_distinct_outcomes_are_counted_separately(self):
        report = collect(plan_with_runs((501, "Plat-X", 10, 2)), 15)

        self.assertEqual(report.result_records_fetched, 15)
        self.assertEqual(len(report.outcomes), 10)  # one outcome per test instance in the run

    def test_cap_across_multiple_runs(self):
        # Runs are processed newest first: 503 (6 records), 502 (6), then 501 gets the remaining 3.
        server = plan_with_runs((501, "Plat-X", 3, 2), (502, "Plat-Y", 3, 2), (503, "Plat-X", 3, 2))
        report = collect(server, 15)

        calls = result_calls(server)
        self.assertEqual([call.rpartition("/")[2] for call, _ in calls], ["503", "502", "501"])
        self.assertEqual([params["limit"] for _, params in calls], ["15", "9", "3"])
        self.assertEqual(report.result_records_fetched, 15)
        scans = {scan.run_id: scan for scan in report.scans}
        self.assertEqual({run_id: scan.result_count for run_id, scan in scans.items()}, {501: 3, 502: 6, 503: 6})
        self.assertEqual([run_id for run_id, scan in scans.items() if scan.results_capped], [501])

    def test_runs_after_the_cap_are_not_requested(self):
        server = plan_with_runs((501, "Plat-X", 3, 2), (502, "Plat-Y", 3, 2), (503, "Plat-X", 10, 2))
        report = collect(server, 15)

        self.assertEqual([call for call, _ in result_calls(server)], ["get_results_for_run/503"])
        self.assertNotIn("get_tests/501", server.calls)
        scans = {scan.run_id: scan for scan in report.scans}
        self.assertTrue(scans[501].skipped_by_cap and not scans[501].results_fetched)
        self.assertTrue(scans[502].skipped_by_cap)
        self.assertTrue(any("results of 2 included run(s) were not fetched" in n for n in report.limitations))
        # Cases of the skipped Platform Y run are not reported as missing or untested.
        platform_y = [h for h in report.histories if h.key.platform.name == "Platform Y"]
        self.assertTrue(platform_y)
        self.assertEqual({h.state for h in platform_y}, {AvailabilityState.RESULTS_INCOMPLETE})

    def test_requested_page_size_is_clamped_to_the_api_maximum(self):
        server = plan_with_runs((501, "Plat-X", 20, 1))
        report = collect(server, 300)

        (calls,) = result_calls(server)
        self.assertEqual(calls[1]["limit"], "250")
        self.assertEqual(report.result_records_fetched, 20)
        self.assertFalse(report.results_capped)

    def test_cap_reached_within_a_later_page_requests_no_further_page(self):
        server = plan_with_runs((501, "Plat-X", 20, 1))
        server.page_size = 4
        original = server._page

        def small_pages(key, items, params):  # serve 4 records per page regardless of limit
            params = {k: v for k, v in params.items() if k != "limit"}
            return original(key, items, params)

        server._page = small_pages
        collection = {"plan_ids": [900], "max_results": 10}
        settings = fx.settings(collection=collection)
        client = fx.make_client(server, settings)
        report = HistoricalCollector(client, settings, fx.resolver(), clock=lambda: NOW).collect()

        self.assertEqual([call for call, _ in result_calls(server)], ["get_results_for_run/501"] * 3)
        self.assertEqual(report.result_records_fetched, 10)
        self.assertTrue(report.results_capped)

    def test_cap_is_deterministic(self):
        def snapshot():
            report = collect(plan_with_runs((501, "Plat-X", 4, 3), (502, "Plat-Y", 4, 3)), 15)
            return [(o.run_id, o.test_id, tuple(r.result.id for r in o.results)) for o in report.outcomes]

        self.assertEqual(snapshot(), snapshot())

    def test_uncapped_stream_below_the_cap_is_complete(self):
        report = collect(plan_with_runs((501, "Plat-X", 3, 2)), 15, history={"lookback_limit_days": 0})

        self.assertEqual(report.result_records_fetched, 6)
        self.assertFalse(report.results_capped)
        self.assertTrue(all(o.is_verified_final for o in report.outcomes))
        self.assertEqual({h.coverage for h in report.histories}, {Coverage.SELECTION_LIMITED})
        self.assertFalse(report.is_partial)

    def test_stream_ending_exactly_at_the_cap_is_treated_as_capped(self):
        report = collect(plan_with_runs((501, "Plat-X", 3, 5)), 15)

        self.assertEqual(report.result_records_fetched, 15)
        self.assertTrue(report.results_capped)  # conservative: one more record would be needed to know

    def test_request_budget_is_enforced_with_a_cap(self):
        server = plan_with_runs((501, "Plat-X", 3, 2), (502, "Plat-Y", 3, 2))
        report = collect(server, 15, http={"max_requests_per_execution": 3})

        self.assertEqual(report.requests_made, 3)
        self.assertTrue(report.budget_exhausted)
        self.assertTrue(report.is_partial)


class PartialFinalOutcomeTests(unittest.TestCase):
    def build(self):
        """Test 1: failed, then automation issue (triage), then a comment-only note; newest first in the API."""
        server = FakeTestRail()
        server.add_plan(900, "App-A Regression", created=days_ago(10))
        t = days_ago(5)
        server.add_plan_run(
            900,
            fx.run(501, "Regression", created=days_ago(10), config="Plat-X"),
            tests=[fx.test(1, 100, 501), fx.test(2, 101, 501)],
            results=[
                fx.result(11, 1, FAILED, t),
                fx.result(12, 1, AUTOMATION_ISSUE, t + 600, comment="synthetic triage"),
                fx.result(13, 1, None, t + 1200, comment="synthetic note"),
                fx.result(14, 2, PASSED, t - 600),
            ],
        )
        return server

    def test_comment_only_tail_is_not_a_verified_final(self):
        report = collect(self.build(), 1)
        outcome = next(o for o in report.outcomes if o.test_id == 1)

        self.assertEqual([r.result.id for r in outcome.results], [13])
        self.assertIsNone(outcome.final)
        self.assertFalse(outcome.results_complete)
        history = next(h for h in report.histories if h.key.case_id == 100 and h.key.platform.name == "Platform X")
        self.assertIs(history.state, AvailabilityState.RESULTS_INCOMPLETE)
        self.assertIs(history.coverage, Coverage.PARTIAL)

    def test_capped_final_is_kept_but_not_verified(self):
        report = collect(self.build(), 2)
        outcome = next(o for o in report.outcomes if o.test_id == 1)

        self.assertEqual([r.result.id for r in outcome.results], [12, 13])
        self.assertEqual(outcome.category, StatusCategory.AUTOMATION_ISSUE)
        self.assertFalse(outcome.is_verified_final)

    def test_tests_without_retrieved_records_are_incomplete_not_untested(self):
        report = collect(self.build(), 3)
        outcome = next(o for o in report.outcomes if o.test_id == 2)

        self.assertEqual(outcome.results, ())
        history = next(h for h in report.histories if h.key.case_id == 101 and h.key.platform.name == "Platform X")
        self.assertIs(history.state, AvailabilityState.RESULTS_INCOMPLETE)

    def test_full_stream_yields_verified_final(self):
        report = collect(self.build(), 0)  # 0 = no cap
        outcome = next(o for o in report.outcomes if o.test_id == 1)

        self.assertEqual([r.result.id for r in outcome.results], [11, 12, 13])
        self.assertTrue(outcome.is_verified_final)
        self.assertIsNone(report.max_results)


if __name__ == "__main__":
    unittest.main()
