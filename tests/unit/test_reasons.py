"""Reason extraction from labelled comment sections, and the line-preserving sanitizer helpers. Synthetic text only."""

import unittest

from testrail_stability_analyzer.collection.reasons import extract_reason
from testrail_stability_analyzer.collection.sanitize import plain_text, sanitize_comment

HEADERS = ("BLOCKED REASON", "SKIPPED REASON")


def reason(comment, headers=HEADERS, max_length=160):
    return extract_reason(comment, headers, max_length=max_length)


class HeaderTests(unittest.TestCase):
    def test_configured_headers_with_common_decorations(self):
        for comment in (
            "BLOCKED REASON: Device farm offline",
            "blocked reason - Device farm offline",
            "**BLOCKED REASON:** Device farm offline",
            "### Skipped Reason\nDevice farm offline",
            "> - SKIPPED  REASON:\n\n  Device farm offline",
            "| BLOCKED REASON | Device farm offline",
            "<p><b>BLOCKED REASON:</b></p><p>Device farm offline</p>",
            "Run notes\r\nBLOCKED REASON:\r\nDevice farm offline",
        ):
            with self.subTest(comment=comment):
                self.assertEqual(reason(comment).rstrip(" |"), "Device farm offline")

    def test_unconfigured_or_partial_headers_are_ignored(self):
        for comment in (
            "Device farm offline",  # no section: never the comment itself
            "REASON: Device farm offline",
            "BLOCKED REASONS: Device farm offline",
            "BLOCKEDREASON: Device farm offline",
            "Status blocked. BLOCKED REASON: Device farm offline",  # not at the start of a line
            "FAILURE REASON: Device farm offline",
            "",
            None,
        ):
            with self.subTest(comment=comment):
                self.assertIsNone(reason(comment))
        self.assertIsNone(reason("BLOCKED REASON: Device farm offline", headers=()))

    def test_custom_headers(self):
        comment = "Root cause:  Vendor sandbox down\nBLOCKED REASON: other"
        self.assertEqual(reason(comment, headers=("Root cause",)), "Vendor sandbox down")
        self.assertIsNone(reason("Root cause: Vendor sandbox down"))  # not configured by default

    def test_first_non_empty_section_wins(self):
        comment = "BLOCKED REASON:\n\nSKIPPED REASON: Waiting for test data\nBLOCKED REASON: later"
        self.assertEqual(reason(comment), "Waiting for test data")


class BoundaryTests(unittest.TestCase):
    def test_section_ends_at_blank_line_next_section_or_heading(self):
        for tail in ("\n\nSecret notes", "\nSTEPS: 1. open", "\n## Details", "\n**Expected Result:** ok"):
            with self.subTest(tail=tail):
                comment = f"BLOCKED REASON:\nBackend returns 503\nfor the search API{tail}"
                self.assertEqual(reason(comment), "Backend returns 503 for the search API")

    def test_html_block_boundaries_are_kept(self):
        comment = "<div>BLOCKED REASON: Login service down</div><div>Secret notes</div>"
        self.assertEqual(reason(comment), "Login service down")  # adjacent blocks leave a blank line
        comment = "BLOCKED REASON: Login service<br>down since deploy<br><br>Secret notes"
        self.assertEqual(reason(comment), "Login service down since deploy")
        comment = "<p>BLOCKED REASON: Login service down</p><p></p><p>Secret notes</p>"
        self.assertEqual(reason(comment), "Login service down")

    def test_logs_and_stack_traces_end_the_section(self):
        for log in (
            "Traceback (most recent call last):",
            '  File "/srv/app/test_login.py", line 10, in test',
            "    at com.example.LoginTest.run(LoginTest.java:42)",
            "java.lang.IllegalStateException: boom",
            "Caused by: java.io.IOException",
            "2026-01-02 10:11:12 ERROR request failed",
            "[12:00:01] retrying",
            "ERROR: request failed",
            "[WARN] slow",
            "```",
        ):
            with self.subTest(log=log):
                comment = f"BLOCKED REASON: Environment unavailable\n{log}\nSECRET-LOG-LINE"
                self.assertEqual(reason(comment), "Environment unavailable")

    def test_html_code_blocks_end_the_section(self):
        for block in (
            "<pre>INTERNAL-LOG line 1\nline 2</pre>",
            '<PRE class="log">INTERNAL-LOG</PRE>',
            "<pre><code>INTERNAL-LOG</code></pre>",
            "<code>INTERNAL-LOG</code>",
            "&lt;pre&gt;INTERNAL-LOG&lt;/pre&gt;",
        ):
            for separator in ("", " ", "\n", "<br>"):
                comment = f"BLOCKED REASON: outage{separator}{block} trailing INTERNAL-TAIL"
                with self.subTest(block=block, separator=separator):
                    self.assertEqual(reason(comment), "outage")

    def test_html_headings_and_rules_end_the_section(self):
        for boundary in (
            "<h2>Details</h2>",
            '<H4 class="x">Details</H4>',
            "&lt;h3&gt;Details&lt;/h3&gt;",
            "<hr>",
            "<hr/>",
            "\n---\n",
            "\n***\n",
            "\n___\n",
        ):
            for separator in ("", " ", "\n", "<br>"):
                comment = f"BLOCKED REASON: outage{separator}{boundary}SECRET-TAIL"
                with self.subTest(boundary=boundary, separator=separator):
                    self.assertEqual(reason(comment), "outage")

    def test_header_inside_an_html_heading(self):
        comment = "<h3>BLOCKED REASON</h3><p>Device lab closed</p><h3>Logs</h3>SECRET-TAIL"
        self.assertEqual(reason(comment), "Device lab closed")
        self.assertIsNone(reason("BLOCKED REASON:<h2>Details</h2>SECRET-TAIL"))

    def test_header_followed_only_by_a_code_block_yields_nothing(self):
        self.assertIsNone(reason("BLOCKED REASON:<pre>INTERNAL-LOG</pre>"))
        self.assertIsNone(reason("<p>BLOCKED REASON:</p><pre>INTERNAL-LOG</pre>"))

    def test_header_followed_only_by_logs_yields_nothing(self):
        self.assertIsNone(reason("BLOCKED REASON:\njava.lang.IllegalStateException: boom\n\tat x.y(Z.java:1)"))
        self.assertIsNone(reason("BLOCKED REASON: 2026-01-02 10:11:12 ERROR request failed"))


