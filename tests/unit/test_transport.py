import unittest
from unittest import mock

from testrail_stability_analyzer.api import StdlibHttpTransport, TransportError, TransportTimeout

URL = "https://example.testrail.io/index.php?/api/v2/get_case/1"


def fake_connection(status=200, body=b"{}", error=None):
    connection = mock.MagicMock()
    response = connection.getresponse.return_value
    response.status = status
    response.read.return_value = body
    response.getheaders.return_value = [("Retry-After", "5")]
    if error is not None:
        connection.request.side_effect = error
    return connection


class StdlibHttpTransportTests(unittest.TestCase):
    """The http.client connection is mocked; no network access occurs."""

    def test_issues_get_with_separate_timeouts(self):
        connection = fake_connection(body=b'{"id": 1}')
        with mock.patch("http.client.HTTPSConnection", return_value=connection) as factory:
            response = StdlibHttpTransport().get(URL, {"Accept": "application/json"}, connect_timeout=2, read_timeout=9)

        self.assertEqual(factory.call_args.args[:2], ("example.testrail.io", None))
        self.assertEqual(factory.call_args.kwargs["timeout"], 2)
        connection.sock.settimeout.assert_called_once_with(9)
        connection.request.assert_called_once_with(
            "GET", "/index.php?/api/v2/get_case/1", headers={"Accept": "application/json"}
        )
        connection.close.assert_called_once()
        self.assertEqual((response.status, response.body), (200, b'{"id": 1}'))
        self.assertEqual(response.headers, {"retry-after": "5"})

    def test_timeout_and_network_errors_are_wrapped(self):
        for error, expected in ((TimeoutError(), TransportTimeout), (ConnectionRefusedError(), TransportError)):
            connection = fake_connection(error=error)
            with self.subTest(error=error), mock.patch("http.client.HTTPSConnection", return_value=connection):
                with self.assertRaises(expected):
                    StdlibHttpTransport().get(URL, {}, connect_timeout=1, read_timeout=1)
            connection.close.assert_called_once()

    def test_unsupported_scheme(self):
        with self.assertRaises(ValueError):
            StdlibHttpTransport().get("ftp://example.invalid/x", {}, connect_timeout=1, read_timeout=1)


if __name__ == "__main__":
    unittest.main()
