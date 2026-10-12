"""Produce short, sanitized failure summaries from TestRail result comments.

Comments can contain HTML, URLs to internal assets, session IDs, tokens, email
addresses, and file paths. The summary keeps the human-readable message and
replaces such values with placeholders. It is best-effort redaction, not a
guarantee: summaries must still not be sent to external services.
"""

from __future__ import annotations

import html
import re
from typing import Final

DEFAULT_MAX_LENGTH: Final = 200

_SCRIPT_OR_STYLE: Final = re.compile(r"<(script|style)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG: Final = re.compile(r"<[^>]*>")
_SUBSTITUTIONS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE), "[url]"),
    (re.compile(r"\bwww\.\S+", re.IGNORECASE), "[url]"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"\b(bearer|basic)\s+\S+", re.IGNORECASE), r"\1 [redacted]"),
    (
        re.compile(
            r"\b([\w-]*(?:session|token|secret|password|passwd|auth|cookie|api[_-]?key|sid)[\w-]*)"
            r"(\s*[:=]\s*)\S+",
            re.IGNORECASE,
        ),
        r"\1\2[redacted]",
    ),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE), "[id]"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.IGNORECASE), "[id]"),
    (re.compile(r"\b(?=[A-Za-z0-9_\-+/]*\d)(?=[A-Za-z0-9_\-+/]*[A-Za-z])[A-Za-z0-9_\-+/]{24,}={0,2}"), "[token]"),
    (re.compile(r"(?:[A-Za-z]:\\|~?/)(?:[^\s/\\]+[/\\])+[^\s/\\]*"), "[path]"),
)
_WHITESPACE: Final = re.compile(r"\s+")


def sanitize_comment(comment: str | None, *, max_length: int = DEFAULT_MAX_LENGTH) -> str | None:
    """Return a redacted single-line summary of ``comment``, or ``None`` if nothing remains."""
    if max_length < 1:
        raise ValueError("max_length must be >= 1")
    if not comment:
        return None
    # Strip markup before unescaping so escaped text such as "&lt;5&gt;" is kept.
    text = html.unescape(_TAG.sub(" ", _SCRIPT_OR_STYLE.sub(" ", comment)))
    for pattern, replacement in _SUBSTITUTIONS:
        text = pattern.sub(replacement, text)
    text = _WHITESPACE.sub(" ", text).strip()
    if not text:
        return None
    if len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"
    return text
