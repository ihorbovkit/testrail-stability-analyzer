"""Identify the application, platform, and execution type of a run from configured patterns."""

from __future__ import annotations

from collections.abc import Mapping

from ..config.settings import DimensionMapping, MappingSettings, MatchSource
from ..models import Run
from .models import NOT_CONFIGURED, DimensionState, DimensionValue, RunClassification


def classify_run(run: Run, plan_name: str | None, mappings: MappingSettings) -> RunClassification:
    """Match configured patterns against the plan name, run name, and configuration names.

    Exactly one matching mapping resolves the dimension. No match is
    ``unresolved`` and several matches are ``ambiguous``; neither is guessed.
    """
    sources = {MatchSource.PLAN_NAME: plan_name, MatchSource.RUN_NAME: run.name, MatchSource.CONFIGURATION: run.config}
    return RunClassification(
        application=match_dimension(mappings.applications, sources),
        platform=match_dimension(mappings.platforms, sources),
        execution_type=match_dimension(mappings.execution_types, sources),
    )


def match_dimension(
    mappings: tuple[DimensionMapping, ...], sources: Mapping[MatchSource, str | None]
) -> DimensionValue:
    if not mappings:
        return NOT_CONFIGURED
    names = tuple(
        mapping.name
        for mapping in mappings
        if any(
            pattern.search(text)
            for source in mapping.sources
            if (text := sources.get(source))
            for pattern in mapping.patterns
        )
    )
    if not names:
        return DimensionValue(DimensionState.UNRESOLVED)
    if len(names) > 1:
        return DimensionValue(DimensionState.AMBIGUOUS, candidates=names)
    return DimensionValue(DimensionState.RESOLVED, name=names[0])
