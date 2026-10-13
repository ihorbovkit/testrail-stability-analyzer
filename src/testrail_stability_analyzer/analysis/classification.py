"""Deterministic diagnosis and confidence for one case/application/platform history.

Rules are applied in order; the first match wins (P = passed, F = failed,
A = automation issue, E = P + F + A, n = ``analysis.min_executions``):

1. ``not_applicable``: the history state is not applicable.
2. ``partial``: coverage is partial, results are incomplete, or the window was interrupted.
3. ``unresolved_applicability``: not executed, and applicability metadata is missing.
4. Empty window: ``not_run_in_window`` if an earlier execution is known, else ``not_run``.
5. ``insufficient_history``: E = 0 (only blocked, retest, not run, or unknown in the window).
6. P = E: ``stable`` if E >= n and no unknown status is in the window, else ``insufficient_history``.
7. ``inconsistent``: P > 0 and F > 0.
8. ``automation_issue_evidence``: A > 0 and F = 0 (with or without passes).
9. P = 0: ``application_failure_evidence`` (A = 0) or ``mixed_failure_evidence`` (A > 0).

Linked defects never influence the diagnosis.

Diagnoses 1-4 do not rest on execution evidence, so their confidence is
``none`` even when the window holds conclusive executions; the metrics are
reported unchanged.
"""

from __future__ import annotations

from ..collection.models import AvailabilityState, Coverage, ExecutionHistory
from .models import Confidence, Diagnosis, StabilityMetrics

NON_EVIDENCE_DIAGNOSES = frozenset(
    {
        Diagnosis.NOT_APPLICABLE,
        Diagnosis.NOT_RUN,
        Diagnosis.NOT_RUN_IN_WINDOW,
        Diagnosis.UNRESOLVED_APPLICABILITY,
        Diagnosis.PARTIAL,
    }
)


def diagnose(
    history: ExecutionHistory, metrics: StabilityMetrics, *, min_executions: int, max_runs: int
) -> tuple[Diagnosis, Confidence, tuple[str, ...]]:
    diagnosis, reasons = _diagnosis(history, metrics, min_executions)
    if diagnosis in NON_EVIDENCE_DIAGNOSES:
        return diagnosis, Confidence.NONE, reasons
    confidence, confidence_reasons = _confidence(history, metrics, min_executions, max_runs)
    return diagnosis, confidence, reasons + confidence_reasons


def _diagnosis(
    history: ExecutionHistory, m: StabilityMetrics, min_executions: int
) -> tuple[Diagnosis, tuple[str, ...]]:
    evidence = m.evidence_count
    if history.state is AvailabilityState.NOT_APPLICABLE:
        return Diagnosis.NOT_APPLICABLE, ("the case is not applicable to this application/platform",)
    if history.coverage is Coverage.PARTIAL or history.state is AvailabilityState.RESULTS_INCOMPLETE:
        return Diagnosis.PARTIAL, ("collection was incomplete for this history",)
    if history.window_interrupted:
        return Diagnosis.PARTIAL, ("the history window was not fully collected",)
    if history.state is AvailabilityState.UNKNOWN_APPLICABILITY:
        return Diagnosis.UNRESOLVED_APPLICABILITY, ("not executed, and applicability metadata is missing",)
    if not history.window:
        executed_before = history.latest_known is not None or (
            history.state is AvailabilityState.NO_RESULT_WITHIN_LOOKBACK and bool(history.outcomes)
        )
        selected = history.coverage is Coverage.SELECTION_LIMITED
        scope = " in the selected runs; runs outside the selection were not scanned" if selected else ""
        if executed_before:
            return Diagnosis.NOT_RUN_IN_WINDOW, (
                f"an earlier execution is known, but none within the history window{scope}",
            )
        return Diagnosis.NOT_RUN, (f"no execution is known{scope}",)
    if evidence == 0:
        return Diagnosis.INSUFFICIENT_HISTORY, (
            f"no conclusive execution among {m.executions} execution(s) in the window",
        )
    summary = (
        f"{m.passed} passed, {m.failed} failed, {m.automation_issue} automation issue(s) "
        f"in {evidence} conclusive execution(s)"
    )
    if m.passed == evidence:
        if evidence < min_executions:
            return Diagnosis.INSUFFICIENT_HISTORY, (
                summary,
                f"fewer than {min_executions} conclusive executions; stability is not established",
            )
        if m.unknown:
            return Diagnosis.INSUFFICIENT_HISTORY, (
                summary,
                f"{m.unknown} execution(s) with an unclassified status; stability is not established",
            )
        return Diagnosis.STABLE, (summary,)
    if m.passed and m.failed:
        return Diagnosis.INCONSISTENT, (summary, "mixed results do not by themselves prove flakiness")
    if m.automation_issue and not m.failed:
        return Diagnosis.AUTOMATION_ISSUE_EVIDENCE, (summary,)
    if m.automation_issue:
        return Diagnosis.MIXED_FAILURE_EVIDENCE, (summary,)
    return Diagnosis.APPLICATION_FAILURE_EVIDENCE, (summary, "failed results do not confirm an application defect")


def _confidence(
    history: ExecutionHistory, m: StabilityMetrics, min_executions: int, max_runs: int
) -> tuple[Confidence, tuple[str, ...]]:
    """Confidence of an evidence-based diagnosis (partial collection is diagnosed ``partial`` before)."""
    evidence = m.evidence_count
    if evidence == 0:
        return Confidence.NONE, ()
    low = []
    if evidence < min_executions:
        low.append(f"confidence is low: fewer than {min_executions} conclusive executions")
    if not history.key.is_identified:
        low.append("confidence is low: the run's application or platform is not identified")
    if low:
        return Confidence.LOW, tuple(low)
    sufficient_coverage = history.coverage in (Coverage.COMPLETE, Coverage.LOOKBACK_LIMITED) and history.window_complete
    if evidence >= max_runs and sufficient_coverage:
        return Confidence.HIGH, ()
    if evidence >= max_runs:
        return Confidence.MEDIUM, ("confidence is at most medium: the history window may be incomplete",)
    return Confidence.MEDIUM, ()
