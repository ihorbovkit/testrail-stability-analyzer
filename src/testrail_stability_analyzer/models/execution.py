"""Test design and execution entities: cases, plans, plan entries, runs, tests, results.

Relationships are kept through source TestRail IDs:
``Plan -> PlanEntry -> Run -> Test (case_id) -> Result (test_id)``. Standalone
runs have ``plan_id = None``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Self

from ._parse import Fields, frozen

_EMPTY: Mapping[str, Any] = frozen({})


@dataclass(frozen=True, slots=True)
class Case:
    id: int
    title: str
    section_id: int | None = None
    suite_id: int | None = None
    template_id: int | None = None
    type_id: int | None = None
    priority_id: int | None = None
    milestone_id: int | None = None
    refs: str | None = None
    estimate: str | None = None
    created_on: datetime | None = None
    created_by: int | None = None
    updated_on: datetime | None = None
    updated_by: int | None = None
    custom_fields: Mapping[str, Any] = _EMPTY
    extra: Mapping[str, Any] = _EMPTY

    @classmethod
    def from_api(cls, data: object) -> Self:
        f = Fields(data, "case")
        return cls(
            id=f.id(),
            title=f.required_string("title"),
            section_id=f.integer("section_id"),
            suite_id=f.integer("suite_id"),
            template_id=f.integer("template_id"),
            type_id=f.integer("type_id"),
            priority_id=f.integer("priority_id"),
            milestone_id=f.integer("milestone_id"),
            refs=f.string("refs"),
            estimate=f.string("estimate"),
            created_on=f.timestamp("created_on"),
            created_by=f.integer("created_by"),
            updated_on=f.timestamp("updated_on"),
            updated_by=f.integer("updated_by"),
            custom_fields=f.custom_fields(),
            extra=f.extra(),
        )


@dataclass(frozen=True, slots=True)
class Run:
    """A Test Run, either standalone (``plan_id is None``) or part of a Test Plan entry."""

    id: int
    name: str
    project_id: int | None = None
    suite_id: int | None = None
    plan_id: int | None = None
    entry_id: str | None = None
    entry_index: int | None = None
    config: str | None = None  # configuration names, e.g. "Platform A, Browser B"
    config_ids: tuple[int, ...] = ()
    milestone_id: int | None = None
    description: str | None = None
    include_all: bool | None = None
    is_completed: bool | None = None
    completed_on: datetime | None = None
    created_on: datetime | None = None
    created_by: int | None = None
    updated_on: datetime | None = None
    assignedto_id: int | None = None
    refs: str | None = None
    url: str | None = None
    status_counts: Mapping[str, int] = _EMPTY
    extra: Mapping[str, Any] = _EMPTY

    @property
    def is_standalone(self) -> bool:
        return self.plan_id is None

    @classmethod
    def from_api(cls, data: object, *, plan_id: int | None = None, entry_id: str | None = None) -> Self:
        """``plan_id``/``entry_id`` are fallbacks for runs nested in a plan entry."""
        f = Fields(data, "run")
        return cls(
            id=f.id(),
            name=f.required_string("name"),
            project_id=f.integer("project_id"),
            suite_id=f.integer("suite_id"),
            plan_id=f.integer("plan_id") or plan_id,
            entry_id=f.string("entry_id") or entry_id,
            entry_index=f.integer("entry_index"),
            config=f.string("config"),
            config_ids=f.int_list("config_ids"),
            milestone_id=f.integer("milestone_id"),
            description=f.string("description"),
            include_all=f.boolean("include_all"),
            is_completed=f.boolean("is_completed"),
            completed_on=f.timestamp("completed_on"),
            created_on=f.timestamp("created_on"),
            created_by=f.integer("created_by"),
            updated_on=f.timestamp("updated_on"),
            assignedto_id=f.integer("assignedto_id"),
            refs=f.string("refs"),
            url=f.string("url"),
            status_counts=f.status_counts(),
            extra=f.extra(),
        )


@dataclass(frozen=True, slots=True)
class PlanEntry:
    """A group of runs in a plan for one suite (one run per configuration combination)."""

    id: str  # TestRail uses GUID strings for entry IDs
    suite_id: int | None = None
    name: str | None = None
    description: str | None = None
    include_all: bool | None = None
    refs: str | None = None
    runs: tuple[Run, ...] = ()
    extra: Mapping[str, Any] = _EMPTY

    @classmethod
    def from_api(cls, data: object, *, plan_id: int | None = None) -> Self:
        f = Fields(data, "plan_entry")
        entry_id = f.required_string("id")
        return cls(
            id=entry_id,
            suite_id=f.integer("suite_id"),
            name=f.string("name"),
            description=f.string("description"),
            include_all=f.boolean("include_all"),
            refs=f.string("refs"),
            runs=tuple(Run.from_api(run, plan_id=plan_id, entry_id=entry_id) for run in f.array("runs")),
            extra=f.extra(),
        )


@dataclass(frozen=True, slots=True)
class Plan:
    """A Test Plan. ``entries`` is only populated by ``get_plan``, not by ``get_plans``."""

    id: int
    name: str
    project_id: int | None = None
    milestone_id: int | None = None
    description: str | None = None
    assignedto_id: int | None = None
    is_completed: bool | None = None
    completed_on: datetime | None = None
    created_on: datetime | None = None
    created_by: int | None = None
    refs: str | None = None
    url: str | None = None
    status_counts: Mapping[str, int] = _EMPTY
    entries: tuple[PlanEntry, ...] = ()
    extra: Mapping[str, Any] = _EMPTY

    @property
    def runs(self) -> tuple[Run, ...]:
        """All runs across all entries, in API order."""
        return tuple(run for entry in self.entries for run in entry.runs)

    @classmethod
    def from_api(cls, data: object) -> Self:
        f = Fields(data, "plan")
        plan_id = f.id()
        return cls(
            id=plan_id,
            name=f.required_string("name"),
            project_id=f.integer("project_id"),
            milestone_id=f.integer("milestone_id"),
            description=f.string("description"),
            assignedto_id=f.integer("assignedto_id"),
            is_completed=f.boolean("is_completed"),
            completed_on=f.timestamp("completed_on"),
            created_on=f.timestamp("created_on"),
            created_by=f.integer("created_by"),
            refs=f.string("refs"),
            url=f.string("url"),
            status_counts=f.status_counts(),
            entries=tuple(PlanEntry.from_api(entry, plan_id=plan_id) for entry in f.array("entries")),
            extra=f.extra(),
        )


@dataclass(frozen=True, slots=True)
class Test:
    """A test instance: one case within one run, carrying the run's current status."""

    __test__ = False  # not a pytest test class

    id: int
    case_id: int
    run_id: int
    title: str | None = None
    status_id: int | None = None
    assignedto_id: int | None = None
    template_id: int | None = None
    type_id: int | None = None
    priority_id: int | None = None
    milestone_id: int | None = None
    refs: str | None = None
    estimate: str | None = None
    custom_fields: Mapping[str, Any] = _EMPTY
    extra: Mapping[str, Any] = _EMPTY

    @classmethod
    def from_api(cls, data: object) -> Self:
        f = Fields(data, "test")
        return cls(
            id=f.id(),
            case_id=f.id("case_id"),
            run_id=f.id("run_id"),
            title=f.string("title"),
            status_id=f.integer("status_id"),
            assignedto_id=f.integer("assignedto_id"),
            template_id=f.integer("template_id"),
            type_id=f.integer("type_id"),
            priority_id=f.integer("priority_id"),
            milestone_id=f.integer("milestone_id"),
            refs=f.string("refs"),
            estimate=f.string("estimate"),
            custom_fields=f.custom_fields(),
            extra=f.extra(),
        )


