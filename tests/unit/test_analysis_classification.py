import unittest
from datetime import timedelta

from testrail_stability_analyzer.analysis import Confidence, Diagnosis, compute_metrics, diagnose
from testrail_stability_analyzer.collection import Applicability, AvailabilityState, Coverage, RunScan

from .analysis_fixtures import PLAT_X, PLAT_Y, UNRESOLVED, history, outcome, rec, runs
from .fake_testrail import AUTOMATION_ISSUE, BLOCKED, FAILED, MYSTERY, NOT_APPLICABLE, NOW, PASSED, RETEST, UNTESTED


def classify(h, min_executions=5, max_runs=10):
    return diagnose(h, compute_metrics(h), min_executions=min_executions, max_runs=max_runs)


class EvidenceTests(unittest.TestCase):
    def test_ten_passes_with_complete_coverage_is_stable_with_high_confidence(self):
        h = history(runs([PASSED] * 10), lookback_limited=True)
        self.assertEqual(h.coverage, Coverage.LOOKBACK_LIMITED)
        diagnosis, confidence, _ = classify(h)
        self.assertEqual((diagnosis, confidence), (Diagnosis.STABLE, Confidence.HIGH))

    def test_min_executions_passes_is_stable_with_medium_confidence(self):
        self.assertEqual(classify(history(runs([PASSED] * 5)))[:2], (Diagnosis.STABLE, Confidence.MEDIUM))

    def test_fewer_than_min_executions_is_insufficient_with_low_confidence(self):
        for count in (1, 4):
            with self.subTest(count=count):
                diagnosis, confidence, reasons = classify(history(runs([PASSED] * count)))
                self.assertEqual((diagnosis, confidence), (Diagnosis.INSUFFICIENT_HISTORY, Confidence.LOW))
                self.assertTrue(any("fewer than 5" in reason for reason in reasons))

    def test_min_executions_is_configurable(self):
        self.assertEqual(classify(history(runs([PASSED] * 3)), min_executions=3)[0], Diagnosis.STABLE)

    def test_excluded_statuses_do_not_make_up_evidence(self):
        h = history(runs([PASSED, PASSED, BLOCKED, BLOCKED, RETEST, PASSED, PASSED]))
        self.assertEqual(classify(h)[:2], (Diagnosis.INSUFFICIENT_HISTORY, Confidence.LOW))

    def test_unknown_status_prevents_stable(self):
        diagnosis, _, reasons = classify(history(runs([PASSED] * 5 + [MYSTERY])))
        self.assertEqual(diagnosis, Diagnosis.INSUFFICIENT_HISTORY)
        self.assertTrue(any("unclassified" in reason for reason in reasons))

    def test_only_inconclusive_executions(self):
        diagnosis, confidence, _ = classify(history(runs([BLOCKED, RETEST])))
        self.assertEqual((diagnosis, confidence), (Diagnosis.INSUFFICIENT_HISTORY, Confidence.NONE))

    def test_mixed_pass_fail_is_inconsistent_not_flaky(self):
        diagnosis, confidence, reasons = classify(history(runs([PASSED, FAILED, PASSED, PASSED, FAILED, PASSED])))
        self.assertEqual((diagnosis, confidence), (Diagnosis.INCONSISTENT, Confidence.MEDIUM))
        self.assertTrue(any("do not by themselves prove flakiness" in reason for reason in reasons))

    def test_consistent_failures_are_failure_evidence_not_inconsistent(self):
        diagnosis, _, reasons = classify(history(runs([FAILED] * 6)))
        self.assertEqual(diagnosis, Diagnosis.APPLICATION_FAILURE_EVIDENCE)
        self.assertTrue(any("do not confirm an application defect" in reason for reason in reasons))

    def test_single_failure_is_evidence_with_low_confidence(self):
        self.assertEqual(
            classify(history(runs([FAILED])))[:2], (Diagnosis.APPLICATION_FAILURE_EVIDENCE, Confidence.LOW)
        )

    def test_automation_issues(self):
        for statuses in ([AUTOMATION_ISSUE] * 3, [PASSED, AUTOMATION_ISSUE, PASSED, PASSED, PASSED]):
            with self.subTest(statuses=statuses):
                self.assertEqual(classify(history(runs(statuses)))[0], Diagnosis.AUTOMATION_ISSUE_EVIDENCE)

    def test_failures_and_automation_issues_without_passes_are_mixed(self):
        for statuses in ([FAILED, AUTOMATION_ISSUE], [AUTOMATION_ISSUE, FAILED, FAILED]):
            with self.subTest(statuses=statuses):
                self.assertEqual(classify(history(runs(statuses)))[0], Diagnosis.MIXED_FAILURE_EVIDENCE)

    def test_failures_with_automation_issues_and_passes_are_inconsistent(self):
        self.assertEqual(classify(history(runs([PASSED, FAILED, AUTOMATION_ISSUE])))[0], Diagnosis.INCONSISTENT)

    def test_linked_defects_do_not_change_the_diagnosis(self):
        plain = history([outcome(500 + i, 100, rec(i + 1, PASSED, i + 1)) for i in range(5)])
        linked = history([outcome(500 + i, 100, rec(i + 1, PASSED, i + 1, defects="BUG-1")) for i in range(5)])
        self.assertEqual(classify(plain), classify(linked))
        failing = history([outcome(500, 100, rec(1, FAILED, 1, defects="BUG-1, BUG-2"))])
        self.assertEqual(classify(failing)[0], Diagnosis.APPLICATION_FAILURE_EVIDENCE)


