import subprocess
import unittest

from testrail_stability_analyzer.config import (
    ConfigurationError,
    CredentialError,
    EnvironmentCredentialProvider,
    KeychainCredentialProvider,
    credential_provider_from_settings,
    settings_from_mapping,
)

# Synthetic placeholders only.
SERVICE = "example-service"
ACCOUNT = "user@example.invalid"
SECRET = "synthetic-keychain-secret-0000"


class FakeRunner:
    def __init__(self, returncode=0, stdout=SECRET + "\n", error=None):
        self.returncode = returncode
        self.stdout = stdout
        self.error = error
        self.calls = []

    def __call__(self, command, **options):
        self.calls.append((command, options))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(command, self.returncode, stdout=self.stdout, stderr="synthetic stderr")


def provider(runner, **kwargs):
    return KeychainCredentialProvider(SERVICE, ACCOUNT, runner=runner, platform="darwin", **kwargs)


class KeychainCredentialProviderTests(unittest.TestCase):
    def test_reads_api_key_with_read_only_lookup(self):
        runner = FakeRunner()
        credentials = provider(runner).get_credentials()

        self.assertEqual((credentials.username, credentials.api_key), (ACCOUNT, SECRET))
        (command, options), = runner.calls
        self.assertEqual(
            command, ["/usr/bin/security", "find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"]
        )
        self.assertNotIn(SECRET, " ".join(command))
        self.assertTrue(options["capture_output"])
        self.assertFalse(options["check"])

    def test_username_can_come_from_an_environment_variable(self):
        credentials = provider(
            FakeRunner(), username_variable="EXAMPLE_USER", environ={"EXAMPLE_USER": "other@example.invalid"}
        ).get_credentials()

        self.assertEqual(credentials.username, "other@example.invalid")

    def test_missing_username_variable_fails_before_reading_the_keychain(self):
        runner = FakeRunner()
        with self.assertRaisesRegex(CredentialError, "EXAMPLE_USER"):
            provider(runner, username_variable="EXAMPLE_USER", environ={}).get_credentials()
        self.assertEqual(runner.calls, [])

    def test_missing_item(self):
        with self.assertRaisesRegex(CredentialError, "no macOS Keychain item found") as context:
            provider(FakeRunner(returncode=44, stdout="")).get_credentials()
        self.assertNotIn(ACCOUNT, str(context.exception))

    def test_access_denied_or_cancelled(self):
        with self.assertRaisesRegex(CredentialError, "access may have been denied") as context:
            provider(FakeRunner(returncode=51, stdout=SECRET)).get_credentials()
        self.assertNotIn(SECRET, str(context.exception))
        self.assertNotIn("synthetic stderr", str(context.exception))

    def test_empty_password(self):
        with self.assertRaisesRegex(CredentialError, "empty password"):
            provider(FakeRunner(stdout="\n")).get_credentials()

    def test_tool_unavailable_or_timing_out(self):
        for error, message in (
            (FileNotFoundError(), "tool not found"),
            (subprocess.TimeoutExpired("security", 60), "timed out"),
            (PermissionError(), "could not run"),
        ):
            with self.subTest(error=type(error).__name__), self.assertRaisesRegex(CredentialError, message):
                provider(FakeRunner(error=error)).get_credentials()

    def test_other_platforms_are_rejected_without_running_anything(self):
        runner = FakeRunner()
        keychain = KeychainCredentialProvider(SERVICE, ACCOUNT, runner=runner, platform="linux")

        with self.assertRaisesRegex(CredentialError, "only available on macOS"):
            keychain.get_credentials()
        self.assertEqual(runner.calls, [])

    def test_service_and_account_are_required(self):
        with self.assertRaises(CredentialError):
            KeychainCredentialProvider("", ACCOUNT)

    def test_repr_hides_account_and_secret(self):
        keychain = provider(FakeRunner())
        keychain.get_credentials()

        self.assertNotIn(ACCOUNT, repr(keychain))
        self.assertNotIn(SECRET, repr(keychain))


class CredentialProviderSettingsTests(unittest.TestCase):
    def settings(self, **section):
        return settings_from_mapping(
            {
                "testrail": {"base_url": "https://example.testrail.io", "project_id": 1},
                "credential_provider": section,
            }
        )

    def test_environment_is_the_default(self):
        settings = settings_from_mapping({"testrail": {"base_url": "https://example.testrail.io", "project_id": 1}})

        self.assertEqual(settings.credential_provider.type, "environment")
        selected = credential_provider_from_settings(settings.credential_provider)
        self.assertIsInstance(selected, EnvironmentCredentialProvider)

    def test_keychain_selection(self):
        settings = self.settings(type="keychain", keychain_service=SERVICE, keychain_account=ACCOUNT)
        selected = credential_provider_from_settings(settings.credential_provider)

        self.assertIsInstance(selected, KeychainCredentialProvider)

    def test_keychain_requires_service_and_account(self):
        with self.assertRaises(ConfigurationError) as context:
            self.settings(type="keychain")
        self.assertEqual(
            context.exception.errors,
            (
                "credential_provider.keychain_service is required when credential_provider.type is 'keychain'",
                "credential_provider.keychain_account is required when credential_provider.type is 'keychain'",
            ),
        )

    def test_unknown_provider_and_secret_keys_are_rejected(self):
        with self.assertRaises(ConfigurationError) as context:
            self.settings(type="vault", api_key=SECRET)
        errors = " ".join(context.exception.errors)
        self.assertIn("credential_provider.type must be one of: environment, keychain", errors)
        self.assertIn("credentials must not be stored in configuration", errors)
        self.assertNotIn(SECRET, errors)

    def test_environment_username_variable_is_honoured(self):
        settings = self.settings(username_variable="EXAMPLE_USER")
        selected = credential_provider_from_settings(
            settings.credential_provider, environ={"EXAMPLE_USER": ACCOUNT, "TESTRAIL_API_KEY": SECRET}
        )

        self.assertEqual(selected.get_credentials().username, ACCOUNT)


class CollectionSettingsTests(unittest.TestCase):
    def settings(self, **section):
        return settings_from_mapping(
            {"testrail": {"base_url": "https://example.testrail.io", "project_id": 1}, "collection": section}
        )

    def test_defaults(self):
        collection = self.settings().collection

        self.assertEqual((collection.plan_ids, collection.run_ids, collection.max_results), ((), (), None))
        self.assertFalse(collection.targeted)

    def test_selection_and_cap(self):
        collection = self.settings(plan_ids=[3, 1, 3], run_ids=[7], max_results=15).collection

        self.assertEqual((collection.plan_ids, collection.run_ids, collection.max_results), ((3, 1), (7,), 15))
        self.assertTrue(collection.targeted)

    def test_zero_cap_means_no_cap(self):
        self.assertIsNone(self.settings(max_results=0).collection.max_results)

    def test_invalid_values(self):
        with self.assertRaises(ConfigurationError) as context:
            self.settings(plan_ids=[0], run_ids="5", max_results=-1)
        self.assertEqual(
            context.exception.errors,
            (
                "collection.max_results must be >= 0",
                "collection.plan_ids must be a list of positive integer IDs",
                "collection.run_ids must be a list of positive integer IDs",
            ),
        )


if __name__ == "__main__":
    unittest.main()
