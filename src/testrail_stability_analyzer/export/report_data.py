"""Format-independent content of the stability report, shared by the XML and XLSX writers.

:func:`build_report` turns a collection and its analysis into three worksheets
of typed cells:

- Overview: one row per test case ID with the shared case columns and one
  compact status cell per configured application/platform combination.
- Details: the same rows with every metric of each combination (latest status,
  latest result date, executions, rates, diagnosis, confidence, coverage).
- Info: collection scope, coverage, summaries, limitations, and a legend.

Only analyzed values are included. Result comments, case steps and other case
content, session IDs, and credentials are never read here. Writers pass every
string through :func:`safe_text`. Content is deterministic for the same input.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from ..analysis.models import AnalysisReport, Confidence, Diagnosis, KeyAnalysis
from ..collection.models import AvailabilityState, CaseGroup, CollectionReport, Coverage, RunOutcome
from ..collection.outcomes import field_value
from ..collection.sections import feature_group_label
from ..config.settings import Settings, StatusCategory
from ..models import Test

GREEN: Final = "green"
YELLOW: Final = "yellow"
RED: Final = "red"
NEUTRAL: Final = "neutral"

# Characters allowed by XML 1.0; everything else is removed from cell text.
_INVALID_XML: Final = re.compile("[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]")
# Leading characters that make spreadsheet applications evaluate text as a formula.
_FORMULA_PREFIXES: Final = ("=", "+", "-", "@", "\t", "\r")
_MAX_CELL_LENGTH: Final = 32_767  # Excel's per-cell limit

STATUS_LABELS: Final[Mapping[StatusCategory, tuple[str, str]]] = {
    StatusCategory.PASSED: ("Passed", GREEN),
    StatusCategory.FAILED: ("Failed", RED),
    StatusCategory.AUTOMATION_ISSUE: ("Automation Issue", YELLOW),
    StatusCategory.BLOCKED: ("Blocked", NEUTRAL),
    StatusCategory.RETEST: ("Retest", NEUTRAL),
    StatusCategory.NOT_RUN: ("Not Run", NEUTRAL),
    StatusCategory.NOT_APPLICABLE: ("N/A", NEUTRAL),
    StatusCategory.UNKNOWN: ("Unknown Status", NEUTRAL),
}
# Latest status of a history without a status-bearing result.
STATE_LABELS: Final[Mapping[AvailabilityState, str]] = {
    AvailabilityState.RESULT_AVAILABLE: "Unknown Status",
    AvailabilityState.UNTESTED: "Not Run",
    AvailabilityState.NOT_APPLICABLE: "N/A",
    AvailabilityState.NOT_INCLUDED_IN_RUN: "Not Included in Run",
    AvailabilityState.NO_RUN_FOR_PLATFORM: "No Run for Platform",
    AvailabilityState.UNKNOWN_APPLICABILITY: "Applicability Unresolved",
    AvailabilityState.NO_RESULT_WITHIN_LOOKBACK: "No Result in Lookback",
    AvailabilityState.RESULTS_INCOMPLETE: "Partial",
}
DIAGNOSIS_LABELS: Final[Mapping[Diagnosis, tuple[str, str]]] = {
    Diagnosis.STABLE: ("Stable", GREEN),
    Diagnosis.INCONSISTENT: ("Inconsistent", YELLOW),
    Diagnosis.AUTOMATION_ISSUE_EVIDENCE: ("Automation Issue", YELLOW),
    Diagnosis.APPLICATION_FAILURE_EVIDENCE: ("Failure Evidence", RED),
    Diagnosis.MIXED_FAILURE_EVIDENCE: ("Failure + Automation Issue", RED),
    Diagnosis.INSUFFICIENT_HISTORY: ("Insufficient History", NEUTRAL),
    Diagnosis.NOT_RUN_IN_WINDOW: ("Not Run in Window", NEUTRAL),
    Diagnosis.NOT_RUN: ("Not Run", NEUTRAL),
    Diagnosis.NOT_APPLICABLE: ("N/A", NEUTRAL),
    Diagnosis.PARTIAL: ("Partial", NEUTRAL),
    Diagnosis.UNRESOLVED_APPLICABILITY: ("Applicability Unresolved", NEUTRAL),
}
# Overview: diagnoses shown as a state instead of the latest status.
OVERVIEW_DIAGNOSIS_STATES: Final[Mapping[Diagnosis, tuple[str, str]]] = {
    Diagnosis.NOT_APPLICABLE: ("Not Applicable", NEUTRAL),
    Diagnosis.PARTIAL: ("Partial", NEUTRAL),
    Diagnosis.UNRESOLVED_APPLICABILITY: ("Applicability Unresolved", NEUTRAL),
    Diagnosis.NOT_RUN: ("Not Run", NEUTRAL),
    Diagnosis.NOT_RUN_IN_WINDOW: ("Not Run", NEUTRAL),
}
OVERVIEW_STATUS_LABELS: Final[Mapping[StatusCategory, tuple[str, str]]] = {
    **STATUS_LABELS,
    StatusCategory.NOT_APPLICABLE: ("Not Applicable", NEUTRAL),
}
NO_HISTORY_LABEL: Final = "No History"  # the case was not in any collected run of this combination
CONFIDENCE_LABELS: Final[Mapping[Confidence, str]] = {
    Confidence.NONE: "None",
    Confidence.LOW: "Low",
    Confidence.MEDIUM: "Medium",
    Confidence.HIGH: "High",
}
COVERAGE_LABELS: Final[Mapping[Coverage, str]] = {
    Coverage.COMPLETE: "Complete",
    Coverage.LOOKBACK_LIMITED: "Lookback Limited",
    Coverage.SELECTION_LIMITED: "Selection Limited",
    Coverage.PARTIAL: "Partial",
}
COMBINATION_METRICS: Final = (
    ("Latest Status", 95),
    ("Latest Result (UTC)", 110),
    ("Executions", 70),
    ("Pass Rate", 65),
    ("Failure Rate", 70),
    ("Automation Issue Rate", 80),
    ("Diagnosis", 150),
    ("Confidence", 75),
    ("Coverage", 105),
)
LEGEND: Final = (
    ("Green", "Stable: every conclusive execution in the window passed, with enough executions."),
    ("Yellow", "Inconsistent or automation issue evidence: needs review. Mixed results do not prove flakiness."),
    (
        "Red",
        "Failure evidence: executions in the window ended as failed. This does not confirm an application defect.",
    ),
    (
        "Neutral",
        "No stability conclusion: N/A, Not Run, Insufficient History, Partial, Applicability Unresolved, "
        "or No History.",
    ),
    ("Executions", "Distinct Test Runs in the history window; retries in one run count once. Capped at max runs."),
    (
        "Rates",
        "Count / conclusive executions (passed + failed + automation issue); blank when there are none.",
    ),
    ("Latest Status", "Latest known result for the combination; it may be older than the history window."),
    ("Defects", "Defect IDs referenced by any result of the case in the period, across all combinations."),
    ("No History", "The case was not in any collected run of this application/platform."),
    (
        "Overview",
        "Each cell shows the latest status of the combination (green Passed, red Failed, yellow Automation Issue, "
        "grey otherwise) or a state: Not Run, Not Applicable, Partial, Applicability Unresolved, No History. "
        "Green Passed means the latest result passed, not that stability is confirmed; yellow also marks an "
        "Inconsistent diagnosis. The cell note and Stability Details show the diagnosis.",
    ),
    (
        "Confidence",
        "High: at least max-runs conclusive executions with complete coverage. Medium: at least min-executions "
        "conclusive executions. Low: fewer, or the application/platform is not identified. None: the diagnosis "
        "does not rest on execution evidence.",
    ),
)


@dataclass(frozen=True, slots=True)
class Cell:
    value: str | int | float | datetime | None
    style: str | None = None
    comment: str | None = None  # short note built from analyzed values only; XLSX writes it, XML ignores it


@dataclass(frozen=True, slots=True)
class Worksheet:
    name: str
    columns: tuple[tuple[str, int], ...]  # (header, width in points)
    rows: tuple[tuple[Cell, ...], ...]
    wrap: frozenset[str] = frozenset()  # headers of columns with long text (wrapped by the XLSX writer)


@dataclass(frozen=True, slots=True)
class ReportContent:
    overview: Worksheet
    details: Worksheet
    info: Worksheet


def combinations(settings: Settings) -> tuple[tuple[str, str], ...]:
    """Application/platform pairs in configuration order, limited by any configured filters."""
    mappings, filters = settings.mappings, settings.filters
    applications = filters.applications or tuple(mapping.name for mapping in mappings.applications)
    platforms = filters.platforms or tuple(mapping.name for mapping in mappings.platforms)
    return tuple((application, platform) for application in applications for platform in platforms)


def build_report(
    collection: CollectionReport,
    analysis: AnalysisReport,
    settings: Settings,
    *,
    case_types: Mapping[int, str] | None = None,
    notes: Sequence[str] = (),
) -> ReportContent:
    """Build every worksheet.

    ``case_types`` maps TestRail case type IDs to names (``None`` when they
    could not be fetched; the type ID is shown instead). ``notes`` are extra
    limitations of report generation.
    """
    case_columns, case_rows = _case_columns(collection, analysis, settings, case_types or {})
    pairs = combinations(settings)
    by_key = {
        (a.key.case_id, a.key.application.name, a.key.platform.name): a
        for a in analysis.analyses
        if a.key.is_identified
    }
    wrap = frozenset({"Feature / Test Group", "Class Name", "Defects"})

    details_columns = list(case_columns)
    overview_columns = list(case_columns)
    for application, platform in pairs:
        details_columns += [(f"{application} | {platform}: {metric}", width) for metric, width in COMBINATION_METRICS]
        overview_columns.append((f"{application} {platform}", 110))
    details_rows, overview_rows = [], []
    for case_id, case_cells in case_rows:
        analyses = [by_key.get((case_id, application, platform)) for application, platform in pairs]
        details_rows.append(tuple(case_cells + [cell for a in analyses for cell in _combination_cells(a)]))
        overview_rows.append(tuple(case_cells + [overview_cell(a) for a in analyses]))
    return ReportContent(
        overview=Worksheet("Stability Overview", tuple(overview_columns), tuple(overview_rows), wrap),
        details=Worksheet("Stability Details", tuple(details_columns), tuple(details_rows), wrap),
        info=_info_sheet(collection, analysis, settings, notes),
    )


# -- Stability worksheets --------------------------------------------------------


def _case_columns(
    collection: CollectionReport, analysis: AnalysisReport, settings: Settings, case_types: Mapping[int, str]
) -> tuple[list[tuple[str, int]], list[tuple[int, list[Cell]]]]:
    """Shared case-level columns and, per case ID in ascending order, their cells."""
    class_field = settings.mappings.custom_fields.get("class_name")
    groups = analysis.case_groups
    columns: list[tuple[str, int]] = [("Test Case ID", 75)]
    if groups is not None:
        columns.append(("Feature / Test Group", 190))
    if class_field is not None:
        columns.append(("Class Name", 220))
    columns += [("Test Type", 100), ("Defects", 150)]

    defects = {case.case_id: case for case in analysis.case_defects}
    tests = _newest_tests(collection.outcomes)
    rows = []
    for case_id in sorted({a.key.case_id for a in analysis.analyses}):
        test = tests.get(case_id)
        row = [Cell(case_id)]
        if groups is not None:
            row.append(_group_cell(groups.get(case_id), settings))
        if class_field is not None:
            row.append(Cell(_display(field_value(test, class_field)) if test is not None else None))
        row.append(Cell(_test_type(test.type_id if test is not None else None, case_types)))
        case_defects = defects.get(case_id)
        row.append(Cell(", ".join(case_defects.defect_ids) or None if case_defects is not None else None))
        rows.append((case_id, row))
    return columns, rows


def _newest_tests(outcomes: Iterable[RunOutcome]) -> dict[int, Test]:
    """The test instance of each case from its newest run (highest run ID), for case-level fields."""
    newest: dict[int, RunOutcome] = {}
    for outcome in outcomes:
        current = newest.get(outcome.case_id)
        if current is None or outcome.run_id > current.run_id:
            newest[outcome.case_id] = outcome
    return {case_id: outcome.test for case_id, outcome in newest.items()}


def _group_cell(group: CaseGroup | None, settings: Settings) -> Cell:
    if group is None:
        return Cell("Unknown (not fetched)", NEUTRAL)
    label = feature_group_label(group, settings.report.feature_group)
    if label is None:
        return Cell(f"Unknown ({group.state.value.replace('_', ' ')})", NEUTRAL)
    return Cell(label)


def _test_type(type_id: int | None, case_types: Mapping[int, str]) -> str | None:
    if type_id is None:
        return None
    return case_types.get(type_id, f"Type {type_id}")


def _display(value: object) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, list | tuple):
        return ", ".join(str(item) for item in value)
    return str(value)


def _combination_cells(analysis: KeyAnalysis | None) -> list[Cell]:
    if analysis is None:
        no_history = Cell(NO_HISTORY_LABEL, NEUTRAL)
        return [no_history, *[Cell(None)] * 5, no_history, Cell(None), Cell(None)]
    m = analysis.metrics
    if m.latest_category is not None:
        status_label, status_style = STATUS_LABELS[m.latest_category]
    else:
        status_label, status_style = STATE_LABELS[analysis.state], NEUTRAL
    diagnosis_label, diagnosis_style = DIAGNOSIS_LABELS[analysis.diagnosis]
    return [
        Cell(status_label, status_style),
        Cell(m.latest_executed_on, "datetime"),
        Cell(m.executions),
        Cell(m.pass_rate.value, "percent"),
        Cell(m.failure_rate.value, "percent"),
        Cell(m.automation_issue_rate.value, "percent"),
        Cell(diagnosis_label, diagnosis_style),
        Cell(CONFIDENCE_LABELS[analysis.confidence]),
        Cell(COVERAGE_LABELS[analysis.coverage]),
    ]


def overview_cell(analysis: KeyAnalysis | None) -> Cell:
    """The compact Overview value of one combination: its latest status or a meaningful state.

    A known latest status is shown even when history is insufficient; green
    Passed means the latest result passed, not that stability is confirmed.
    An inconsistent diagnosis is highlighted yellow while keeping the status.
    """
    if analysis is None:
        return Cell(NO_HISTORY_LABEL, NEUTRAL, "No collected run of this application/platform included the case.")
    m, diagnosis = analysis.metrics, analysis.diagnosis
    label, style = OVERVIEW_DIAGNOSIS_STATES.get(diagnosis, (None, NEUTRAL))
    if label is None and m.latest_category is not None:
        label, style = OVERVIEW_STATUS_LABELS[m.latest_category]
        if diagnosis is Diagnosis.INCONSISTENT:
            style = YELLOW
    elif label is None:
        label = "Insufficient History" if diagnosis is Diagnosis.INSUFFICIENT_HISTORY else STATE_LABELS[analysis.state]
    return Cell(label, style, _overview_comment(analysis))


def _overview_comment(analysis: KeyAnalysis) -> str:
    m = analysis.metrics
    rate = m.pass_rate.value
    latest = "none" if m.latest_executed_on is None else f"{m.latest_executed_on.astimezone(UTC):%Y-%m-%d %H:%M} UTC"
    return "\n".join(
        (
            f"Diagnosis: {DIAGNOSIS_LABELS[analysis.diagnosis][0]}",
            f"Confidence: {CONFIDENCE_LABELS[analysis.confidence]}",
            f"Executions: {m.executions}",
            f"Pass rate: {'n/a' if rate is None else f'{rate:.1%}'}",
            f"Coverage: {COVERAGE_LABELS[analysis.coverage]}",
            f"Latest result: {latest}",
        )
    )


# -- Report Info worksheet -------------------------------------------------------


def _info_sheet(
    collection: CollectionReport, analysis: AnalysisReport, settings: Settings, notes: Sequence[str]
) -> Worksheet:
    discovery = collection.discovery
    identified = [a for a in analysis.analyses if a.key.is_identified]
    pairs = combinations(settings)
    placed = set(pairs)
    shown = [a for a in identified if (a.key.application.name, a.key.platform.name) in placed]
    items: list[tuple[str, Cell]] = [
        ("Report", Cell("TestRail stability report (read-only)")),
        ("Collected at (UTC)", Cell(collection.collected_at, "datetime")),
        ("Collection mode", Cell("targeted" if discovery.targeted else "project-wide")),
        ("Project ID", Cell(settings.testrail.project_id)),
        ("Suite ID", Cell(settings.testrail.suite_id if settings.testrail.suite_id is not None else "not configured")),
    ]
    if discovery.targeted:
        items += [
            ("Selected plan IDs", Cell(_ids(discovery.selected_plan_ids))),
            ("Selected run IDs", Cell(_ids(discovery.selected_run_ids))),
        ]
    lookback = Cell(collection.lookback_start, "datetime") if collection.lookback_start else Cell("no limit")
    items += [
        ("History window start (UTC)", Cell(collection.window_start, "datetime")),
        ("Lookback start (UTC)", lookback),
        ("Max distinct runs per history", Cell(analysis.max_runs)),
        ("Min conclusive executions for Stable", Cell(analysis.min_executions)),
        ("Collection status", Cell("PARTIAL", NEUTRAL) if collection.is_partial else Cell("OK")),
        ("API requests", Cell(f"{collection.requests_made} (budget {settings.http.max_requests_per_execution})")),
        ("Request budget exhausted during collection", Cell("yes" if collection.budget_exhausted else "no")),
        ("Result records fetched", Cell(collection.result_records_fetched)),
        ("Result-record cap", Cell(collection.max_results if collection.max_results is not None else "none")),
        ("Test cases", Cell(len({a.key.case_id for a in analysis.analyses}))),
        ("Application / platform combinations", Cell(len(pairs))),
        ("Histories (case / application / platform)", Cell(len(analysis.analyses))),
        ("Histories shown in combination columns", Cell(len(shown))),
        ("Histories not shown (unidentified application or platform)", Cell(len(analysis.analyses) - len(shown))),
        ("Coverage", Cell(_counts(COVERAGE_LABELS[a.coverage] for a in analysis.analyses))),
        ("Diagnoses", Cell(_counts(DIAGNOSIS_LABELS[a.diagnosis][0] for a in analysis.analyses))),
        ("Overview statuses (shown histories)", Cell(_counts(str(overview_cell(a).value) for a in shown))),
        ("Defect coverage", Cell(analysis.defect_coverage.value.replace("_", " "))),
        ("Feature / Test Group", Cell(_group_summary(analysis.case_groups))),
    ]
    limitations = list(dict.fromkeys([*analysis.limitations, *notes]))
    if len(shown) < len(analysis.analyses):
        limitations.append(
            f"{len(analysis.analyses) - len(shown)} history(ies) with an unidentified application or platform "
            "are not shown in the combination columns"
        )
    items += _coverage_notes(analysis)
    items += [("Limitation", Cell(note)) for note in limitations] or [("Limitations", Cell("none"))]
    styles = {"Green": GREEN, "Yellow": YELLOW, "Red": RED, "Neutral": NEUTRAL}
    items += [(f"Legend: {term}", Cell(text, styles.get(term))) for term, text in LEGEND]
    rows = tuple((Cell(label), value) for label, value in items)
    return Worksheet("Report Info", (("Item", 260), ("Value", 620)), rows)


def _coverage_notes(analysis: AnalysisReport) -> list[tuple[str, Cell]]:
    """Explicit disclosure of histories whose coverage is limited."""
    coverage = Counter(a.coverage for a in analysis.analyses)
    partial = sum(1 for a in analysis.analyses if a.diagnosis is Diagnosis.PARTIAL)
    notes = []
    if coverage[Coverage.SELECTION_LIMITED]:
        notes.append(
            f"Selection limited ({coverage[Coverage.SELECTION_LIMITED]} history(ies)): only the selected plans and "
            "runs were scanned; newer results in other runs are not reflected"
        )
    if coverage[Coverage.LOOKBACK_LIMITED]:
        notes.append(
            f"Lookback limited ({coverage[Coverage.LOOKBACK_LIMITED]} history(ies)): results before the lookback "
            "start were not searched"
        )
    if coverage[Coverage.PARTIAL] or partial:
        notes.append(
            f"Partial ({coverage[Coverage.PARTIAL]} history(ies) with partial coverage, {partial} diagnosed Partial): "
            "collection was incomplete, so evidence may be missing"
        )
    return [("Coverage note", Cell(note, NEUTRAL)) for note in notes]


def _group_summary(groups: Mapping[int, CaseGroup] | None) -> str:
    if groups is None:
        return "not requested"
    resolved = sum(1 for group in groups.values() if group.is_resolved)
    unknown = Counter(group.state.value for group in groups.values() if not group.is_resolved)
    summary = f"{resolved} resolved from TestRail sections"
    return summary + (f"; unknown: {_counts(unknown.elements())}" if unknown else "")


def _counts(values: Iterable[str]) -> str:
    counter = Counter(values)
    return ", ".join(f"{key}={counter[key]}" for key in sorted(counter)) or "none"


def _ids(values: Sequence[int]) -> str:
    return ", ".join(str(value) for value in values) or "none"


def safe_text(value: str) -> str:
    """Cell text without XML-invalid characters, neutralized against formula injection, within Excel's limit."""
    text = _INVALID_XML.sub("", value)
    if text.startswith(_FORMULA_PREFIXES):
        text = "'" + text
    if len(text) > _MAX_CELL_LENGTH:
        text = text[: _MAX_CELL_LENGTH - 1] + "…"
    return text


