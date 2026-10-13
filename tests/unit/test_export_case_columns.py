"""Test Case Title and Applicability columns, and Not Applicable versus No History. Synthetic data only."""

import dataclasses
import io
import unittest
from types import MappingProxyType

from testrail_stability_analyzer.analysis import analyze
from testrail_stability_analyzer.collection import ApplicabilityValueState, CaseApplicability
from testrail_stability_analyzer.export import build_report, render_spreadsheet, render_xlsx

from . import analysis_fixtures as af
from . import fake_testrail as fx
from .analysis_fixtures import PLAT_X, PLAT_Y, outcome, rec
from .fake_testrail import PASSED
from .test_export_spreadsheet import table

try:
    import openpyxl
except ImportError:  # pragma: no cover - exercised only without the optional dependency
    openpyxl = None

READ = ApplicabilityValueState.READ
HOSTILE_TITLE = '=HYPERLINK("https://evil.example.invalid","x")'
COMBINATIONS = ("App A Platform X", "App A Platform Y", "App B Platform X", "App B Platform Y")


def settings(all_label=None, **sections):
    mappings = {
        "applications": [
            {"name": "App A", "patterns": ["(?i)app-a"], "applicability_values": ["1", 3]},
            {"name": "App B", "patterns": ["(?i)app-b"], "applicability_values": [2, "3"]},
        ],
        "custom_fields": {"applicability": "custom_apps", **sections.pop("custom_fields", {})},
    }
    report = {"applicability": {"all_label": all_label}} if all_label else {}
    return fx.settings(mappings=mappings, report=report, **sections)


# Case ID -> applicability value. Every case ran once on App A / Platform X only.
VALUES = {
    100: CaseApplicability(READ, ("1",)),  # App A only: App B is excluded
    101: CaseApplicability(READ, ("3",)),  # both applications
    102: CaseApplicability(READ, ()),  # not set
    103: CaseApplicability(READ, ("9",)),  # unmapped
    104: CaseApplicability(READ, ("2", "9")),  # App B and an unmapped value
    105: CaseApplicability(ApplicabilityValueState.NOT_FETCHED),
    106: CaseApplicability(ApplicabilityValueState.CASE_NOT_FOUND),
    107: CaseApplicability(READ, ("2",)),  # App B only, yet executed on App A
}


def collection(outcomes=None, case_applicability=MappingProxyType(VALUES)):
    outcomes = outcomes or [outcome(500 + i, case_id, rec(5000 + i, PASSED, 1)) for i, case_id in enumerate(VALUES)]
    hist = af.histories(outcomes, platforms=(PLAT_X, PLAT_Y))
    return af.report(outcomes, hist=hist, case_applicability=case_applicability)


def content(collected=None, config=None):
    collected = collected or collection()
    config = config or settings()
    return build_report(collected, analyze(collected, config), config)


def rows(sheet):
    headers = [name for name, _ in sheet.columns]
    return headers, {row[0].value: dict(zip(headers, row, strict=True)) for row in sheet.rows}


def info(report):
    return [(row[0].value, row[1].value) for row in report.info.rows]


class ApplicabilityColumnTests(unittest.TestCase):
    def test_column_is_added_to_overview_and_details_when_configured(self):
        report = content()
        for sheet in (report.overview, report.details):
            headers, _ = rows(sheet)
            with self.subTest(sheet=sheet.name):
                self.assertEqual(
                    headers[:5], ["Test Case ID", "Test Case Title", "Test Type", "Defects", "Applicability"]
                )

    def test_column_is_omitted_when_not_configured(self):
        report = content(collection(case_applicability=None), fx.settings())
        for sheet in (report.overview, report.details):
            self.assertNotIn("Applicability", [name for name, _ in sheet.columns])
        self.assertIn(("Test case applicability", "not configured"), info(report))

    def test_labels_come_from_application_mappings(self):
        expected = {
            100: ("App A", None),
            101: ("App A, App B", None),
            102: ("Not set", "neutral"),
            103: ("Unmapped", "neutral"),
            104: ("App B, Unmapped", "neutral"),
            105: ("Unknown (not fetched)", "neutral"),
            106: ("Unknown (case not found)", "neutral"),
            107: ("App B", None),
        }
        _, by_case = rows(content().overview)
        for case_id, label in expected.items():
            with self.subTest(case_id=case_id):
                cell = by_case[case_id]["Applicability"]
                self.assertEqual((cell.value, cell.style), label)

    def test_all_label_only_when_every_application_matches(self):
        _, by_case = rows(content(config=settings(all_label="Both")).overview)
        self.assertEqual(by_case[101]["Applicability"].value, "Both")
        self.assertEqual(by_case[100]["Applicability"].value, "App A")
        self.assertEqual(by_case[104]["Applicability"].value, "App B, Unmapped")

    def test_all_label_is_not_used_with_an_unmapped_value(self):
        values = MappingProxyType({100: CaseApplicability(READ, ("3", "9"))})
        collected = collection([outcome(500, 100, rec(5000, PASSED, 1))], values)
        _, by_case = rows(content(collected, settings(all_label="Both")).overview)
        self.assertEqual(by_case[100]["Applicability"].value, "App A, App B, Unmapped")


