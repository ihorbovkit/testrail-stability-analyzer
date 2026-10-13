import dataclasses
import unittest
import xml.etree.ElementTree as ET
from types import MappingProxyType

from testrail_stability_analyzer.analysis import analyze
from testrail_stability_analyzer.collection import Applicability, CaseGroup, FeatureGroupState
from testrail_stability_analyzer.export import combinations, render_spreadsheet, safe_text

from . import analysis_fixtures as af
from . import fake_testrail as fx
from .analysis_fixtures import PLAT_X, PLAT_Y, UNRESOLVED, histories, outcome, rec, report, runs
from .fake_testrail import AUTOMATION_ISSUE, FAILED, NOT_APPLICABLE, PASSED

NS = {"ss": "urn:schemas-microsoft-com:office:spreadsheet"}
SS = "{urn:schemas-microsoft-com:office:spreadsheet}"
METRICS = 9


def parse(xml):
    root = ET.fromstring(xml)
    sheets = {}
    for sheet in root.findall("ss:Worksheet", NS):
        rows = []
        for row in sheet.findall("ss:Table/ss:Row", NS):
            cells = []
            for cell in row.findall("ss:Cell", NS):
                data = cell.find("ss:Data", NS)
                cells.append(
                    (
                        None if data is None else data.text,
                        None if data is None else data.get(SS + "Type"),
                        cell.get(SS + "StyleID"),
                    )
                )
            rows.append(cells)
        sheets[sheet.get(SS + "Name")] = rows
    return root, sheets


def render(outcomes, *, settings=None, hist=None, case_types=None, notes=(), **fields):
    settings = settings or fx.settings()
    collected = report(outcomes, hist=hist, **fields)
    return render_spreadsheet(collected, analyze(collected, settings), settings, case_types=case_types, notes=notes)


def table(xml):
    """Stability rows as dicts keyed by header."""
    _, sheets = parse(xml)
    header, *rows = sheets["Stability"]
    names = [text for text, _, _ in header]
    return names, [dict(zip(names, row, strict=True)) for row in rows]


def info(xml):
    _, sheets = parse(xml)
    return [(row[0][0], row[1][0], row[1][2]) for row in sheets["Report Info"][1:]]


def column(app="App A", platform="Platform X", metric="Diagnosis"):
    return f"{app} | {platform}: {metric}"


