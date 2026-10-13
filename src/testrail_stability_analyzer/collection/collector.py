"""Collect historical executions: discover runs, fetch results and tests, build histories.

Collection order (so that an exhausted request budget loses the least useful data):

1. Discover runs (see :mod:`.discovery`): project-wide, or only the plans and
   runs selected in ``[collection]`` (targeted collection).
2. Fetch results (``created_after`` = lookback start) and tests for runs that
   can be active in the analysis window: open runs and runs created in it.
3. Fetch results for all other included runs. Closed runs are not assumed to
   be immutable, so each one is checked.
4. Fetch tests for every remaining run with results in the lookback, newest
   actual result first. Results carry no case ID, so this is the only way to
   find every case executed within the lookback.

An optional result-record cap (``collection.max_results``) is enforced while
results are retrieved: each request asks for at most the remaining number of
records, and retrieval stops once the cap is reached. Every record counts,
including retries and comment-only records. Outcomes of a capped run are
marked incomplete and their histories ``partial``; runs after the cap are not
fetched.

Comments are kept on the result models for later analysis but are never
logged; use :meth:`ResolvedResult.failure_summary` for a sanitized summary.
"""

from __future__ import annotations

import itertools
import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from ..api import TestRailClient, TestRailRequestBudgetExceeded
from ..api.client import MAX_PAGE_LIMIT
from ..config.settings import DimensionMapping, Settings
from ..models import Plan, Result, StatusResolver, Test
from .case_metadata import CaseMetadataCache
from .discovery import RunDiscovery, created_after_cutoff
from .history import Group, build_histories, group_of
from .models import (
    Applicability,
    CollectionReport,
    DimensionValue,
    DiscoveredRun,
    DiscoveryResult,
    ResolvedResult,
    ResultConflict,
    RunOutcome,
    RunScan,
)
from .outcomes import build_outcome, deduplicate, field_value

_MISSING: Any = object()


class HistoricalCollector:
    def __init__(
        self,
        client: TestRailClient,
        settings: Settings,
        resolver: StatusResolver,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        plans: Mapping[int, Plan] | None = None,
        case_cache: CaseMetadataCache | None = None,
    ) -> None:
        """``plans``: already fetched selected plans (with entries), reused by targeted collection.

        ``case_cache``: bulk case metadata shared with later steps (e.g. Feature / Test Group enrichment).
        """
        self._client = client
        self._settings = settings
        self._resolver = resolver
        self._clock = clock
        self._plans = plans
        self._case_cache = case_cache or CaseMetadataCache(client, settings.testrail.project_id)

    def collect(self) -> CollectionReport:
        now = self._clock()
        history = self._settings.history
        window_start = now - timedelta(days=history.window_days)
        lookback_days = history.lookback_limit_days or None  # 0 means no limit, as in configuration
        lookback_start = None if lookback_days is None else now - timedelta(days=lookback_days)
        discoverer = RunDiscovery(self._client, self._settings)
        selection = self._settings.collection
        if selection.targeted:
            discovery = discoverer.discover_selected(selection.plan_ids, selection.run_ids, plans=self._plans)
        else:
            discovery = discoverer.discover(lookback_start)
        session = _Session(
            self._client, self._settings, self._resolver, window_start, lookback_start, self._case_cache
        )
        return session.run(now, discovery)


