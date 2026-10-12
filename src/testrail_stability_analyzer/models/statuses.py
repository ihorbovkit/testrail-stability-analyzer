"""Resolve instance-specific TestRail status IDs to labels and reporting categories."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from ..config.settings import StatusCategory
from .metadata import Status

# TestRail's built-in system statuses, identified by system name (not ID).
SYSTEM_STATUS_CATEGORIES: Final[Mapping[str, StatusCategory]] = MappingProxyType(
    {
        "passed": StatusCategory.PASSED,
        "failed": StatusCategory.FAILED,
        "blocked": StatusCategory.BLOCKED,
        "retest": StatusCategory.RETEST,
        "untested": StatusCategory.NOT_RUN,
    }
)


class ResolutionSource(StrEnum):
    """How a category was determined, for auditability."""

    CONFIGURED = "configured"  # mappings.statuses
    SYSTEM_DEFAULT = "system_default"  # built-in TestRail system status
    UNTESTED_FLAG = "untested_flag"  # status reports is_untested
    UNRESOLVED = "unresolved"  # no rule matched, or the ID is not defined on the instance


@dataclass(frozen=True, slots=True)
class ResolvedStatus:
    status_id: int
    category: StatusCategory
    source: ResolutionSource
    name: str | None = None  # system name; None when the ID is not defined on the instance
    label: str | None = None

    @property
    def is_known(self) -> bool:
        return self.name is not None


class StatusResolver:
    """Maps status IDs to categories using, in order of precedence:

    1. configured mappings (``mappings.statuses``, by system name or ID),
    2. TestRail's built-in system statuses (by system name),
    3. the ``is_untested`` flag (-> ``not_run``),
    4. otherwise ``unknown``. Custom statuses are never guessed from their names.
    """

    def __init__(
        self,
        statuses: Iterable[Status],
        mappings: Mapping[StatusCategory, Iterable[str | int]] | None = None,
    ) -> None:
        by_id: dict[int, Status] = {}
        for status in statuses:
            if status.id in by_id:
                raise ValueError(f"duplicate status ID {status.id}")
            by_id[status.id] = status
        by_name = {status.name: status for status in by_id.values()}

        configured: dict[int, StatusCategory] = {}
        unmatched: list[tuple[StatusCategory, str | int]] = []
        for category, identifiers in (mappings or {}).items():
            if category is StatusCategory.UNKNOWN:
                raise ValueError("statuses cannot be mapped to the 'unknown' category")
            for identifier in identifiers:
                status = by_id.get(identifier) if isinstance(identifier, int) else by_name.get(identifier)
                if status is None:
                    unmatched.append((category, identifier))
                    continue
                previous = configured.setdefault(status.id, category)
                if previous is not category:
                    raise ValueError(f"status {status.name!r} is mapped to both '{previous}' and '{category}'")

        self._resolved = MappingProxyType(
            {status_id: _resolve(status, configured.get(status_id)) for status_id, status in by_id.items()}
        )
        self._unmatched = tuple(unmatched)

    @property
    def resolved(self) -> Mapping[int, ResolvedStatus]:
        """Resolution of every status defined on the instance, keyed by ID."""
        return self._resolved

    @property
    def unmatched_mappings(self) -> tuple[tuple[StatusCategory, str | int], ...]:
        """Configured identifiers that do not exist on this instance (e.g. an optional custom status)."""
        return self._unmatched

    def resolve(self, status_id: int) -> ResolvedStatus:
        resolved = self._resolved.get(status_id)
        if resolved is None:
            return ResolvedStatus(status_id, StatusCategory.UNKNOWN, ResolutionSource.UNRESOLVED)
        return resolved


def _resolve(status: Status, configured: StatusCategory | None) -> ResolvedStatus:
    if configured is not None:
        category, source = configured, ResolutionSource.CONFIGURED
    elif status.is_system is not False and status.name in SYSTEM_STATUS_CATEGORIES:
        category, source = SYSTEM_STATUS_CATEGORIES[status.name], ResolutionSource.SYSTEM_DEFAULT
    elif status.is_untested:
        category, source = StatusCategory.NOT_RUN, ResolutionSource.UNTESTED_FLAG
    else:
        category, source = StatusCategory.UNKNOWN, ResolutionSource.UNRESOLVED
    return ResolvedStatus(status.id, category, source, status.name, status.label)
