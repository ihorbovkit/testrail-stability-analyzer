import unittest

from testrail_stability_analyzer.analysis import Rate, compute_metrics
from testrail_stability_analyzer.config import StatusCategory

from .analysis_fixtures import history, outcome, rec, runs
from .fake_testrail import AUTOMATION_ISSUE, BLOCKED, FAILED, MYSTERY, NOT_APPLICABLE, PASSED, RETEST, UNTESTED


class DistinctRunTests(unittest.TestCase):
    def test_retries_in_one_run_count_as_one_execution(self):
        retried = outcome(
            500,
            100,
            rec(1, FAILED, 3),
            rec(2, FAILED, 2.9),
            rec(3, None, 2.8),  # comment-only triage
            rec(4, PASSED, 2.7),
        )
        m = compute_metrics(history([retried]))
        self.assertEqual((m.executions, m.passed, m.failed), (1, 1, 0))
        self.assertEqual(m.retry_triage_count, 1)
        self.assertEqual(m.retry_triage_rate, Rate(1, 1))
        self.assertEqual(m.status_changed_within_run, 1)
        self.assertEqual(m.latest_result_id, 4)

    def test_ten_distinct_runs_versus_twelve_capped(self):
        m = compute_metrics(history(runs([PASSED] * 12)))
        self.assertEqual((m.executions, m.window_total, m.passed), (10, 12, 10))
        self.assertEqual(m.retry_triage_count, 0)
        self.assertEqual(m.retry_triage_rate, Rate(0, 10))

    def test_window_is_newest_runs_including_inconclusive(self):
        # Newest first: 3 blocked, then 9 passed. The 10 newest runs include the blocked ones.
        m = compute_metrics(history(runs([BLOCKED] * 3 + [PASSED] * 9)))
        self.assertEqual((m.executions, m.window_total, m.blocked, m.passed), (10, 12, 3, 7))
        self.assertEqual(m.pass_rate, Rate(7, 7))

    def test_max_runs_is_configurable(self):
        m = compute_metrics(history(runs([PASSED] * 8), max_runs=3))
        self.assertEqual((m.executions, m.window_total), (3, 8))


class PassRateTests(unittest.TestCase):
    def test_denominator_is_passed_failed_and_automation_issue(self):
        statuses = [PASSED] * 3 + [FAILED, AUTOMATION_ISSUE, BLOCKED, RETEST, NOT_APPLICABLE, MYSTERY, UNTESTED]
        m = compute_metrics(history(runs(statuses)))
        self.assertEqual(m.evidence_count, 5)
        self.assertEqual(m.pass_rate, Rate(3, 5))
        self.assertEqual(m.failure_rate, Rate(1, 5))
        self.assertEqual(m.automation_issue_rate, Rate(1, 5))
        self.assertEqual(m.pass_rate.value, 0.6)
        # Excluded categories remain visible.
        self.assertEqual((m.blocked, m.retest, m.not_applicable, m.unknown, m.not_run), (1, 1, 1, 1, 1))
        self.assertEqual(m.executions, 10)

    def test_rate_without_conclusive_execution_is_none(self):
        m = compute_metrics(history(runs([BLOCKED, BLOCKED])))
        self.assertEqual(m.pass_rate, Rate(0, 0))
        self.assertIsNone(m.pass_rate.value)

    def test_automation_issue_is_not_counted_as_failure(self):
        m = compute_metrics(history(runs([AUTOMATION_ISSUE, PASSED])))
        self.assertEqual((m.failed, m.automation_issue), (0, 1))
        self.assertEqual(m.failure_rate, Rate(0, 2))


class LatestAndTransitionTests(unittest.TestCase):
    def test_latest_known_outside_window_is_reported_but_not_counted(self):
        old = outcome(500, 100, rec(1, FAILED, 90))
        m = compute_metrics(history([old]))
        self.assertEqual(m.executions, 0)
        self.assertEqual(m.latest_category, StatusCategory.FAILED)
        self.assertEqual(m.latest_run_id, 500)
        self.assertFalse(m.latest_in_window)
        self.assertTrue(m.latest_is_verified_final)

    def test_latest_uses_result_timestamp(self):
        # Run 501 has the lower ID but the newer final result.
        newer = outcome(501, 100, rec(2, PASSED, 1))
        older = outcome(502, 100, rec(3, FAILED, 5))
        m = compute_metrics(history([older, newer]))
        self.assertEqual((m.latest_run_id, m.latest_category), (501, StatusCategory.PASSED))
        self.assertTrue(m.latest_in_window)

    def test_transitions_are_chronological(self):
        # Newest first: PASSED (1 day ago), FAILED, FAILED, PASSED (4 days ago).
        m = compute_metrics(history(runs([PASSED, FAILED, FAILED, PASSED])))
        self.assertEqual(
            [(t.from_category, t.to_category, t.from_run_id, t.to_run_id) for t in m.transitions],
            [
                (StatusCategory.PASSED, StatusCategory.FAILED, 503, 502),
                (StatusCategory.FAILED, StatusCategory.PASSED, 501, 500),
            ],
        )

    def test_unverified_finals_are_counted(self):
        capped = outcome(500, 100, rec(1, PASSED, 1), complete=False)
        m = compute_metrics(history([capped]))
        self.assertEqual(m.unverified_finals, 1)
        self.assertFalse(m.latest_is_verified_final)


class StatusChangedWithinRunTests(unittest.TestCase):
    def changed(self, *records):
        return compute_metrics(history([outcome(500, 100, *records)])).status_changed_within_run

    def test_failed_passed_failed_is_a_change(self):
        # First and final categories are equal, but the run changed in between.
        self.assertEqual(self.changed(rec(1, FAILED, 3), rec(2, PASSED, 2.9), rec(3, FAILED, 2.8)), 1)

    def test_any_category_transition_counts(self):
        self.assertEqual(self.changed(rec(1, BLOCKED, 3), rec(2, AUTOMATION_ISSUE, 2.9)), 1)

    def test_repeated_category_is_not_a_change(self):
        self.assertEqual(self.changed(rec(1, FAILED, 3), rec(2, FAILED, 2.9)), 0)

    def test_comment_only_records_are_ignored(self):
        self.assertEqual(self.changed(rec(1, FAILED, 3), rec(2, None, 2.9), rec(3, FAILED, 2.8)), 0)
        self.assertEqual(self.changed(rec(1, None, 3), rec(2, PASSED, 2.9), rec(3, None, 2.8)), 0)
        self.assertEqual(self.changed(rec(1, FAILED, 3), rec(2, None, 2.9), rec(3, PASSED, 2.8)), 1)

    def test_counted_once_per_execution(self):
        records = (rec(1, FAILED, 3), rec(2, PASSED, 2.9), rec(3, FAILED, 2.8), rec(4, PASSED, 2.7))
        m = compute_metrics(history([outcome(500, 100, *records), outcome(501, 100, rec(5, PASSED, 1))]))
        self.assertEqual((m.status_changed_within_run, m.executions), (1, 2))


if __name__ == "__main__":
    unittest.main()
