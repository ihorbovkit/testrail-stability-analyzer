"""Typed records produced by historical collection.

Every record keeps the source TestRail objects (plan/run/test/result IDs and
timestamps) so that downstream analysis and reports remain auditable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ..config.settings import StatusCategory
from ..models import ResolvedStatus, Result, Run, Test
from .sanitize import sanitize_comment


class DimensionState(StrEnum):
    """How an application, platform, or execution type was determined for a run."""

    RESOLVED = "resolved"  # exactly one configured mapping matched
    UNRESOLVED = "unresolved"  # mappings are configured but none matched
    AMBIGUOUS = "ambiguous"  # several mappings matched; none is chosen
    NOT_CONFIGURED = "not_configured"  # no mappings exist for this dimension


@dataclass(frozen=True, slots=True)
class DimensionValue:
    state: DimensionState
    name: str | None = None  # set only when RESOLVED
    candidates: tuple[str, ...] = ()  # matching names when AMBIGUOUS

    @property
    def is_identified(self) -> bool:
        """True when runs sharing this value may be grouped together.

        Only a resolved mapping is evidence that two runs share an application or
        platform; ``not_configured`` is treated like ``unresolved``.
        """
        return self.state is DimensionState.RESOLVED


NOT_CONFIGURED = DimensionValue(DimensionState.NOT_CONFIGURED)


@dataclass(frozen=True, slots=True)
class RunClassification:
    application: DimensionValue = NOT_CONFIGURED
    platform: DimensionValue = NOT_CONFIGURED
    execution_type: DimensionValue = NOT_CONFIGURED


class RunDecision(StrEnum):
    INCLUDED = "included"
    EXCLUDED_BY_NAME = "excluded_by_name"  # naming.*_name_patterns did not match
    EXCLUDED_BY_FILTER = "excluded_by_filter"  # resolved to a value outside [filters] or suite
    UNCLASSIFIED = "unclassified"  # filters are active but a filtered dimension is not resolved
    OUTSIDE_LOOKBACK = "outside_lookback"  # completed plan run created before the lookback start


@dataclass(frozen=True, slots=True)
class DiscoveredRun:
    """A run found during discovery, with its plan context and inclusion decision."""

    run: Run
    classification: RunClassification
    decision: RunDecision
    reason: str | None = None
    plan_name: str | None = None

    @property
    def run_id(self) -> int:
        return self.run.id

    @property
    def plan_id(self) -> int | None:
        return self.run.plan_id

    @property
    def included(self) -> bool:
        return self.decision is RunDecision.INCLUDED


class SelectionKind(StrEnum):
    PLAN = "plan"
    RUN = "run"


@dataclass(frozen=True, slots=True)
class InvalidSelection:
    """An explicitly selected plan or run ID that could not be collected."""

    kind: SelectionKind
    id: int
    reason: str
    http_status: int | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    runs: tuple[DiscoveredRun, ...]
    # False when runs may be missing: request budget, or selected IDs that could not be collected.
    complete: bool
    limitations: tuple[str, ...] = ()
    budget_exhausted: bool = False
    # Targeted collection: only explicitly selected plans and runs; no project-wide discovery.
    targeted: bool = False
    selected_plan_ids: tuple[int, ...] = ()
    selected_run_ids: tuple[int, ...] = ()
    invalid_selections: tuple[InvalidSelection, ...] = ()

    @property
    def included(self) -> tuple[DiscoveredRun, ...]:
        return tuple(run for run in self.runs if run.included)

    @property
    def unclassified(self) -> tuple[DiscoveredRun, ...]:
        return tuple(run for run in self.runs if run.decision is RunDecision.UNCLASSIFIED)


@dataclass(frozen=True, slots=True)
class ResolvedResult:
    """A result record with its resolved status (``None`` for comment-only records)."""

    result: Result
    status: ResolvedStatus | None

    @property
    def is_comment_only(self) -> bool:
        return self.result.status_id is None

    def failure_summary(self, max_length: int = 200) -> str | None:
        """A sanitized, truncated comment suitable for reports; never the raw comment."""
        return sanitize_comment(self.result.comment, max_length=max_length)


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """The final outcome of one test (one case in one run), with all supporting records.

    ``final`` is the latest status-bearing result; ``None`` when the test has no
    status-bearing results in the collected range. ``results`` are chronological
    and include retries and comment-only records.
    """

    source: DiscoveredRun
    test: Test
    results: tuple[ResolvedResult, ...]
    final: ResolvedResult | None
    current_status: ResolvedStatus | None  # the test's status as reported by get_tests
    application_version: str | None = None
    # True when results created before the lookback start were not fetched.
    evidence_truncated: bool = False
    # False when the run's result stream was cut by the result-record cap, so
    # records of this test (possibly its final triage) may be missing.
    results_complete: bool = True

    @property
    def case_id(self) -> int:
        return self.test.case_id

    @property
    def test_id(self) -> int:
        return self.test.id

    @property
    def run_id(self) -> int:
        return self.source.run_id

    @property
    def plan_id(self) -> int | None:
        return self.source.plan_id

    @property
    def executed_on(self) -> datetime | None:
        """Timestamp of the final result; never inferred from run or plan dates."""
        return None if self.final is None else self.final.result.created_on

    @property
    def category(self) -> StatusCategory | None:
        return None if self.final is None or self.final.status is None else self.final.status.category

    @property
    def is_verified_final(self) -> bool:
        """True when ``final`` comes from an uncapped result stream for the run."""
        return self.final is not None and self.results_complete


class AvailabilityState(StrEnum):
    RESULT_AVAILABLE = "result_available"
    UNTESTED = "untested"  # included in a run, no status-bearing result
    NOT_APPLICABLE = "not_applicable"  # final status is not_applicable, or configured metadata says so
    NOT_INCLUDED_IN_RUN = "not_included_in_run"  # runs exist for the app/platform, case not in them
    NO_RUN_FOR_PLATFORM = "no_run_for_platform"  # no collected run for this app/platform
    UNKNOWN_APPLICABILITY = "unknown_applicability"  # not included, and applicability metadata is missing
    NO_RESULT_WITHIN_LOOKBACK = "no_result_within_lookback"  # executed, but no result record within lookback
    RESULTS_INCOMPLETE = "results_incomplete"  # no status-bearing record retrieved before the record cap


class Applicability(StrEnum):
    APPLICABLE = "applicable"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"  # configured, but the value or the application is unavailable
    NOT_CONFIGURED = "not_configured"


class Coverage(StrEnum):
    """How far the latest-known result is backed by the scan."""

    # Every discovered run attributed to this case/app/platform was scanned with no lookback limit.
    COMPLETE = "complete"
    # As COMPLETE, but only runs created within the lookback (or still open) were discovered.
    LOOKBACK_LIMITED = "lookback_limited"
    # Targeted collection: complete within the selected runs; other runs were not scanned.
    SELECTION_LIMITED = "selection_limited"
    # Some relevant runs could not be scanned (request budget, incomplete discovery).
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class HistoryKey:
    """Grouping key. ``run_id`` is set only when application or platform is not identified,
    so unidentified executions are never merged with each other or with identified ones."""

    case_id: int
    application: DimensionValue
    platform: DimensionValue
    run_id: int | None = None

    @property
    def is_identified(self) -> bool:
        return self.run_id is None


@dataclass(frozen=True, slots=True)
class ExecutionHistory:
    key: HistoryKey
    state: AvailabilityState
    applicability: Applicability
    # All collected outcomes for the key (one per run), newest final result first.
    outcomes: tuple[RunOutcome, ...]
    # Analytical view: outcomes with a final result inside the window, newest first, capped.
    window: tuple[RunOutcome, ...]
    window_total: int  # outcomes inside the window before the cap
    latest_known: RunOutcome | None
    coverage: Coverage
    coverage_notes: tuple[str, ...] = ()
    # False when runs that may hold results inside the window were not scanned, so
    # ``window``/``window_total`` can miss executions even if ``latest_known`` is backed.
    window_complete: bool = True

    @property
    def is_verified_latest(self) -> bool:
        return self.latest_known is not None and self.coverage is Coverage.COMPLETE


@dataclass(frozen=True, slots=True)
class RunScan:
    """What was fetched for one included run."""

    run_id: int
    results_fetched: bool
    tests_fetched: bool
    result_count: int = 0
    newest_result_on: datetime | None = None
    has_untimed_results: bool = False
    # Open, or created in the window: its tests are needed even without results,
    # since they can hold untested instances.
    active_in_window: bool = False
    # The result-record cap was reached in this run's stream; older records may be missing.
    results_capped: bool = False
    # Results were not fetched because the cap had already been reached.
    skipped_by_cap: bool = False

    @property
    def cut_by_cap(self) -> bool:
        return self.results_capped or self.skipped_by_cap

    @property
    def complete(self) -> bool:
        return self.results_fetched and self.tests_fetched and not self.results_capped


@dataclass(frozen=True, slots=True)
class ResultConflict:
    """Two records with the same result ID but different content; the first is kept."""

    run_id: int
    result_id: int


@dataclass(frozen=True, slots=True)
class CollectionReport:
    collected_at: datetime
    window_start: datetime
    lookback_start: datetime | None
    discovery: DiscoveryResult
    scans: tuple[RunScan, ...]
    outcomes: tuple[RunOutcome, ...]
    histories: tuple[ExecutionHistory, ...]
    # Results whose test ID is not among the run's tests (e.g. removed from the run).
    orphan_results: tuple[ResolvedResult, ...]
    conflicts: tuple[ResultConflict, ...]
    budget_exhausted: bool
    requests_made: int
    limitations: tuple[str, ...]
    # Result records retrieved from the API (including retries and comment-only records),
    # as opposed to the number of distinct run outcomes.
    result_records_fetched: int = 0
    max_results: int | None = None  # configured result-record cap, if any
    results_capped: bool = False  # the cap was reached; further records were not retrieved

    @property
    def is_partial(self) -> bool:
        """True when collection was interrupted (cap, budget, invalid selections) or histories are partial."""
        return (
            self.budget_exhausted
            or self.results_capped
            or not self.discovery.complete
            or any(not scan.results_fetched for scan in self.scans)
            or any(history.coverage is Coverage.PARTIAL for history in self.histories)
        )
