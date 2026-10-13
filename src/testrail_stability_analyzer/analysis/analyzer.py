"""Analyze a :class:`CollectionReport`: stability per history and defects per case.

Analysis is a pure function of collected data: it makes no TestRail requests.
"""

from __future__ import annotations

from ..collection.models import CollectionReport, ExecutionHistory
from ..collection.reasons import extract_reason
from ..config.settings import BlockedReasonSettings, Settings, StatusCategory
from .classification import diagnose
from .defects import aggregate_defects, defect_coverage, orphan_records_with_defects
from .metrics import compute_metrics
from .models import AnalysisReport, KeyAnalysis


def analyze(report: CollectionReport, settings: Settings) -> AnalysisReport:
    min_executions = settings.analysis.min_executions
    max_runs = settings.history.max_runs
    case_defects = aggregate_defects(report, (history.key.case_id for history in report.histories))
    defects_by_case = {defects.case_id: defects for defects in case_defects}
    analyses = []
    for history in report.histories:
        metrics = compute_metrics(history)
        diagnosis, confidence, reasons = diagnose(history, metrics, min_executions=min_executions, max_runs=max_runs)
        defects = defects_by_case[history.key.case_id]
        blocked = blocked_reason(history, settings.report.blocked_reason)
        analyses.append(KeyAnalysis(history, metrics, diagnosis, confidence, reasons, defects, blocked))

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
        case_groups=report.case_groups,
    )


def blocked_reason(history: ExecutionHistory, settings: BlockedReasonSettings) -> str | None:
    """The reason of a latest known result that is a verified Blocked final, else ``None``.

    The final result's comment is searched first, then comment-only records
    added after it in the same run (triage notes), oldest first. Older results
    and other runs are never used. The reason does not affect the diagnosis.
    """
    latest = history.latest_known
    if not settings.enabled or latest is None or latest.final is None or not latest.is_verified_final:
        return None
    if latest.category is not StatusCategory.BLOCKED:
        return None
    position = next(index for index, record in enumerate(latest.results) if record is latest.final)
    for record in (latest.final, *(r for r in latest.results[position + 1 :] if r.is_comment_only)):
        reason = extract_reason(record.result.comment, settings.section_headers, max_length=settings.max_length)
        if reason is not None:
            return reason
    return None
