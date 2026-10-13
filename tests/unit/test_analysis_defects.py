import dataclasses
import unittest
from datetime import timedelta

from testrail_stability_analyzer.analysis import (
    DefectCoverage,
    aggregate_defects,
    analyze,
    defect_coverage,
    parse_defects,
)
from testrail_stability_analyzer.collection import (
    DiscoveredRun,
    DiscoveryResult,
    ResolvedResult,
    RunClassification,
    RunDecision,
    RunScan,
)
from testrail_stability_analyzer.config import StatusCategory
from testrail_stability_analyzer.models import Result, Run

from . import fake_testrail as fx
from .analysis_fixtures import APP_A, APP_B, PLAT_X, PLAT_Y, RESOLVER, UNRESOLVED, histories, outcome, rec, report
from .fake_testrail import AUTOMATION_ISSUE, FAILED, NOW, PASSED


def days(count):
    return NOW - timedelta(days=count)


def case(report_obj, case_id=100):
    return next(d for d in aggregate_defects(report_obj) if d.case_id == case_id)


class ParseTests(unittest.TestCase):
    def test_splits_trims_and_deduplicates_case_sensitively(self):
        self.assertEqual(parse_defects(" BUG-1, BUG-2 ,,BUG-1, bug-1 "), ("BUG-1", "BUG-2", "bug-1"))

    def test_empty_values(self):
        for value in (None, "", " , ,"):
            with self.subTest(value=value):
                self.assertEqual(parse_defects(value), ())

    def test_identifiers_are_kept_verbatim(self):
        value = "PROJ-12,#345, https://tracker.example/9"
        self.assertEqual(parse_defects(value), ("PROJ-12", "#345", "https://tracker.example/9"))


