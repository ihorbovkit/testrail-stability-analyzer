"""Report export: Excel-compatible XML Spreadsheet 2003 (standard library) and XLSX (optional ``openpyxl``).

:mod:`.xlsx` imports ``openpyxl`` lazily, so XML export works without it.
"""

from .report_data import ReportContent, build_report, combinations, overview_cell, safe_text
from .spreadsheet import render_spreadsheet
from .xlsx import XlsxUnavailableError, render_xlsx, xlsx_available

__all__ = [
    "ReportContent",
    "XlsxUnavailableError",
    "build_report",
    "combinations",
    "overview_cell",
    "render_spreadsheet",
    "render_xlsx",
    "safe_text",
    "xlsx_available",
]
