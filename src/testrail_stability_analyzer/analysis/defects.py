"""Aggregate defect references per Test Case across applications, platforms, and runs.

Every collected result record of a case counts, including automated retries,
manual triage, and comment-only records, if its own timestamp lies within the
reporting period (the history window). The stability ``max_runs`` cap does not
apply. Only the result ``defects`` field is read; case-level ``refs`` are not
execution defects. Identifiers are kept exactly as entered: no tracker URLs
are derived.

Coverage is report-wide, since a run that was not scanned may hold records of
any collected case (see :func:`defect_coverage`).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime

from ..collection.history import may_hold_window_results
from ..collection.models import CollectionReport, DiscoveredRun, ResolvedResult, RunDecision, RunOutcome
from .models import CaseDefects, DefectCoverage, DefectReference, DefectSource


def parse_defects(value: str | None) -> tuple[str, ...]:
    """Split a comma-separated defect field; trim, drop empty entries, deduplicate case-sensitively."""
    if not value:
        return ()
    return tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))


def defect_coverage(report: CollectionReport) -> tuple[DefectCoverage, tuple[str, ...]]:
    """Report-wide, since any run that was not scanned may hold records of any case.

    ``partial``: runs within the collection scope may be missing (interrupted
    collection, incomplete discovery, unscanned included runs, or runs that
    could not be classified for the configured filters). ``selection_limited``:
    the scope itself excludes runs (targeted selection, naming patterns,
    filters). Runs of another suite cannot hold the collected cases and are
    ignored when the suites are known. Completed runs created before the
    lookback start are outside the scan, as for the latest known result.
    """
    notes = []
    if report.budget_exhausted:
        notes.append("request budget exhausted: defect references may be missing")
    if report.results_capped:
        notes.append("result-record cap reached: defect references may be missing")
    if not report.discovery.complete:
        notes.append("run discovery was incomplete: defect references may be missing")
    unscanned = [
        scan.run_id
        for scan in report.scans
        if not scan.complete and not scan.cut_by_cap and may_hold_window_results(scan, report.window_start)
    ]
    if unscanned:
        notes.append(f"{len(unscanned)} run(s) with possible results in the period were not fully scanned")
    not_collected = _not_collected(report)
    unclassified = sum(1 for run in not_collected if run.decision is RunDecision.UNCLASSIFIED)
    if unclassified:
        notes.append(
            f"{unclassified} run(s) could not be classified for the configured filters and were not scanned: "
            "defect references may be missing"
        )
    selection = []
    if report.discovery.targeted:
        selection.append("only the selected plans and runs were scanned")
    excluded = len(not_collected) - unclassified
    if excluded:
        selection.append(f"{excluded} run(s) excluded by naming patterns or filters were not scanned")
    if notes:
        return DefectCoverage.PARTIAL, tuple(notes + selection)
    if selection:
        return DefectCoverage.SELECTION_LIMITED, tuple(selection)
    return DefectCoverage.COMPLETE, ()


def _not_collected(report: CollectionReport) -> list[DiscoveredRun]:
    """Discovered runs that were not collected but may hold records of the collected cases."""
    included = report.discovery.included
    suites = {run.run.suite_id for run in included}
    # With nothing included no suite is known, so every excluded run may hold the cases.
    known_suites = suites if suites and None not in suites else None
    return [
        run
        for run in report.discovery.runs
        if run.decision in (RunDecision.EXCLUDED_BY_NAME, RunDecision.EXCLUDED_BY_FILTER, RunDecision.UNCLASSIFIED)
        and (known_suites is None or run.run.suite_id is None or run.run.suite_id in known_suites)
    ]


def aggregate_defects(report: CollectionReport, case_ids: Iterable[int] = ()) -> tuple[CaseDefects, ...]:
    """One entry per case with collected outcomes (plus ``case_ids``), ordered by case ID."""
    coverage, notes = defect_coverage(report)
    by_case: dict[int, list[RunOutcome]] = defaultdict(list)
    for case_id in case_ids:
        by_case.setdefault(case_id, [])
    for outcome in report.outcomes:
        by_case[outcome.case_id].append(outcome)
    return tuple(
        _case_defects(case_id, by_case[case_id], report.window_start, coverage, notes) for case_id in sorted(by_case)
    )


def orphan_records_with_defects(report: CollectionReport) -> int:
    """Defect-bearing records within the period whose case is unknown (test not among the run's tests)."""
    return sum(
        1
        for record in report.orphan_results
        if parse_defects(record.result.defects) and _in_period(record, report.window_start)
    )


def _case_defects(
    case_id: int,
    outcomes: list[RunOutcome],
    window_start: datetime,
    coverage: DefectCoverage,
    coverage_notes: tuple[str, ...],
) -> CaseDefects:
    sources: dict[str, list[DefectSource]] = defaultdict(list)
    examined = with_defects = undated = 0
    for outcome in outcomes:
        final_id = None if outcome.final is None else outcome.final.result.id
        for record in outcome.results:
            ids = parse_defects(record.result.defects)
            in_period = _in_period(record, window_start)
            if in_period is None:
                undated += bool(ids)
                continue
            if not in_period:
                continue
            examined += 1
            with_defects += bool(ids)
            for defect_id in ids:
                sources[defect_id].append(_source(outcome, record, final_id))
    defects = sorted(
        (DefectReference(defect_id, tuple(sorted(items, key=_source_order))) for defect_id, items in sources.items()),
        key=lambda defect: (_source_order(defect.sources[0]), defect.defect_id),
    )
    notes = list(coverage_notes)
    if undated:
        notes.append(f"{undated} defect-bearing result record(s) without a timestamp were excluded")
    return CaseDefects(
        case_id=case_id,
        defects=tuple(defects),
        records_examined=examined,
        records_with_defects=with_defects,
        undated_records_with_defects=undated,
        coverage=coverage,
        notes=tuple(notes),
    )


def _in_period(record: ResolvedResult, window_start: datetime) -> bool | None:
    created = record.result.created_on
    return None if created is None else created >= window_start


def _source(outcome: RunOutcome, record: ResolvedResult, final_id: int | None) -> DefectSource:
    classification = outcome.source.classification
    assert record.result.created_on is not None  # undated records are excluded before
    return DefectSource(
        result_id=record.result.id,
        test_id=outcome.test_id,
        run_id=outcome.run_id,
        plan_id=outcome.plan_id,
        created_on=record.result.created_on,
        status_category=None if record.status is None else record.status.category,
        is_final_result=record.result.id == final_id,
        application=classification.application,
        platform=classification.platform,
        # The record's own version only: the run's (final) version is not attributed to retries or triage.
        application_version=record.application_version,
    )


def _source_order(source: DefectSource) -> tuple[datetime, int]:
    return (source.created_on, source.result_id)