class AggregationTests(unittest.TestCase):
    def test_duplicates_across_results_are_merged_with_provenance(self):
        outcomes = [
            outcome(500, 100, rec(1, FAILED, 3, "BUG-1"), plan_id=900),
            outcome(501, 100, rec(2, FAILED, 2, "BUG-1")),
        ]
        defects = case(report(outcomes))
        self.assertEqual(defects.defect_ids, ("BUG-1",))
        sources = defects.defects[0].sources
        self.assertEqual(
            [(s.result_id, s.run_id, s.plan_id, s.test_id) for s in sources],
            [(1, 500, 900, 500100), (2, 501, None, 501100)],
        )
        self.assertEqual((defects.defects[0].first_seen, defects.defects[0].last_seen), (days(3), days(2)))
        self.assertEqual((defects.records_examined, defects.records_with_defects), (2, 2))

    def test_multiple_defects_in_one_result(self):
        defects = case(report([outcome(500, 100, rec(1, FAILED, 1, "BUG-2, BUG-1, BUG-2"))]))
        self.assertEqual(defects.defect_ids, ("BUG-1", "BUG-2"))  # same record: ordered by ID
        self.assertEqual(len(defects.defects[0].sources), 1)

    def test_defects_only_in_intermediate_and_triage_records(self):
        retried = outcome(
            500,
            100,
            rec(1, FAILED, 2, "BUG-1"),
            rec(2, AUTOMATION_ISSUE, 1.9),
            rec(3, None, 1.8, "BUG-2"),  # comment-only triage
            rec(4, PASSED, 1.7),
        )
        defects = case(report([retried]))
        self.assertEqual(defects.defect_ids, ("BUG-1", "BUG-2"))
        first, triage = defects.defects[0].sources[0], defects.defects[1].sources[0]
        self.assertEqual((first.is_final_result, first.status_category), (False, StatusCategory.FAILED))
        self.assertEqual((triage.is_final_result, triage.status_category), (False, None))
        self.assertEqual(defects.records_examined, 4)

    def test_final_record_is_marked(self):
        defects = case(report([outcome(500, 100, rec(1, FAILED, 1, "BUG-1"))]))
        self.assertTrue(defects.defects[0].sources[0].is_final_result)

    def test_defects_across_applications_and_platforms_are_shared(self):
        outcomes = [
            outcome(500, 100, rec(1, FAILED, 3, "BUG-X")),
            outcome(501, 100, rec(2, FAILED, 2, "BUG-Y"), platform=PLAT_Y),
            outcome(502, 100, rec(3, FAILED, 1, "BUG-B"), app=APP_B),
            outcome(503, 200, rec(4, FAILED, 1, "OTHER-1")),
        ]
        rep = report(outcomes, hist=histories(outcomes, platforms=(PLAT_X, PLAT_Y)))
        analysis = analyze(rep, fx.settings())
        case_100 = [a for a in analysis.analyses if a.key.case_id == 100]
        self.assertEqual(len(case_100), 4)  # App A x {X, Y}, App B x {X, Y}
        self.assertTrue(all(a.defects is case_100[0].defects for a in case_100))
        self.assertEqual(case_100[0].defects.defect_ids, ("BUG-X", "BUG-Y", "BUG-B"))
        self.assertEqual(analysis.defects_for(200).defect_ids, ("OTHER-1",))
        sources = {d.defect_id: d.sources[0] for d in case_100[0].defects.defects}
        self.assertEqual((sources["BUG-Y"].application.name, sources["BUG-Y"].platform.name), ("App A", "Platform Y"))
        self.assertEqual(sources["BUG-B"].application.name, "App B")

    def test_defects_outside_reporting_window_are_excluded(self):
        outcomes = [
            outcome(500, 100, rec(1, FAILED, 90, "OLD-1")),
            # Final result inside the window, earlier retry outside it.
            outcome(501, 100, rec(2, FAILED, 61, "OLD-2"), rec(3, PASSED, 59, "NEW-1")),
        ]
        defects = case(report(outcomes))
        self.assertEqual(defects.defect_ids, ("NEW-1",))
        self.assertEqual(defects.records_examined, 1)

    def test_more_than_max_runs_are_aggregated(self):
        outcomes = [outcome(500 + i, 100, rec(i + 1, FAILED, i + 1, f"BUG-{i}")) for i in range(12)]
        rep = report(outcomes)
        self.assertEqual(len(rep.histories[0].window), 10)
        self.assertEqual(len(case(rep).defects), 12)

    def test_case_refs_are_not_execution_defects(self):
        plain = outcome(500, 100, rec(1, FAILED, 1))
        with_refs = dataclasses.replace(plain, test=dataclasses.replace(plain.test, refs="REQ-1"))
        self.assertEqual(case(report([with_refs])).defect_ids, ())

    def test_undated_records_are_counted_and_excluded(self):
        outcomes = [outcome(500, 100, rec(1, FAILED, None, "BUG-U"), rec(2, PASSED, 1))]
        rep = report(outcomes)
        defects = case(rep)
        self.assertEqual(defects.defect_ids, ())
        self.assertEqual(defects.undated_records_with_defects, 1)
        self.assertTrue(any("without a timestamp" in note for note in defects.notes))
        self.assertTrue(any("without a timestamp" in note for note in analyze(rep, fx.settings()).limitations))

    def test_no_comment_text_is_exposed(self):
        source = case(report([outcome(500, 100, rec(1, FAILED, 1, "BUG-1"))])).defects[0].sources[0]
        self.assertNotIn("comment", {field.name for field in dataclasses.fields(source)})


