"""Synthetic outcomes, histories, and collection reports for analysis tests. No API access."""

import dataclasses
from datetime import timedelta

from testrail_stability_analyzer.collection import (
    Applicability,
    CollectionReport,
    DimensionState,
    DimensionValue,
    DiscoveredRun,
    DiscoveryResult,
    RunClassification,
    RunDecision,
    build_histories,
    build_outcome,
)
from testrail_stability_analyzer.models import Result, Run, Test

from . import fake_testrail as fx
from .fake_testrail import NOW

WINDOW_DAYS = 60
WINDOW_START = NOW - timedelta(days=WINDOW_DAYS)
APP_A = DimensionValue(DimensionState.RESOLVED, "App A")
APP_B = DimensionValue(DimensionState.RESOLVED, "App B")
PLAT_X = DimensionValue(DimensionState.RESOLVED, "Platform X")
PLAT_Y = DimensionValue(DimensionState.RESOLVED, "Platform Y")
UNRESOLVED = DimensionValue(DimensionState.UNRESOLVED)
RESOLVER = fx.resolver()


def rec(result_id, status_id, days, defects=None, version=None):
    """A result record spec; ``days`` before NOW, or ``None`` for an undated record."""
    return (result_id, status_id, days, defects, version)


def outcome(
    run_id, case_id, *records, app=APP_A, platform=PLAT_X, plan_id=None, complete=True, test_status=None, title=None
):
    """``title`` defaults to "Case <case_id>"; pass ``""`` for a test without a title."""
    test_id = run_id * 1000 + case_id
    source = DiscoveredRun(
        Run(id=run_id, name=f"Run {run_id}", plan_id=plan_id), RunClassification(app, platform), RunDecision.INCLUDED
    )
    results = [
        Result(
            id=result_id,
            test_id=test_id,
            status_id=status_id,
            created_on=None if days is None else NOW - timedelta(days=days),
            defects=defects,
            version=version,
            run_id=run_id,
        )
        for result_id, status_id, days, defects, version in records
    ]
    title = f"Case {case_id}" if title is None else title or None
    test = Test(id=test_id, case_id=case_id, run_id=run_id, status_id=test_status, title=title)
    return build_outcome(source, test, results, RESOLVER, results_complete=complete)


def runs(statuses, *, case_id=100, first_run=500, app=APP_A, platform=PLAT_X):
    """One single-record outcome per status, newest first: statuses[0] is 1 day ago."""
    return [
        outcome(first_run + i, case_id, rec(first_run * 10 + i, status, i + 1), app=app, platform=platform)
        for i, status in enumerate(statuses)
    ]


def histories(outcomes, *, max_runs=10, platforms=(PLAT_X,), applicability=Applicability.NOT_CONFIGURED, **kwargs):
    groups = {(o.source.classification.application, o.source.classification.platform) for o in outcomes}
    groups |= {(APP_A, platform) for platform in platforms}
    return build_histories(
        outcomes,
        window_start=WINDOW_START,
        max_runs=max_runs,
        groups_with_runs=kwargs.pop("groups_with_runs", groups),
        scanned_groups=kwargs.pop("scanned_groups", groups),
        platforms=platforms,
        gaps=kwargs.pop("gaps", {}),
        applicability=lambda case_id, application: applicability,
        **kwargs,
    )


def history(outcomes, *, case_id=100, app=APP_A, platform=PLAT_X, **kwargs):
    matches = [
        h
        for h in histories(outcomes, **kwargs)
        if h.key.case_id == case_id and h.key.application == app and h.key.platform == platform
    ]
    assert len(matches) == 1, matches
    return matches[0]


def report(outcomes, *, hist=None, targeted=False, orphans=(), **fields):
    hist = histories(outcomes) if hist is None else hist
    base = CollectionReport(
        collected_at=NOW,
        window_start=WINDOW_START,
        lookback_start=NOW - timedelta(days=365),
        discovery=DiscoveryResult(runs=(), complete=True, targeted=targeted),
        scans=(),
        outcomes=tuple(outcomes),
        histories=tuple(hist),
        orphan_results=tuple(orphans),
        conflicts=(),
        budget_exhausted=False,
        requests_made=0,
        limitations=(),
    )
    return dataclasses.replace(base, **fields)
