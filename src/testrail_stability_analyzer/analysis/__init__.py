"""Historical stability analysis and case-level defect aggregation."""

from .analyzer import analyze
from .classification import diagnose
from .defects import aggregate_defects, defect_coverage, parse_defects
from .metrics import compute_metrics
from .models import (
    AnalysisReport,
    CaseDefects,
    Confidence,
    DefectCoverage,
    DefectReference,
    DefectSource,
    Diagnosis,
    KeyAnalysis,
    Rate,
    StabilityMetrics,
    StatusTransition,
)

__all__ = [
    "AnalysisReport",
    "CaseDefects",
    "Confidence",
    "DefectCoverage",
    "DefectReference",
    "DefectSource",
    "Diagnosis",
    "KeyAnalysis",
    "Rate",
    "StabilityMetrics",
    "StatusTransition",
    "aggregate_defects",
    "analyze",
    "compute_metrics",
    "defect_coverage",
    "diagnose",
    "parse_defects",
]
