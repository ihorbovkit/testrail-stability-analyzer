import io
import tempfile
import unittest
from pathlib import Path

from testrail_stability_analyzer import cli

from . import fake_testrail as fx
from .fake_testrail import AUTOMATION_ISSUE, FAILED, NOW, PASSED, FakeTestRail, days_ago
from .fakes import FAKE_KEY, FAKE_USER

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
        self.assertIn("coverage: selection_limited=", stdout)
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

    def test_authentication_failure_is_an_api_error(self):
        self.server.errors["get_statuses"] = (401, "Authentication failed: invalid or missing user/password.")
        code, stdout, stderr = self.run_cli("collect", "plan", "900")

        self.assertEqual(code, cli.EXIT_API_ERROR)
        self.assertIn("TestRailAuthenticationError", stderr)
        self.assertEqual(stdout, "")
        self.assertNotIn(FAKE_KEY, stderr)

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


if __name__ == "__main__":
    unittest.main()
