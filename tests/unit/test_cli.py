import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from testrail_stability_analyzer import cli

from . import fake_testrail as fx
from .fake_testrail import AUTOMATION_ISSUE, FAILED, NOW, PASSED, FakeTestRail, days_ago
from .fakes import FAKE_KEY, FAKE_USER
from .test_export_spreadsheet import table

try:
    import openpyxl
except ImportError:  # pragma: no cover - exercised only without the optional dependency
    openpyxl = None

CONFIG = """
[testrail]
base_url = "https://example.testrail.io"
project_id = 1

[http]
max_retries = 0

[[mappings.applications]]
name = "App A"
patterns = ['(?i)app-a']
sources = ["plan_name", "run_name"]

[[mappings.platforms]]
name = "Platform X"
patterns = ['(?i)plat-x']
sources = ["configuration", "run_name"]

[[mappings.platforms]]
name = "Platform Y"
patterns = ['(?i)plat-y']
sources = ["configuration", "run_name"]

[mappings.statuses]
automation_issue = ["custom_status1"]
not_applicable = ["custom_status2"]
"""

ENVIRON = {"TESTRAIL_USERNAME": FAKE_USER, "TESTRAIL_API_KEY": FAKE_KEY}
SENSITIVE_COMMENT = "Synthetic failure session_id=abc123 at https://assets.example.invalid/shot.png"
SENSITIVE_DEFECT = "SYNTH-4242"
PLAN_NAME = "App-A Confidential Regression"
RUN_NAME = "Confidential nightly"


def build_server():
    server = FakeTestRail()
    t = days_ago(3)
    server.add_plan(900, PLAN_NAME, created=days_ago(4))
    server.add_plan_run(
        900,
        fx.run(501, RUN_NAME, created=days_ago(4), config="Plat-X"),
        tests=[fx.test(1, 100, 501), fx.test(2, 101, 501)],
        results=[
            fx.result(11, 1, FAILED, t, comment=SENSITIVE_COMMENT, defects=SENSITIVE_DEFECT),
            fx.result(12, 1, AUTOMATION_ISSUE, t + 60, comment="Synthetic triage note"),
            fx.result(13, 2, PASSED, t),
        ],
    )
    server.add_plan_run(
        900,
        fx.run(502, RUN_NAME, created=days_ago(4), config="Unknown-Platform"),
        tests=[fx.test(3, 100, 502)],
        results=[fx.result(14, 3, PASSED, t)],
    )
    server.add_run(
        fx.run(601, "App-A smoke Plat-X", created=days_ago(1)),
        tests=[fx.test(5, 100, 601)],
        results=[fx.result(16, 5, PASSED, days_ago(1))],
    )
    return server


class CliTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name) / "config.toml"
        self.config.write_text(CONFIG)
        self.server = build_server()

    def run_cli(self, *argv, environ=ENVIRON, config=True):
        stdout, stderr = io.StringIO(), io.StringIO()
        args = list(argv) + (["--config", str(self.config)] if config else [])
        code = cli.main(args, stdout=stdout, stderr=stderr, environ=environ, transport=self.server, clock=lambda: NOW)
        return code, stdout.getvalue(), stderr.getvalue()


class ArgumentValidationTests(CliTestCase):
    def test_missing_command_or_ids(self):
        for argv in ([], ["collect"], ["collect", "plan"], ["collect", "run"]):
            with self.subTest(argv=argv):
                code, _, stderr = self.run_cli(*argv, config=False)
                self.assertEqual(code, cli.EXIT_INVALID_CONFIGURATION)
                self.assertIn("usage:", stderr)
        self.assertEqual(self.server.calls, [])

    def test_invalid_numbers(self):
        for argv in (
            ["collect", "plan", "0"],
            ["collect", "plan", "abc"],
            ["collect", "run", "5", "--max-results", "-1"],
            ["collect", "run", "5", "--max-requests", "0"],
            ["collect", "plan", "900", "--run-id", "x"],
        ):
            with self.subTest(argv=argv):
                code, _, _ = self.run_cli(*argv, config=False)
                self.assertEqual(code, cli.EXIT_INVALID_CONFIGURATION)

    def test_invalid_configuration(self):
        code, _, stderr = self.run_cli("collect", "plan", "900", config=False, environ={})

        self.assertEqual(code, cli.EXIT_INVALID_CONFIGURATION)
        self.assertIn("testrail.base_url is required", stderr)
        self.assertIn("testrail.project_id is required", stderr)

    def test_inconsistent_overrides_are_validated(self):
        code, _, stderr = self.run_cli("collect", "plan", "900", "--window-days", "90", "--lookback-days", "30")

        self.assertEqual(code, cli.EXIT_INVALID_CONFIGURATION)
        self.assertIn("history.lookback_limit_days", stderr)

    def test_keychain_without_names_is_a_configuration_error(self):
        code, _, stderr = self.run_cli("collect", "plan", "900", "--credential-provider", "keychain")

        self.assertEqual(code, cli.EXIT_INVALID_CONFIGURATION)
        self.assertIn("credential_provider.keychain_service is required", stderr)
        self.assertEqual(self.server.calls, [])