class CompletenessTests(unittest.TestCase):
    def setUp(self):
        self.outcomes = [outcome(500, 100, rec(1, PASSED, 1))]

    def test_empty_list_with_complete_collection(self):
        defects = case(report(self.outcomes))
        self.assertEqual(defects.defect_ids, ())
        self.assertEqual((defects.coverage, defects.records_examined), (DefectCoverage.COMPLETE, 1))

    def test_empty_list_with_incomplete_collection(self):
        for fields in ({"budget_exhausted": True}, {"results_capped": True}):
            with self.subTest(fields=fields):
                defects = case(report(self.outcomes, **fields))
                self.assertEqual(defects.coverage, DefectCoverage.PARTIAL)
                self.assertTrue(defects.notes)

    def test_unscanned_run_with_results_in_period_is_partial(self):
        gap = RunScan(999, results_fetched=True, tests_fetched=False, result_count=2, newest_result_on=days(5))
        old = RunScan(998, results_fetched=True, tests_fetched=False, result_count=2, newest_result_on=days(90))
        self.assertEqual(defect_coverage(report(self.outcomes, scans=(old,)))[0], DefectCoverage.COMPLETE)
        self.assertEqual(defect_coverage(report(self.outcomes, scans=(gap,)))[0], DefectCoverage.PARTIAL)

    def test_selection_limited(self):
        self.assertEqual(case(report(self.outcomes, targeted=True)).coverage, DefectCoverage.SELECTION_LIMITED)

    def test_orphan_defects_are_a_limitation_without_downgrading_cases(self):
        orphan = Result(id=9, test_id=777, status_id=FAILED, created_on=days(1), defects="BUG-9", run_id=500)
        rep = report(self.outcomes, orphans=(ResolvedResult(orphan, RESOLVER.resolve(FAILED)),))
        analysis = analyze(rep, fx.settings())
        self.assertEqual(analysis.orphan_records_with_defects, 1)
        self.assertTrue(any("not attributed to a case" in note for note in analysis.limitations))
        self.assertEqual(analysis.defects_for(100).coverage, DefectCoverage.COMPLETE)
        self.assertEqual(analysis.defects_for(100).defect_ids, ())


def discovered(run_id, decision, *, suite_id=10, platform=PLAT_X):
    run = Run(id=run_id, name=f"Run {run_id}", suite_id=suite_id)
    return DiscoveredRun(run, RunClassification(APP_A, platform), decision)