class _Session:
    """Mutable state of one collection."""

    def __init__(
        self,
        client: TestRailClient,
        settings: Settings,
        resolver: StatusResolver,
        window_start: datetime,
        lookback_start: datetime | None,
        case_cache: CaseMetadataCache,
    ) -> None:
        self.client = client
        self.case_cache = case_cache
        self.settings = settings
        self.resolver = resolver
        self.window_start = window_start
        self.lookback_start = lookback_start
        self.exhausted = False
        self.max_results = settings.collection.max_results
        self.records_fetched = 0
        self.capped_runs: set[int] = set()
        self.skipped_runs: set[int] = set()  # results not fetched because the cap was reached
        self.results: dict[int, list[Result]] = {}
        self.tests: dict[int, list[Test]] = {}
        self.outcomes: list[RunOutcome] = []
        self.orphans: list[ResolvedResult] = []
        self.conflicts: list[ResultConflict] = []
        # Cases whose applicability could not be read because a get_cases request was not made.
        self.applicability_unchecked: set[int] = set()
        custom_fields = settings.mappings.custom_fields
        self.applicability_field = custom_fields.get("applicability")
        self.version_field = custom_fields.get("application_version")

    # -- orchestration -----------------------------------------------------

    def run(self, now: datetime, discovery: DiscoveryResult) -> CollectionReport:
        self.exhausted = discovery.budget_exhausted
        included = sorted(discovery.included, key=_priority)
        early = [run for run in included if self._active_in_window(run)]
        for run in early:
            self._fetch_results(run)
            self._fetch_tests(run)
        for run in included:
            if run.run_id not in self.results:
                self._fetch_results(run)
        with_results = [run for run in included if run.run_id not in self.tests and self.results.get(run.run_id)]
        for run in sorted(with_results, key=self._newest_result_order, reverse=True):
            self._fetch_tests(run)

        # Before coverage is assessed: the bulk case fallback can exhaust the budget.
        applicability = self._applicability_lookup()
        scans = tuple(self._scan(run) for run in included)
        groups = {run.run_id: group_of(run) for run in included}
        gaps: dict[Group, list[RunScan]] = defaultdict(list)
        for scan in scans:
            group = groups[scan.run_id]
            if group is not None and not scan.complete:
                gaps[group].append(scan)
        global_notes = []
        if discovery.invalid_selections:
            global_notes.append(f"{len(discovery.invalid_selections)} selected plan/run ID(s) could not be collected")
        if discovery.budget_exhausted or (not discovery.complete and not discovery.invalid_selections):
            global_notes.append("run discovery was incomplete")
        histories = build_histories(
            self.outcomes,
            window_start=self.window_start,
            max_runs=self.settings.history.max_runs,
            groups_with_runs={group for group in groups.values() if group is not None},
            scanned_groups={groups[run_id] for run_id in self.tests if groups[run_id] is not None},
            platforms={group[1] for group in groups.values() if group is not None},
            gaps=gaps,
            applicability=applicability,
            global_notes=global_notes,
            case_notes=dict.fromkeys(
                self.applicability_unchecked, "applicability metadata could not be fetched (request budget)"
            ),
            lookback_limited=self.lookback_start is not None,
            discovery_complete=discovery.complete,
            selection_limited=discovery.targeted,
        )
        return CollectionReport(
            collected_at=now,
            window_start=self.window_start,
            lookback_start=self.lookback_start,
            discovery=discovery,
            scans=scans,
            outcomes=tuple(self.outcomes),
            histories=histories,
            orphan_results=tuple(self.orphans),
            conflicts=tuple(self.conflicts),
            budget_exhausted=self.exhausted,
            requests_made=self.client.requests_made,
            limitations=self._limitations(discovery, scans, groups),
            result_records_fetched=self.records_fetched,
            max_results=self.max_results,
            results_capped=bool(self.capped_runs),
        )

    # -- API calls ---------------------------------------------------------

    def _call[T](self, fetch: Callable[[], T]) -> T | None:
        if self.exhausted:
            return None
        try:
            return fetch()
        except TestRailRequestBudgetExceeded:
            self.exhausted = True
            return None

    def _fetch_results(self, run: DiscoveredRun) -> None:
        created_after = None if self.lookback_start is None else created_after_cutoff(self.lookback_start)
        remaining = None if self.max_results is None else self.max_results - self.records_fetched
        if remaining is not None and remaining <= 0:
            self.skipped_runs.add(run.run_id)  # not fetched, so coverage of this run stays partial
            return

        def fetch() -> list[Result]:
            if remaining is None:
                return list(self.client.get_results_for_run(run.run_id, created_after=created_after))
            page = min(remaining, MAX_PAGE_LIMIT)
            stream = self.client.get_results_for_run(run.run_id, created_after=created_after, limit=page)
            # islice stops before requesting another page once the cap is reached.
            return list(itertools.islice(stream, remaining))

        fetched = self._call(fetch)
        if fetched is None:
            return
        self.records_fetched += len(fetched)
        if remaining is not None and len(fetched) >= remaining:
            # Without retrieving one more record it is unknown whether the stream ended
            # exactly here, so the run is conservatively treated as capped.
            self.capped_runs.add(run.run_id)
        unique, conflicts = deduplicate(fetched, run.run_id)
        self.results[run.run_id] = unique
        self.conflicts.extend(conflicts)

    def _fetch_tests(self, run: DiscoveredRun) -> None:
        if run.run_id not in self.results or run.run_id in self.tests:
            return
        tests = self._call(lambda: list(self.client.get_tests(run.run_id)))
        if tests is None:
            return
        self.tests[run.run_id] = tests
        by_test: dict[int, list[Result]] = defaultdict(list)
        for result in self.results[run.run_id]:
            by_test[result.test_id].append(result)
        truncated = self.lookback_start is not None and (
            run.run.created_on is None or run.run.created_on < self.lookback_start
        )
        outcomes = [
            build_outcome(
                run,
                test,
                by_test.pop(test.id, ()),
                self.resolver,
                version_field=self.version_field,
                evidence_truncated=truncated,
                results_complete=run.run_id not in self.capped_runs,
            )
            for test in tests
        ]
        for results in by_test.values():
            self.orphans.extend(
                ResolvedResult(r, None if r.status_id is None else self.resolver.resolve(r.status_id)) for r in results
            )
        self.outcomes.extend(outcomes)

    # -- applicability -----------------------------------------------------

    def _applicability_lookup(self) -> Callable[[int, DimensionValue], Applicability]:
        field = self.applicability_field
        if field is None:
            return lambda case_id, application: Applicability.NOT_CONFIGURED
        values = self._applicability_values(field)
        mappings: dict[str, DimensionMapping] = {app.name: app for app in self.settings.mappings.applications}

        def lookup(case_id: int, application: DimensionValue) -> Applicability:
            mapping = mappings.get(application.name) if application.name is not None else None
            raw = values.get(case_id)
            if mapping is None or not mapping.applicability_values or raw in (None, "", []):
                return Applicability.UNKNOWN
            allowed = {str(value) for value in mapping.applicability_values}
            raw_values = raw if isinstance(raw, list) else [raw]
            if any(str(value) in allowed for value in raw_values):
                return Applicability.APPLICABLE
            return Applicability.NOT_APPLICABLE

        return lookup

    def _applicability_values(self, field: str) -> dict[int, Any]:
        """Case field values from ``get_tests``; ``get_cases`` (bulk, per suite) only for cases lacking it."""
        values: dict[int, Any] = {}
        suites_by_case: dict[int, int | None] = {}
        for outcome in self.outcomes:
            value = field_value(outcome.test, field, _MISSING)
            if value is not _MISSING:
                values.setdefault(outcome.case_id, value)
            else:
                suite_id = outcome.source.run.suite_id or self.settings.testrail.suite_id
                suites_by_case.setdefault(outcome.case_id, suite_id)
        missing = {case: suite for case, suite in suites_by_case.items() if case not in values}
        for suite_id in sorted(set(missing.values()), key=lambda suite: suite or 0):
            cases = self._call(lambda: self.case_cache.cases(suite_id))
            if cases is None:
                self.applicability_unchecked.update(case for case, suite in missing.items() if suite == suite_id)
                continue
            for case in cases.values():
                if case.id in missing:
                    value = field_value(case, field, _MISSING)
                    if value is not _MISSING:
                        values[case.id] = value
        return values

    # -- bookkeeping -------------------------------------------------------

    def _active_in_window(self, run: DiscoveredRun) -> bool:
        created = run.run.created_on
        # An unknown completion flag (None) is treated as possibly open.
        return run.run.is_completed is not True or created is None or created >= self.window_start

    def _newest(self, run_id: int) -> tuple[float | None, bool]:
        results = self.results.get(run_id, ())
        stamps = [r.created_on.timestamp() for r in results if r.created_on is not None]
        return (max(stamps) if stamps else None), len(stamps) < len(results)

    def _newest_result_order(self, run: DiscoveredRun) -> tuple[bool, float, int]:
        """Sort key (reverse): newest known result first; wholly untimed runs last.

        Untimed results keep coverage uncertain (see ``RunScan.has_untimed_results``)
        but do not move a run ahead of runs with newer dated results.
        """
        newest, _ = self._newest(run.run_id)
        return (newest is not None, newest if newest is not None else -math.inf, run.run_id)

    def _scan(self, run: DiscoveredRun) -> RunScan:
        results = self.results.get(run.run_id)
        newest, untimed = self._newest(run.run_id)
        return RunScan(
            run_id=run.run_id,
            results_fetched=results is not None,
            tests_fetched=run.run_id in self.tests,
            result_count=len(results or ()),
            newest_result_on=None if newest is None else datetime.fromtimestamp(newest, UTC),
            has_untimed_results=untimed,
            active_in_window=self._active_in_window(run),
            results_capped=run.run_id in self.capped_runs,
            skipped_by_cap=run.run_id in self.skipped_runs and results is None,
        )

    def _limitations(
        self, discovery: DiscoveryResult, scans: Iterable[RunScan], groups: dict[int, Group | None]
    ) -> tuple[str, ...]:
        notes = list(discovery.limitations)
        if self.exhausted:
            notes.append("request budget exhausted during collection; coverage is partial")
        if self.capped_runs:
            notes.append(
                f"result-record cap of {self.max_results} reached: older records of {len(self.capped_runs)} run(s) "
                "may be missing and later runs were not fetched; their outcomes are not verified finals"
            )
        unfetched = sum(1 for scan in scans if not scan.results_fetched)
        if unfetched:
            notes.append(f"results of {unfetched} included run(s) were not fetched")
        unidentified = sum(1 for group in groups.values() if group is None)
        if unidentified:
            notes.append(f"{unidentified} run(s) have an unidentified application or platform and are grouped per run")
        if discovery.unclassified:
            notes.append(
                f"{len(discovery.unclassified)} run(s) could not be classified for the configured filters "
                "and were not collected"
            )
        if self.conflicts:
            notes.append(f"{len(self.conflicts)} conflicting duplicate result record(s) found; the first was kept")
        return tuple(notes)


def _priority(run: DiscoveredRun) -> tuple[bool, float, int]:
    """Open runs first, then newest created first; deterministic by run ID."""
    created = run.run.created_on
    return (run.run.is_completed is True, -(created.timestamp() if created else math.inf), -run.run_id)