class ExitCodeTests(CliTestCase):
    def test_complete_targeted_collection_succeeds(self):
        code, stdout, _ = self.run_cli("collect", "plan", "900", "--lookback-days", "0")

        self.assertEqual(code, cli.EXIT_OK, stdout)
        # Run 502 is unidentified (per-run key), so the selection does not limit it.
        self.assertIn("coverage: complete=1, selection_limited=2", stdout)
        self.assertIn("Collection: OK", stdout)

    def test_capped_collection_is_partial(self):
        code, stdout, _ = self.run_cli("collect", "plan", "900", "--max-results", "2")

        self.assertEqual(code, cli.EXIT_PARTIAL)
        self.assertIn("Result records fetched: 2 (cap: 2; cap reached: yes)", stdout)
        self.assertIn("Collection: PARTIAL", stdout)

    def test_some_invalid_ids_are_partial(self):
        self.server.errors["get_plan/999"] = (400, "Field :plan_id is not a valid test plan.")
        code, stdout, _ = self.run_cli("collect", "plan", "999", "900")

        self.assertEqual(code, cli.EXIT_PARTIAL)
        self.assertIn("Invalid selection: plan 999: plan does not exist or is not a valid ID (HTTP 400)", stdout)

    def test_only_invalid_ids_is_an_api_error(self):
        self.server.errors["get_run/999"] = (400, "Field :run_id is not a valid test run.")
        code, _, stderr = self.run_cli("collect", "run", "999")

        self.assertEqual(code, cli.EXIT_API_ERROR)
        self.assertIn("none of the selected plans or runs could be collected", stderr)

    def test_only_invalid_ids_write_no_report(self):
        self.server.errors["get_run/999"] = (400, "Field :run_id is not a valid test run.")
        report = self.config.parent / "report.xml"
        code, stdout, stderr = self.run_cli("collect", "run", "999", "--xml-report", str(report))

        self.assertEqual(code, cli.EXIT_API_ERROR)
        self.assertIn("none of the selected plans or runs could be collected", stderr)
        self.assertNotIn("report written", stdout)
        self.assertFalse(report.exists())
        metadata = ("get_sections/", "get_cases/", "get_suites/", "get_case_types")
        self.assertEqual([call for call in self.server.calls if call.startswith(metadata)], [])

    def test_authentication_failure_is_an_api_error(self):
        self.server.errors["get_statuses"] = (401, "Authentication failed: invalid or missing user/password.")
        code, stdout, stderr = self.run_cli("collect", "plan", "900")

        self.assertEqual(code, cli.EXIT_API_ERROR)
        self.assertIn("TestRailAuthenticationError (HTTP 401)", stderr)
        self.assertNotIn("Authentication failed", stderr)
        self.assertEqual(stdout, "")
        self.assertNotIn(FAKE_KEY, stderr)

    def test_server_error_text_is_never_printed(self):
        server_text = f"{SENSITIVE_COMMENT} defect {SENSITIVE_DEFECT} echoed key {FAKE_KEY} user {FAKE_USER}"
        self.server.errors["get_plan/900"] = (500, server_text)
        code, stdout, stderr = self.run_cli("collect", "plan", "900")

        self.assertEqual(code, cli.EXIT_API_ERROR)
        self.assertEqual(stderr, "TestRail API error: TestRailHTTPError (HTTP 500); response details are not shown\n")
        for sensitive in (SENSITIVE_COMMENT, "abc123", "assets.example.invalid", SENSITIVE_DEFECT, FAKE_KEY, FAKE_USER):
            self.assertNotIn(sensitive, stdout + stderr)

    def test_missing_credentials_are_reported_without_requests(self):
        code, _, stderr = self.run_cli("collect", "plan", "900", environ={})

        self.assertEqual(code, cli.EXIT_API_ERROR)
        self.assertIn("TESTRAIL_USERNAME", stderr)
        self.assertEqual(self.server.calls, [])

    def test_request_budget_exhaustion_is_partial(self):
        code, stdout, _ = self.run_cli("collect", "plan", "900", "--max-requests", "3")

        self.assertEqual(code, cli.EXIT_PARTIAL)
        self.assertIn("API requests: 3 (budget: 3)", stdout)
        self.assertIn("Request budget exhausted: yes", stdout)

    def test_sample_without_eligible_plan(self):
        code, stdout, _ = self.run_cli("collect", "sample", "--max-candidates", "1")
        self.assertEqual(code, cli.EXIT_OK)  # plan 900 is eligible

        self.server.plans.clear()
        code, stdout, _ = self.run_cli("collect", "sample")
        self.assertEqual(code, cli.EXIT_NO_TARGET)
        self.assertIn("No eligible Test Plan found", stdout)