class NotApplicableVersusNoHistoryTests(unittest.TestCase):
    def test_overview_distinguishes_exclusion_from_missing_history(self):
        _, by_case = rows(content().overview)
        for platform in ("Platform X", "Platform Y"):
            cell = by_case[100][f"App B {platform}"]
            self.assertEqual((cell.value, cell.style), ("Not Applicable", "neutral"))
            self.assertIn("applicability value excludes this application", cell.comment)
        self.assertIn("the case applies to this application", by_case[101]["App B Platform X"].comment)
        for case_id in (102, 103, 104, 105, 106):  # unset, unmapped, or unread values never exclude
            with self.subTest(case_id=case_id):
                self.assertEqual(by_case[case_id]["App B Platform X"].value, "No History")

    def test_unresolved_values_are_explained_in_the_note(self):
        _, by_case = rows(content().overview)
        for case_id in (102, 103, 105, 106):
            with self.subTest(case_id=case_id):
                note = by_case[case_id]["App B Platform X"].comment
                self.assertEqual(
                    note.splitlines(),
                    ["No collected run of this application/platform included the case.",
                     "Applicability: unresolved (not set, unmapped, or not fetched)."],
                )  # fmt: skip

    def test_an_actual_result_takes_precedence(self):
        _, by_case = rows(content().overview)
        self.assertEqual(by_case[107]["App A Platform X"].value, "Passed")  # executed despite the exclusion
        self.assertEqual(by_case[107]["App B Platform X"].value, "No History")  # applies, never executed

    def test_details_show_na_for_an_excluded_combination(self):
        _, by_case = rows(content().details)
        row = by_case[100]
        self.assertEqual(row["App B | Platform X: Latest Status"].value, "N/A")
        self.assertEqual(row["App B | Platform X: Diagnosis"].value, "N/A")
        self.assertIsNone(row["App B | Platform X: Executions"].value)
        self.assertEqual(by_case[101]["App B | Platform X: Latest Status"].value, "No History")
        self.assertEqual(by_case[100]["App A | Platform X: Latest Status"].value, "Passed")

    def test_without_configuration_missing_history_stays_no_history(self):
        _, by_case = rows(content(collection(case_applicability=None), fx.settings()).overview)
        cell = by_case[100]["App B Platform X"]
        self.assertEqual(cell.value, "No History")
        self.assertEqual(cell.comment, "No collected run of this application/platform included the case.")

    def test_report_info_summarizes_applicability(self):
        items = dict(info(content()))
        self.assertEqual(
            items["Test case applicability"],
            "App A=1, App A, App B=1, App B=1, App B, Unmapped=1, Not set=1, Unknown (case not found)=1, "
            "Unknown (not fetched)=1, Unmapped=1",
        )
        # Case 100 on both platforms of App B.
        self.assertEqual(items["Not Applicable without history (excluded by applicability)"], 2)
        self.assertIn("Legend: Applicability", items)


