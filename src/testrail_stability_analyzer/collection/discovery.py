"""Discover standalone runs and Test Plan runs relevant to a lookback period.

Projects may use Test Plans, standalone runs, or both. Every run found is
returned with an inclusion decision; nothing is dropped silently.

With a lookback start ``L`` the following are discovered:

- runs and plans created at or after ``L``,
- runs and plans created before ``L`` that are still open (they may still
  receive results), and child runs of such plans that are open or created
  at or after ``L``.

Completed runs and plans created before ``L`` are not listed, so results they
may hold after ``L`` are outside the scan; collection reports this as a
lookback limitation. Without a lookback limit every run and plan is listed.

The configured suite is applied client-side, so runs from other suites are
recorded as excluded instead of being filtered out by the server unseen.

Targeted discovery (:meth:`RunDiscovery.discover_selected`) fetches only the
explicitly selected plans (``get_plan``) and runs (``get_run``, plus the parent
plan of a run for its name), without any project-wide listing. Selected runs
are not excluded for their age, but suite, naming, and filter decisions apply.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from ..api import TestRailClient, TestRailHTTPError, TestRailRequestBudgetExceeded
from ..config.settings import FilterSettings, Settings
from ..models import Plan, Run
from ..progress import DiscoveryProgress, ProgressListener, ignore_progress
from .classification import classify_run
from .models import (
    DimensionValue,
    DiscoveredRun,
    DiscoveryResult,
    InvalidSelection,
    RunClassification,
    RunDecision,
    SelectionKind,
)

# HTTP statuses TestRail returns for an ID that does not exist (400) or cannot be accessed.
_INVALID_ID_REASONS = {
    400: "does not exist or is not a valid ID",
    403: "is not accessible with the configured credentials",
    404: "was not found",
}

TARGETED_LIMITATION = (
    "targeted collection: only the selected plans and runs were scanned; "
    "other runs may hold newer results for the same cases"
)


@dataclass(frozen=True, slots=True)
class SamplePlanSelection:
    """Outcome of choosing the newest eligible plan for a sample collection."""

    plan: Plan | None  # with entries and runs, so it need not be fetched again
    plans_listed: int
    inspected_plan_ids: tuple[int, ...]
    notes: tuple[str, ...] = ()
    budget_exhausted: bool = False


class RunDiscovery:
    def __init__(
        self, client: TestRailClient, settings: Settings, *, progress: ProgressListener = ignore_progress
    ) -> None:
        """``progress`` receives cumulative :class:`~..progress.DiscoveryProgress` counts."""
        self._client = client
        self._settings = settings
        self._progress = progress

    def discover(self, lookback_start: datetime | None) -> DiscoveryResult:
        found: dict[int, DiscoveredRun] = {}
        limitations: list[str] = []
        budget_exhausted = False
        plans_fetched = 0
        try:
            for run in self._standalone_runs(lookback_start):
                self._add(found, run, None, lookback_start)
            self._report(found, plans_fetched)
            for plan in self._plans(lookback_start):
                detailed = self._client.get_plan(plan.id)
                plans_fetched += 1
                for run in detailed.runs:
                    self._add(found, run, detailed, lookback_start)
                self._report(found, plans_fetched)
        except TestRailRequestBudgetExceeded:
            budget_exhausted = True
            limitations.append("request budget exhausted during run discovery; some runs were not discovered")
        if lookback_start is not None:
            limitations.append(
                "completed runs and plans created before the lookback start were not scanned; "
                "any results they received later are not included"
            )
        runs = tuple(sorted(found.values(), key=lambda discovered: discovered.run_id))
        return DiscoveryResult(runs, not budget_exhausted, tuple(limitations), budget_exhausted)

    def discover_selected(
        self,
        plan_ids: Sequence[int] = (),
        run_ids: Sequence[int] = (),
        *,
        plans: Mapping[int, Plan] | None = None,
    ) -> DiscoveryResult:
        """Runs of the selected plans plus the selected runs, deduplicated by run ID.

        ``plans`` may supply already fetched plans (with entries) to avoid
        requesting them again. IDs that do not exist, are not accessible, or
        belong to another project are reported in ``invalid_selections``.
        """
        plan_ids, run_ids = tuple(dict.fromkeys(plan_ids)), tuple(dict.fromkeys(run_ids))
        found: dict[int, DiscoveredRun] = {}
        invalid: list[InvalidSelection] = []
        limitations: list[str] = []
        known: dict[int, Plan] = dict(plans or {})
        budget_exhausted = False
        total = len(plan_ids) + len(run_ids)
        done = 0
        try:
            for plan_id in plan_ids:
                self._report(found, len(known), targets_done=done, targets_total=total)
                done += 1
                plan = known.get(plan_id) or self._fetch(SelectionKind.PLAN, plan_id, self._client.get_plan, invalid)
                if plan is None or not self._in_project(SelectionKind.PLAN, plan_id, plan.project_id, invalid):
                    continue
                known[plan_id] = plan
                if not plan.runs:
                    limitations.append(f"selected plan {plan_id} has no runs")
                for run in plan.runs:
                    self._add(found, run, plan, None)
            for run_id in run_ids:
                self._report(found, len(known), targets_done=done, targets_total=total)
                done += 1
                if run_id in found:
                    continue  # already selected through its plan
                run = self._fetch(SelectionKind.RUN, run_id, self._client.get_run, invalid)
                if run is None or not self._in_project(SelectionKind.RUN, run_id, run.project_id, invalid):
                    continue
                parent = None
                if run.plan_id is not None:
                    parent = known.get(run.plan_id)
                    if parent is None:
                        try:
                            parent = self._parent_plan(run.plan_id, limitations)
                        except TestRailRequestBudgetExceeded:
                            # Keep the known run, as for an inaccessible parent; discovery stops here.
                            limitations.append(_parent_note(run.plan_id, "request budget exhausted"))
                            self._add(found, run, None, None)
                            raise
                    if parent is not None:
                        known[run.plan_id] = parent
                self._add(found, run, parent, None)
            self._report(found, len(known), targets_done=done, targets_total=total)
        except TestRailRequestBudgetExceeded:
            budget_exhausted = True
            limitations.append("request budget exhausted while resolving selected plans and runs")
        if invalid:
            limitations.append(f"{len(invalid)} selected plan/run ID(s) could not be collected")
        limitations.append(TARGETED_LIMITATION)
        runs = tuple(sorted(found.values(), key=lambda discovered: discovered.run_id))
        return DiscoveryResult(
            runs,
            complete=not budget_exhausted and not invalid,
            limitations=tuple(limitations),
            budget_exhausted=budget_exhausted,
            targeted=True,
            selected_plan_ids=plan_ids,
            selected_run_ids=run_ids,
            invalid_selections=tuple(invalid),
        )

    def find_sample_plan(self, lookback_start: datetime | None, *, max_candidates: int = 5) -> SamplePlanSelection:
        """The newest plan (by creation date) with at least one run eligible for collection.

        Plans created within the lookback are listed (all plans without a
        limit). Plans whose name does not match ``naming.plan_name_patterns``
        are skipped without a request; up to ``max_candidates`` remaining plans
        are fetched, newest first, until one has a run that the configured
        suite, naming patterns, and filters include. No unmatched plan is
        ever selected.
        """
        if max_candidates < 1:
            raise ValueError("max_candidates must be >= 1")
        project_id = self._settings.testrail.project_id
        created_after = None if lookback_start is None else created_after_cutoff(lookback_start)
        notes: list[str] = []
        try:
            listed = list(self._client.get_plans(project_id, created_after=created_after))
        except TestRailRequestBudgetExceeded:
            return SamplePlanSelection(None, 0, (), ("request budget exhausted while listing plans",), True)
        patterns = self._settings.naming.plan_name_patterns
        candidates = [
            plan
            for plan in sorted(listed, key=_newest_first, reverse=True)
            if not patterns or _matches_any(patterns, plan.name)
        ]
        if len(candidates) < len(listed):
            notes.append(f"{len(listed) - len(candidates)} plan(s) do not match naming.plan_name_patterns")
        inspected: list[int] = []
        limit = min(len(candidates), max_candidates)
        for done, summary in enumerate(candidates[:max_candidates]):
            self._progress(
                DiscoveryProgress(
                    plans_fetched=done,
                    plans_listed=len(listed),
                    targets_done=done,
                    targets_total=limit,
                    target_kind="candidate plans",
                )
            )
            try:
                plan = self._client.get_plan(summary.id)
            except TestRailRequestBudgetExceeded:
                notes.append("request budget exhausted while inspecting candidate plans")
                return SamplePlanSelection(None, len(listed), tuple(inspected), tuple(notes), True)
            except TestRailHTTPError as exc:
                if exc.status not in _INVALID_ID_REASONS:
                    raise
                notes.append(f"plan {summary.id} {_INVALID_ID_REASONS[exc.status]} (HTTP {exc.status})")
                continue
            inspected.append(plan.id)
            if any(self._decision(run, plan) is RunDecision.INCLUDED for run in plan.runs):
                return SamplePlanSelection(plan, len(listed), tuple(inspected), tuple(notes))
            notes.append(f"plan {plan.id} has no run eligible under the configured suite, naming patterns and filters")
        if len(candidates) > max_candidates:
            notes.append(f"stopped after inspecting {max_candidates} candidate plan(s)")
        if not listed:
            notes.append("no plans found" + (" within the lookback period" if lookback_start is not None else ""))
        return SamplePlanSelection(None, len(listed), tuple(inspected), tuple(notes))

    def _report(
        self,
        found: Mapping[int, DiscoveredRun],
        plans_fetched: int,
        *,
        targets_done: int = 0,
        targets_total: int | None = None,
    ) -> None:
        included = sum(1 for run in found.values() if run.included)
        self._progress(
            DiscoveryProgress(
                plans_fetched, len(found), included, targets_done=targets_done, targets_total=targets_total
            )
        )

    # -- targeted helpers ----------------------------------------------------

    def _fetch[T](
        self, kind: SelectionKind, item_id: int, fetch: Callable[[int], T], invalid: list[InvalidSelection]
    ) -> T | None:
        try:
            return fetch(item_id)
        except TestRailHTTPError as exc:
            if exc.status not in _INVALID_ID_REASONS:
                raise  # authentication, rate limiting and server errors stop the collection
            invalid.append(InvalidSelection(kind, item_id, f"{kind} {_INVALID_ID_REASONS[exc.status]}", exc.status))
            return None

    def _in_project(
        self, kind: SelectionKind, item_id: int, project_id: int | None, invalid: list[InvalidSelection]
    ) -> bool:
        if project_id is None or project_id == self._settings.testrail.project_id:
            return True
        invalid.append(InvalidSelection(kind, item_id, f"{kind} belongs to another project"))
        return False

    def _parent_plan(self, plan_id: int, limitations: list[str]) -> Plan | None:
        try:
            return self._client.get_plan(plan_id)
        except TestRailHTTPError as exc:
            if exc.status not in _INVALID_ID_REASONS:
                raise
            limitations.append(_parent_note(plan_id, f"HTTP {exc.status}"))
            return None

    def _decision(self, run: Run, plan: Plan) -> RunDecision:
        classification = classify_run(run, plan.name, self._settings.mappings)
        return self._decide(run, plan, classification, None)[0]

    # -- listing -----------------------------------------------------------

    def _standalone_runs(self, lookback_start: datetime | None) -> Iterator[Run]:
        project_id = self._settings.testrail.project_id
        if lookback_start is None:
            yield from self._client.get_runs(project_id)
            return
        boundary = created_after_cutoff(lookback_start)
        yield from self._client.get_runs(project_id, created_after=boundary)
        # Complement: open runs created before the cutoff; any overlap is deduplicated.
        yield from self._client.get_runs(project_id, created_before=boundary + 1, is_completed=False)

    def _plans(self, lookback_start: datetime | None) -> Iterator[Plan]:
        project_id = self._settings.testrail.project_id
        if lookback_start is None:
            listings: Iterable[Iterable[Plan]] = (self._client.get_plans(project_id),)
        else:
            boundary = created_after_cutoff(lookback_start)
            listings = (
                self._client.get_plans(project_id, created_after=boundary),
                self._client.get_plans(project_id, created_before=boundary + 1, is_completed=False),
            )
        seen: set[int] = set()
        for listing in listings:
            for plan in listing:
                if plan.id not in seen:
                    seen.add(plan.id)
                    yield plan

    # -- decisions ---------------------------------------------------------

    def _add(
        self, found: dict[int, DiscoveredRun], run: Run, plan: Plan | None, lookback_start: datetime | None
    ) -> None:
        existing = found.get(run.id)
        # The same run can be listed twice; prefer the copy that carries plan context.
        if existing is not None and (existing.plan_name is not None or plan is None):
            return
        plan_name = plan.name if plan is not None else None
        classification = classify_run(run, plan_name, self._settings.mappings)
        decision, reason = self._decide(run, plan, classification, lookback_start)
        found[run.id] = DiscoveredRun(run, classification, decision, reason, plan_name)

    def _decide(
        self, run: Run, plan: Plan | None, classification: RunClassification, lookback_start: datetime | None
    ) -> tuple[RunDecision, str | None]:
        if (
            plan is not None
            and lookback_start is not None
            and run.is_completed
            and run.created_on is not None
            and run.created_on < lookback_start
        ):
            return RunDecision.OUTSIDE_LOOKBACK, "completed run created before the lookback start"
        suite_id = self._settings.testrail.suite_id
        if suite_id is not None and run.suite_id is not None and run.suite_id != suite_id:
            return RunDecision.EXCLUDED_BY_FILTER, f"suite {run.suite_id} is not the configured suite"
        naming = self._settings.naming
        if plan is not None and naming.plan_name_patterns and not _matches_any(naming.plan_name_patterns, plan.name):
            return RunDecision.EXCLUDED_BY_NAME, "plan name does not match naming.plan_name_patterns"
        if naming.run_name_patterns and not _matches_any(naming.run_name_patterns, run.name):
            return RunDecision.EXCLUDED_BY_NAME, "run name does not match naming.run_name_patterns"
        return _apply_filters(classification, self._settings.filters)


def _parent_note(plan_id: int, cause: str) -> str:
    return (
        f"parent plan {plan_id} of a selected run could not be fetched ({cause}); "
        "plan-name mappings and patterns were not applied to that run"
    )


def _newest_first(plan: Plan) -> tuple[bool, float, int]:
    created = plan.created_on
    return (created is not None, created.timestamp() if created is not None else 0.0, plan.id)


def created_after_cutoff(moment: datetime) -> int:
    """The ``created_after`` value that includes everything at or after ``moment``.

    ``created_after`` may be compared strictly, so the largest whole second
    before ``moment`` is used; if it is inclusive, one extra second is returned.
    """
    return math.ceil(moment.timestamp()) - 1


def _matches_any(patterns: Iterable[re.Pattern[str]], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _apply_filters(classification: RunClassification, filters: FilterSettings) -> tuple[RunDecision, str | None]:
    checks: tuple[tuple[str, DimensionValue, tuple[str, ...]], ...] = (
        ("application", classification.application, filters.applications),
        ("platform", classification.platform, filters.platforms),
        ("execution type", classification.execution_type, filters.execution_types),
    )
    unclassified = []
    for label, value, allowed in checks:
        if not allowed:
            continue
        if value.name is not None:
            if value.name not in allowed:
                return RunDecision.EXCLUDED_BY_FILTER, f"{label} '{value.name}' is not in the configured filter"
        else:
            unclassified.append(f"{label} is {value.state}")
    if unclassified:
        return RunDecision.UNCLASSIFIED, "; ".join(unclassified)
    return RunDecision.INCLUDED, None
