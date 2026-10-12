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
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Iterator
from datetime import datetime

from ..api import TestRailClient, TestRailRequestBudgetExceeded
from ..config.settings import FilterSettings, Settings
from ..models import Plan, Run
from .classification import classify_run
from .models import DimensionValue, DiscoveredRun, DiscoveryResult, RunClassification, RunDecision


class RunDiscovery:
    def __init__(self, client: TestRailClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    def discover(self, lookback_start: datetime | None) -> DiscoveryResult:
        found: dict[int, DiscoveredRun] = {}
        limitations: list[str] = []
        complete = True
        try:
            for run in self._standalone_runs(lookback_start):
                self._add(found, run, None, lookback_start)
            for plan in self._plans(lookback_start):
                detailed = self._client.get_plan(plan.id)
                for run in detailed.runs:
                    self._add(found, run, detailed, lookback_start)
        except TestRailRequestBudgetExceeded:
            complete = False
            limitations.append("request budget exhausted during run discovery; some runs were not discovered")
        if lookback_start is not None:
            limitations.append(
                "completed runs and plans created before the lookback start were not scanned; "
                "any results they received later are not included"
            )
        runs = tuple(sorted(found.values(), key=lambda discovered: discovered.run_id))
        return DiscoveryResult(runs, complete, tuple(limitations))

    # -- listing -----------------------------------------------------------

    def _standalone_runs(self, lookback_start: datetime | None) -> Iterator[Run]:
        project_id, suite_id = self._settings.testrail.project_id, self._settings.testrail.suite_id
        if lookback_start is None:
            yield from self._client.get_runs(project_id, suite_id=suite_id)
            return
        boundary = math.floor(lookback_start.timestamp())
        yield from self._client.get_runs(project_id, suite_id=suite_id, created_after=boundary)
        # +1 s: overlap is deduplicated, a gap at the boundary second would lose runs.
        yield from self._client.get_runs(project_id, suite_id=suite_id, created_before=boundary + 1, is_completed=False)

    def _plans(self, lookback_start: datetime | None) -> Iterator[Plan]:
        project_id = self._settings.testrail.project_id
        if lookback_start is None:
            listings: Iterable[Iterable[Plan]] = (self._client.get_plans(project_id),)
        else:
            boundary = math.floor(lookback_start.timestamp())
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
