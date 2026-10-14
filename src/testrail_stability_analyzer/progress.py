"""Progress events emitted by the API client, run discovery, and collection.

Producers accept an optional :data:`ProgressListener` and call it
synchronously with small immutable events. The events carry counts, TestRail
endpoint paths (no query string), and IDs only: never credentials, headers,
plan or run names, comments, or response bodies. This module has no terminal
or UI dependencies; see :mod:`.console_progress` for the CLI rendering.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class Stage(StrEnum):
    DISCOVERY = "discovery"
    COLLECTION = "collection"


@dataclass(frozen=True, slots=True)
class StageStarted:
    """A collection stage started (emitted by :class:`~.collection.HistoricalCollector`)."""

    stage: Stage


@dataclass(frozen=True, slots=True)
class RequestStarted:
    """One HTTP attempt is about to be sent; ``requests_made`` already counts it."""

    endpoint: str  # e.g. "get_results_for_run/123"
    attempt: int
    requests_made: int
    budget: int


@dataclass(frozen=True, slots=True)
class RequestFinished:
    """The attempt returned a response (``status``) or failed without one (``None``)."""

    endpoint: str
    status: int | None


@dataclass(frozen=True, slots=True)
class RetryScheduled:
    """The client waits ``delay_seconds`` before retrying ``endpoint``."""

    endpoint: str
    reason: str  # e.g. "rate limited (HTTP 429)", "HTTP 503", "timeout"
    delay_seconds: float
    attempt: int  # the attempt that will be made after the delay
    max_attempts: int


@dataclass(frozen=True, slots=True)
class BudgetExhausted:
    """The configured request budget is used up; the next request was not sent."""

    requests_made: int
    budget: int


@dataclass(frozen=True, slots=True)
class DiscoveryProgress:
    """Cumulative run-discovery counts. ``plans_listed`` / ``targets_total`` are ``None`` when unknown."""

    plans_fetched: int = 0
    runs_found: int = 0
    runs_included: int = 0
    plans_listed: int | None = None
    targets_done: int = 0
    targets_total: int | None = None
    target_kind: str = "selected IDs"


@dataclass(frozen=True, slots=True)
class CollectionProgress:
    """Cumulative collection counts; ``processed`` of ``total`` runs in the current pass."""

    step: str
    processed: int
    total: int
    runs_with_results: int
    runs_skipped: int  # runs with a fetch skipped (request budget or result-record cap)
    records: int


type ProgressEvent = (
    StageStarted
    | RequestStarted
    | RequestFinished
    | RetryScheduled
    | BudgetExhausted
    | DiscoveryProgress
    | CollectionProgress
)
type ProgressListener = Callable[[ProgressEvent], None]


def ignore_progress(event: ProgressEvent) -> None:
    """The default listener: does nothing."""


__all__ = [
    "BudgetExhausted",
    "CollectionProgress",
    "DiscoveryProgress",
    "ProgressEvent",
    "ProgressListener",
    "RequestFinished",
    "RequestStarted",
    "RetryScheduled",
    "Stage",
    "StageStarted",
    "ignore_progress",
]
