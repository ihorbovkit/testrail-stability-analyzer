"""Typed domain models for TestRail entities."""

from ._parse import ModelError
from .execution import Case, Plan, PlanEntry, Result, Run, Test
from .metadata import CaseField, CaseType, CustomField, CustomFieldConfig, Project, ResultField, Status, Suite
from .statuses import SYSTEM_STATUS_CATEGORIES, ResolutionSource, ResolvedStatus, StatusResolver

__all__ = [
    "SYSTEM_STATUS_CATEGORIES",
    "Case",
    "CaseField",
    "CaseType",
    "CustomField",
    "CustomFieldConfig",
    "ModelError",
    "Plan",
    "PlanEntry",
    "Project",
    "ResolutionSource",
    "ResolvedStatus",
    "Result",
    "ResultField",
    "Run",
    "Status",
    "StatusResolver",
    "Suite",
    "Test",
]
