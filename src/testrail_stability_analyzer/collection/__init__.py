"""Historical execution collection from TestRail."""

from .classification import classify_run, match_dimension
from .collector import HistoricalCollector
from .discovery import RunDiscovery, SamplePlanSelection
from .history import build_histories, group_of, history_key
from .models import (
    Applicability,
    AvailabilityState,
    CollectionReport,
    Coverage,
    DimensionState,
    DimensionValue,
    DiscoveredRun,
    DiscoveryResult,
    ExecutionHistory,
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

__all__ = [
    "Applicability",
    "AvailabilityState",
    "CollectionReport",
    "Coverage",
    "DimensionState",
    "DimensionValue",
    "DiscoveredRun",
    "DiscoveryResult",
    "ExecutionHistory",
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
    "SelectionKind",
    "build_histories",
    "build_outcome",
    "classify_run",
    "deduplicate",
    "group_of",
    "history_key",
    "match_dimension",
    "sanitize_comment",
    "select_final",
    "sort_chronologically",
]
