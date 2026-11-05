"""Native XLSX stability report (Excel, Google Sheets), written with the optional ``openpyxl`` package.

Worksheets, in order: ``Stability Overview`` (a two-row header; per
application/platform a Latest Status and a numeric Pass Rate column, with
short notes), ``Stability Details`` (every metric,
as numeric percentage and date cells), and ``Report Info``. The content is the
same as the XML report's (see :mod:`.report_data`).

``openpyxl`` is imported only when a workbook is rendered, so collection and
XML export work without it; :func:`xlsx_available` lets callers check first.
Every string is passed through :func:`.report_data.safe_text` and stored as a
text cell: no formulas, hyperlinks, or external links are written.
"""

from __future__ import annotations

import importlib.util
import io
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from ..analysis.models import AnalysisReport
from ..collection.models import CollectionReport
from ..config.settings import Settings
from .report_data import (
    GREEN,
    NEUTRAL,
    OVERVIEW_GREEN,
    OVERVIEW_NEUTRAL,
    OVERVIEW_RED,
    OVERVIEW_YELLOW,
    RED,
    YELLOW,
    Cell,
    ReportContent,
    Worksheet,
    build_report,
    safe_text,
)

INSTALL_HINT: Final = (
    'XLSX reports need the optional openpyxl package: pip install "openpyxl>=3.1,<4" '
    "(or: poetry install --extras xlsx)"
)
AUTHOR: Final = "testrail-stability-analyzer"
OVERVIEW_FONT: Final = "1F1F1F"
# (font colour, fill colour) per report style; the same palette as the XML report.
_COLOURS: Final = {
    GREEN: ("006100", "C6EFCE"),
    YELLOW: ("9C5700", "FFEB9C"),
    RED: ("9C0006", "FFC7CE"),
    NEUTRAL: ("3F3F3F", "EDEDED"),
    # Stability Overview: the same fills (historical Pass Rate) with one dark, neutral font for every status.
    OVERVIEW_GREEN: (OVERVIEW_FONT, "C6EFCE"),
    OVERVIEW_YELLOW: (OVERVIEW_FONT, "FFEB9C"),
    OVERVIEW_RED: (OVERVIEW_FONT, "FFC7CE"),
    OVERVIEW_NEUTRAL: (OVERVIEW_FONT, "EDEDED"),
}
_HEADER_FILL: Final = "D9D9D9"
_NUMBER_FORMATS: Final = {"percent": "0.0%", "datetime": "yyyy-mm-dd hh:mm"}
_POINTS_PER_CHARACTER: Final = 6.5  # converts the shared column widths (points) to Excel character units


class XlsxUnavailableError(RuntimeError):
    """``openpyxl`` is not installed."""


def xlsx_available() -> bool:
    try:
        return importlib.util.find_spec("openpyxl") is not None
    except (ImportError, ValueError):  # e.g. blocked through sys.modules
        return False


def render_xlsx(
    collection: CollectionReport,
    analysis: AnalysisReport,
    settings: Settings,
    *,
    case_types: Mapping[int, str] | None = None,
    notes: Sequence[str] = (),
    content: ReportContent | None = None,
) -> bytes:
    """Render the workbook as XLSX bytes; an already built ``content`` is reused."""
    try:
        from openpyxl import Workbook
    except ImportError:
        raise XlsxUnavailableError(INSTALL_HINT) from None
    content = content or build_report(collection, analysis, settings, case_types=case_types, notes=notes)
    workbook = Workbook()
    workbook.remove(workbook.active)
    styles = _Styles()
    # Frozen columns: Test Case ID on the stability worksheets; frozen rows: every header row.
    for sheet, frozen_column in ((content.overview, "B"), (content.details, "B"), (content.info, "A")):
        _write_sheet(workbook.create_sheet(sheet.name), sheet, styles, frozen_column)
    collected = collection.collected_at.astimezone(UTC).replace(tzinfo=None)
    workbook.properties.creator = AUTHOR
    workbook.properties.created = collected
    workbook.properties.modified = collected
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class _Styles:
    """openpyxl style objects, created once per workbook."""

    def __init__(self) -> None:
        from openpyxl.styles import Alignment, Font, PatternFill

        self.top = Alignment(vertical="top")
        self.wrapped = Alignment(vertical="top", wrap_text=True)
        self.group = Alignment(horizontal="center", vertical="center", wrap_text=True)
        self.header_font = Font(bold=True)
        self.header_fill = PatternFill("solid", fgColor=_HEADER_FILL)
        self.fonts = {style: Font(color=font) for style, (font, _) in _COLOURS.items()}
        self.fills = {style: PatternFill("solid", fgColor=fill) for style, (_, fill) in _COLOURS.items()}


