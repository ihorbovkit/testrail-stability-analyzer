"""Shared report content: Overview statuses, comments, and Report Info additions. No openpyxl needed."""

import dataclasses
import re
import unittest
from pathlib import Path
from types import MappingProxyType

from testrail_stability_analyzer.analysis import analyze
from testrail_stability_analyzer.collection import Applicability
from testrail_stability_analyzer.config import FeatureGroupSettings
from testrail_stability_analyzer.export import build_report, render_spreadsheet

from . import analysis_fixtures as af
from .fake_testrail import PASSED
from .report_fixtures import CASE_TYPES, _with_test, rows_by_case, sample_inputs, sample_settings

CLASS_NAME = MappingProxyType({"custom_class": "pkg.Newest"})

SNAPSHOT = Path(__file__).parents[1] / "fixtures" / "xml_stability_worksheet.xml"


def content(**kwargs):
    collection, analysis, settings = sample_inputs(**kwargs)
    return build_report(collection, analysis, settings, case_types=CASE_TYPES, notes=("synthetic report note",))


def overview(**kwargs):
    return rows_by_case(content(**kwargs).overview)


STATUS, RATE = "App A Platform X: Latest Status", "App A Platform X: Pass Rate"
STATUS_Y = "App A Platform Y: Latest Status"


class OverviewTests(unittest.TestCase):
    def test_columns_are_compact_and_dynamic(self):
        sheet = content().overview
        case_columns = ["Test Case ID", "Test Case Title", "Feature / Test Group", "Class Name", "Test Type", "Defects"]
        self.assertEqual(
            [name for name, _ in sheet.columns], [*case_columns, *["Latest Status", "Pass Rate"] * 4]
        )
        # Row 1: blank above the case columns, then one group spanning each Latest Status / Pass Rate pair.
        self.assertEqual(
            sheet.header_groups,
            ((None, 6), ("App A Platform X", 2), ("App A Platform Y", 2), ("App B Platform X", 2),
             ("App B Platform Y", 2)),
        )  # fmt: skip
        headers, rows = overview()
        self.assertEqual(headers[6:8], [STATUS, RATE])
        self.assertEqual(sorted(rows), list(range(100, 109)))  # one row per test case
        self.assertEqual([row[0].value for row in sheet.rows], list(range(100, 109)))

    def test_statuses_and_states(self):
        _, rows = overview()
        # Text: latest status or state. Background: historical Pass Rate (min_executions = 5).
        expected = {
            100: ("Passed", "overview_green"),  # 5/5 passed
            101: ("Failed", "overview_neutral"),  # 2 conclusive executions: too few for a Pass Rate band
            102: ("Passed", "overview_yellow"),  # inconsistent, 3/5 = 60%: the band, not the diagnosis
            103: ("Automation Issue", "overview_neutral"),  # 2 conclusive executions
            104: ("Not Applicable", "overview_neutral"),
            105: ("Not Run", "overview_neutral"),
            106: ("Partial", "overview_neutral"),
            107: ("Blocked", "overview_neutral"),  # insufficient history keeps the known latest status
            108: ("No History", "overview_neutral"),  # only an unidentified run
        }
        for case_id, (label, style) in expected.items():
            with self.subTest(case_id=case_id):
                cell, rate = rows[case_id][STATUS], rows[case_id][RATE]
                self.assertEqual((cell.value, cell.style), (label, style))
                self.assertEqual(rate.style, style)  # one background per Latest Status / Pass Rate pair
        self.assertEqual(rows[100]["App B Platform X: Latest Status"].value, "No History")

    def test_insufficient_history_shows_latest_status_with_diagnosis_in_comment(self):
        _, rows = overview()
        cell = rows[100][STATUS_Y]  # a single pass: the status is kept, the background is grey
        self.assertEqual((cell.value, cell.style), ("Passed", "overview_neutral"))
        self.assertEqual(
            cell.comment.splitlines(),
            ["Diagnosis: Insufficient History", "Confidence: Low", "Executions: 1", "Pass rate: 100.0%",
             "Coverage: Complete", "Latest result: 2026-05-31 00:00 UTC"],
        )  # fmt: skip
        self.assertNotIn("Stable", [row[STATUS_Y].value for row in rows.values()])

    def test_not_run_versus_applicability_unresolved(self):
        _, rows = overview()
        self.assertEqual(rows[101][STATUS_Y].value, "Not Run")  # not included in a scanned run
        _, rows = overview(applicability=Applicability.UNKNOWN)
        self.assertEqual(rows[101][STATUS_Y].value, "Applicability Unresolved")

    def test_comments_hold_only_analyzed_values(self):
        _, rows = overview()
        comments = [cell.comment for row in rows.values() for cell in row.values() if cell.comment]
        self.assertTrue(comments)
        base = ["Diagnosis", "Confidence", "Executions", "Pass rate", "Coverage", "Latest result"]
        rate = ["Pass rate", "Executions", "Diagnosis", "Confidence", "Coverage"]
        allowed = (base, [*base, "Blocked reason"], rate, [*rate, "Grey"],
                   ["No collected run of this application/platform included the case."])  # fmt: skip
        for comment in comments:
            self.assertIn([line.split(":", 1)[0] for line in comment.splitlines()], allowed)


