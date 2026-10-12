"""Group run outcomes into per case/application/platform histories.

Each distinct run contributes at most one outcome per case (retries stay inside
the outcome). Histories are ordered by the actual final-result timestamp. The
analytical window view is capped; the full list of outcomes is kept.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from datetime import datetime

from ..config.settings import StatusCategory
from .models import (
    Applicability,
    AvailabilityState,
    Coverage,
    DimensionValue,
    DiscoveredRun,
    ExecutionHistory,
    HistoryKey,
    RunOutcome,
    RunScan,
)
from .outcomes import chronological_key

type Group = tuple[DimensionValue, DimensionValue]  # (application, platform)


def group_of(source: DiscoveredRun) -> Group | None:
    """The application/platform group of a run, or ``None`` if either is not identified."""
    classification = source.classification
    if classification.application.is_identified and classification.platform.is_identified:
        return classification.application, classification.platform
    return None


def history_key(case_id: int, source: DiscoveredRun) -> HistoryKey:
    classification = source.classification
    run_id = None if group_of(source) is not None else source.run_id
    return HistoryKey(case_id, classification.application, classification.platform, run_id)


def newest_first(outcome: RunOutcome) -> tuple[bool, tuple[bool, float, int], int]:
    """Sort key (use with ``reverse=True``): outcomes with a final result first, by its timestamp."""
    if outcome.final is None:
        return (False, (False, 0.0, 0), outcome.run_id)
    return (True, chronological_key(outcome.final.result), outcome.run_id)


def build_histories(
    outcomes: Iterable[RunOutcome],
    *,
    window_start: datetime,
    max_runs: int,
    groups_with_runs: Collection[Group],
    scanned_groups: Collection[Group],
    platforms: Collection[DimensionValue],
    gaps: Mapping[Group, Sequence[RunScan]],
    applicability: Callable[[int, DimensionValue], Applicability],
    global_notes: Sequence[str] = (),
    lookback_limited: bool = False,
) -> tuple[ExecutionHistory, ...]:
    """Build one history per key.

    Keys come from collected outcomes. An identified key is additionally
    expanded to every identified platform in ``platforms``, so a case without
    a run on one platform is reported as such instead of being absent.
    """
    by_key: dict[HistoryKey, list[RunOutcome]] = defaultdict(list)
    for outcome in outcomes:
        by_key[history_key(outcome.case_id, outcome.source)].append(outcome)
    for key in list(by_key):
        if key.is_identified:
            for platform in platforms:
                by_key.setdefault(HistoryKey(key.case_id, key.application, platform), [])

    histories = []
    for key in sorted(by_key, key=_key_order):
        ordered = tuple(sorted(by_key[key], key=newest_first, reverse=True))
        latest = next((outcome for outcome in ordered if outcome.final is not None), None)
        in_window = [o for o in ordered if o.executed_on is not None and o.executed_on >= window_start]
        group = (key.application, key.platform) if key.is_identified else None
        key_applicability = applicability(key.case_id, key.application)
        coverage, notes = _coverage(group, latest, gaps, global_notes, lookback_limited)
        histories.append(
            ExecutionHistory(
                key=key,
                state=_state(ordered, latest, group, key_applicability, groups_with_runs, scanned_groups),
                applicability=key_applicability,
                outcomes=ordered,
                window=tuple(in_window[:max_runs]),
                window_total=len(in_window),
                latest_known=latest,
                coverage=coverage,
                coverage_notes=notes,
            )
        )
    return tuple(histories)


def _key_order(key: HistoryKey) -> tuple[int, str, str, int]:
    return (key.case_id, _dimension_order(key.application), _dimension_order(key.platform), key.run_id or 0)


def _dimension_order(value: DimensionValue) -> str:
    return f"{value.state}:{value.name or ''}:{','.join(value.candidates)}"


def _state(
    ordered: Sequence[RunOutcome],
    latest: RunOutcome | None,
    group: Group | None,
    applicability: Applicability,
    groups_with_runs: Collection[Group],
    scanned_groups: Collection[Group],
) -> AvailabilityState:
    if latest is not None:
        if latest.category is StatusCategory.NOT_APPLICABLE:
            return AvailabilityState.NOT_APPLICABLE
        return AvailabilityState.RESULT_AVAILABLE
    if applicability is Applicability.NOT_APPLICABLE:
        return AvailabilityState.NOT_APPLICABLE
    if ordered:
        # Included in a run but no status-bearing record collected. If TestRail
        # reports a non-untested current status, the results predate the lookback.
        executed = any(
            o.current_status is not None and o.current_status.category is not StatusCategory.NOT_RUN for o in ordered
        )
        return AvailabilityState.NO_RESULT_WITHIN_LOOKBACK if executed else AvailabilityState.UNTESTED
    if group in scanned_groups:
        if applicability is Applicability.UNKNOWN:
            return AvailabilityState.UNKNOWN_APPLICABILITY
        return AvailabilityState.NOT_INCLUDED_IN_RUN
    if group in groups_with_runs:
        # Runs exist, but none had results within the lookback, so their tests were not fetched.
        return AvailabilityState.NO_RESULT_WITHIN_LOOKBACK
    return AvailabilityState.NO_RUN_FOR_PLATFORM


def _coverage(
    group: Group | None,
    latest: RunOutcome | None,
    gaps: Mapping[Group, Sequence[RunScan]],
    global_notes: Sequence[str],
    lookback_limited: bool,
) -> tuple[Coverage, tuple[str, ...]]:
    notes = list(global_notes)
    if group is not None:
        unscanned = [scan.run_id for scan in gaps.get(group, ()) if _may_hold_newer(scan, latest)]
        if unscanned:
            notes.append(
                f"{len(unscanned)} run(s) of this application/platform that may hold newer results were not scanned"
            )
    if notes:
        return Coverage.PARTIAL, tuple(notes)
    if lookback_limited:
        return Coverage.LOOKBACK_LIMITED, ("results before the lookback start were not searched",)
    return Coverage.COMPLETE, ()


def _may_hold_newer(scan: RunScan, latest: RunOutcome | None) -> bool:
    if not scan.results_fetched:
        return True
    if scan.result_count == 0:
        return False
    if latest is None or latest.executed_on is None or scan.has_untimed_results:
        return True
    return scan.newest_result_on is not None and scan.newest_result_on >= latest.executed_on