class AvailabilityTests(unittest.TestCase):
    def test_latest_outside_window_is_not_run_in_window(self):
        h = history([outcome(500, 100, rec(1, PASSED, 90))])
        self.assertEqual(classify(h)[:2], (Diagnosis.NOT_RUN_IN_WINDOW, Confidence.NONE))

    def test_executed_before_lookback_is_not_run_in_window(self):
        # Included in a run, no result in the lookback, but the test's status shows an earlier execution.
        h = history([outcome(500, 100, test_status=PASSED)])
        self.assertEqual(h.state, AvailabilityState.NO_RESULT_WITHIN_LOOKBACK)
        self.assertEqual(classify(h)[0], Diagnosis.NOT_RUN_IN_WINDOW)

    def test_untested_instance_is_not_run(self):
        h = history([outcome(500, 100)])
        self.assertEqual(h.state, AvailabilityState.UNTESTED)
        self.assertEqual(classify(h)[0], Diagnosis.NOT_RUN)

    def test_missing_platform_is_not_run_without_invented_executions(self):
        h = history(runs([PASSED]), platform=PLAT_Y, platforms=(PLAT_X, PLAT_Y))
        self.assertEqual(h.outcomes, ())
        self.assertEqual(classify(h)[0], Diagnosis.NOT_RUN)
        self.assertEqual(compute_metrics(h).executions, 0)

    def test_not_applicable_final_status(self):
        self.assertEqual(classify(history(runs([NOT_APPLICABLE])))[0], Diagnosis.NOT_APPLICABLE)

    def test_not_applicable_by_metadata(self):
        h = history(
            runs([PASSED]), platform=PLAT_Y, platforms=(PLAT_X, PLAT_Y), applicability=Applicability.NOT_APPLICABLE
        )
        self.assertEqual(classify(h)[0], Diagnosis.NOT_APPLICABLE)

    def test_unknown_applicability_is_unresolved(self):
        h = history(runs([PASSED]), platform=PLAT_Y, platforms=(PLAT_X, PLAT_Y), applicability=Applicability.UNKNOWN)
        self.assertEqual(h.state, AvailabilityState.UNKNOWN_APPLICABILITY)
        self.assertEqual(classify(h)[0], Diagnosis.UNRESOLVED_APPLICABILITY)

    def test_untested_status_result_is_not_conclusive(self):
        self.assertEqual(classify(history(runs([UNTESTED])))[0], Diagnosis.INSUFFICIENT_HISTORY)