class OverviewPassRateTests(unittest.TestCase):
    def test_numeric_pass_rate_or_empty(self):
        _, rows = overview()
        expected = {
            100: 1.0,  # 5/5
            101: 0.0,  # 0/2: below the minimum, still shown
            102: 0.6,  # 3/5
            103: 0.5,  # 1/2: an automation issue is conclusive
            104: None,  # not applicable: no conclusive execution
            105: None,  # not run
            106: 1.0,  # partial: the calculated rate is kept, the background is grey
            107: None,  # only blocked
            108: None,  # no history for the identified combination
        }
        for case_id, value in expected.items():
            with self.subTest(case_id=case_id):
                cell = rows[case_id][RATE]
                self.assertEqual(cell.value, value)
                self.assertNotIsInstance(cell.value, str)
                self.assertEqual(cell.number_format, "percent")

    def test_below_minimum_shows_the_rate_on_grey(self):
        _, rows = overview()
        cell = rows[101][RATE]
        self.assertEqual((cell.value, cell.style), (0.0, "overview_neutral"))
        self.assertEqual(
            cell.comment.splitlines(),
            ["Pass rate: 0.0% (0 of 2 conclusive executions passed)", "Executions: 2",
             "Diagnosis: Failure Evidence", "Confidence: Low", "Coverage: Complete",
             "Grey: fewer than 5 conclusive executions"],
        )  # fmt: skip

    def test_notes_without_a_rate_or_history(self):
        _, rows = overview()
        self.assertEqual(
            rows[107][RATE].comment.splitlines()[0], "Pass rate: n/a (no conclusive executions in the window)"
        )
        self.assertNotIn("Grey", rows[107][RATE].comment)
        no_history = rows[108][RATE]
        self.assertEqual((no_history.value, no_history.style, no_history.comment), (None, "overview_neutral", None))