class CollectionCommandTests(CliTestCase):
    def test_run_command_with_plan_option_is_a_mixed_selection(self):
        code, stdout, _ = self.run_cli("collect", "run", "601", "--plan-id", "900", "--lookback-days", "0")

        self.assertEqual(code, cli.EXIT_OK, stdout)
        self.assertIn("Selected plan IDs: 900", stdout)
        self.assertIn("Selected run IDs: 601", stdout)
        self.assertIn("Runs inspected: 3 (included 3)", stdout)

    def test_targeted_command_skips_project_wide_discovery(self):
        self.run_cli("collect", "plan", "900")

        self.assertNotIn("get_runs/1", self.server.calls)
        self.assertNotIn("get_plans/1", self.server.calls)
        self.assertEqual(self.server.calls[:2], ["get_statuses", "get_plan/900"])

    def test_sample_uses_validation_defaults(self):
        code, stdout, _ = self.run_cli("collect", "sample")

        self.assertIn("Sample selection: plan 900", stdout)
        self.assertIn("(cap: 15;", stdout)
        self.assertIn("(budget: 20)", stdout)
        self.assertEqual(self.server.calls.count("get_plan/900"), 1)  # fetched once, reused by the collector
        calls = zip(self.server.calls, self.server.params, strict=True)
        results_params = [params for call, params in calls if call.startswith("get_results")]
        self.assertEqual(results_params[0]["limit"], "15")

    def test_sample_defaults_can_be_overridden(self):
        _, stdout, _ = self.run_cli("collect", "sample", "--max-results", "0", "--max-requests", "50")

        self.assertIn("(cap: none;", stdout)
        self.assertIn("(budget: 50)", stdout)

    def test_selection_in_configuration_is_replaced_by_the_command_line(self):
        self.config.write_text(CONFIG + "\n[collection]\nrun_ids = [601]\n")
        _, stdout, _ = self.run_cli("collect", "plan", "900")

        self.assertIn("Selected run IDs: none", stdout)
        self.assertNotIn("get_run/601", self.server.calls)


class SanitizedOutputTests(CliTestCase):
    def test_summary_contains_counts_but_no_sensitive_values(self):
        code, stdout, stderr = self.run_cli("collect", "plan", "900", "--lookback-days", "0")
        output = stdout + stderr

        for secret in (FAKE_KEY, FAKE_USER, SENSITIVE_COMMENT, "abc123", "assets.example.invalid", SENSITIVE_DEFECT,
                       "Synthetic triage note", PLAN_NAME, RUN_NAME, "Authorization"):
            self.assertNotIn(secret, output)
        self.assertIn("Project ID: 1", stdout)
        self.assertIn("Plans inspected: 900", stdout)
        self.assertIn("run 501 (plan 900): included; application=App A, platform=Platform X", stdout)
        self.assertIn("Unresolved classification: run 502 platform unresolved", stdout)
        self.assertIn("Test instances examined: 3", stdout)
        self.assertIn("Result records fetched: 4 (cap: none; cap reached: no)", stdout)
        self.assertIn("  with comments: 2", stdout)
        self.assertIn("  with linked defects: 1", stdout)
        self.assertIn("tests with retries or triage (more than one record in a run): 1", stdout)
        self.assertIn("Final status categories: automation_issue=1, passed=2", stdout)
        self.assertIn("groups: App A / Platform X=", stdout)
        self.assertIn("unidentified (grouped per run)=1", stdout)
        self.assertIn("API requests: 6", stdout)

    def test_summary_is_deterministic(self):
        first = self.run_cli("collect", "plan", "900")
        self.server = build_server()
        self.assertEqual(first, self.run_cli("collect", "plan", "900"))