class CoverageTests(unittest.TestCase):
    def test_partial_coverage_overrides_evidence(self):
        h = history(runs([PASSED] * 10), global_notes=("run discovery was incomplete",))
        self.assertEqual(h.coverage, Coverage.PARTIAL)
        self.assertEqual(classify(h)[:2], (Diagnosis.PARTIAL, Confidence.NONE))

    def test_capped_results_are_partial(self):
        h = history([outcome(500, 100, rec(1, PASSED, 1), complete=False)])
        self.assertEqual(classify(h)[0], Diagnosis.PARTIAL)

    def test_interrupted_window_is_partial(self):
        # An unscanned run with results in the window, but older than the latest result.
        gap = RunScan(
            999, results_fetched=True, tests_fetched=False, result_count=3, newest_result_on=NOW - timedelta(days=30)
        )
        outcomes = runs([PASSED] * 10)
        h = history(outcomes, gaps={(outcomes[0].source.classification.application, PLAT_X): [gap]})
        self.assertNotEqual(h.coverage, Coverage.PARTIAL)
        self.assertTrue(h.window_interrupted)
        self.assertEqual(classify(h)[:2], (Diagnosis.PARTIAL, Confidence.NONE))

    def test_selection_limited_cannot_exceed_medium(self):
        h = history(runs([PASSED] * 10), selection_limited=True)
        self.assertEqual(h.coverage, Coverage.SELECTION_LIMITED)
        self.assertFalse(h.window_complete)
        self.assertFalse(h.window_interrupted)
        diagnosis, confidence, reasons = classify(h)
        self.assertEqual((diagnosis, confidence), (Diagnosis.STABLE, Confidence.MEDIUM))
        self.assertTrue(any("at most medium" in reason for reason in reasons))

    def test_single_selected_plan_is_insufficient(self):
        h = history(runs([PASSED]), selection_limited=True)
        self.assertEqual(classify(h)[:2], (Diagnosis.INSUFFICIENT_HISTORY, Confidence.LOW))

    def test_unidentified_platform_has_low_confidence(self):
        h = history([outcome(500, 100, rec(1, FAILED, 1), platform=UNRESOLVED)], platform=UNRESOLVED, platforms=())
        self.assertFalse(h.key.is_identified)
        self.assertEqual(classify(h, min_executions=1)[:2], (Diagnosis.APPLICATION_FAILURE_EVIDENCE, Confidence.LOW))



class NonEvidenceConfidenceTests(unittest.TestCase):
    """Diagnoses that do not rest on execution evidence have no confidence; metrics stay unchanged."""

    def assert_none(self, h, expected):
        metrics = compute_metrics(h)
        diagnosis, confidence, reasons = classify(h)
        self.assertEqual((diagnosis, confidence), (expected, Confidence.NONE))
        self.assertFalse(any(reason.startswith("confidence is") for reason in reasons))
        return metrics

    def test_partial_with_ten_passes(self):
        h = history(runs([PASSED] * 10), global_notes=("run discovery was incomplete",))
        m = self.assert_none(h, Diagnosis.PARTIAL)
        self.assertEqual((m.executions, m.evidence_count, m.pass_rate.value), (10, 10, 1.0))

    def test_not_applicable_latest_after_conclusive_executions(self):
        h = history(runs([NOT_APPLICABLE] + [PASSED] * 9))
        m = self.assert_none(h, Diagnosis.NOT_APPLICABLE)
        self.assertEqual((m.not_applicable, m.passed), (1, 9))

    def test_not_run_in_window(self):
        self.assert_none(history([outcome(500, 100, rec(1, PASSED, 90))]), Diagnosis.NOT_RUN_IN_WINDOW)

    def test_not_run(self):
        self.assert_none(history([outcome(500, 100)]), Diagnosis.NOT_RUN)

    def test_unresolved_applicability(self):
        h = history(runs([PASSED]), platform=PLAT_Y, platforms=(PLAT_X, PLAT_Y), applicability=Applicability.UNKNOWN)
        self.assert_none(h, Diagnosis.UNRESOLVED_APPLICABILITY)

    def test_evidence_diagnoses_keep_their_confidence(self):
        self.assertEqual(classify(history(runs([PASSED])))[1], Confidence.LOW)
        self.assertEqual(classify(history(runs([FAILED] * 5)))[1], Confidence.MEDIUM)
        self.assertEqual(classify(history(runs([BLOCKED])))[:2], (Diagnosis.INSUFFICIENT_HISTORY, Confidence.NONE))


class SelectionLimitedWordingTests(unittest.TestCase):
    def test_not_run_names_the_selection(self):
        h = history(runs([PASSED]), platform=PLAT_Y, platforms=(PLAT_X, PLAT_Y), selection_limited=True)
        self.assertEqual(h.coverage, Coverage.SELECTION_LIMITED)
        diagnosis, _, reasons = classify(h)
        self.assertEqual(diagnosis, Diagnosis.NOT_RUN)
        self.assertEqual(
            reasons, ("no execution is known in the selected runs; runs outside the selection were not scanned",)
        )

    def test_not_run_in_window_names_the_selection(self):
        h = history([outcome(500, 100, rec(1, PASSED, 90))], selection_limited=True)
        diagnosis, _, reasons = classify(h)
        self.assertEqual(diagnosis, Diagnosis.NOT_RUN_IN_WINDOW)
        self.assertIn("in the selected runs", reasons[0])

    def test_project_wide_wording_is_unchanged(self):
        h = history(runs([PASSED]), platform=PLAT_Y, platforms=(PLAT_X, PLAT_Y))
        self.assertEqual(classify(h)[2], ("no execution is known",))


if __name__ == "__main__":
    unittest.main()
