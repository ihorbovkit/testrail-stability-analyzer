"""Known Issue: a configurable status for confirmed product defects.

It counts against the pass rate like Failed (in the conclusive-execution
denominator) and is weighed like Failed by the diagnosis rules, but stays a
separate category in metrics and reports. All data is synthetic.
"""

import unittest
from unittest import mock

from testrail_stability_analyzer.analysis import Confidence, Diagnosis, Rate, analyze, compute_metrics, diagnose
from testrail_stability_analyzer.config import StatusCategory, settings_from_mapping
from testrail_stability_analyzer.export import overview_cell, overview_pass_rate_cell
from testrail_stability_analyzer.models import ResolutionSource, Status, StatusResolver

from . import analysis_fixtures as af
from . import fake_testrail as fx
from .analysis_fixtures import history, report, runs
from .fake_testrail import AUTOMATION_ISSUE, BLOCKED, FAILED, KNOWN_ISSUE, PASSED, RETEST
from .test_export_spreadsheet import column, info, render, table

STATUSES = [Status.from_api(item) for item in fx.STATUSES]


def classify(h, min_executions=5, max_runs=10):
    return diagnose(h, compute_metrics(h), min_executions=min_executions, max_runs=max_runs)


def key_analysis(outcomes, settings=None):
    settings = settings or fx.settings()
    (analysis,) = analyze(report(outcomes), settings).analyses
    return analysis


class StatusMappingTests(unittest.TestCase):
    def test_known_issue_maps_by_system_name(self):
        settings = settings_from_mapping(
            {
                "testrail": {"base_url": "https://example.testrail.io", "project_id": 1},
                "mappings": {"statuses": {"known_issue": ["triage"], "automation_issue": ["custom_status1"]}},
            }
        )
        self.assertEqual(settings.mappings.statuses[StatusCategory.KNOWN_ISSUE], ("triage",))
        resolved = StatusResolver(STATUSES, settings.mappings.statuses).resolve(KNOWN_ISSUE)
        self.assertEqual(resolved.category, StatusCategory.KNOWN_ISSUE)
        self.assertEqual(resolved.source, ResolutionSource.CONFIGURED)
        self.assertEqual(resolved.label, "Known Issue")

    def test_known_issue_maps_by_id(self):
        resolved = StatusResolver(STATUSES, {StatusCategory.KNOWN_ISSUE: (KNOWN_ISSUE,)}).resolve(KNOWN_ISSUE)
        self.assertEqual(resolved.category, StatusCategory.KNOWN_ISSUE)

    def test_known_issue_is_distinct_from_failed(self):
        resolver = fx.resolver()
        self.assertEqual(resolver.resolve(FAILED).category, StatusCategory.FAILED)
        self.assertEqual(resolver.resolve(KNOWN_ISSUE).category, StatusCategory.KNOWN_ISSUE)

    def test_without_mapping_the_status_stays_unknown(self):
        # Custom statuses are never guessed from their name or label.
        resolved = StatusResolver(STATUSES, {StatusCategory.AUTOMATION_ISSUE: ("custom_status1",)}).resolve(KNOWN_ISSUE)
        self.assertEqual((resolved.category, resolved.source), (StatusCategory.UNKNOWN, ResolutionSource.UNRESOLVED))

    def test_mapping_absent_from_instance_is_reported_not_fatal(self):
        resolver = StatusResolver(STATUSES[:5], {StatusCategory.KNOWN_ISSUE: ("triage",)})
        self.assertEqual(resolver.unmatched_mappings, ((StatusCategory.KNOWN_ISSUE, "triage"),))

    def test_status_cannot_be_both_failed_and_known_issue(self):
        with self.assertRaises(ValueError):
            StatusResolver(STATUSES, {StatusCategory.FAILED: ("triage",), StatusCategory.KNOWN_ISSUE: (KNOWN_ISSUE,)})