class XmlReportCommandTests(CliTestCase):
    METADATA = ("get_sections/", "get_cases/", "get_suites/", "get_case_types")

    def setUp(self):
        super().setUp()
        self.server.cases[10] = [fx.payloads.case(100, section_id=2), fx.payloads.case(101, section_id=1)]
        self.server.sections[10] = [fx.payloads.section(1, "Area One"), fx.payloads.section(2, "Feature", parent_id=1)]
        self.report = self.config.parent / "report.xml"

    def metadata_calls(self):
        return [call for call in self.server.calls if call.startswith(self.METADATA)]

    def test_report_with_feature_groups(self):
        code, stdout, _ = self.run_cli("collect", "plan", "900", "--lookback-days", "0", "--xml-report", str(self.report))

        self.assertEqual(code, cli.EXIT_OK, stdout)
        self.assertIn(f"XML report written: {self.report}", stdout)
        self.assertIn("Feature / Test Groups: resolved=2; unknown: none", stdout)
        self.assertEqual(self.metadata_calls(), ["get_cases/1", "get_sections/1", "get_case_types"])
        self.assertIn("API requests: 9", stdout)
        xml = self.report.read_text(encoding="utf-8")
        self.assertIn("<Data ss:Type=\"String\">Feature / Test Group</Data>", xml)
        self.assertIn("<Data ss:Type=\"String\">Area One/Feature</Data>", xml)
        self.assertIn("<Data ss:Type=\"String\">Functional</Data>", xml)  # case type 3
        for sensitive in (SENSITIVE_COMMENT, "abc123", "assets.example.invalid", "Synthetic triage note",
                          PLAN_NAME, RUN_NAME, FAKE_KEY, FAKE_USER):
            self.assertNotIn(sensitive, xml)

    def test_collection_only_makes_no_metadata_requests(self):
        code, stdout, _ = self.run_cli("collect", "plan", "900", "--lookback-days", "0")

        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(self.metadata_calls(), [])
        self.assertNotIn("Feature / Test Groups", stdout)
        self.assertNotIn("XML report written", stdout)

    def test_disabled_feature_groups(self):
        self.config.write_text(CONFIG + "\n[report.feature_group]\nenabled = false\n")
        code, _, _ = self.run_cli("collect", "plan", "900", "--xml-report", str(self.report))

        self.assertEqual(self.metadata_calls(), ["get_case_types"])
        names, _ = table(self.report.read_text(encoding="utf-8"))
        self.assertEqual(names[:3], ["Test Case ID", "Test Type", "Defects"])

    def test_budget_exhausted_by_metadata_keeps_the_collection_result(self):
        code, stdout, _ = self.run_cli(
            "collect", "plan", "900", "--lookback-days", "0", "--max-requests", "6", "--xml-report", str(self.report)
        )

        self.assertEqual(code, cli.EXIT_OK, stdout)  # collection itself completed
        self.assertIn("Request budget exhausted: no", stdout)
        self.assertIn("Feature / Test Groups: resolved=0; unknown: not_fetched=2", stdout)
        xml = self.report.read_text(encoding="utf-8")
        self.assertIn("Unknown (not fetched)", xml)
        self.assertIn("Test Type shows TestRail type IDs", xml)
        # Stability results are exported unchanged.
        self.assertIn('<Cell ss:StyleID="yellow"><Data ss:Type="String">Automation Issue</Data></Cell>', xml)

    def test_invalid_report_path_is_rejected_before_requests(self):
        for path in (self.config.parent / "missing" / "report.xml", self.config.parent):
            with self.subTest(path=path):
                code, _, stderr = self.run_cli("collect", "plan", "900", "--xml-report", str(path))
                self.assertEqual(code, cli.EXIT_INVALID_CONFIGURATION)
                self.assertIn("invalid report path", stderr)
        self.assertEqual(self.server.calls, [])

    def test_same_report_file_via_another_path_is_rejected_before_requests(self):
        link = self.config.parent / "link.xml"
        link.symlink_to(self.report)
        hard_link = self.config.parent / "hard.xml"
        self.report.write_text("existing")
        hard_link.hardlink_to(self.report)
        for other in (self.report.absolute(), self.config.parent / "." / "report.xml", link, hard_link):
            with self.subTest(other=other):
                code, _, stderr = self.run_cli(
                    "collect", "plan", "900", "--xml-report", str(self.report), "--xlsx-report", str(other)
                )
                self.assertEqual(code, cli.EXIT_INVALID_CONFIGURATION)
                self.assertIn("--xml-report and --xlsx-report must be different files", stderr)
        self.assertEqual(self.server.calls, [])