class TitleColumnTests(unittest.TestCase):
    def overview(self, outcomes, config=None):
        collected = af.report(outcomes)
        config = config or fx.settings()
        report = build_report(collected, analyze(collected, config), config)
        return report, rows(report.overview)[1]

    def test_title_is_the_second_column_in_both_worksheets(self):
        report, by_case = self.overview([outcome(500, 100, rec(1, PASSED, 1))])
        for sheet in (report.overview, report.details):
            self.assertEqual([name for name, _ in sheet.columns][:2], ["Test Case ID", "Test Case Title"])
        self.assertEqual(by_case[100]["Test Case Title"].value, "Case 100")

    def test_title_is_exported_as_entered(self):
        title = "  Leading and trailing spaces\t "
        _, by_case = self.overview([outcome(500, 100, rec(1, PASSED, 1), title=title)])
        self.assertEqual(by_case[100]["Test Case Title"].value, title)

    def test_newest_test_title_wins_and_older_titles_fill_gaps(self):
        newest = outcome(400, 100, rec(4000, PASSED, 1), title="Renamed title")
        older = outcome(900, 100, rec(9000, PASSED, 3), title="Original title")
        _, by_case = self.overview([newest, older])
        self.assertEqual(by_case[100]["Test Case Title"].value, "Renamed title")
        untitled = outcome(400, 100, rec(4000, PASSED, 1), title="")
        _, by_case = self.overview([untitled, older])
        self.assertEqual(by_case[100]["Test Case Title"].value, "Original title")

    def test_missing_title_is_reported_and_never_taken_from_the_class_name(self):
        base = outcome(500, 100, rec(1, PASSED, 1), title="   ")
        test = dataclasses.replace(base.test, custom_fields=MappingProxyType({"custom_class": "pkg.TestClass"}))
        config = fx.settings(mappings={"custom_fields": {"class_name": "custom_class"}})
        outcomes = [dataclasses.replace(base, test=test), outcome(501, 101, rec(2, PASSED, 1))]
        report, by_case = self.overview(outcomes, config)
        cell = by_case[100]["Test Case Title"]
        self.assertEqual((cell.value, cell.style), ("Title unavailable", "neutral"))
        self.assertEqual(by_case[100]["Class Name"].value, "pkg.TestClass")
        limitations = [value for label, value in info(report) if label == "Limitation"]
        self.assertIn(
            "1 test case title(s) not available in the collected test data; shown as 'Title unavailable'", limitations
        )

    def test_hostile_title_is_neutralized_in_xml(self):
        collected = af.report([outcome(500, 100, rec(1, PASSED, 1), title=HOSTILE_TITLE + " <&>")])
        config = fx.settings()
        names, parsed = table(render_spreadsheet(collected, analyze(collected, config), config))
        self.assertEqual(names[1], "Test Case Title")
        self.assertEqual(parsed[0]["Test Case Title"], ("'" + HOSTILE_TITLE + " <&>", "String", None))


@unittest.skipUnless(openpyxl, "openpyxl is not installed")
class WorkbookTests(unittest.TestCase):
    def test_title_applicability_and_notes_in_xlsx(self):
        outcomes = [outcome(500, 100, rec(1, PASSED, 1), title=HOSTILE_TITLE)]
        values = MappingProxyType({100: CaseApplicability(READ, ("1",))})
        collected = collection(outcomes, values)
        config = settings(all_label="Both")
        workbook = openpyxl.load_workbook(io.BytesIO(render_xlsx(collected, analyze(collected, config), config)))
        for name in ("Stability Overview", "Stability Details"):
            ws = workbook[name]
            headers = [cell.value for cell in ws[1]]
            with self.subTest(sheet=name):
                self.assertEqual(headers[:2], ["Test Case ID", "Test Case Title"])
                self.assertIn("Applicability", headers)
                title = ws.cell(2, 2)
                self.assertEqual((title.value, title.data_type), ("'" + HOSTILE_TITLE, "s"))
                self.assertTrue(title.alignment.wrap_text)
        ws = workbook["Stability Overview"]
        headers = [cell.value for cell in ws[1]]
        row = dict(zip(headers, ws[2], strict=True))
        self.assertEqual(row["Applicability"].value, "App A")
        self.assertEqual(row["App B Platform X"].value, "Not Applicable")
        self.assertIn("applicability value excludes", row["App B Platform X"].comment.text)
        self.assertEqual([row[c].value for c in COMBINATIONS[:2]], ["Passed", "Not Run"])


if __name__ == "__main__":
    unittest.main()
