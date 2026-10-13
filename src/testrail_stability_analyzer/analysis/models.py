"""Typed results of stability analysis and case-level defect aggregation.

Objective metrics (:class:`StabilityMetrics`) are kept separate from the
diagnostic interpretation (:class:`Diagnosis`, :class:`Confidence`). Every
record links back to the collected history, so source TestRail IDs stay
available for auditing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ..collection.models import (
    Applicability,
    AvailabilityState,
    Coverage,
    DimensionValue,
    ExecutionHistory,
    HistoryKey,
)
from ..config.settings import StatusCategory


class Diagnosis(StrEnum):
    """Diagnostic interpretation of one case/application/platform history.

    Labels describe evidence, not causes: ``application_failure_evidence`` means
    executions ended as failed, not that an application defect is confirmed.
    """

    STABLE = "stable"  # at least min_executions conclusive executions, all passed
    INCONSISTENT = "inconsistent"  # passed and failed executions coexist; not proof of flakiness
    AUTOMATION_ISSUE_EVIDENCE = "automation_issue_evidence"  # automation issues, no failures
    APPLICATION_FAILURE_EVIDENCE = "application_failure_evidence"  # failures only, no passes or automation issues
    MIXED_FAILURE_EVIDENCE = "mixed_failure_evidence"  # failures and automation issues, no passes
    INSUFFICIENT_HISTORY = "insufficient_history"  # too few conclusive executions to call it stable
    NOT_RUN_IN_WINDOW = "not_run_in_window"  # executed before, but not within the window
    NOT_RUN = "not_run"  # no execution is known
    NOT_APPLICABLE = "not_applicable"
    PARTIAL = "partial"  # collection was interrupted; evidence may be missing
    UNRESOLVED_APPLICABILITY = "unresolved_applicability"  # not executed, applicability metadata missing


class Confidence(StrEnum):
    NONE = "none"  # no conclusive execution, or a diagnosis that does not rest on execution evidence
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class Rate:
    """An exact ratio; ``value`` is ``None`` when the denominator is zero."""

    numerator: int
    denominator: int

    @property
    def value(self) -> float | None:
        return None if self.denominator == 0 else self.numerator / self.denominator


@dataclass(frozen=True, slots=True)
class StatusTransition:
    """A change of final category between two consecutive executions (oldest to newest)."""

    from_category: StatusCategory
    to_category: StatusCategory
    from_run_id: int
    to_run_id: int
    at: datetime | None  # final result timestamp of the later execution


@dataclass(frozen=True, slots=True)
class StabilityMetrics:
    """Objective metrics over the capped history window (one execution per distinct run).

    ``evidence_count`` (the pass-rate denominator) counts conclusive executions:
    passed + failed + automation issue. Blocked, retest, not run, not applicable,
    and unknown executions are counted but excluded from the rates.
    """

    # Latest known result; may be older than the window.
    latest_category: StatusCategory | None
    latest_executed_on: datetime | None
    latest_result_id: int | None
    latest_run_id: int | None
    latest_is_verified_final: bool  # its run's result stream was not cut by the record cap
    latest_in_window: bool
    executions: int  # distinct runs in the window (after the max_runs cap)
    window_total: int  # distinct runs in the window before the cap
    passed: int
    failed: int
    blocked: int
    automation_issue: int
    not_applicable: int
    retest: int
    not_run: int
    unknown: int
    unverified_finals: int  # executions whose run's result stream was cut by the record cap
    pass_rate: Rate  # passed / evidence_count
    failure_rate: Rate  # failed / evidence_count
    automation_issue_rate: Rate  # automation_issue / evidence_count
    retry_triage_count: int  # executions with more than one result record
    retry_triage_rate: Rate  # retry_triage_count / executions
    # Executions with a category change between consecutive status-bearing records
    # (e.g. failed -> passed, or failed -> passed -> failed); comment-only records are ignored.
    status_changed_within_run: int
    transitions: tuple[StatusTransition, ...]

    @property
    def evidence_count(self) -> int:
        return self.passed + self.failed + self.automation_issue


class DefectCoverage(StrEnum):
    COMPLETE = "complete"  # every discovered run that may hold results in the period was scanned
    # Everything in scope was scanned, but the scope excludes runs (targeted selection, naming patterns, filters).
    SELECTION_LIMITED = "selection_limited"
    # Runs within the scope may be missing (interrupted collection, unscanned or unclassified runs).
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class DefectSource:
    """One result record that references a defect. Never holds comment text."""

    result_id: int
    test_id: int
    run_id: int
    plan_id: int | None
    created_on: datetime
    status_category: StatusCategory | None  # None for comment-only records
    is_final_result: bool  # the record is the run's final outcome (not a retry or triage record)
    application: DimensionValue
    platform: DimensionValue
    application_version: str | None  # recorded on this result itself; not inherited from the final result


@dataclass(frozen=True, slots=True)
class DefectReference:
    """A defect identifier exactly as entered in TestRail, with every record that references it."""

    defect_id: str
    sources: tuple[DefectSource, ...]  # chronological

    @property
    def first_seen(self) -> datetime:
        return self.sources[0].created_on

    @property
    def last_seen(self) -> datetime:
        return self.sources[-1].created_on


@dataclass(frozen=True, slots=True)
class CaseDefects:
    """Defects referenced by result records of one case within the reporting period.

    Aggregated across all applications and platforms, retries, and triage
    records. An empty ``defects`` tuple means "none found": check ``coverage``
    and ``records_examined`` before reading it as "none linked".
    """

    case_id: int
    defects: tuple[DefectReference, ...]  # by first appearance, then by identifier
    records_examined: int  # dated result records within the period
    records_with_defects: int
    undated_records_with_defects: int  # excluded: no timestamp to place them in the period
    coverage: DefectCoverage
    notes: tuple[str, ...] = ()

    @property
    def defect_ids(self) -> tuple[str, ...]:
        return tuple(defect.defect_id for defect in self.defects)


@dataclass(frozen=True, slots=True)
class KeyAnalysis:
    """Analysis of one case/application/platform history."""

    history: ExecutionHistory
    metrics: StabilityMetrics
    diagnosis: Diagnosis
    confidence: Confidence
    reasons: tuple[str, ...]
    defects: CaseDefects  # shared by every application/platform of the case

    @property
    def key(self) -> HistoryKey:
        return self.history.key

    @property
    def state(self) -> AvailabilityState:
        return self.history.state

    @property
    def applicability(self) -> Applicability:
        return self.history.applicability

    @property
    def coverage(self) -> Coverage:
        return self.history.coverage

    @property
    def coverage_notes(self) -> tuple[str, ...]:
        return self.history.coverage_notes


@dataclass(frozen=True, slots=True)
class AnalysisReport:
    collected_at: datetime
    window_start: datetime
    min_executions: int
    max_runs: int
    analyses: tuple[KeyAnalysis, ...]
    case_defects: tuple[CaseDefects, ...]  # by case ID
    defect_coverage: DefectCoverage
    # Defect-bearing result records within the period whose test is not among the
    # run's tests, so their case is unknown and they are not aggregated.
    orphan_records_with_defects: int
    limitations: tuple[str, ...]

    def defects_for(self, case_id: int) -> CaseDefects | None:
        return next((defects for defects in self.case_defects if defects.case_id == case_id), None)
