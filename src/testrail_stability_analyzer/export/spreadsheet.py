"""Excel-compatible XML Spreadsheet 2003 (SpreadsheetML) stability report.

The workbook has two worksheets built from :mod:`.report_data`: ``Stability``
(the detailed per-combination metrics) and ``Report Info``. The format needs
no third-party package. Every string is passed through :func:`safe_text` and
escaped; cell notes and text wrapping are not written.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Final
from xml.sax.saxutils import escape

from ..analysis.models import AnalysisReport
from ..collection.models import CollectionReport
from ..config.settings import Settings
from .report_data import Cell, ReportContent, Worksheet, build_report, safe_text

_ESCAPES: Final = {'"': "&quot;", "\n": "&#10;", "\r": "&#13;", "\t": "&#9;"}


def render_spreadsheet(
    collection: CollectionReport,
    analysis: AnalysisReport,
    settings: Settings,
    *,
    case_types: Mapping[int, str] | None = None,
    notes: Sequence[str] = (),
    content: ReportContent | None = None,
) -> str:
    """Render the workbook as an XML string.

    ``case_types`` and ``notes`` are passed to :func:`.report_data.build_report`;
    an already built ``content`` is reused instead.
    """
    content = content or build_report(collection, analysis, settings, case_types=case_types, notes=notes)
    stability = dataclasses.replace(content.details, name="Stability")
    return _workbook((stability, content.info))


def _escaped(value: str) -> str:
    return escape(safe_text(value), _ESCAPES)


_STYLES: Final = """ <Styles>
  <Style ss:ID="Default" ss:Name="Normal"><Alignment ss:Vertical="Top"/></Style>
  <Style ss:ID="header"><Alignment ss:Vertical="Top" ss:WrapText="1"/><Font ss:Bold="1"/>\
<Interior ss:Color="#D9D9D9" ss:Pattern="Solid"/></Style>
  <Style ss:ID="green"><Font ss:Color="#006100"/><Interior ss:Color="#C6EFCE" ss:Pattern="Solid"/></Style>
  <Style ss:ID="yellow"><Font ss:Color="#9C5700"/><Interior ss:Color="#FFEB9C" ss:Pattern="Solid"/></Style>
  <Style ss:ID="red"><Font ss:Color="#9C0006"/><Interior ss:Color="#FFC7CE" ss:Pattern="Solid"/></Style>
  <Style ss:ID="neutral"><Font ss:Color="#3F3F3F"/><Interior ss:Color="#EDEDED" ss:Pattern="Solid"/></Style>
  <Style ss:ID="percent"><NumberFormat ss:Format="0.0%"/></Style>
  <Style ss:ID="datetime"><NumberFormat ss:Format="yyyy\\-mm\\-dd\\ hh:mm"/></Style>
 </Styles>
"""


def _workbook(sheets: Sequence[Worksheet]) -> str:
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        '<?mso-application progid="Excel.Sheet"?>\n',
        '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"'
        ' xmlns:o="urn:schemas-microsoft-com:office:office"'
        ' xmlns:x="urn:schemas-microsoft-com:office:excel"'
        ' xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">\n',
        _STYLES,
    ]
    for sheet in sheets:
        parts.append(_worksheet(sheet))
    parts.append("</Workbook>\n")
    return "".join(parts)


def _worksheet(sheet: Worksheet) -> str:
    lines = [f' <Worksheet ss:Name="{_escaped(sheet.name)}">', "  <Table>"]
    lines += [f'   <Column ss:Width="{width}"/>' for _, width in sheet.columns]
    header = "".join(_cell(Cell(name, "header")) for name, _ in sheet.columns)
    lines.append(f"   <Row>{header}</Row>")
    lines += [f"   <Row>{''.join(_cell(cell) for cell in row)}</Row>" for row in sheet.rows]
    lines.append("  </Table>")
    lines.append(
        '  <WorksheetOptions xmlns="urn:schemas-microsoft-com:office:excel">'
        "<FreezePanes/><FrozenNoSplit/><SplitHorizontal>1</SplitHorizontal>"
        "<TopRowBottomPane>1</TopRowBottomPane><ActivePane>2</ActivePane></WorksheetOptions>"
    )
    lines.append(
        f'  <AutoFilter x:Range="R1C1:R1C{len(sheet.columns)}" xmlns="urn:schemas-microsoft-com:office:excel"/>'
    )
    lines.append(" </Worksheet>\n")
    return "\n".join(lines)


def _cell(cell: Cell) -> str:
    style = f' ss:StyleID="{cell.style}"' if cell.style else ""
    value = cell.value
    if value is None:
        return f"<Cell{style}/>"
    if isinstance(value, datetime):
        stamp = value.astimezone(UTC).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S.000")
        return f'<Cell{style}><Data ss:Type="DateTime">{stamp}</Data></Cell>'
    if isinstance(value, bool):  # never exported as a number
        return f'<Cell{style}><Data ss:Type="String">{"yes" if value else "no"}</Data></Cell>'
    if isinstance(value, int | float):
        return f'<Cell{style}><Data ss:Type="Number">{value!r}</Data></Cell>'
    return f'<Cell{style}><Data ss:Type="String">{_escaped(value)}</Data></Cell>'
