import unittest

from testrail_stability_analyzer.collection import (
    DimensionState,
    DiscoveredRun,
    RunClassification,
    RunDecision,
    build_outcome,
    classify_run,
    deduplicate,
    sanitize_comment,
)
from testrail_stability_analyzer.config import StatusCategory
from testrail_stability_analyzer.models import Result, Run, Test

from . import fake_testrail as fx
from .fake_testrail import AUTOMATION_ISSUE, BLOCKED, FAILED, MYSTERY, NOT_APPLICABLE, PASSED, days_ago


def source(run_id=500, plan_id=None):
    run = Run.from_api(fx.run(run_id, f"Run {run_id}", plan_id=plan_id))
    return DiscoveredRun(run, RunClassification(), RunDecision.INCLUDED)


def outcome(results, *, test_status=FAILED, **kwargs):
    test = Test.from_api(fx.test(7000, 100, 500, status_id=test_status))
    return build_outcome(
        source(), test, [Result.from_api(r, run_id=500) for r in results], fx.resolver(), **kwargs
    )


class FinalOutcomeTests(unittest.TestCase):
    def test_blocked_failed_automation_issue_keeps_every_record_and_ends_as_automation_issue(self):
        t = days_ago(1)
        result = outcome(
            [
                fx.result(3, 7000, AUTOMATION_ISSUE, t + 600),
                fx.result(1, 7000, BLOCKED, t),
                fx.result(2, 7000, FAILED, t + 60),
            ]
        )
        self.assertEqual([r.result.id for r in result.results], [1, 2, 3])
        self.assertEqual(result.final.result.id, 3)
        self.assertEqual(result.category, StatusCategory.AUTOMATION_ISSUE)
        self.assertEqual(result.executed_on.timestamp(), t + 600)

    def test_failed_then_passed_is_passed(self):
        t = days_ago(1)
        result = outcome([fx.result(1, 7000, FAILED, t), fx.result(2, 7000, PASSED, t + 1)])
        self.assertEqual(result.category, StatusCategory.PASSED)

    def test_final_failed_stays_failed_and_blocked_stays_blocked(self):
        t = days_ago(1)
        self.assertEqual(outcome([fx.result(1, 7000, FAILED, t)]).category, StatusCategory.FAILED)
        self.assertEqual(outcome([fx.result(1, 7000, BLOCKED, t)]).category, StatusCategory.BLOCKED)

    def test_comment_only_record_after_final_status_does_not_overwrite_it(self):
        t = days_ago(1)
        result = outcome(
            [fx.result(1, 7000, FAILED, t), fx.result(2, 7000, None, t + 60, comment="triage note")]
        )
        self.assertEqual(result.final.result.id, 1)
        self.assertEqual(result.category, StatusCategory.FAILED)
        self.assertEqual(len(result.results), 2)
        self.assertTrue(result.results[1].is_comment_only)
        self.assertIsNone(result.results[1].status)

    def test_equal_timestamps_use_result_id_as_tie_breaker(self):
        t = days_ago(1)
        result = outcome([fx.result(11, 7000, PASSED, t), fx.result(10, 7000, FAILED, t)])
        self.assertEqual(result.final.result.id, 11)
        self.assertEqual([r.result.id for r in result.results], [10, 11])

    def test_missing_timestamps_never_override_timestamped_results_and_are_not_fabricated(self):
        t = days_ago(1)
        result = outcome([fx.result(99, 7000, PASSED, None), fx.result(5, 7000, FAILED, t)])
        self.assertEqual(result.final.result.id, 5)
        untimed_only = outcome([fx.result(2, 7000, FAILED, None), fx.result(3, 7000, PASSED, None)])
        self.assertEqual(untimed_only.final.result.id, 3)
        self.assertIsNone(untimed_only.executed_on)

    def test_only_comment_records_or_no_records_give_no_final_outcome(self):
        self.assertIsNone(outcome([fx.result(1, 7000, None, days_ago(1))]).final)
        empty = outcome([], test_status=fx.UNTESTED)
        self.assertIsNone(empty.final)
        self.assertIsNone(empty.executed_on)
        self.assertEqual(empty.current_status.category, StatusCategory.NOT_RUN)

    def test_unknown_custom_and_undefined_statuses_stay_unknown(self):
        t = days_ago(1)
        mystery = outcome([fx.result(1, 7000, MYSTERY, t)])
        self.assertEqual(mystery.category, StatusCategory.UNKNOWN)
        self.assertEqual(mystery.final.status.label, "Mystery")
        undefined = outcome([fx.result(1, 7000, 77, t)])
        self.assertEqual(undefined.category, StatusCategory.UNKNOWN)
        self.assertEqual(undefined.final.status.status_id, 77)

    def test_not_applicable_status_is_distinct(self):
        self.assertEqual(outcome([fx.result(1, 7000, NOT_APPLICABLE, days_ago(1))]).category, StatusCategory.NOT_APPLICABLE)

    def test_source_ids_and_version_are_preserved(self):
        t = days_ago(1)
        test = Test.from_api(fx.test(7001, 101, 600))
        result = build_outcome(
            source(600, plan_id=900),
            test,
            [Result.from_api(fx.result(1, 7001, FAILED, t, version="2.0"), run_id=600)],
            fx.resolver(),
        )
        self.assertEqual((result.case_id, result.test_id, result.run_id, result.plan_id), (101, 7001, 600, 900))
        self.assertEqual(result.final.result.id, 1)
        self.assertEqual(result.application_version, "2.0")

    def test_version_from_configured_custom_result_field(self):
        result = outcome(
            [fx.result(1, 7000, FAILED, days_ago(1), version="ignored", custom_app_build="b-42")],
            version_field="custom_app_build",
        )
        self.assertEqual(result.application_version, "b-42")