class PassRateTests(unittest.TestCase):
    def test_known_issue_counts_in_the_denominator(self):
        cases = (
            # (passed, failed, automation issue, known issue) -> pass rate
            ((8, 0, 0, 2), Rate(8, 10), 0.8),
            ((8, 0, 0, 0), Rate(8, 8), 1.0),
            ((0, 0, 0, 5), Rate(0, 5), 0.0),
            ((5, 1, 2, 2), Rate(5, 10), 0.5),
        )
        for (p, f, a, k), rate, value in cases:
            with self.subTest(passed=p, failed=f, automation_issue=a, known_issue=k):
                statuses = [PASSED] * p + [FAILED] * f + [AUTOMATION_ISSUE] * a + [KNOWN_ISSUE] * k
                m = compute_metrics(history(runs(statuses)))
                self.assertEqual(m.pass_rate, rate)
                self.assertEqual(m.pass_rate.value, value)
                self.assertEqual(m.evidence_count, p + f + a + k)
                self.assertEqual((m.passed, m.failed, m.automation_issue, m.known_issue), (p, f, a, k))

    def test_failure_rate_counts_failed_only(self):
        m = compute_metrics(history(runs([PASSED] * 5 + [FAILED] + [AUTOMATION_ISSUE] * 2 + [KNOWN_ISSUE] * 2)))
        self.assertEqual(m.failure_rate, Rate(1, 10))
        self.assertEqual(m.automation_issue_rate, Rate(2, 10))
        self.assertEqual(m.failures, 3)

    def test_blocked_and_retest_stay_excluded(self):
        m = compute_metrics(history(runs([KNOWN_ISSUE, BLOCKED, BLOCKED, RETEST, PASSED])))
        self.assertEqual((m.executions, m.blocked, m.retest, m.known_issue), (5, 2, 1, 1))
        self.assertEqual(m.pass_rate, Rate(1, 2))

    def test_without_known_issue_results_metrics_are_unchanged(self):
        m = compute_metrics(history(runs([PASSED] * 3 + [FAILED, AUTOMATION_ISSUE, BLOCKED])))
        self.assertEqual(m.known_issue, 0)
        self.assertEqual((m.evidence_count, m.pass_rate), (5, Rate(3, 5)))


class DiagnosisTests(unittest.TestCase):
    def test_passes_and_known_issues_are_inconsistent_like_failures(self):
        diagnosis, confidence, reasons = classify(history(runs([PASSED] * 8 + [KNOWN_ISSUE] * 2)))
        self.assertEqual((diagnosis, confidence), (Diagnosis.INCONSISTENT, Confidence.HIGH))
        summary = "8 passed, 0 failed, 0 automation issue(s), 2 known issue(s) in 10 conclusive execution(s)"
        self.assertIn(summary, reasons)
        self.assertEqual(classify(history(runs([PASSED] * 8 + [FAILED] * 2)))[:2], (diagnosis, confidence))

    def test_half_known_issues_is_inconsistent(self):
        h = history(runs([PASSED, KNOWN_ISSUE] * 5))
        self.assertEqual(compute_metrics(h).pass_rate, Rate(5, 10))
        self.assertEqual(classify(h)[0], Diagnosis.INCONSISTENT)

    def test_only_known_issues_is_failure_evidence_not_flaky(self):
        diagnosis, confidence, reasons = classify(history(runs([KNOWN_ISSUE] * 10)))
        self.assertEqual((diagnosis, confidence), (Diagnosis.APPLICATION_FAILURE_EVIDENCE, Confidence.HIGH))
        self.assertIn("known issue results are confirmed product defects", reasons)
        self.assertFalse(any("do not confirm" in reason for reason in reasons))

    def test_failed_and_known_issues_keep_the_failed_caveat(self):
        _, _, reasons = classify(history(runs([FAILED, KNOWN_ISSUE, FAILED])))
        self.assertIn("failed results do not confirm an application defect", reasons)

    def test_known_issues_count_toward_min_executions(self):
        self.assertEqual(
            classify(history(runs([KNOWN_ISSUE] * 3)))[:2], (Diagnosis.APPLICATION_FAILURE_EVIDENCE, Confidence.LOW)
        )
        # 4 passes alone are too few for min_executions = 5; one known issue makes 5 conclusive executions.
        self.assertEqual(classify(history(runs([PASSED] * 4)))[0], Diagnosis.INSUFFICIENT_HISTORY)
        self.assertEqual(
            classify(history(runs([PASSED] * 4 + [KNOWN_ISSUE])))[:2], (Diagnosis.INCONSISTENT, Confidence.MEDIUM)
        )

    def test_known_issues_with_automation_issues(self):
        self.assertEqual(classify(history(runs([KNOWN_ISSUE, AUTOMATION_ISSUE])))[0], Diagnosis.MIXED_FAILURE_EVIDENCE)
        self.assertEqual(
            classify(history(runs([PASSED, AUTOMATION_ISSUE, KNOWN_ISSUE])))[0], Diagnosis.INCONSISTENT
        )
        self.assertEqual(
            classify(history(runs([PASSED, AUTOMATION_ISSUE, PASSED])))[0], Diagnosis.AUTOMATION_ISSUE_EVIDENCE
        )

    def test_only_blocked_and_known_issue(self):
        diagnosis, confidence, _ = classify(history(runs([BLOCKED, KNOWN_ISSUE, BLOCKED])))
        self.assertEqual((diagnosis, confidence), (Diagnosis.APPLICATION_FAILURE_EVIDENCE, Confidence.LOW))

    def test_reasons_without_known_issues_are_unchanged(self):
        _, _, reasons = classify(history(runs([PASSED] * 5)))
        self.assertEqual(reasons[0], "5 passed, 0 failed, 0 automation issue(s) in 5 conclusive execution(s)")


