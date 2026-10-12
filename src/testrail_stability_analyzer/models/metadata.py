"""Project-level TestRail metadata: projects, suites, statuses, case types, custom fields."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Self

from ._parse import Fields, ModelError, frozen

_EMPTY: Mapping[str, Any] = frozen({})


@dataclass(frozen=True, slots=True)
class Project:
    id: int
    name: str
    suite_mode: int | None = None  # 1 single suite, 2 single + baselines, 3 multiple suites
    is_completed: bool | None = None
    completed_on: datetime | None = None
    announcement: str | None = None
    url: str | None = None
    extra: Mapping[str, Any] = _EMPTY

    @classmethod
    def from_api(cls, data: object) -> Self:
        f = Fields(data, "project")
        return cls(
            id=f.id(),
            name=f.required_string("name"),
            suite_mode=f.integer("suite_mode"),
            is_completed=f.boolean("is_completed"),
            completed_on=f.timestamp("completed_on"),
            announcement=f.string("announcement"),
            url=f.string("url"),
            extra=f.extra(),
        )


@dataclass(frozen=True, slots=True)
class Suite:
    id: int
    name: str
    project_id: int | None = None
    description: str | None = None
    is_master: bool | None = None
    is_baseline: bool | None = None
    is_completed: bool | None = None
    completed_on: datetime | None = None
    url: str | None = None
    extra: Mapping[str, Any] = _EMPTY

    @classmethod
    def from_api(cls, data: object) -> Self:
        f = Fields(data, "suite")
        return cls(
            id=f.id(),
            name=f.required_string("name"),
            project_id=f.integer("project_id"),
            description=f.string("description"),
            is_master=f.boolean("is_master"),
            is_baseline=f.boolean("is_baseline"),
            is_completed=f.boolean("is_completed"),
            completed_on=f.timestamp("completed_on"),
            url=f.string("url"),
            extra=f.extra(),
        )


@dataclass(frozen=True, slots=True)
class CaseType:
    id: int
    name: str
    is_default: bool | None = None
    extra: Mapping[str, Any] = _EMPTY

    @classmethod
    def from_api(cls, data: object) -> Self:
        f = Fields(data, "case_type")
        return cls(id=f.id(), name=f.required_string("name"), is_default=f.boolean("is_default"), extra=f.extra())


@dataclass(frozen=True, slots=True)
class Status:
    """An execution status as defined on the instance (system or custom)."""

    id: int
    name: str  # system name, e.g. "passed" or "custom_status1"
    label: str  # display label
    is_system: bool | None = None
    is_untested: bool | None = None
    is_final: bool | None = None
    color_dark: int | None = None
    color_medium: int | None = None
    color_bright: int | None = None
    extra: Mapping[str, Any] = _EMPTY

    @classmethod
    def from_api(cls, data: object) -> Self:
        f = Fields(data, "status")
        return cls(
            id=f.id(),
            name=f.required_string("name"),
            label=f.required_string("label"),
            is_system=f.boolean("is_system"),
            is_untested=f.boolean("is_untested"),
            is_final=f.boolean("is_final"),
            color_dark=f.integer("color_dark"),
            color_medium=f.integer("color_medium"),
            color_bright=f.integer("color_bright"),
            extra=f.extra(),
        )


@dataclass(frozen=True, slots=True)
class CustomFieldConfig:
    """One context/options block of a custom field definition."""

    id: str | None = None
    is_global: bool | None = None
    project_ids: tuple[int, ...] = ()
    is_required: bool | None = None
    default_value: Any = None
    # Raw options as returned (e.g. "items", "format", "rows"); type-dependent.
    options: Mapping[str, Any] = _EMPTY

    @classmethod
    def from_api(cls, data: object) -> Self:
        f = Fields(data, "custom_field.config")
        context = Fields(f.mapping("context"), "custom_field.config.context")
        options = f.mapping("options")
        option_fields = Fields(options, "custom_field.config.options")
        return cls(
            id=f.string("id"),
            is_global=context.boolean("is_global"),
            project_ids=context.int_list("project_ids"),
            is_required=option_fields.boolean("is_required"),
            default_value=options.get("default_value"),
            options=frozen(options),
        )

    def applies_to_project(self, project_id: int) -> bool:
        return bool(self.is_global) or project_id in self.project_ids

    def items(self) -> dict[int, str]:
        """Parse dropdown/multi-select ``items`` (``"1, First\\n2, Second"``) into ``{id: label}``."""
        raw = self.options.get("items")
        if raw is None:
            return {}
        if not isinstance(raw, str):
            raise ModelError("custom_field.config.options.items: expected a string")
        parsed: dict[int, str] = {}
        for line in raw.splitlines():
            if not line.strip():
                continue
            key, separator, label = line.partition(",")
            if not separator or not key.strip().isdecimal():
                raise ModelError(f"custom_field.config.options.items: malformed line {line!r}")
            parsed[int(key)] = label.strip()
        return parsed


@dataclass(frozen=True, slots=True)
class CustomField:
    """A custom case or result field definition (``get_case_fields`` / ``get_result_fields``)."""

    id: int
    system_name: str  # e.g. "custom_preconds"; the key used on cases/tests/results
    name: str | None = None
    label: str | None = None
    type_id: int | None = None
    description: str | None = None
    display_order: int | None = None
    is_active: bool | None = None
    include_all: bool | None = None
    template_ids: tuple[int, ...] = ()
    configs: tuple[CustomFieldConfig, ...] = ()
    extra: Mapping[str, Any] = _EMPTY

    @classmethod
    def from_api(cls, data: object) -> Self:
        f = Fields(data, "custom_field")
        return cls(
            id=f.id(),
            system_name=f.required_string("system_name"),
            name=f.string("name"),
            label=f.string("label"),
            type_id=f.integer("type_id"),
            description=f.string("description"),
            display_order=f.integer("display_order"),
            is_active=f.boolean("is_active"),
            include_all=f.boolean("include_all"),
            template_ids=f.int_list("template_ids"),
            configs=tuple(CustomFieldConfig.from_api(config) for config in f.array("configs")),
            extra=f.extra(),
        )

    def applies_to_project(self, project_id: int) -> bool:
        return any(config.applies_to_project(project_id) for config in self.configs)


# Case and result field definitions share the same structure.
CaseField = CustomField
ResultField = CustomField