@dataclass(frozen=True, slots=True)
class Result:
    """One result entry for a test. A test can have many (retries, triage updates).

    ``status_id`` is ``None`` for entries that only add a comment or reassign.
    ``comment`` and custom/extra values are excluded from ``repr`` so free text
    is never written to logs by accident.
    """

    id: int
    test_id: int
    status_id: int | None = None
    created_on: datetime | None = None
    created_by: int | None = None
    assignedto_id: int | None = None
    version: str | None = None
    elapsed: str | None = None
    defects: str | None = None
    comment: str | None = field(default=None, repr=False)
    attachment_ids: tuple[int, ...] = ()
    # Not part of the result payload; filled from the request context when known.
    run_id: int | None = None
    case_id: int | None = None
    custom_fields: Mapping[str, Any] = field(default=_EMPTY, repr=False)
    extra: Mapping[str, Any] = field(default=_EMPTY, repr=False)

    @classmethod
    def from_api(cls, data: object, *, run_id: int | None = None, case_id: int | None = None) -> Self:
        f = Fields(data, "result")
        return cls(
            id=f.id(),
            test_id=f.id("test_id"),
            status_id=f.integer("status_id"),
            created_on=f.timestamp("created_on"),
            created_by=f.integer("created_by"),
            assignedto_id=f.integer("assignedto_id"),
            version=f.string("version"),
            elapsed=f.string("elapsed"),
            defects=f.string("defects"),
            comment=f.string("comment"),
            attachment_ids=f.int_list("attachment_ids"),
            run_id=f.integer("run_id") or run_id,
            case_id=f.integer("case_id") or case_id,
            custom_fields=f.custom_fields(),
            extra=f.extra(),
        )