@unittest.skipUnless(openpyxl, "openpyxl is not installed")
class XlsxReportCommandTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.server.cases[10] = [fx.payloads.case(100, section_id=2), fx.payloads.case(101, section_id=1)]
        self.server.sections[10] = [fx.payloads.section(1, "Area One"), fx.payloads.section(2, "Feature", parent_id=1)]
        self.xlsx = self.config.parent / "report.xlsx"
        self.xml = self.config.parent / "report.xml"

    def metadata_calls(self):
        return [call for call in self.server.calls if call.startswith(XmlReportCommandTests.METADATA)]

    def test_xlsx_report(self):
        code, stdout, _ = self.run_cli(
            "collect", "plan", "900", "--lookback-days", "0", "--xlsx-report", str(self.xlsx)
        )

        self.assertEqual(code, cli.EXIT_OK, stdout)
        self.assertIn(f"XLSX report written: {self.xlsx}", stdout)
        workbook = openpyxl.load_workbook(self.xlsx)
        self.assertEqual(workbook.sheetnames, ["Stability Overview", "Stability Details", "Report Info"])
        overview = workbook["Stability Overview"]
        self.assertEqual(
            [cell.value for cell in overview[1]],
            ["Test Case ID", "Feature / Test Group", "Test Type", "Defects", "App A Platform X", "App A Platform Y"],
        )
        self.assertEqual(overview["B2"].value, "Area One/Feature")
        self.assertEqual(overview["E2"].value, "Automation Issue")
        texts = " ".join(str(c.value) for ws in workbook.worksheets for row in ws.iter_rows() for c in row)
        for sensitive in (SENSITIVE_COMMENT, "abc123", "Synthetic triage note", PLAN_NAME, RUN_NAME, FAKE_KEY):
            self.assertNotIn(sensitive, texts)

    def test_xml_and_xlsx_share_one_metadata_fetch(self):
        code, stdout, _ = self.run_cli(
            "collect", "plan", "900", "--lookback-days", "0",
            "--xml-report", str(self.xml), "--xlsx-report", str(self.xlsx),
        )  # fmt: skip

        self.assertEqual(code, cli.EXIT_OK, stdout)
        self.assertEqual(self.metadata_calls(), ["get_cases/1", "get_sections/1", "get_case_types"])
        self.assertIn("API requests: 9", stdout)
        self.assertIn("XML report written", stdout)
        self.assertIn("XLSX report written", stdout)
        xml_names, xml_rows = table(self.xml.read_text(encoding="utf-8"))
        details = openpyxl.load_workbook(self.xlsx)["Stability Details"]
        self.assertEqual([cell.value for cell in details[1]], xml_names)
        self.assertEqual(details.max_row - 1, len(xml_rows))

    def test_invalid_xlsx_paths(self):
        for argv in (
            ["--xlsx-report", str(self.config.parent / "missing" / "r.xlsx")],
            ["--xml-report", str(self.xml), "--xlsx-report", str(self.xml)],
        ):
            with self.subTest(argv=argv):
                code, _, stderr = self.run_cli("collect", "plan", "900", *argv)
                self.assertEqual(code, cli.EXIT_INVALID_CONFIGURATION)
                self.assertIn("invalid report path", stderr)
        self.assertEqual(self.server.calls, [])

    def test_collection_only_still_makes_no_metadata_requests(self):
        self.run_cli("collect", "plan", "900")
        self.assertEqual(self.metadata_calls(), [])


class MissingOpenpyxlTests(CliTestCase):
    def test_missing_openpyxl_is_reported_before_requests(self):
        with mock.patch.object(cli, "xlsx_available", return_value=False):
            path = self.config.parent / "report.xlsx"
            code, _, stderr = self.run_cli("collect", "plan", "900", "--xlsx-report", str(path))

        self.assertEqual(code, cli.EXIT_INVALID_CONFIGURATION)
        self.assertIn('pip install "openpyxl>=3.1,<4"', stderr)
        self.assertEqual(self.server.calls, [])
        self.assertFalse((self.config.parent / "report.xlsx").exists())


if __name__ == "__main__":
    unittest.main()
