"""Native XLSX workbook, validated by reading it back with openpyxl. Skipped when openpyxl is not installed."""

import dataclasses
import io
import sys
import unittest
import zipfile
from datetime import datetime
from types import MappingProxyType
from unittest import mock

from testrail_stability_analyzer.analysis import analyze
from testrail_stability_analyzer.collection import CaseGroup, FeatureGroupState
from testrail_stability_analyzer.export import XlsxUnavailableError, render_xlsx, xlsx_available

from . import analysis_fixtures as af
from . import fake_testrail as fx
from .analysis_fixtures import outcome, rec
from .fake_testrail import FAILED, PASSED
from .report_fixtures import CASE_TYPES, HOSTILE_DEFECT, sample_inputs, xlsx_rows_by_case

try:
    import openpyxl
except ImportError:  # pragma: no cover - exercised only without the optional dependency
    openpyxl = None


def render(**kwargs):
    collection, analysis, settings = sample_inputs(**kwargs)
    return render_xlsx(collection, analysis, settings, case_types=CASE_TYPES, notes=("synthetic report note",))


def by_header(ws):
    return xlsx_rows_by_case(ws)


def fill(cell):
    return cell.fill.fgColor.rgb[-6:] if cell.fill.fill_type == "solid" else None


@unittest.skipUnless(openpyxl, "openpyxl is not installed")
class WorkbookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = render()
        cls.workbook = openpyxl.load_workbook(io.BytesIO(cls.data))

    def test_valid_package_without_formulas_or_external_links(self):
        with zipfile.ZipFile(io.BytesIO(self.data)) as package:
            self.assertIsNone(package.testzip())
            names = package.namelist()
        self.assertIn("xl/workbook.xml", names)
        self.assertFalse([name for name in names if "externalLink" in name])
        for ws in self.workbook.worksheets:
            self.assertEqual(ws._hyperlinks, [])
            for row in ws.iter_rows():
                for cell in row:
                    self.assertNotEqual(cell.data_type, "f", cell.coordinate)

    def test_worksheet_order(self):
        self.assertEqual(self.workbook.sheetnames, ["Stability Overview", "Stability Details", "Report Info"])

    def test_overview_columns_rows_and_statuses(self):
        headers, rows = by_header(self.workbook["Stability Overview"])
        combinations = ["App A Platform X", "App A Platform Y", "App B Platform X", "App B Platform Y"]
        self.assertEqual(
            headers[6:], [f"{name}: {metric}" for name in combinations for metric in ("Latest Status", "Pass Rate")]
        )
        self.assertEqual(list(rows), list(range(100, 109)))  # one row per case, numeric IDs
        cases = {
            100: ("Passed", "C6EFCE"),
            101: ("Failed", "EDEDED"),  # too few conclusive executions for a Pass Rate band
            102: ("Passed", "FFEB9C"),  # 60% Pass Rate
            104: ("Not Applicable", "EDEDED"),
            106: ("Partial", "EDEDED"),
            108: ("No History", "EDEDED"),
        }
        for case_id, (label, colour) in cases.items():
            with self.subTest(case_id=case_id):
                cell = rows[case_id]["App A Platform X: Latest Status"]
                self.assertEqual((cell.value, fill(cell)), (label, colour))
                self.assertEqual(fill(rows[case_id]["App A Platform X: Pass Rate"]), colour)
        note = rows[100]["App A Platform Y: Latest Status"].comment
        self.assertTrue(note.text.startswith("Diagnosis: Insufficient History\n"))

    def test_case_columns(self):
        _, rows = by_header(self.workbook["Stability Overview"])
        self.assertEqual(rows[100]["Feature / Test Group"].value, "Area Two/Feature_Alpha")
        self.assertEqual(rows[101]["Feature / Test Group"].value, "R&D <Area>")
        self.assertEqual(rows[102]["Feature / Test Group"].value, "Unknown (case not found)")
        self.assertIsNone(rows[101]["Class Name"].value)  # the field is not set on its tests
        self.assertEqual(rows[100]["Test Type"].value, "Functional")
        defects = rows[101]["Defects"].value
        # Identifiers are kept as entered; only a leading formula character of the whole cell is neutralized.
        self.assertEqual(sorted(defects.split(", ")), [HOSTILE_DEFECT, "BUG-1", "BUG-2"])
        self.assertFalse(defects.startswith("="))
        self.assertTrue(rows[101]["Defects"].alignment.wrap_text)
        self.assertIsNone(rows[105]["Defects"].value)

    def test_class_name_from_the_newest_test(self):
        tests = MappingProxyType({"custom_class": "pkg.module.TestClass"})
        base = outcome(500, 100, rec(1, PASSED, 1))
        collection = af.report([dataclasses.replace(base, test=dataclasses.replace(base.test, custom_fields=tests))])
        settings = fx.settings(mappings={"custom_fields": {"class_name": "custom_class"}})
        data = render_xlsx(collection, analyze(collection, settings), settings)
        _, rows = by_header(openpyxl.load_workbook(io.BytesIO(data))["Stability Overview"])
        self.assertEqual(rows[100]["Class Name"].value, "pkg.module.TestClass")
        self.assertTrue(rows[100]["Class Name"].alignment.wrap_text)

    def test_formula_injection_is_neutralized(self):
        outcomes = [outcome(500, 100, rec(1, FAILED, 1, defects="=1+1"))]
        groups = {100: CaseGroup(100, FeatureGroupState.RESOLVED, 10, 1, ("@SUM(A1)", "-x"))}
        collection = af.report(outcomes, case_groups=groups)
        settings = fx.settings()
        data = render_xlsx(collection, analyze(collection, settings), settings)
        _, rows = by_header(openpyxl.load_workbook(io.BytesIO(data))["Stability Overview"])
        self.assertEqual(rows[100]["Defects"].value, "'=1+1")
        self.assertEqual(rows[100]["Defects"].data_type, "s")
        self.assertEqual(rows[100]["Feature / Test Group"].value, "'@SUM(A1)/-x")

    def test_details_numeric_percentages_and_dates(self):
        headers, rows = by_header(self.workbook["Stability Details"])
        self.assertEqual(len(headers), 6 + 4 * 10)
        row = rows[102]
        rate = row["App A | Platform X: Pass Rate"]
        self.assertEqual((rate.value, rate.number_format), (0.6, "0.0%"))
        latest = row["App A | Platform X: Latest Result (UTC)"]
        self.assertIsInstance(latest.value, datetime)
        self.assertEqual(latest.number_format, "yyyy-mm-dd hh:mm")
        self.assertEqual(row["App A | Platform X: Executions"].value, 5)
        self.assertEqual(row["App A | Platform X: Diagnosis"].value, "Inconsistent")
        self.assertEqual(fill(row["App A | Platform X: Diagnosis"]), "FFEB9C")
        self.assertEqual(rows[100]["App A | Platform Y: Diagnosis"].value, "Insufficient History")
        self.assertEqual(rows[106]["App A | Platform X: Coverage"].value, "Partial")
        self.assertEqual(rows[106]["App A | Platform X: Diagnosis"].value, "Partial")

    def test_frozen_headers_filters_and_widths(self):
        for name, frozen in (("Stability Details", "B2"), ("Report Info", "A2")):
            ws = self.workbook[name]
            with self.subTest(sheet=name):
                self.assertEqual(ws.freeze_panes, frozen)
                self.assertEqual(ws.auto_filter.ref, f"A1:{ws.cell(1, ws.max_column).column_letter}{ws.max_row}")
                self.assertTrue(all(cell.font.bold for cell in ws[1]))
                self.assertGreater(ws.column_dimensions["A"].width, 10)
                self.assertEqual(list(ws.merged_cells.ranges), [])
        overview = self.workbook["Stability Overview"]
        self.assertEqual(overview.max_column, 6 + 4 * 2)  # compact: Latest Status and Pass Rate per combination
        self.assertEqual(overview.max_row, 2 + 9)  # two header rows, one row per test case
        self.assertGreater(overview.column_dimensions["A"].width, 10)

    def test_overview_two_level_header(self):
        ws = self.workbook["Stability Overview"]
        self.assertEqual(ws.freeze_panes, "B3")  # both header rows and the Test Case ID column
        self.assertEqual(ws.auto_filter.ref, "A2:N11")  # starts at the second header row, covers every data row
        # Only horizontal merges in row 1, one per combination over its Latest Status / Pass Rate pair.
        self.assertEqual(sorted(str(r) for r in ws.merged_cells.ranges), ["G1:H1", "I1:J1", "K1:L1", "M1:N1"])
        for merged in ws.merged_cells.ranges:
            self.assertEqual((merged.min_row, merged.max_row, merged.max_col - merged.min_col), (1, 1, 1))
        self.assertEqual(
            [ws.cell(1, column).value for column in (7, 9, 11, 13)],
            ["App A Platform X", "App A Platform Y", "App B Platform X", "App B Platform Y"],
        )
        # Case columns: headers in row 2 (the filter row), blank grey header cells above them.
        self.assertEqual(
            [ws.cell(2, column).value for column in range(1, 7)],
            ["Test Case ID", "Test Case Title", "Feature / Test Group", "Class Name", "Test Type", "Defects"],
        )
        for column in range(1, 7):
            above = ws.cell(1, column)
            self.assertIsNone(above.value)
            self.assertEqual(fill(above), "D9D9D9")
        self.assertEqual([ws.cell(2, column).value for column in range(7, 15)], ["Latest Status", "Pass Rate"] * 4)
        for column in range(1, 15):
            self.assertTrue(ws.cell(2, column).font.bold)
        self.assertTrue(ws.cell(1, 7).font.bold)
        self.assertEqual(ws.cell(1, 7).alignment.horizontal, "center")

    def test_overview_pass_rate_is_a_numeric_percentage(self):
        _, rows = by_header(self.workbook["Stability Overview"])
        rate = rows[102]["App A Platform X: Pass Rate"]
        self.assertEqual((rate.value, rate.number_format, rate.data_type), (0.6, "0.0%", "n"))
        self.assertEqual(fill(rate), "FFEB9C")
        self.assertTrue(rate.comment.text.startswith("Pass rate: 60.0% (3 of 5 conclusive executions passed)"))
        thin = rows[101]["App A Platform X: Pass Rate"]  # 0 of 2: shown, grey
        self.assertEqual((thin.value, fill(thin)), (0, "EDEDED"))
        self.assertIn("Grey: fewer than 5 conclusive executions", thin.comment.text)
        empty = rows[108]["App A Platform X: Pass Rate"]
        self.assertEqual((empty.value, fill(empty), empty.comment), (None, "EDEDED", None))
        status = rows[102]["App A Platform X: Latest Status"]
        self.assertEqual(status.number_format, "General")  # the status stays text

    def test_report_info(self):
        ws = self.workbook["Report Info"]
        items = [(row[0].value, row[1].value) for row in ws.iter_rows(min_row=2)]
        labels = [label for label, _ in items]
        self.assertIn("Overview statuses (shown histories)", labels)
        self.assertIn(("Limitation", "synthetic report note"), items)
        self.assertIn("Legend: Confidence", labels)
        self.assertIsInstance(dict(items)["Collected at (UTC)"], datetime)

    def test_sensitive_content_is_not_exported(self):
        text = "\n".join(
            str(cell.value) + str(cell.comment.text if cell.comment else "")
            for ws in self.workbook.worksheets
            for row in ws.iter_rows()
            for cell in row
        )
        for secret in ("Authorization", "session_id", "custom_steps"):
            self.assertNotIn(secret, text)


class MissingDependencyTests(unittest.TestCase):
    def test_clear_error_without_openpyxl(self):
        collection, analysis, settings = sample_inputs()
        with mock.patch.dict(sys.modules, {"openpyxl": None}):
            self.assertFalse(xlsx_available())
            with self.assertRaises(XlsxUnavailableError) as ctx:
                render_xlsx(collection, analysis, settings)
        self.assertIn('pip install "openpyxl>=3.1,<4"', str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
