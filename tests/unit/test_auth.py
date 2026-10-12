import unittest

from testrail_stability_analyzer.config import (
    CredentialError,
    CredentialProvider,
    Credentials,
    EnvironmentCredentialProvider,
)

FAKE_USER = "user@example.invalid"
FAKE_KEY = "fake-api-key-0000"


class CredentialsTests(unittest.TestCase):
    def test_repr_and_str_are_redacted(self):
        credentials = Credentials(username=FAKE_USER, api_key=FAKE_KEY)
        for text in (repr(credentials), str(credentials), f"{credentials}", repr([credentials])):
            self.assertNotIn(FAKE_KEY, text)
            self.assertNotIn(FAKE_USER, text)
            self.assertIn("<redacted>", text)


class EnvironmentCredentialProviderTests(unittest.TestCase):
    def test_reads_credentials_from_environment(self):
        provider = EnvironmentCredentialProvider(environ={"TESTRAIL_USERNAME": FAKE_USER, "TESTRAIL_API_KEY": FAKE_KEY})
        credentials = provider.get_credentials()
        self.assertEqual((credentials.username, credentials.api_key), (FAKE_USER, FAKE_KEY))
        self.assertIsInstance(provider, CredentialProvider)

    def test_custom_variable_names(self):
        provider = EnvironmentCredentialProvider("MY_USER", "MY_KEY", environ={"MY_USER": FAKE_USER, "MY_KEY": FAKE_KEY})
        self.assertEqual(provider.get_credentials().api_key, FAKE_KEY)

    def test_missing_variables_named_without_values(self):
        provider = EnvironmentCredentialProvider(environ={"TESTRAIL_USERNAME": FAKE_USER, "TESTRAIL_API_KEY": "  "})
        with self.assertRaises(CredentialError) as ctx:
            provider.get_credentials()
        message = str(ctx.exception)
        self.assertIn("TESTRAIL_API_KEY", message)
        self.assertNotIn("TESTRAIL_USERNAME", message)
        self.assertNotIn(FAKE_USER, message)

    def test_provider_repr_does_not_expose_values(self):
        provider = EnvironmentCredentialProvider(environ={"TESTRAIL_USERNAME": FAKE_USER, "TESTRAIL_API_KEY": FAKE_KEY})
        self.assertNotIn(FAKE_KEY, repr(provider))
        self.assertNotIn(FAKE_USER, repr(provider))


if __name__ == "__main__":
    unittest.main()
