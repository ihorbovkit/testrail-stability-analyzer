import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from testrail_stability_analyzer.config import (
    ConfigurationError,
    MappingSettings,
    MatchSource,
    StatusCategory,
    load_settings,
    settings_from_mapping,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "config.example.toml"


def minimal(**sections):
    data = {"testrail": {"base_url": "https://example.testrail.io", "project_id": 1}}
    data.update(sections)
    return data


class RequiredSettingsTests(unittest.TestCase):
    def test_minimal_configuration_uses_defaults(self):
        settings = settings_from_mapping(minimal())

        self.assertEqual(settings.testrail.base_url, "https://example.testrail.io")
        self.assertEqual(settings.testrail.project_id, 1)
        self.assertIsNone(settings.testrail.suite_id)
        self.assertEqual(settings.history.window_days, 60)
        self.assertEqual(settings.history.max_runs, 10)
        self.assertEqual(settings.history.lookback_limit_days, 365)
        self.assertEqual(settings.http.connect_timeout_seconds, 10.0)
        self.assertEqual(settings.http.read_timeout_seconds, 30.0)
        self.assertEqual(settings.http.max_requests_per_execution, 1000)
        self.assertEqual(settings.http.max_retries, 3)
        self.assertEqual(settings.http.retry_backoff_seconds, 1.0)
        self.assertEqual(settings.http.max_retry_delay_seconds, 60.0)

    def test_missing_required_settings_are_all_reported(self):
        with self.assertRaises(ConfigurationError) as ctx:
            settings_from_mapping({})
        self.assertIn("testrail.base_url is required", ctx.exception.errors)
        self.assertIn("testrail.project_id is required", ctx.exception.errors)

    def test_invalid_project_and_suite_ids(self):
        for value in ("abc", 0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                settings_from_mapping({"testrail": {"base_url": "https://x.io", "project_id": value}})
        with self.assertRaises(ConfigurationError):
            settings_from_mapping(minimal(testrail={"base_url": "https://x.io", "project_id": 1, "suite_id": 0}))

    def test_optional_suite_id(self):
        settings = settings_from_mapping({"testrail": {"base_url": "https://x.io", "project_id": 3, "suite_id": 7}})
        self.assertEqual(settings.testrail.suite_id, 7)

    def test_unknown_keys_are_rejected(self):
        with self.assertRaises(ConfigurationError) as ctx:
            settings_from_mapping(minimal(history={"window_dayz": 30}, extra={}))
        self.assertIn("history.window_dayz: unknown setting", ctx.exception.errors)
        self.assertIn("extra: unknown setting", ctx.exception.errors)

    def test_credentials_in_configuration_are_rejected_without_echoing_value(self):
        data = minimal()
        data["testrail"]["api_key"] = "super-secret-value"
        with self.assertRaises(ConfigurationError) as ctx:
            settings_from_mapping(data)
        self.assertIn("credentials must not be stored", str(ctx.exception))
        self.assertNotIn("super-secret-value", str(ctx.exception))


class BaseUrlTests(unittest.TestCase):
    def parse_url(self, url, **extra):
        return settings_from_mapping({"testrail": {"base_url": url, "project_id": 1, **extra}})

    def test_trailing_slash_is_removed_and_subpath_kept(self):
        self.assertEqual(self.parse_url("https://host.example/testrail/").testrail.base_url, "https://host.example/testrail")

    def test_plain_http_requires_explicit_opt_in(self):
        with self.assertRaises(ConfigurationError):
            self.parse_url("http://host.example")
        settings = self.parse_url("http://host.example", allow_insecure_http=True)
        self.assertEqual(settings.testrail.base_url, "http://host.example")

    def test_invalid_urls(self):
        for url in ("host.example", "ftp://host.example", "https://", "https://h.example/?a=1", "https://h.example/#x"):
            with self.subTest(url=url), self.assertRaises(ConfigurationError):
                self.parse_url(url)

    def test_embedded_credentials_rejected_and_not_echoed(self):
        with self.assertRaises(ConfigurationError) as ctx:
            self.parse_url("https://user:hunter2@host.example")
        self.assertIn("testrail.base_url must not contain credentials", ctx.exception.errors)
        self.assertNotIn("hunter2", str(ctx.exception))


class HistoryAndHttpTests(unittest.TestCase):
    def test_custom_history_values(self):
        settings = settings_from_mapping(minimal(history={"window_days": 30, "max_runs": 5, "lookback_limit_days": 90}))
        self.assertEqual((settings.history.window_days, settings.history.max_runs), (30, 5))
        self.assertEqual(settings.history.lookback_limit_days, 90)

    def test_zero_lookback_disables_limit(self):
        settings = settings_from_mapping(minimal(history={"lookback_limit_days": 0}))
        self.assertIsNone(settings.history.lookback_limit_days)

    def test_environment_can_set_and_disable_lookback_limit(self):
        env = {"TESTRAIL_BASE_URL": "https://x.io", "TESTRAIL_PROJECT_ID": "1"}
        settings = load_settings(environ={**env, "TESTRAIL_HISTORY_LOOKBACK_LIMIT_DAYS": "400"})
        self.assertEqual(settings.history.lookback_limit_days, 400)
        settings = load_settings(environ={**env, "TESTRAIL_HISTORY_LOOKBACK_LIMIT_DAYS": "0"})
        self.assertIsNone(settings.history.lookback_limit_days)

    def test_lookback_shorter_than_window_is_rejected(self):
        with self.assertRaises(ConfigurationError):
            settings_from_mapping(minimal(history={"window_days": 60, "lookback_limit_days": 30}))

    def test_invalid_history_and_http_values(self):
        cases = [
            {"history": {"window_days": 0}},
            {"history": {"max_runs": -1}},
            {"http": {"connect_timeout_seconds": 0}},
            {"http": {"read_timeout_seconds": "slow"}},
            {"http": {"max_requests_per_execution": 0}},
            {"http": {"max_retries": -1}},
            {"http": {"max_retries": 1.5}},
            {"http": {"retry_backoff_seconds": 0}},
            {"http": {"max_retry_delay_seconds": "inf"}},
        ]
        for sections in cases:
            with self.subTest(sections=sections), self.assertRaises(ConfigurationError):
                settings_from_mapping(minimal(**sections))

    def test_retry_settings(self):
        settings = settings_from_mapping(
            minimal(http={"max_retries": 0, "retry_backoff_seconds": 0.5, "max_retry_delay_seconds": 5})
        )
        self.assertEqual(
            (settings.http.max_retries, settings.http.retry_backoff_seconds, settings.http.max_retry_delay_seconds),
            (0, 0.5, 5.0),
        )
        with self.assertRaises(ConfigurationError) as ctx:
            settings_from_mapping(minimal(http={"retry_backoff_seconds": 10, "max_retry_delay_seconds": 5}))
        self.assertIn("http.max_retry_delay_seconds must be >= http.retry_backoff_seconds", ctx.exception.errors)

    def test_non_finite_timeouts_are_rejected(self):
        for key in ("connect_timeout_seconds", "read_timeout_seconds"):
            for value in (float("inf"), float("nan"), "inf", "nan"):
                with self.subTest(key=key, value=value), self.assertRaises(ConfigurationError) as ctx:
                    settings_from_mapping(minimal(http={key: value}))
                self.assertIn(f"http.{key} must be a finite positive number", ctx.exception.errors)

    def test_non_finite_timeout_from_toml_and_environment(self):
        path = Path(tempfile.mkdtemp()) / "config.toml"
        self.addCleanup(path.parent.rmdir)
        self.addCleanup(path.unlink)
        path.write_text('[testrail]\nbase_url = "https://x.io"\nproject_id = 1\n[http]\nread_timeout_seconds = inf\n')
        with self.assertRaises(ConfigurationError):
            load_settings(path, environ={})
        with self.assertRaises(ConfigurationError):
            load_settings(
                environ={
                    "TESTRAIL_BASE_URL": "https://x.io",
                    "TESTRAIL_PROJECT_ID": "1",
                    "TESTRAIL_HTTP_CONNECT_TIMEOUT_SECONDS": "inf",
                }
            )


class MappingTests(unittest.TestCase):
    def test_no_mappings_or_patterns_required(self):
        settings = settings_from_mapping(minimal())
        self.assertEqual(settings.mappings.applications, ())
        self.assertEqual(settings.mappings.platforms, ())
        self.assertEqual(dict(settings.mappings.statuses), {})
        self.assertEqual(dict(settings.mappings.custom_fields), {})
        self.assertEqual(settings.naming.plan_name_patterns, ())
        self.assertEqual(settings.naming.run_name_patterns, ())

    def test_arbitrary_application_and_platform_names(self):
        settings = settings_from_mapping(
            minimal(
                mappings={
                    "applications": [{"name": "Billing Portal", "patterns": ["(?i)billing"]}],
                    "platforms": [
                        {"name": "Desktop Linux", "patterns": ["linux"], "sources": ["configuration"]},
                        {"name": "Smart TV", "patterns": ["tv"]},
                    ],
                }
            )
        )
        application = settings.mappings.applications[0]
        self.assertEqual(application.name, "Billing Portal")
        self.assertTrue(application.patterns[0].search("BILLING nightly"))
        self.assertEqual(application.sources, tuple(MatchSource))
        self.assertEqual([p.name for p in settings.mappings.platforms], ["Desktop Linux", "Smart TV"])
        self.assertEqual(settings.mappings.platforms[0].sources, (MatchSource.CONFIGURATION,))

    def test_invalid_mapping_entries(self):
        cases = [
            {"applications": [{"name": "A", "patterns": ["("]}]},
            {"applications": [{"name": "A"}]},
            {"applications": [{"name": "A", "patterns": []}]},
            {"applications": [{"patterns": ["a"]}]},
            {"applications": [{"name": "A", "patterns": ["a"]}, {"name": "A", "patterns": ["b"]}]},
            {"platforms": [{"name": "P", "patterns": ["p"], "sources": ["milestone"]}]},
            {"platforms": {"name": "P"}},
        ]
        for mappings in cases:
            with self.subTest(mappings=mappings), self.assertRaises(ConfigurationError):
                settings_from_mapping(minimal(mappings=mappings))

    def test_execution_types_filters_and_applicability(self):
        settings = settings_from_mapping(
            minimal(
                mappings={
                    "applications": [{"name": "App A", "patterns": ["a"], "applicability_values": [1, " App A "]}],
                    "platforms": [{"name": "Web", "patterns": ["web"]}],
                    "execution_types": [{"name": "Manual", "patterns": ["(?i)manual"]}],
                    "custom_fields": {"applicability": "custom_target_apps"},
                },
                filters={"applications": ["App A"], "execution_types": ["Manual"]},
            )
        )
        self.assertEqual(settings.mappings.applications[0].applicability_values, (1, "App A"))
        self.assertEqual([m.name for m in settings.mappings.execution_types], ["Manual"])
        self.assertEqual(settings.filters.applications, ("App A",))
        self.assertEqual(settings.filters.platforms, ())
        self.assertTrue(settings.filters.enabled)

    def test_mapping_settings_keeps_original_positional_order(self):
        fields = MappingProxyType({"application_version": "version"})
        statuses = MappingProxyType({StatusCategory.AUTOMATION_ISSUE: ("au_issue",)})
        mappings = MappingSettings((), (), fields, statuses)
        self.assertEqual(mappings.custom_fields, fields)
        self.assertEqual(mappings.statuses, statuses)
        self.assertEqual(mappings.execution_types, ())

    def test_invalid_filters_and_applicability(self):
        app = {"name": "App A", "patterns": ["a"]}
        cases = [
            {"filters": {"applications": ["Unknown App"]}, "mappings": {"applications": [app]}},
            {"filters": {"platforms": ["Web"]}},
            {"filters": {"applications": "App A"}, "mappings": {"applications": [app]}},
            {"filters": {"owner": []}},
            {"mappings": {"applications": [{**app, "applicability_values": [1]}]}},
            {
                "mappings": {
                    "applications": [{**app, "applicability_values": [True]}],
                    "custom_fields": {"applicability": "custom_apps"},
                }
            },
            {"mappings": {"platforms": [{"name": "Web", "patterns": ["w"], "applicability_values": [1]}]}},
        ]
        for sections in cases:
            with self.subTest(sections=sections), self.assertRaises(ConfigurationError):
                settings_from_mapping(minimal(**sections))

    def test_naming_patterns_compile(self):
        settings = settings_from_mapping(minimal(naming={"plan_name_patterns": ["^Nightly"], "run_name_patterns": []}))
        self.assertTrue(settings.naming.plan_name_patterns[0].match("Nightly 42"))
        with self.assertRaises(ConfigurationError):
            settings_from_mapping(minimal(naming={"run_name_patterns": ["[unclosed"]}))

    def test_status_mapping_accepts_names_and_ids(self):
        settings = settings_from_mapping(
            minimal(mappings={"statuses": {"automation_issue": ["au_issue", 12], "not_applicable": ["n_a"]}})
        )
        self.assertEqual(settings.mappings.statuses[StatusCategory.AUTOMATION_ISSUE], ("au_issue", 12))
        self.assertEqual(settings.mappings.statuses[StatusCategory.NOT_APPLICABLE], ("n_a",))

    def test_invalid_status_mappings(self):
        cases = [
            {"flaky": ["x"]},
            {"unknown": ["x"]},
            {"failed": "failed"},
            {"failed": [0]},
            {"failed": [True]},
            {"failed": ["failed"], "automation_issue": ["failed"]},
        ]
        for statuses in cases:
            with self.subTest(statuses=statuses), self.assertRaises(ConfigurationError):
                settings_from_mapping(minimal(mappings={"statuses": statuses}))

    def test_custom_fields(self):
        settings = settings_from_mapping(minimal(mappings={"custom_fields": {"application_version": "custom_app_ver"}}))
        self.assertEqual(settings.mappings.custom_fields["application_version"], "custom_app_ver")
        with self.assertRaises(ConfigurationError):
            settings_from_mapping(minimal(mappings={"custom_fields": {"Bad Name": "custom_x"}}))
        with self.assertRaises(ConfigurationError):
            settings_from_mapping(minimal(mappings={"custom_fields": {"version": ""}}))

    def test_credential_keys_rejected_in_custom_fields(self):
        for key in ("api_key", "password", "Token"):
            with self.subTest(key=key), self.assertRaises(ConfigurationError) as ctx:
                settings_from_mapping(minimal(mappings={"custom_fields": {key: "supersecret"}}))
            self.assertIn("credentials must not be stored", str(ctx.exception))
            self.assertNotIn("supersecret", str(ctx.exception))


class LoadSettingsTests(unittest.TestCase):
    def write_config(self, content):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.toml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_example_configuration_is_valid(self):
        settings = load_settings(EXAMPLE_CONFIG, environ={})
        self.assertEqual(settings.testrail.base_url, "https://example.testrail.io")
        self.assertEqual(settings.history.window_days, 60)
        self.assertEqual(settings.history.lookback_limit_days, 365)
        self.assertFalse(settings.filters.enabled)

    def test_environment_only(self):
        settings = load_settings(environ={"TESTRAIL_BASE_URL": "https://env.example", "TESTRAIL_PROJECT_ID": "4"})
        self.assertEqual(settings.testrail.base_url, "https://env.example")
        self.assertEqual(settings.testrail.project_id, 4)

    def test_environment_overrides_file(self):
        path = self.write_config('[testrail]\nbase_url = "https://file.example"\nproject_id = 1\n[history]\nmax_runs = 3\n')
        settings = load_settings(
            path,
            environ={"TESTRAIL_PROJECT_ID": "9", "TESTRAIL_HISTORY_WINDOW_DAYS": "14", "TESTRAIL_SUITE_ID": " "},
        )
        self.assertEqual(settings.testrail.base_url, "https://file.example")
        self.assertEqual(settings.testrail.project_id, 9)
        self.assertEqual(settings.history.window_days, 14)
        self.assertEqual(settings.history.max_runs, 3)
        self.assertIsNone(settings.testrail.suite_id)

    def test_retry_settings_from_environment(self):
        settings = load_settings(
            environ={
                "TESTRAIL_BASE_URL": "https://env.example",
                "TESTRAIL_PROJECT_ID": "4",
                "TESTRAIL_HTTP_MAX_RETRIES": "5",
                "TESTRAIL_HTTP_RETRY_BACKOFF_SECONDS": "0.25",
                "TESTRAIL_HTTP_MAX_RETRY_DELAY_SECONDS": "8",
            }
        )
        self.assertEqual(
            (settings.http.max_retries, settings.http.retry_backoff_seconds, settings.http.max_retry_delay_seconds),
            (5, 0.25, 8.0),
        )

    def test_invalid_environment_value(self):
        with self.assertRaises(ConfigurationError) as ctx:
            load_settings(environ={"TESTRAIL_BASE_URL": "https://env.example", "TESTRAIL_PROJECT_ID": "abc"})
        self.assertIn("testrail.project_id must be an integer", ctx.exception.errors)

    def test_missing_file(self):
        with self.assertRaises(ConfigurationError):
            load_settings(Path(tempfile.gettempdir()) / "does-not-exist-tsa.toml", environ={})

    def test_invalid_toml(self):
        with self.assertRaises(ConfigurationError):
            load_settings(self.write_config("[testrail\n"), environ={})


if __name__ == "__main__":
    unittest.main()
