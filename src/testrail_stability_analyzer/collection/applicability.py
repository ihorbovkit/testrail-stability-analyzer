"""Resolve a case's applicability value against the configured application mappings.

Values are compared as text, so integer option IDs and their string form
match. A case is excluded from an application only when its value was read,
is set, and every value is mapped to some configured application: an empty,
unread, or unmapped value never establishes an exclusion.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..config.settings import DimensionMapping
from .models import Applicability, CaseApplicability


def normalize_values(raw: Any) -> tuple[str, ...]:
    """A raw case field value (scalar, list, or ``None``) as non-empty strings, in order."""
    if raw is None:
        return ()
    items = raw if isinstance(raw, list | tuple) else [raw]
    return tuple(text for item in items if item is not None and (text := str(item).strip()))


def _allowed(mapping: DimensionMapping) -> frozenset[str]:
    return frozenset(str(value) for value in mapping.applicability_values)


def matched_applications(case: CaseApplicability, applications: Sequence[DimensionMapping]) -> tuple[str, ...]:
    """Names of the applications whose ``applicability_values`` match the case, in configuration order."""
    if not case.is_set:
        return ()
    values = set(case.values)
    return tuple(mapping.name for mapping in applications if values & _allowed(mapping))


def unmapped_values(case: CaseApplicability, applications: Sequence[DimensionMapping]) -> tuple[str, ...]:
    """Values of the case that no configured application lists."""
    if not case.is_set:
        return ()
    known = frozenset().union(*(_allowed(mapping) for mapping in applications))
    return tuple(value for value in case.values if value not in known)


def resolve_applicability(
    case: CaseApplicability | None, application: str | None, applications: Sequence[DimensionMapping]
) -> Applicability:
    """Applicability of a case to one application (by mapped name)."""
    mapping = next((m for m in applications if application is not None and m.name == application), None)
    if case is None or not case.is_set or mapping is None or not mapping.applicability_values:
        return Applicability.UNKNOWN
    if set(case.values) & _allowed(mapping):
        return Applicability.APPLICABLE
    if unmapped_values(case, applications):
        return Applicability.UNKNOWN
    return Applicability.NOT_APPLICABLE
