"""End-to-end: synthetic TestRail data -> collection -> analysis. No real API requests."""

import unittest

from testrail_stability_analyzer.analysis import Confidence, DefectCoverage, Diagnosis, analyze
from testrail_stability_analyzer.collection import Coverage, HistoricalCollector

from . import fake_testrail as fx
from .fake_testrail import AUTOMATION_ISSUE, FAILED, NOW, PASSED, FakeTestRail, days_ago


def collect(server, settings):
    client = fx.make_client(server, settings)
    return HistoricalCollector(client, settings, fx.resolver(), clock=lambda: NOW).collect()


def build_server():
    """Six nightly plans for App A on Platform X and Y; case 100 is retried and triaged with defects."""
    server = FakeTestRail()
    for i in range(6):
        plan_id, created = 900 + i, days_ago(6 * i + 2)
        t = created + 3600
        server.add_plan(plan_id, f"App-A Nightly {i}", created=created, completed=created + 86_400)
        run_x, run_y = 500 + 2 * i, 501 + 2 * i
        x_results = [fx.result(10_000 + i, 1000 * run_x + 1, PASSED, t)]
        if i == 1:
            x_results = [
                fx.result(10_000 + i, 1000 * run_x + 1, FAILED, t, defects="BUG-1, BUG-2"),
                fx.result(20_000 + i, 1000 * run_x + 1, None, t + 60, defects="BUG-2"),
                fx.result(30_000 + i, 1000 * run_x + 1, AUTOMATION_ISSUE, t + 120),
            ]
        server.add_plan_run(
            plan_id,
            fx.run(run_x, "Nightly", created=created, completed=created + 86_400, config="Plat-X", plan_id=plan_id),
            tests=[fx.test(1000 * run_x + 1, 100, run_x, status_id=PASSED)],
            results=x_results,
        )
        server.add_plan_run(
            plan_id,
            fx.run(run_y, "Nightly", created=created, completed=created + 86_400, config="Plat-Y", plan_id=plan_id),
            tests=[fx.test(1000 * run_y + 1, 100, run_y, status_id=FAILED)],
            results=[fx.result(40_000 + i, 1000 * run_y + 1, FAILED, t, defects="BUG-3" if i == 0 else None)],
        )
    return server


def analysis_for(analysis, platform):
    return next(a for a in analysis.analyses if a.key.case_id == 100 and a.key.platform.name == platform)


class ProjectWideAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.server = build_server()
        self.settings = fx.settings()
        self.report = collect(self.server, self.settings)
        calls = len(self.server.calls)
        self.analysis = analyze(self.report, self.settings)
        self.assertEqual(len(self.server.calls), calls)  # analysis makes no requests

    def test_platform_x_mixed_history(self):
        x = analysis_for(self.analysis, "Platform X")
        m = x.metrics
        self.assertEqual((m.executions, m.passed, m.automation_issue, m.failed), (6, 5, 1, 0))
        self.assertEqual(m.pass_rate.value, 5 / 6)
        self.assertEqual((m.retry_triage_count, m.status_changed_within_run), (1, 1))
        self.assertEqual((x.diagnosis, x.confidence), (Diagnosis.AUTOMATION_ISSUE_EVIDENCE, Confidence.MEDIUM))

    def test_platform_y_consistent_failures(self):
        y = analysis_for(self.analysis, "Platform Y")
        self.assertEqual(y.metrics.failed, 6)
        self.assertEqual(y.diagnosis, Diagnosis.APPLICATION_FAILURE_EVIDENCE)

    def test_defects_are_shared_across_platforms(self):
        x, y = analysis_for(self.analysis, "Platform X"), analysis_for(self.analysis, "Platform Y")
        self.assertIs(x.defects, y.defects)
        self.assertEqual(x.defects.defect_ids, ("BUG-1", "BUG-2", "BUG-3"))  # by first appearance
        bug_2 = next(d for d in x.defects.defects if d.defect_id == "BUG-2")
        self.assertEqual([s.result_id for s in bug_2.sources], [10_001, 20_001])
        self.assertTrue(all(not s.is_final_result for s in bug_2.sources))
        self.assertEqual(x.defects.coverage, DefectCoverage.COMPLETE)

    def test_only_get_requests_are_made(self):
        self.assertTrue(all(call.startswith("get_") for call in self.server.calls))


class TargetedAnalysisTests(unittest.TestCase):
    def test_single_selected_plan_is_selection_limited_and_insufficient(self):
        settings = fx.settings(collection={"plan_ids": [900]})
        report = collect(build_server(), settings)
        analysis = analyze(report, settings)
        x = analysis_for(analysis, "Platform X")
        self.assertEqual(x.coverage, Coverage.SELECTION_LIMITED)
        self.assertEqual((x.diagnosis, x.confidence), (Diagnosis.INSUFFICIENT_HISTORY, Confidence.LOW))
        self.assertEqual(x.defects.coverage, DefectCoverage.SELECTION_LIMITED)
        self.assertTrue(any("not project-wide" in note for note in analysis.limitations))


class PartialAnalysisTests(unittest.TestCase):
    def test_request_budget_makes_analysis_partial(self):
        settings = fx.settings(http={"max_requests_per_execution": 12})
        report = collect(build_server(), settings)
        self.assertTrue(report.budget_exhausted)
        analysis = analyze(report, settings)
        self.assertEqual(analysis.defect_coverage, DefectCoverage.PARTIAL)
        self.assertTrue(all(a.diagnosis is Diagnosis.PARTIAL for a in analysis.analyses))
        self.assertTrue(all(a.defects.coverage is DefectCoverage.PARTIAL for a in analysis.analyses))


if __name__ == "__main__":
    unittest.main()