class NotCollectedRunTests(unittest.TestCase):
    """Discovered runs that were not collected may hold records of any collected case."""

    def setUp(self):
        self.outcomes = [outcome(500, 100, rec(1, FAILED, 1, "BUG-1"))]
        self.included = discovered(500, RunDecision.INCLUDED)

    def with_runs(self, *extra, targeted=False, complete=True):
        discovery = DiscoveryResult(runs=(self.included, *extra), complete=complete, targeted=targeted)
        return report(self.outcomes, discovery=discovery)

    def test_filtered_run_makes_a_non_empty_list_selection_limited(self):
        for decision in (RunDecision.EXCLUDED_BY_FILTER, RunDecision.EXCLUDED_BY_NAME):
            with self.subTest(decision=decision):
                defects = case(self.with_runs(discovered(600, decision, platform=PLAT_Y)))
                self.assertEqual(defects.defect_ids, ("BUG-1",))
                self.assertEqual(defects.coverage, DefectCoverage.SELECTION_LIMITED)
                self.assertTrue(any("excluded by naming patterns or filters" in note for note in defects.notes))

    def test_unclassified_run_is_partial(self):
        defects = case(self.with_runs(discovered(600, RunDecision.UNCLASSIFIED, platform=UNRESOLVED)))
        self.assertEqual(defects.defect_ids, ("BUG-1",))
        self.assertEqual(defects.coverage, DefectCoverage.PARTIAL)
        self.assertTrue(any("could not be classified" in note for note in defects.notes))

    def test_partial_wins_and_keeps_selection_notes(self):
        rep = self.with_runs(
            discovered(600, RunDecision.UNCLASSIFIED), discovered(601, RunDecision.EXCLUDED_BY_FILTER), targeted=True
        )
        coverage, notes = defect_coverage(rep)
        self.assertEqual(coverage, DefectCoverage.PARTIAL)
        self.assertEqual(len(notes), 3)
        self.assertTrue(any("only the selected plans and runs" in note for note in notes))
        self.assertTrue(any("1 run(s) excluded" in note for note in notes))

    def test_incomplete_discovery_is_partial(self):
        defects = case(self.with_runs(complete=False))
        self.assertEqual((defects.coverage, defects.defect_ids), (DefectCoverage.PARTIAL, ("BUG-1",)))
        self.assertTrue(any("discovery was incomplete" in note for note in defects.notes))

    def test_runs_of_another_suite_cannot_hold_the_cases(self):
        rep = self.with_runs(discovered(600, RunDecision.EXCLUDED_BY_FILTER, suite_id=99))
        self.assertEqual(defect_coverage(rep), (DefectCoverage.COMPLETE, ()))

    def test_unknown_suites_are_conservative(self):
        self.included = discovered(500, RunDecision.INCLUDED, suite_id=None)
        rep = self.with_runs(discovered(600, RunDecision.EXCLUDED_BY_FILTER, suite_id=99))
        self.assertEqual(defect_coverage(rep)[0], DefectCoverage.SELECTION_LIMITED)

    def test_runs_outside_the_lookback_do_not_limit_coverage(self):
        rep = self.with_runs(discovered(600, RunDecision.OUTSIDE_LOOKBACK))
        self.assertEqual(defect_coverage(rep), (DefectCoverage.COMPLETE, ()))

    def test_analysis_reports_the_same_coverage(self):
        rep = self.with_runs(discovered(600, RunDecision.EXCLUDED_BY_FILTER))
        analysis = analyze(rep, fx.settings())
        self.assertEqual(analysis.defect_coverage, DefectCoverage.SELECTION_LIMITED)
        self.assertEqual(analysis.defects_for(100).coverage, DefectCoverage.SELECTION_LIMITED)


class SourceVersionTests(unittest.TestCase):
    def test_each_source_carries_its_own_version(self):
        retried = outcome(
            500,
            100,
            rec(1, FAILED, 2, "BUG-1", version="1.0"),
            rec(2, None, 1.9, "BUG-2"),  # comment-only triage without a version
            rec(3, FAILED, 1.8, "BUG-3", version="1.1"),
        )
        self.assertEqual(retried.application_version, "1.1")
        defects = case(report([retried]))
        versions = {d.defect_id: d.sources[0].application_version for d in defects.defects}
        self.assertEqual(versions, {"BUG-1": "1.0", "BUG-2": None, "BUG-3": "1.1"})

    def test_final_version_is_not_attributed_to_intermediate_records(self):
        retried = outcome(500, 100, rec(1, FAILED, 2, "BUG-1"), rec(2, PASSED, 1.9, version="2.0"))
        self.assertEqual(retried.application_version, "2.0")
        source = case(report([retried])).defects[0].sources[0]
        self.assertEqual((source.is_final_result, source.application_version), (False, None))

    def test_configured_version_field(self):
        plain = outcome(500, 100, rec(1, FAILED, 1, "BUG-1", version="ignored"))
        result = dataclasses.replace(plain.results[0].result, custom_fields={"custom_app_build": "b-42"})
        record = dataclasses.replace(plain.results[0], result=result)
        configured = dataclasses.replace(plain, results=(record,), final=record)
        rep = report([configured])
        self.assertEqual(case(rep).defects[0].sources[0].application_version, "ignored")
        defects = aggregate_defects(rep, version_field="custom_app_build")[0]
        self.assertEqual(defects.defects[0].sources[0].application_version, "b-42")
        settings = fx.settings(mappings={"custom_fields": {"application_version": "custom_app_build"}})
        analysis = analyze(rep, settings)
        self.assertEqual(analysis.defects_for(100).defects[0].sources[0].application_version, "b-42")


if __name__ == "__main__":
    unittest.main()
