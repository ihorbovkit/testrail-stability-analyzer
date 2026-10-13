"""Historical execution collection from TestRail."""

from .applicability import matched_applications, normalize_values, resolve_applicability, unmapped_values
from .case_metadata import CaseMetadataCache
from .classification import classify_run, match_dimension
from .collector import HistoricalCollector
from .discovery import RunDiscovery, SamplePlanSelection
from .history import build_histories, group_of, history_key
from .models import (
    Applicability,
    ApplicabilityValueState,
    AvailabilityState,
    CaseApplicability,
    CaseGroup,
    CollectionReport,
    Coverage,
    DimensionState,
    DimensionValue,
    DiscoveredRun,
    DiscoveryResult,
    ExecutionHistory,
    FeatureGroupState,
    HistoryKey,
    InvalidSelection,
    ResolvedResult,
    ResultConflict,
    RunClassification,
    RunDecision,
    RunOutcome,
    RunScan,
    SelectionKind,
)
from .outcomes import build_outcome, deduplicate, select_final, sort_chronologically
from .sanitize import sanitize_comment
from .sections import SectionPath, enrich_feature_groups, feature_group_label, resolve_section_path

__all__ = [
    "Applicability",
    "ApplicabilityValueState",
    "AvailabilityState",
    "CaseApplicability",
    "CaseGroup",
    "CaseMetadataCache",
    "CollectionReport",
    "Coverage",
    "DimensionState",
    "DimensionValue",
    "DiscoveredRun",
    "DiscoveryResult",
    "ExecutionHistory",
    "FeatureGroupState",
    "HistoricalCollector",
    "HistoryKey",
    "InvalidSelection",
    "ResolvedResult",
    "ResultConflict",
    "RunClassification",
    "RunDecision",
    "RunDiscovery",
    "RunOutcome",
    "RunScan",
    "SamplePlanSelection",
    "SectionPath",
    "SelectionKind",
    "build_histories",
    "build_outcome",
    "classify_run",
    "deduplicate",
    "enrich_feature_groups",
    "feature_group_label",
    "group_of",
    "history_key",
    "match_dimension",
    "matched_applications",
    "normalize_values",
    "resolve_applicability",
    "resolve_section_path",
    "sanitize_comment",
    "select_final",
    "sort_chronologically",
    "unmapped_values",
]