def _write_sheet(ws: Any, sheet: Worksheet, styles: _Styles, frozen_column: str) -> None:
    """Write headers, rows, notes, the freeze, and the AutoFilter (on the last header row).

    With ``header_groups``, row 1 holds the group labels, merged horizontally
    over their columns; spans without a label stay blank and grey. Nothing is
    merged vertically and every merge lies above the filter row, so Google
    Sheets keeps the filter and can sort the data.
    """
    from openpyxl.comments import Comment
    from openpyxl.utils import get_column_letter

    header_row = 2 if sheet.header_groups else 1
    if sheet.header_groups:
        _write_header_groups(ws, sheet.header_groups, styles)
    wrapped = {index for index, (header, _) in enumerate(sheet.columns, start=1) if header in sheet.wrap}
    for index, (header, width) in enumerate(sheet.columns, start=1):
        ws.column_dimensions[get_column_letter(index)].width = round(width / _POINTS_PER_CHARACTER, 1)
        cell = ws.cell(row=header_row, column=index)
        _set_text(cell, header)
        cell.font, cell.fill, cell.alignment = styles.header_font, styles.header_fill, styles.wrapped
    for row_index, row in enumerate(sheet.rows, start=header_row + 1):
        for column_index, value in enumerate(row, start=1):
            cell = ws.cell(row=row_index, column=column_index)
            _set_value(cell, value)
            cell.alignment = styles.wrapped if column_index in wrapped else styles.top
            number_format = value.number_format or value.style
            if number_format in _NUMBER_FORMATS:
                cell.number_format = _NUMBER_FORMATS[number_format]
            if value.style in styles.fills:
                cell.font, cell.fill = styles.fonts[value.style], styles.fills[value.style]
            if value.comment:
                comment = Comment(safe_text(value.comment), AUTHOR)
                comment.width, comment.height = 260, 130
                cell.comment = comment
    ws.freeze_panes = f"{frozen_column}{header_row + 1}"
    last = get_column_letter(len(sheet.columns))
    ws.auto_filter.ref = f"A{header_row}:{last}{header_row + len(sheet.rows)}"


def _write_header_groups(ws: Any, groups: Sequence[tuple[str | None, int]], styles: _Styles) -> None:
    start = 1
    for label, span in groups:
        for column in range(start, start + span):  # styled before merging: merged cells keep the anchor's look
            cell = ws.cell(row=1, column=column)
            cell.font, cell.fill, cell.alignment = styles.header_font, styles.header_fill, styles.group
        if label is not None:
            _set_text(ws.cell(row=1, column=start), label)
            if span > 1:
                ws.merge_cells(start_row=1, start_column=start, end_row=1, end_column=start + span - 1)
        start += span


def _set_value(cell: Any, value: Cell) -> None:
    data = value.value
    if data is None:
        return
    if isinstance(data, datetime):
        cell.value = data.astimezone(UTC).replace(tzinfo=None)  # Excel dates carry no time zone; reports use UTC
    elif isinstance(data, bool):  # never exported as a number
        _set_text(cell, "yes" if data else "no")
    elif isinstance(data, int | float):
        cell.value = data
    else:
        _set_text(cell, data)


def _set_text(cell: Any, text: str) -> None:
    """A text cell, never a formula: openpyxl would otherwise treat a leading '=' as one."""
    cell.value = safe_text(text)
    cell.data_type = "s"