class DeduplicationTests(unittest.TestCase):
    def test_identical_records_are_merged_and_conflicts_reported(self):
        same = Result.from_api(fx.result(1, 7000, FAILED, days_ago(1)))
        changed = Result.from_api(fx.result(1, 7000, PASSED, days_ago(1)))
        other = Result.from_api(fx.result(2, 7000, FAILED, days_ago(1)))
        unique, conflicts = deduplicate([same, same, other, changed], run_id=500)
        self.assertEqual([r.id for r in unique], [1, 2])
        self.assertEqual(unique[0].status_id, FAILED)
        self.assertEqual([(c.run_id, c.result_id) for c in conflicts], [(500, 1)])


class SanitizeTests(unittest.TestCase):
    def test_removes_markup_urls_identifiers_and_secrets(self):
        comment = (
            "<p>Login <b>failed</b>: expected &lt;200&gt;</p><script>alert(1)</script>"
            " see https://assets.example.invalid/run/123/screenshot.png"
            " sessionId=ab12cd34ef56ab78cd90 token: abc user qa@example.invalid"
            " trace 0123456789abcdef0123 id 123e4567-e89b-12d3-a456-426614174000"
            " file /var/lib/agent/workspace/log.txt Authorization: Bearer xyz.abc"
        )
        summary = sanitize_comment(comment, max_length=1000)
        for secret in ("assets.example", "ab12cd34", "qa@example", "0123456789abcdef", "123e4567", "/var/lib", "xyz.abc", "alert"):
            self.assertNotIn(secret, summary)
        self.assertTrue(summary.startswith("Login failed : expected <200> see [url]"), summary)
        self.assertIn("[url]", summary)
        self.assertIn("sessionId=[redacted]", summary)

    def test_redacts_quoted_and_structured_credentials(self):
        cases = {
            '{"api_key":"supersecret"}': '{"api_key":[redacted]}',
            "{'password': 'p@ss word'}": "{'password': [redacted]}",
            'login failed password="hunter2" retry': "login failed password=[redacted] retry",
            "token=abc123&step=2": "token=[redacted]&step=2",
            "api-key: xyz, then timeout": "api-key: [redacted], then timeout",
            '{"user": "qa", "access_key": "AKIA", "client_secret":"s\\"x"}': (
                '{"user": "qa", "access_key": [redacted], "client_secret":[redacted]}'
            ),
            "&quot;session_id&quot;:&quot;s1&quot;": '"session_id":[redacted]',
        }
        for comment, expected in cases.items():
            with self.subTest(comment=comment):
                self.assertEqual(sanitize_comment(comment, max_length=500), expected)

    def test_html_encoded_markup_is_removed_but_literal_comparisons_are_kept(self):
        cases = {
            "&lt;img src=x onerror=alert(1)&gt;": None,
            "&lt;img src=x onerror=alert(1)": None,
            "&lt;script&gt;alert(1)&lt;/script&gt; boom": "boom",
            "&lt;a href='javascript:x'&gt;link&lt;/a&gt;": "link",
            "&lt;!-- note --&gt;done": "done",
            "expected &lt;200&gt; but a &lt; b": "expected <200> but a < b",
        }
        for comment, expected in cases.items():
            with self.subTest(comment=comment):
                self.assertEqual(sanitize_comment(comment, max_length=500), expected)

    def test_truncates_and_handles_empty(self):
        self.assertIsNone(sanitize_comment(None))
        self.assertIsNone(sanitize_comment("<br/>  "))
        summary = sanitize_comment("word " * 100, max_length=20)
        self.assertEqual(len(summary), 20)
        self.assertTrue(summary.endswith("…"))

    def test_result_repr_never_contains_comment(self):
        resolved = outcome([fx.result(1, 7000, FAILED, days_ago(1), comment="secret-session-value")]).final
        self.assertNotIn("secret-session-value", repr(resolved))
        self.assertEqual(resolved.failure_summary(), "secret-session-value")


class ClassificationTests(unittest.TestCase):
    def classify(self, name, *, plan_name=None, config=None):
        return classify_run(Run.from_api(fx.run(1, name, config=config)), plan_name, fx.settings().mappings)

    def test_configured_patterns_match_plan_run_and_configuration(self):
        result = self.classify("Nightly", plan_name="App-B release", config="Plat-Y, Chrome")
        self.assertEqual((result.application.name, result.platform.name), ("App B", "Platform Y"))
        self.assertEqual(result.execution_type.state, DimensionState.NOT_CONFIGURED)

    def test_unmatched_names_are_unresolved_not_guessed(self):
        result = self.classify("Smoke 2026-05-01")
        self.assertEqual(result.application.state, DimensionState.UNRESOLVED)
        self.assertIsNone(result.application.name)
        self.assertEqual(result.platform.state, DimensionState.UNRESOLVED)

    def test_multiple_matches_are_ambiguous(self):
        result = self.classify("app-a and app-b plat-x")
        self.assertEqual(result.application.state, DimensionState.AMBIGUOUS)
        self.assertEqual(result.application.candidates, ("App A", "App B"))
        self.assertIsNone(result.application.name)
