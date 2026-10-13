"""Feature / Test Group per case, reconstructed from the TestRail section hierarchy.

A case's group is the path of section names from the top-level section down to
the case's own section, following ``parent_id`` links (for example
``("Regression", "Search")``). Nothing about section names is assumed, so the
grouping works for any project.

Enrichment runs after collection and only adds :attr:`CollectionReport.case_groups`
and limitations: histories, outcomes, and the collection's partial state are
never changed, also when metadata cannot be fetched. Metadata is fetched at
most once per suite (``get_cases`` and ``get_sections``, both paginated), plus
one ``get_suites`` request when suite names are included in labels.

The group is the case's *current* section. A case is looked up first in the
suites of its own runs, newest first, then in the other suites of the report,
so a case moved to another collected suite is still found; ``get_cases``
returns current cases only, so the suite where it is found is authoritative.
Sections are fetched only for suites where a case was found.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ..api import TestRailClient, TestRailError, TestRailHTTPError, TestRailRequestBudgetExceeded
from ..config.settings import FeatureGroupSettings, Settings
from ..models import Case, Section
from .case_metadata import CaseMetadataCache
from .history import newest_first
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
    default_suite = settings.testrail.suite_id
    own_suites: dict[int, list[int | None]] = {}
    for outcome in sorted(report.outcomes, key=newest_first, reverse=True):
        suites = own_suites.setdefault(outcome.case_id, [])
        suite_id = outcome.source.run.suite_id or default_suite
        if suite_id not in suites:
            suites.append(suite_id)
    report_suites = sorted({suite for suites in own_suites.values() for suite in suites}, key=lambda suite: suite or 0)

    session = _Enrichment(client, cache)
    suite_names = session.suite_names(settings) if settings.report.feature_group.include_suite_name else {}
    groups: dict[int, CaseGroup] = {}
    for case_id in sorted({history.key.case_id for history in report.histories}):
        own = own_suites.get(case_id) or [default_suite]
        candidates = [*own, *(suite for suite in report_suites if suite not in own)]
        groups[case_id] = session.group(case_id, candidates, suite_names)

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
        # Per suite: the fetched metadata, or the failure state (so a failed request is not repeated).
        self._cases: dict[int | None, Mapping[int, Case] | FeatureGroupState] = {}
        self._sections: dict[int | None, Mapping[int, Section] | FeatureGroupState] = {}

    def _fetch[T](self, fetch: Callable[[], T], what: str, consequence: str) -> T | FeatureGroupState:
        """The value on success; the failure state when the request failed. Never raises API errors."""
        try:
            return fetch()
        except TestRailRequestBudgetExceeded:
            self.notes.append(f"{what} not fetched (request budget exhausted); {consequence}")
            return FeatureGroupState.NOT_FETCHED
        except TestRailError as exc:
            # Response text can contain untrusted server content; report only the error type and status.
            status = f" HTTP {exc.status}" if isinstance(exc, TestRailHTTPError) and isinstance(exc.status, int) else ""
            self.notes.append(f"{what} could not be fetched ({type(exc).__name__}{status}); {consequence}")
            return FeatureGroupState.INACCESSIBLE

    def suite_names(self, settings: Settings) -> dict[int | None, str]:
        suites = self._fetch(
            lambda: self.client.get_suites(settings.testrail.project_id), "suite names", "labels omit suite names"
        )
        if isinstance(suites, FeatureGroupState):
            return {}
        names: dict[int | None, str] = {suite.id: suite.name for suite in suites}
        if len(suites) == 1:
            names[None] = suites[0].name  # single-suite project collected without a suite ID
        return names

    def cases(self, suite_id: int | None) -> Mapping[int, Case] | FeatureGroupState:
        if suite_id not in self._cases:
            label = "case metadata" if suite_id is None else f"case metadata of suite {suite_id}"
            unknown = "Feature / Test Group is unknown for its cases"
            self._cases[suite_id] = self._fetch(lambda: self.cache.cases(suite_id), label, unknown)
        return self._cases[suite_id]

    def sections(self, suite_id: int | None) -> Mapping[int, Section] | FeatureGroupState:
        if suite_id not in self._sections:
            label = "sections" if suite_id is None else f"sections of suite {suite_id}"
            unknown = "Feature / Test Group is unknown for its cases"
            self._sections[suite_id] = self._fetch(lambda: self.cache.sections(suite_id), label, unknown)
        return self._sections[suite_id]

    def group(
        self, case_id: int, candidates: list[int | None], suite_names: Mapping[int | None, str]
    ) -> CaseGroup:
        """The group of a case, from the first candidate suite that contains it.

        When no suite contains it, the state is the first fetch failure (the
        case may be in a suite that could not be fetched), else ``case_not_found``.
        """
        failure: FeatureGroupState | None = None
        for suite_id in candidates:
            cases = self.cases(suite_id)
            if isinstance(cases, FeatureGroupState):
                failure = failure or cases
                continue
            case = cases.get(case_id)
            if case is None:
                continue
            sections = self.sections(suite_id)
            if isinstance(sections, FeatureGroupState):
                return CaseGroup(case_id, sections, suite_id=suite_id)
            path = resolve_section_path(case.section_id, sections)
            return CaseGroup(
                case_id,
                path.state,
                suite_id=case.suite_id or suite_id,
                section_id=case.section_id,
                path=path.names,
                suite_name=suite_names.get(suite_id),
            )
        return CaseGroup(case_id, failure or FeatureGroupState.CASE_NOT_FOUND, suite_id=candidates[0])
