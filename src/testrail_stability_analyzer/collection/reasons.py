"""Extract a short reason from an explicitly labelled section of a TestRail result comment.

Only text following a configured header (e.g. ``BLOCKED REASON:``) is used;
a comment without one yields no reason, never the comment itself. Section
boundaries are found on the markup-free text before anything is collapsed:
a section ends at a blank line, the next labelled section or heading, a code
block (markdown fence or HTML ``<pre>``/``<code>``), or the first log or
stack-trace line. The section is then stripped of
asset references, redacted (see :mod:`.sanitize`), collapsed to one line, and
truncated. Redaction is best-effort, so reasons must still not be sent to
external services.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Final

from .sanitize import plain_text, redact

# Decoration allowed around a header: markdown emphasis, heading and quote markers, list bullets, table pipes.
_DECORATION: Final = r"[\s>#*_~`|\-]*"
_SEPARATOR: Final = r"[\s*_~`]*(?:[:\-–—=]+[\s*_~`]*)?"
# A different labelled section: an upper-case label followed by a colon, a bold label with a colon
# (e.g. "**Expected Result:**"), or a markdown heading.
_NEXT_SECTION: Final = re.compile(
    r"^(?:[\s>*_|]*[A-Z][A-Z0-9 _/()&.\-]{2,}[*_]*\s*:"
    r"|[\s>|]*(?:\*\*|__)[^*_\n]{1,60}?(?::\s*(?:\*\*|__)|(?:\*\*|__)\s*:)"
    r"|\s*#{1,6}\s)"
)
_CODE_FENCE: Final = re.compile(r"^\s*(?:```|~~~)")
# Opening of an HTML preformatted/code block, also entity-encoded; replaced by a fence line before
# markup removal, which would otherwise turn it into an ordinary line break.
_HTML_CODE_BLOCK: Final = re.compile(r"(?:<|&lt;)\s*(?:pre|code|samp|xmp|listing)\b(?:[^>&]|&(?!gt;))*(?:>|&gt;)", re.I)
_LOG_LINE: Final = re.compile(
    r"^\s*(?:"
    r"Traceback \(most recent call last\)"
    r"|at\s+[\w$.<>]+\(.*\)"  # Java/JavaScript/C# stack frame
    r"|File \".*\", line \d+"  # Python stack frame
    r"|Caused by:"
    r"|Exception in thread"
    r"|(?:[\w$]+\.)+[\w$]*(?:Exception|Error)\b"  # qualified exception name
    r"|\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}"  # timestamped log line
    r"|\[?\d{2}:\d{2}:\d{2}"
    r"|\[?(?:TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|SEVERE)\]?(?:\s|:|$)"
    r")"
)
_ASSETS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), " "),  # markdown image (screenshots, attachments)
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),  # markdown link: keep the label only
    (re.compile(r"\S*(?:index\.php\?/|attachments?/get/|/attachments?/)\S*", re.IGNORECASE), "[asset]"),
    (re.compile(r"\S+\.(?:png|jpe?g|gif|bmp|webp|mp4|webm|mov|har|zip|log|txt|html?)\b\S*", re.IGNORECASE), "[asset]"),
)
_PLACEHOLDER: Final = re.compile(r"\[(?:url|email|redacted|id|token|path|asset)\]")
_WHITESPACE: Final = re.compile(r"\s+")
_LEADING_PUNCTUATION: Final = " -–—:;,.|*_~`>#"
_TRAILING_PUNCTUATION: Final = " -–—:;,|*_~`>#"
# A reason needs this many letters outside placeholders; otherwise little more than redacted values remains.
_MIN_LETTERS: Final = 3


def extract_reason(comment: str | None, headers: Sequence[str], *, max_length: int) -> str | None:
    """The sanitized text of the first non-empty section labelled with one of ``headers``, or ``None``."""
    if max_length < 1:
        raise ValueError("max_length must be >= 1")
    if not comment or not headers:
        return None
    lines = plain_text(_HTML_CODE_BLOCK.sub("\n```\n", comment)).split("\n")
    pattern = _header_pattern(headers)
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if match is None:
            continue
        reason = _clean(_section(match.group("rest"), lines[index + 1 :], pattern), max_length)
        if reason is not None:
            return reason
    return None


def _header_pattern(headers: Iterable[str]) -> re.Pattern[str]:
    names = "|".join(r"\s+".join(map(re.escape, header.split())) for header in sorted(headers, key=len, reverse=True))
    return re.compile(rf"^{_DECORATION}(?:{names})(?![\w]){_SEPARATOR}(?P<rest>.*)$", re.IGNORECASE)


def _section(rest: str, following: Sequence[str], header: re.Pattern[str]) -> str:
    """The header line's remainder plus following lines up to the section boundary."""
    parts = [rest] if rest.strip() else []
    if parts and _ends_section(rest, header):
        return ""
    for line in following:
        if not line.strip():
            if parts:
                break
            continue  # blank lines between the header and its text
        if _ends_section(line, header):
            break
        parts.append(line)
    return " ".join(parts)


def _ends_section(line: str, header: re.Pattern[str]) -> bool:
    return bool(
        header.match(line) or _NEXT_SECTION.match(line) or _CODE_FENCE.match(line) or _LOG_LINE.match(line)
    )


def _clean(text: str, max_length: int) -> str | None:
    for pattern, replacement in _ASSETS:
        text = pattern.sub(replacement, text)
    text = _WHITESPACE.sub(" ", redact(text)).lstrip(_LEADING_PUNCTUATION).rstrip(_TRAILING_PUNCTUATION)
    if sum(char.isalpha() for char in _PLACEHOLDER.sub("", text)) < _MIN_LETTERS:
        return None
    if len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"
    return text
