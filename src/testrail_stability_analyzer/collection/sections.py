"""Feature / Test Group per case, reconstructed from the TestRail section hierarchy.

A case's group is the path of section names from the top-level section down to
the case's own section, following ``parent_id`` links (for example
``("Regression", "Search")``). Nothing about section names is assumed, so the
grouping works for any project.

Enrichment runs after collection and only adds :attr:`CollectionReport.case_groups`
and limitations: histories, outcomes, and the collection's partial state are
never changed, also when metadata cannot be fetched. Metadata is fetched once
per distinct suite (``get_cases`` and ``get_sections``, both paginated), plus
one ``get_suites`` request when suite names are included in labels.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ..api import TestRailClient, TestRailError, TestRailHTTPError, TestRailRequestBudgetExceeded
from ..config.settings import FeatureGroupSettings, Settings
from ..models import Section
from .case_metadata import CaseMetadataCache
from .models import CaseGroup, CollectionReport, FeatureGroupState


@dataclass(frozen=True, slots=True)
class SectionPath:
    """The resolved path of one section; ``names`` is empty unless ``state`` is ``resolved``."""

    state: FeatureGroupState
    names: tuple[str, ...] = ()


def resolve_section_path(section_id: int | None, sections: Mapping[int, Section]) -> SectionPath:
    """Follow ``parent_id`` links from ``section_id`` to the top level."""
    if section_id is None or section_id not in sections:
        return SectionPath(FeatureGroupState.SECTION_NOT_FOUND)
    names: list[str] = []
    seen: set[int] = set()
    current: int | None = section_id
    while current is not None:
        if current in seen:
            return SectionPath(FeatureGroupState.CYCLIC_HIERARCHY)
        section = sections.get(current)
        if section is None:
            return SectionPath(FeatureGroupState.BROKEN_HIERARCHY)
        seen.add(current)
        names.append(section.name)
        current = section.parent_id
    return SectionPath(FeatureGroupState.RESOLVED, tuple(reversed(names)))


def feature_group_label(group: CaseGroup, settings: FeatureGroupSettings) -> str | None:
    """The display label of a resolved group (e.g. ``"Regression/Search"``); ``None`` when unresolved.

    Section names are joined as they are: a name that contains the separator
    is not escaped, so ``path`` remains the authoritative structure.
    """
    if not group.is_resolved:
        return None
    names = group.path[: settings.max_depth] if settings.max_depth else group.path
    if settings.include_suite_name and group.suite_name:
        names = (group.suite_name, *names)
    return settings.separator.join(names)


def enrich_feature_groups(
    report: CollectionReport, client: TestRailClient, settings: Settings, cache: CaseMetadataCache
) -> CollectionReport:
    """Return ``report`` with ``case_groups`` for every case that has a history."""
    suites: dict[int, int | None] = {}
    for outcome in report.outcomes:
        suites.setdefault(outcome.case_id, outcome.source.run.suite_id or settings.testrail.suite_id)
    case_ids = sorted({history.key.case_id for history in report.histories})
    by_suite: dict[int | None, list[int]] = {}
    for case_id in case_ids:
        by_suite.setdefault(suites.get(case_id, settings.testrail.suite_id), []).append(case_id)

    session = _Enrichment(client, cache)
    suite_names = session.suite_names(settings) if settings.report.feature_group.include_suite_name else {}
    groups: dict[int, CaseGroup] = {}
    for suite_id in sorted(by_suite, key=lambda suite: suite or 0):
        groups.update(session.groups(suite_id, by_suite[suite_id], suite_names.get(suite_id)))

    limitations = list(report.limitations)
    limitations.extend(session.notes)
    unresolved = Counter(group.state for group in groups.values() if not group.is_resolved)
    if unresolved:
        counts = ", ".join(f"{state}={unresolved[state]}" for state in sorted(unresolved))
        limitations.append(f"Feature / Test Group is unknown for {unresolved.total()} case(s): {counts}")
    return dataclasses.replace(
        report,
        case_groups=MappingProxyType(groups),
        requests_made=client.requests_made,
        limitations=tuple(dict.fromkeys(limitations)),
    )


class _Enrichment:
    def __init__(self, client: TestRailClient, cache: CaseMetadataCache) -> None:
        self.client = client
        self.cache = cache
        self.notes: list[str] = []

    def _fetch[T](
        self, fetch: Callable[[], T], what: str, consequence: str
    ) -> tuple[T | None, FeatureGroupState | None]:
        """(value, None) on success; (None, failure state) when the request failed. Never raises API errors."""
        try:
            return fetch(), None
        except TestRailRequestBudgetExceeded:
            self.notes.append(f"{what} not fetched (request budget exhausted); {consequence}")
            return None, FeatureGroupState.NOT_FETCHED
        except TestRailError as exc:
            # Response text can contain untrusted server content; report only the error type and status.
            status = f" HTTP {exc.status}" if isinstance(exc, TestRailHTTPError) and isinstance(exc.status, int) else ""
            self.notes.append(f"{what} could not be fetched ({type(exc).__name__}{status}); {consequence}")
            return None, FeatureGroupState.INACCESSIBLE

    def suite_names(self, settings: Settings) -> dict[int | None, str]:
        suites, _ = self._fetch(
            lambda: self.client.get_suites(settings.testrail.project_id), "suite names", "labels omit suite names"
        )
        if suites is None:
            return {}
        names: dict[int | None, str] = {suite.id: suite.name for suite in suites}
        if len(suites) == 1:
            names[None] = suites[0].name  # single-suite project collected without a suite ID
        return names

    def groups(self, suite_id: int | None, case_ids: list[int], suite_name: str | None) -> dict[int, CaseGroup]:
        label = "case metadata" if suite_id is None else f"case metadata of suite {suite_id}"
        unknown = "Feature / Test Group is unknown for its cases"
        cases, failure = self._fetch(lambda: self.cache.cases(suite_id), label, unknown)
        sections: Mapping[int, Section] | None = None
        if cases is not None:
            label = "sections" if suite_id is None else f"sections of suite {suite_id}"
            sections, failure = self._fetch(lambda: self.cache.sections(suite_id), label, unknown)
        groups = {}
        for case_id in case_ids:
            if cases is None or sections is None:
                assert failure is not None
                groups[case_id] = CaseGroup(case_id, failure, suite_id=suite_id)
                continue
            case = cases.get(case_id)
            if case is None:
                groups[case_id] = CaseGroup(case_id, FeatureGroupState.CASE_NOT_FOUND, suite_id=suite_id)
                continue
            path = resolve_section_path(case.section_id, sections)
            groups[case_id] = CaseGroup(
                case_id,
                path.state,
                suite_id=case.suite_id or suite_id,
                section_id=case.section_id,
                path=path.names,
                suite_name=suite_name,
            )
        return groups
