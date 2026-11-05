"""A synthetic, deterministic report covering every Overview state, for XML and XLSX export tests."""

import dataclasses
from types import MappingProxyType

from testrail_stability_analyzer.analysis import analyze
from testrail_stability_analyzer.collection import Applicability, CaseGroup, FeatureGroupState

from . import analysis_fixtures as af
from . import fake_testrail as fx
from .analysis_fixtures import PLAT_X, PLAT_Y, UNRESOLVED, histories, outcome, rec, runs
from .fake_testrail import AUTOMATION_ISSUE, BLOCKED, FAILED, NOT_APPLICABLE, PASSED

HOSTILE_DEFECT = '=HYPERLINK("https://evil.example.invalid")'
CASE_TYPES = MappingProxyType({1: "Automated", 3: "Functional"})


def _with_test(outcomes, **fields):
    return [dataclasses.replace(o, test=dataclasses.replace(o.test, **fields)) for o in outcomes]


def sample_outcomes():
    """Case IDs and what they exercise (App A unless noted):

    100 stable on X, single pass on Y; 101 failure evidence with defects; 102 inconsistent;
    103 automation issue; 104 not applicable; 105 not run; 106 partial; 107 blocked only;
    108 unidentified platform run only.
    """
    class_name = MappingProxyType({"custom_class": "pkg.module.TestClass"})
    outcomes = _with_test(runs([PASSED] * 5, case_id=100, first_run=500), type_id=3, custom_fields=class_name)
    outcomes += _with_test(runs([PASSED], case_id=100, first_run=510, platform=PLAT_Y), type_id=3)
    outcomes += [
        outcome(520, 101, rec(5201, FAILED, 1, defects=f"BUG-1, {HOSTILE_DEFECT}")),
        outcome(521, 101, rec(5211, FAILED, 2, defects="BUG-2")),
    ]
    outcomes += runs([PASSED, FAILED, PASSED, FAILED, PASSED], case_id=102, first_run=530)
    outcomes += runs([AUTOMATION_ISSUE, PASSED], case_id=103, first_run=540)
    outcomes += runs([NOT_APPLICABLE], case_id=104, first_run=550)
    outcomes += [outcome(560, 105)]
    outcomes += [outcome(570, 106, rec(5701, PASSED, 1), complete=False)]
    outcomes += runs([BLOCKED], case_id=107, first_run=580)
    outcomes += [outcome(590, 108, rec(5901, FAILED, 1), app=UNRESOLVED, platform=PLAT_X)]
    return outcomes


def sample_groups():
    return MappingProxyType(
        {
            100: CaseGroup(100, FeatureGroupState.RESOLVED, 10, 3, ("Area Two", "Feature_Alpha")),
            101: CaseGroup(101, FeatureGroupState.RESOLVED, 10, 1, ("R&D <Area>",)),
            102: CaseGroup(102, FeatureGroupState.CASE_NOT_FOUND, 10),
        }
    )


def sample_settings():
    return fx.settings(mappings={"custom_fields": {"class_name": "custom_class"}})


def sample_inputs(*, applicability=Applicability.NOT_CONFIGURED):
    """(collection, analysis, settings) with feature groups and limitations."""
    settings = sample_settings()
    outcomes = sample_outcomes()
    hist = histories(outcomes, platforms=(PLAT_X, PLAT_Y), applicability=applicability)
    collection = af.report(
        outcomes, hist=hist, case_groups=sample_groups(), limitations=("synthetic collection limitation",)
    )
    return collection, analyze(collection, settings), settings


def headers(sheet):
    """Column names of a content worksheet; under a header group they are qualified as '<group>: <header>'."""
    if not sheet.header_groups:
        return [name for name, _ in sheet.columns]
    names, columns = [], iter(sheet.columns)
    for label, span in sheet.header_groups:
        for _ in range(span):
            header, _ = next(columns)
            names.append(header if label is None else f"{label}: {header}")
    return names


def rows_by_case(sheet):
    """(headers, {case ID: {header: cell}}) of a content worksheet, keyed as :func:`headers`."""
    names = headers(sheet)
    return names, {row[0].value: dict(zip(names, row, strict=True)) for row in sheet.rows}


def xlsx_headers(ws):
    """(header row, column names) of an openpyxl worksheet, qualified as :func:`headers` below a merged group.

    The header row is the last frozen row; a group label is read from row 1 (its merge anchor).
    """
    header_row = int("".join(ch for ch in ws.freeze_panes if ch.isdigit())) - 1
    if header_row == 1:
        return 1, [cell.value for cell in ws[1]]
    group_of = {}
    for column in range(1, ws.max_column + 1):
        group_of[column] = ws.cell(1, column).value
    for merged in ws.merged_cells.ranges:
        for column in range(merged.min_col, merged.max_col + 1):
            group_of[column] = ws.cell(1, merged.min_col).value
    names = []
    for column in range(1, ws.max_column + 1):
        header = ws.cell(header_row, column).value
        names.append(header if group_of[column] is None else f"{group_of[column]}: {header}")
    return header_row, names


def xlsx_rows_by_case(ws):
    """(column names, {case ID: {name: cell}}) of an openpyxl worksheet, keyed as :func:`xlsx_headers`."""
    header_row, names = xlsx_headers(ws)
    return names, {row[0].value: dict(zip(names, row, strict=True)) for row in ws.iter_rows(min_row=header_row + 1)}