class BackwardCompatibilityTests(unittest.TestCase):
    def test_unmapped_known_issue_status_is_treated_as_unknown(self):
        mappings = {StatusCategory.AUTOMATION_ISSUE: ("custom_status1",)}
        with mock.patch.object(af, "RESOLVER", StatusResolver(STATUSES, mappings)):
            h = history(runs([PASSED] * 5 + [KNOWN_ISSUE]))
        m = compute_metrics(h)
        self.assertEqual((m.known_issue, m.unknown, m.pass_rate), (0, 1, Rate(5, 5)))
        self.assertEqual(classify(h)[0], Diagnosis.INSUFFICIENT_HISTORY)


class ReportTests(unittest.TestCase):
    def test_latest_status_known_issue_in_details(self):
        _, rows = table(render(runs([KNOWN_ISSUE] + [PASSED] * 5)))
        self.assertEqual(rows[0][column(metric="Latest Status")], ("Known Issue", "String", "red"))
        self.assertEqual(rows[0][column(metric="Known Issue Count")], ("1", "Number", None))
        self.assertEqual(rows[0][column(metric="Pass Rate")][:2], ("0.8333333333333334", "Number"))
        self.assertEqual(rows[0][column(metric="Diagnosis")][0], "Inconsistent")

    def test_known_issue_count_is_zero_without_known_issues(self):
        _, rows = table(render(runs([PASSED] * 5)))
        self.assertEqual(rows[0][column(metric="Known Issue Count")], ("0", "Number", None))
        self.assertEqual(rows[0][column(metric="Latest Status")][0], "Passed")

    def test_failed_stays_a_separate_label(self):
        _, rows = table(render(runs([FAILED] + [KNOWN_ISSUE] * 4)))
        self.assertEqual(rows[0][column(metric="Latest Status")][0], "Failed")
        self.assertEqual(rows[0][column(metric="Known Issue Count")][0], "4")
        self.assertEqual(rows[0][column(metric="Failure Rate")][0], "0.2")

    def test_overview_shows_known_issue_and_pass_rate(self):
        analysis = key_analysis(runs([KNOWN_ISSUE] * 2 + [PASSED] * 8))
        cell = overview_cell(analysis)
        self.assertEqual((cell.value, cell.style), ("Known Issue", "overview_yellow"))
        rate = overview_pass_rate_cell(analysis)
        self.assertEqual((rate.value, rate.style), (0.8, "overview_yellow"))
        self.assertEqual(rate.comment.splitlines()[1], "P P P P P P P P K K")

    def test_all_known_issues_is_red_in_overview(self):
        analysis = key_analysis(runs([KNOWN_ISSUE] * 10))
        cell = overview_cell(analysis)
        self.assertEqual((cell.value, cell.style), ("Known Issue", "overview_red"))
        self.assertEqual(overview_pass_rate_cell(analysis).value, 0.0)

    def test_too_few_conclusive_executions_stay_grey(self):
        analysis = key_analysis(runs([KNOWN_ISSUE] * 3))
        self.assertEqual(overview_cell(analysis).style, "overview_neutral")
        self.assertEqual(overview_pass_rate_cell(analysis).value, 0.0)

    def test_legend_explains_known_issue(self):
        legend = {label: value for label, value, _ in info(render(runs([PASSED] * 5)))}
        self.assertIn("confirmed product defect", legend["Legend: Known Issue"])
        self.assertIn("counts against this report's Pass Rate", legend["Legend: Known Issue"])
        self.assertIn("TestRail dashboard", legend["Legend: Known Issue"])
        self.assertIn("passed + failed + automation issue + known issue", legend["Legend: Rates"])
        statuses = legend["Legend: Statuses"]
        for text in (
            "Passed: test execution completed successfully.",
            "Failed: execution failed and has not been classified as AU Issue or Known Issue.",
            "failure attributed to automation or test maintenance.",
            "Known Issue: failure attributed to a confirmed product defect.",
            "Blocked: test execution was skipped or blocked for an expected reason.",
        ):
            self.assertIn(text, statuses)


if __name__ == "__main__":
    unittest.main()