class CaseColumnTests(unittest.TestCase):
    def test_disabled_feature_groups_omit_the_column_of_an_enriched_analysis(self):
        collection, analysis, settings = sample_inputs()
        self.assertIsNotNone(analysis.case_groups)
        disabled = dataclasses.replace(
            settings, report=dataclasses.replace(settings.report, feature_group=FeatureGroupSettings(enabled=False))
        )
        report = build_report(collection, analysis, disabled, case_types=CASE_TYPES)
        self.assertNotIn("Feature / Test Group", [name for name, _ in report.overview.columns])
        self.assertNotIn("Feature / Test Group", [name for name, _ in report.details.columns])
        info = {row[0].value: row[1].value for row in report.info.rows}
        self.assertEqual(info["Feature / Test Group"], "not requested")

    def case_fields(self, outcomes):
        """(Class Name, Test Type) of case 100."""
        settings = sample_settings()
        collection = af.report(outcomes, hist=af.histories(outcomes))
        sheet = build_report(collection, analyze(collection, settings), settings, case_types=CASE_TYPES).overview
        _, rows = rows_by_case(sheet)
        row = rows[100]
        return row["Class Name"].value, row["Test Type"].value

    def test_case_fields_come_from_the_newest_result_not_the_highest_run_id(self):
        newest = _with_test([af.outcome(400, 100, af.rec(4000, PASSED, 1))], type_id=3, custom_fields=CLASS_NAME)
        older = _with_test([af.outcome(900, 100, af.rec(9000, PASSED, 3))], type_id=1)
        self.assertEqual(self.case_fields([*newest, *older]), ("pkg.Newest", "Functional"))

    def test_equal_timestamps_prefer_the_higher_run_id(self):
        low = _with_test([af.outcome(400, 100, af.rec(4000, PASSED, 1))], type_id=1)
        high = _with_test([af.outcome(900, 100, af.rec(9000, PASSED, 1))], type_id=3)
        self.assertEqual(self.case_fields([*high, *low]), (None, "Functional"))

    def test_outcomes_without_a_dated_result_only_count_when_none_has_one(self):
        dated = _with_test([af.outcome(400, 100, af.rec(4000, PASSED, 3))], type_id=3)
        undated = _with_test([af.outcome(900, 100)], type_id=1)
        self.assertEqual(self.case_fields([*undated, *dated]), (None, "Functional"))
        only_undated = _with_test([af.outcome(901, 100)], type_id=1) + _with_test([af.outcome(900, 100)], type_id=3)
        self.assertEqual(self.case_fields(only_undated), (None, "Automated"))


class FilteredHistoryTests(unittest.TestCase):
    def test_histories_excluded_by_filters_are_not_counted_as_unidentified(self):
        collection, analysis, settings = sample_inputs()
        only_b = dataclasses.replace(settings, filters=dataclasses.replace(settings.filters, applications=("App B",)))
        info = build_report(collection, analysis, only_b, case_types=CASE_TYPES).info
        values = [(row[0].value, row[1].value) for row in info.rows]
        app_a = sum(1 for a in analysis.analyses if a.key.is_identified and a.key.application.name == "App A")
        self.assertGreater(app_a, 0)
        items = dict(values)
        self.assertEqual(items["Histories not shown (unidentified application or platform)"], 1)  # case 108
        self.assertEqual(items["Histories not shown (application or platform excluded by filters)"], app_a)
        limitations = [value for label, value in values if label == "Limitation"]
        self.assertIn(
            "1 history(ies) with an unidentified application or platform are not shown in the combination columns",
            limitations,
        )
        self.assertIn(
            f"{app_a} history(ies) with an application or platform excluded by the configured filters "
            "are not shown in the combination columns",
            limitations,
        )


class XmlCompatibilityTests(unittest.TestCase):
    def render(self):
        collection, analysis, settings = sample_inputs()
        return render_spreadsheet(
            collection, analysis, settings, case_types=CASE_TYPES, notes=("synthetic report note",)
        )

    def test_stability_worksheet_is_unchanged(self):
        """Snapshot generated before XLSX support was added; since extended only by the Test Case Title column."""
        xml = self.render()
        sheet = re.search(r' <Worksheet ss:Name="Stability">.*? </Worksheet>\n', xml, re.S).group(0)
        self.assertEqual(sheet, SNAPSHOT.read_text(encoding="utf-8"))
        self.assertEqual(re.findall(r'<Worksheet ss:Name="([^"]+)"', xml), ["Stability", "Report Info"])
        self.assertNotIn("Comment", xml)

    def test_report_info_gains_additive_summaries(self):
        xml = self.render()
        for text in ("Overview statuses (shown histories)", "Coverage note", "Partial (", "Legend: Confidence",
                     "Legend: Overview", "synthetic collection limitation", "synthetic report note"):  # fmt: skip
            self.assertIn(text, xml)


if __name__ == "__main__":
    unittest.main()
