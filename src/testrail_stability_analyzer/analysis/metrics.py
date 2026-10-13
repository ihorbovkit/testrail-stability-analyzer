"""Objective stability metrics for one case/application/platform history.

The population is ``ExecutionHistory.window``: distinct runs whose final result
lies in the history window, capped at ``history.max_runs``. Each run counts
once; retries and triage records stay inside it and are only reflected in the
retry/triage metrics.
"""

from __future__ import annotations

from collections import Counter

from ..collection.models import ExecutionHistory, RunOutcome
from ..config.settings import StatusCategory
from .models import Rate, StabilityMetrics, StatusTransition


def compute_metrics(history: ExecutionHistory) -> StabilityMetrics:
    window = history.window
    counts = Counter(outcome.category for outcome in window)
    evidence = counts[StatusCategory.PASSED] + counts[StatusCategory.FAILED] + counts[StatusCategory.AUTOMATION_ISSUE]
    retried = sum(1 for outcome in window if len(outcome.results) > 1)
    latest = history.latest_known
    return StabilityMetrics(
        latest_category=None if latest is None else latest.category,
        latest_executed_on=None if latest is None else latest.executed_on,
        latest_result_id=None if latest is None or latest.final is None else latest.final.result.id,
        latest_run_id=None if latest is None else latest.run_id,
        latest_is_verified_final=latest is not None and latest.is_verified_final,
        latest_in_window=any(outcome is latest for outcome in window),
        executions=len(window),
        window_total=history.window_total,
        passed=counts[StatusCategory.PASSED],
        failed=counts[StatusCategory.FAILED],
        blocked=counts[StatusCategory.BLOCKED],
        automation_issue=counts[StatusCategory.AUTOMATION_ISSUE],
        not_applicable=counts[StatusCategory.NOT_APPLICABLE],
        retest=counts[StatusCategory.RETEST],
        not_run=counts[StatusCategory.NOT_RUN],
        unknown=counts[StatusCategory.UNKNOWN],
        unverified_finals=sum(1 for outcome in window if not outcome.results_complete),
        pass_rate=Rate(counts[StatusCategory.PASSED], evidence),
        failure_rate=Rate(counts[StatusCategory.FAILED], evidence),
        automation_issue_rate=Rate(counts[StatusCategory.AUTOMATION_ISSUE], evidence),
        retry_triage_count=retried,
        retry_triage_rate=Rate(retried, len(window)),
        status_changed_within_run=sum(1 for outcome in window if _changed_within_run(outcome)),
        transitions=_transitions(window),
    )


def _changed_within_run(outcome: RunOutcome) -> bool:
    """True when consecutive status-bearing records differ in category (e.g. failed -> passed -> failed).

    Comment-only records carry no status and are skipped.
    """
    categories = [r.status.category for r in outcome.results if r.status is not None]
    return any(previous is not current for previous, current in zip(categories, categories[1:]))


def _transitions(window: tuple[RunOutcome, ...]) -> tuple[StatusTransition, ...]:
    chronological = list(reversed(window))  # the window is newest first
    transitions = []
    for previous, current in zip(chronological, chronological[1:]):
        if previous.category is not None and current.category is not None and previous.category is not current.category:
            transitions.append(
                StatusTransition(
                    previous.category, current.category, previous.run_id, current.run_id, current.executed_on
                )
            )
    return tuple(transitions)
