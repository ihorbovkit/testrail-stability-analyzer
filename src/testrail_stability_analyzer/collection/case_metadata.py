"""Bulk case metadata, fetched at most once per suite and shared within one execution.

Both the applicability fallback of the collector and Feature / Test Group
enrichment read cases from here, so ``get_cases`` is never requested twice for
the same suite. Cases are held in memory only and never written to reports
beyond the fields a report explicitly selects.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..api import TestRailClient
from ..models import Case, Section


class CaseMetadataCache:
    """Caches ``get_cases`` and ``get_sections`` per suite (``None`` for single-suite projects without a suite ID).

    Only successful fetches are cached; a failed request (for example an
    exhausted request budget) raises and may be repeated by a later caller.
    """

    def __init__(self, client: TestRailClient, project_id: int) -> None:
        self._client = client
        self._project_id = project_id
        self._cases: dict[int | None, Mapping[int, Case]] = {}
        self._sections: dict[int | None, Mapping[int, Section]] = {}

    def cases(self, suite_id: int | None) -> Mapping[int, Case]:
        """Cases of a suite by case ID."""
        if suite_id not in self._cases:
            cases = self._client.get_cases(self._project_id, suite_id=suite_id)
            self._cases[suite_id] = MappingProxyType({case.id: case for case in cases})
        return self._cases[suite_id]

    def sections(self, suite_id: int | None) -> Mapping[int, Section]:
        """Sections of a suite by section ID."""
        if suite_id not in self._sections:
            sections = self._client.get_sections(self._project_id, suite_id=suite_id)
            self._sections[suite_id] = MappingProxyType({section.id: section for section in sections})
        return self._sections[suite_id]