class SanitizationTests(unittest.TestCase):
    def test_sensitive_values_are_redacted(self):
        comment = (
            "BLOCKED REASON: Gateway down for qa.user@example.com, see https://ci.example.invalid/job/1?token=abc "
            "sessionId=SESSION-SECRET-1 Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload "
            "api_key: 'KEY-SECRET-2' build 0123456789abcdef0123 run 123e4567-e89b-12d3-a456-426614174000 "
            "file /var/log/app/out.txt"
        )
        text = reason(comment, max_length=1000)
        self.assertTrue(text.startswith("Gateway down for"))
        for secret in ("qa.user", "ci.example", "abc", "SESSION-SECRET-1", "eyJhbGci", "KEY-SECRET-2",
                       "0123456789abcdef0123", "123e4567", "/var/log"):  # fmt: skip
            self.assertNotIn(secret, text)

    def test_asset_references_are_removed(self):
        comment = (
            "BLOCKED REASON: Login page broken ![screenshot](index.php?/attachments/get/987) "
            "[ticket](https://tracker.example.invalid/T-1) attachment attachments/get/555 shot-01.png"
        )
        text = reason(comment)
        self.assertEqual(text, "Login page broken ticket attachment [asset] [asset]")

    def test_reason_of_only_redacted_values_is_dropped(self):
        for comment in ("BLOCKED REASON: https://example.invalid/a", "BLOCKED REASON: - ", "BLOCKED REASON: x9"):
            with self.subTest(comment=comment):
                self.assertIsNone(reason(comment))

    def test_maximum_length_is_enforced(self):
        text = reason("BLOCKED REASON: " + "word " * 100, max_length=40)
        self.assertEqual(len(text), 40)
        self.assertTrue(text.endswith("…"))
        with self.assertRaises(ValueError):
            reason("BLOCKED REASON: x", max_length=0)

    def test_markup_and_scripts_are_stripped(self):
        comment = "BLOCKED REASON: <script>alert(1)</script>Server &lt;img onerror=x&gt; down &amp; out"
        self.assertEqual(reason(comment), "Server down & out")


class PlainTextTests(unittest.TestCase):
    def test_line_breaks_survive_markup_removal(self):
        self.assertEqual(plain_text("a<br/>b<p>c</p>d\r\ne"), "a\nb\nc\nd\ne")

    def test_failure_summary_stays_single_line(self):
        self.assertEqual(sanitize_comment("a<br>b\n\nc <p>d</p>"), "a b c d")


if __name__ == "__main__":
    unittest.main()
