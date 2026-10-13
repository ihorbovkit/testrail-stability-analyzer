"""Analyze a :class:`CollectionReport`: stability per history and defects per case.

Analysis is a pure function of collected data: it makes no TestRail requests.
"""

from __future__ import annotations

from ..collection.models import CollectionReport
from ..config.settings import Settings
from .classification import diagnose
from .defects import aggregate_defects, defect_coverage, orphan_records_with_defects
from .metrics import compute_metrics
from .models import AnalysisReport, KeyAnalysis


def analyze(report: CollectionReport, settings: Settings) -> AnalysisReport:
    min_executions = settings.analysis.min_executions
    max_runs = settings.history.max_runs
    case_defects = aggregate_defects(
        report,
        (history.key.case_id for history in report.histories),
        version_field=settings.mappings.custom_fields.get("application_version"),
    )
    defects_by_case = {defects.case_id: defects for defects in case_defects}
    analyses = []
    for history in report.histories:
        metrics = compute_metrics(history)
        diagnosis, confidence, reasons = diagnose(history, metrics, min_executions=min_executions, max_runs=max_runs)
        defects = defects_by_case[history.key.case_id]
        analyses.append(KeyAnalysis(history, metrics, diagnosis, confidence, reasons, defects))

    coverage, _ = defect_coverage(report)
    orphans = orphan_records_with_defects(report)
    limitations = list(report.limitations)  # includes the targeted-collection note of discovery
    if orphans:
        limitations.append(
            f"{orphans} defect-bearing result record(s) in the period belong to tests no longer in their run "
            "and are not attributed to a case"
        )
    undated = sum(defects.undated_records_with_defects for defects in case_defects)
    if undated:
        limitations.append(f"{undated} defect-bearing result record(s) without a timestamp were excluded")
    return AnalysisReport(
        collected_at=report.collected_at,
        window_start=report.window_start,
        min_executions=min_executions,
        max_runs=max_runs,
        analyses=tuple(analyses),
        case_defects=case_defects,
        defect_coverage=coverage,
        orphan_records_with_defects=orphans,
        limitations=tuple(dict.fromkeys(limitations)),
    )
