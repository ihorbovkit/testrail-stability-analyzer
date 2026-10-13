"""Resolve the final outcome of each test within one run.

A test can have several result records in one run (automated retries, manual
triage, comment-only updates). They are all kept in chronological order; the
final outcome is the latest record that carries a status.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..models import Case, Result, StatusResolver, Test
from .models import DiscoveredRun, ResolvedResult, ResultConflict, RunOutcome


def chronological_key(result: Result) -> tuple[bool, float, int]:
    """Order by timestamp, then result ID. Records without a timestamp sort first, by ID,
    so they never override a timestamped record."""
    created = result.created_on
    return (created is not None, created.timestamp() if created is not None else 0.0, result.id)


def sort_chronologically(results: Iterable[Result]) -> list[Result]:
    return sorted(results, key=chronological_key)


def select_final(results: Iterable[ResolvedResult]) -> ResolvedResult | None:
    """The last status-bearing record of a chronologically ordered sequence."""
    final = None
    for resolved in results:
        if not resolved.is_comment_only:
            final = resolved
    return final


def deduplicate(results: Iterable[Result], run_id: int) -> tuple[list[Result], list[ResultConflict]]:
    """Drop exact duplicates (e.g. repeated across pages); report same-ID records that differ."""
    unique: dict[int, Result] = {}
    conflicts: list[ResultConflict] = []
    for result in results:
        existing = unique.setdefault(result.id, result)
        if existing is not result and existing != result:
            conflicts.append(ResultConflict(run_id, result.id))
    return list(unique.values()), conflicts


def build_outcome(
    source: DiscoveredRun,
    test: Test,
    results: Iterable[Result],
    resolver: StatusResolver,
    *,
    version_field: str | None = None,
    evidence_truncated: bool = False,
    results_complete: bool = True,
) -> RunOutcome:
    ordered = tuple(
        ResolvedResult(
            result,
            None if result.status_id is None else resolver.resolve(result.status_id),
            result_version(result, version_field),
        )
        for result in sort_chronologically(results)
    )
    final = select_final(ordered)
    return RunOutcome(
        source=source,
        test=test,
        results=ordered,
        final=final,
        current_status=None if test.status_id is None else resolver.resolve(test.status_id),
        application_version=_application_version(final, ordered),
        evidence_truncated=evidence_truncated,
        results_complete=results_complete,
    )


def field_value(record: Case | Test | Result, system_name: str, default: Any = None) -> Any:
    """A field by TestRail system name: custom fields, then other returned fields, then typed attributes."""
    for values in (record.custom_fields, record.extra):
        if system_name in values:
            return values[system_name]
    if system_name in type(record).__dataclass_fields__ and system_name not in ("custom_fields", "extra", "comment"):
        return getattr(record, system_name)
    return default


def _application_version(final: ResolvedResult | None, ordered: tuple[ResolvedResult, ...]) -> str | None:
    """The version on the final result, else the most recent record that has one."""
    candidates = ([final] if final is not None else []) + list(reversed(ordered))
    return next((resolved.application_version for resolved in candidates if resolved.application_version), None)


def result_version(result: Result, version_field: str | None = None) -> str | None:
    """The application version recorded on this result itself (``version_field``, else ``version``)."""
    value = field_value(result, version_field or "version")
    return None if value in (None, "") else str(value)