class WorkbookStructureTests(unittest.TestCase):
    def test_valid_spreadsheetml_with_two_worksheets(self):
        xml = render(runs([PASSED] * 5))
        self.assertTrue(xml.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<?mso-application progid="Excel.Sheet"?>'))
        root, sheets = parse(xml)
        self.assertEqual(root.tag, SS + "Workbook")
        self.assertEqual(list(sheets), ["Stability", "Report Info"])
        styles = {style.get(SS + "ID") for style in root.findall("ss:Styles/ss:Style", NS)}
        self.assertLessEqual({"header", "green", "yellow", "red", "neutral", "percent", "datetime"}, styles)

    def test_columns_are_generated_from_configured_combinations(self):
        settings = fx.settings()
        self.assertEqual(
            combinations(settings),
            (("App A", "Platform X"), ("App A", "Platform Y"), ("App B", "Platform X"), ("App B", "Platform Y")),
        )
        names, _ = table(render(runs([PASSED] * 5), settings=settings))
        self.assertEqual(names[:3], ["Test Case ID", "Test Type", "Defects"])
        self.assertEqual(len(names), 3 + 4 * METRICS)
        self.assertEqual(names[3:12], [column(metric=m) for m in (
            "Latest Status", "Latest Result (UTC)", "Executions", "Pass Rate", "Failure Rate",
            "Automation Issue Rate", "Diagnosis", "Confidence", "Coverage",
        )])  # fmt: skip

    def test_filters_limit_the_combinations(self):
        settings = fx.settings(filters={"applications": ["App B"], "platforms": ["Platform Y"]})
        self.assertEqual(combinations(settings), (("App B", "Platform Y"),))

    def test_one_row_per_case_sorted_by_id(self):
        outcomes = [*runs([PASSED] * 5, case_id=101), *runs([FAILED] * 2, case_id=100, first_run=600)]
        outcomes += runs([PASSED], case_id=100, first_run=700, platform=PLAT_Y)
        _, rows = table(render(outcomes, hist=histories(outcomes, platforms=(PLAT_X, PLAT_Y))))
        self.assertEqual([row["Test Case ID"][:2] for row in rows], [("100", "Number"), ("101", "Number")])

    def test_output_is_deterministic(self):
        outcomes = runs([PASSED, FAILED, PASSED])
        self.assertEqual(render(outcomes), render(outcomes))


class CombinationValueTests(unittest.TestCase):
    def row(self, outcomes, **kwargs):
        _, rows = table(render(outcomes, **kwargs))
        return rows[0]

    def test_stable_combination(self):
        row = self.row(runs([PASSED] * 5))
        self.assertEqual(row[column(metric="Latest Status")], ("Passed", "String", "green"))
        self.assertEqual(row[column(metric="Executions")], ("5", "Number", None))
        self.assertEqual(row[column(metric="Pass Rate")], ("1.0", "Number", "percent"))
        self.assertEqual(row[column(metric="Failure Rate")], ("0.0", "Number", "percent"))
        self.assertEqual(row[column()], ("Stable", "String", "green"))
        self.assertEqual(row[column(metric="Confidence")][0], "Medium")
        self.assertEqual(row[column(metric="Coverage")][0], "Complete")
        latest = row[column(metric="Latest Result (UTC)")]
        self.assertEqual(latest[1:], ("DateTime", "datetime"))
        self.assertEqual(latest[0], (af.NOW - af.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000"))

    def test_diagnosis_colours(self):
        cases = {
            "Failure Evidence": ([FAILED] * 3, "red"),
            "Inconsistent": ([PASSED, FAILED, PASSED], "yellow"),
            "Automation Issue": ([AUTOMATION_ISSUE, PASSED], "yellow"),
            "Failure + Automation Issue": ([FAILED, AUTOMATION_ISSUE], "red"),
            "Insufficient History": ([PASSED] * 2, "neutral"),
            "N/A": ([NOT_APPLICABLE], "neutral"),
        }
        for label, (statuses, style) in cases.items():
            with self.subTest(label=label):
                self.assertEqual(self.row(runs(statuses))[column()], (label, "String", style))

    def test_failed_latest_status_is_not_labelled_a_defect(self):
        row = self.row(runs([FAILED] * 3))
        self.assertEqual(row[column(metric="Latest Status")], ("Failed", "String", "red"))
        self.assertNotIn("defect", row[column()][0].lower())

    def test_not_run_partial_and_unresolved_applicability_are_distinct(self):
        not_run = [outcome(500, 100)]  # included in a run, no result
        self.assertEqual(self.row(not_run)[column()][0], "Not Run")
        self.assertEqual(self.row(not_run)[column(metric="Latest Status")], ("Not Run", "String", "neutral"))

        partial = [outcome(500, 100, rec(1, PASSED, 1), complete=False)]
        self.assertEqual(self.row(partial)[column()], ("Partial", "String", "neutral"))

        executed = runs([PASSED])
        hist = histories(executed, platforms=(PLAT_X, PLAT_Y), applicability=Applicability.UNKNOWN)
        row = self.row(executed, hist=hist)
        self.assertEqual(row[column(platform="Platform Y")], ("Applicability Unresolved", "String", "neutral"))
        self.assertEqual(row[column(platform="Platform Y", metric="Latest Status")][0], "Applicability Unresolved")

    def test_combination_without_history(self):
        row = self.row(runs([PASSED] * 5))
        self.assertEqual(row[column(app="App B")], ("No History", "String", "neutral"))
        self.assertEqual(row[column(app="App B", metric="Executions")], (None, None, None))

    def test_rates_without_conclusive_executions_are_blank(self):
        row = self.row([outcome(500, 100)])
        self.assertEqual(row[column(metric="Pass Rate")], (None, None, "percent"))


class CaseColumnTests(unittest.TestCase):
    def test_shared_defects(self):
        outcomes = [
            outcome(500, 100, rec(1, FAILED, 2, defects="BUG-1, BUG-2")),
            outcome(501, 100, rec(2, FAILED, 1, defects="BUG-3"), platform=PLAT_Y),
        ]
        _, rows = table(render(outcomes, hist=histories(outcomes, platforms=(PLAT_X, PLAT_Y))))
        self.assertEqual(rows[0]["Defects"][0], "BUG-1, BUG-2, BUG-3")

    def test_test_type_names_or_ids(self):
        outcomes = [dataclasses.replace(o, test=dataclasses.replace(o.test, type_id=3)) for o in runs([PASSED])]
        _, rows = table(render(outcomes, case_types={3: "Functional"}))
        self.assertEqual(rows[0]["Test Type"][0], "Functional")
        _, rows = table(render(outcomes))
        self.assertEqual(rows[0]["Test Type"][0], "Type 3")

    def test_class_name_column_only_when_configured(self):
        tests = {"custom_class": "pkg.module.TestClass"}
        outcomes = [
            dataclasses.replace(o, test=dataclasses.replace(o.test, custom_fields=MappingProxyType(tests)))
            for o in runs([PASSED])
        ]
        names, _ = table(render(outcomes))
        self.assertNotIn("Class Name", names)
        settings = fx.settings(mappings={"custom_fields": {"class_name": "custom_class"}})
        names, rows = table(render(outcomes, settings=settings))
        self.assertEqual(names[:4], ["Test Case ID", "Class Name", "Test Type", "Defects"])
        self.assertEqual(rows[0]["Class Name"][0], "pkg.module.TestClass")

    def test_feature_group_column(self):
        outcomes = [*runs([PASSED], case_id=100), *runs([PASSED], case_id=101, first_run=600)]
        groups = {
            100: CaseGroup(100, FeatureGroupState.RESOLVED, 10, 3, ("Area Two", "Feature_Alpha", "Deep")),
            101: CaseGroup(101, FeatureGroupState.CASE_NOT_FOUND, 10),
        }
        names, _ = table(render(outcomes))
        self.assertNotIn("Feature / Test Group", names)

        names, rows = table(render(outcomes, case_groups=MappingProxyType(groups)))
        self.assertEqual(names[1], "Feature / Test Group")
        self.assertEqual(rows[0]["Feature / Test Group"], ("Area Two/Feature_Alpha/Deep", "String", None))
        self.assertEqual(rows[1]["Feature / Test Group"], ("Unknown (case not found)", "String", "neutral"))

        settings = fx.settings(report={"feature_group": {"max_depth": 2}})
        _, rows = table(render(outcomes, settings=settings, case_groups=MappingProxyType(groups)))
        self.assertEqual(rows[0]["Feature / Test Group"][0], "Area Two/Feature_Alpha")


class SafetyTests(unittest.TestCase):
    def test_safe_text(self):
        for raw, expected in (
            ("=SUM(A1:A2)", "'=SUM(A1:A2)"),
            ("+1", "'+1"),
            ("-cmd", "'-cmd"),
            ("@SUM(1)", "'@SUM(1)"),
            ("\tTAB", "'\tTAB"),
            ("plain=text", "plain=text"),
            ("bad\x00\x01\x1fchars￾", "badchars"),
            ("x" * 40_000, "x" * 32_766 + "…"),
        ):
            with self.subTest(raw=raw[:20]):
                self.assertEqual(safe_text(raw), expected)

    def test_external_values_are_escaped_and_neutralized(self):
        hostile = '=HYPERLINK("https://evil.example.invalid")'
        groups = {100: CaseGroup(100, FeatureGroupState.RESOLVED, 10, 1, ("R&D <Area>", 'Quote " \x07 bell'))}
        settings = fx.settings(
            mappings={
                "applications": [{"name": "@App <&>", "patterns": ["a"]}],
                "platforms": [{"name": "Platform X", "patterns": ["x"]}],
            }
        )
        app = af.DimensionValue(af.DimensionState.RESOLVED, "@App <&>")
        outcomes = [outcome(500, 100, rec(1, FAILED, 1, defects=hostile), app=app)]
        xml = render(outcomes, settings=settings, case_groups=MappingProxyType(groups))
        names, rows = table(xml)  # parses: escaping and invalid characters are handled
        self.assertEqual(rows[0]["Feature / Test Group"][0], 'R&D <Area>/Quote "  bell')
        self.assertTrue(rows[0]["Defects"][0].startswith("'=HYPERLINK"))
        self.assertIn("'@App <&> | Platform X: Diagnosis", names)
        self.assertNotIn("\x07", xml)

    def test_comments_and_case_content_are_never_exported(self):
        base = outcome(500, 100, rec(1, FAILED, 1))
        result = dataclasses.replace(base.results[0].result, comment="SECRET-COMMENT session_id=abc123")
        record = dataclasses.replace(base.results[0], result=result)
        content = MappingProxyType({"custom_steps": "SECRET-STEPS", "custom_preconds": "SECRET-PRECONDS"})
        test = dataclasses.replace(base.test, custom_fields=content, title="Confidential title")
        leaked = dataclasses.replace(base, results=(record,), final=record, test=test)
        xml = render([leaked])
        for secret in ("SECRET-COMMENT", "abc123", "SECRET-STEPS", "SECRET-PRECONDS", "Confidential title"):
            self.assertNotIn(secret, xml)


class ReportInfoTests(unittest.TestCase):
    def test_scope_coverage_limitations_and_legend(self):
        outcomes = [*runs([PASSED] * 5), outcome(800, 102, rec(9, FAILED, 1), app=UNRESOLVED, platform=PLAT_X)]
        xml = render(outcomes, limitations=("synthetic collection limitation",), notes=("synthetic report note",))
        items = info(xml)
        values = {label: value for label, value, _ in items}
        self.assertEqual(values["Collection mode"], "project-wide")
        self.assertEqual(values["Test cases"], "2")
        self.assertEqual(values["Histories not shown (unidentified application or platform)"], "1")
        self.assertEqual(values["Feature / Test Group"], "not requested")
        self.assertEqual(values["Collection status"], "OK")
        limitations = [value for label, value, _ in items if label == "Limitation"]
        self.assertIn("synthetic collection limitation", limitations)
        self.assertIn("synthetic report note", limitations)
        self.assertTrue(any("unidentified application or platform" in note for note in limitations))
        legend = {label: (value, style) for label, value, style in items if label.startswith("Legend")}
        self.assertEqual(legend["Legend: Red"][1], "red")
        self.assertIn("does not confirm an application defect", legend["Legend: Red"][0])

    def test_group_summary_and_partial_status(self):
        groups = {
            100: CaseGroup(100, FeatureGroupState.RESOLVED, 10, 1, ("Area One",)),
            101: CaseGroup(101, FeatureGroupState.NOT_FETCHED, 10),
        }
        outcomes = [*runs([PASSED], case_id=100), *runs([PASSED], case_id=101, first_run=600)]
        values = {
            label: (value, style)
            for label, value, style in info(render(outcomes, case_groups=groups, budget_exhausted=True))
        }
        self.assertEqual(values["Feature / Test Group"][0], "1 resolved from TestRail sections; unknown: not_fetched=1")
        self.assertEqual(values["Collection status"], ("PARTIAL", "neutral"))


if __name__ == "__main__":
    unittest.main()
