"""Real-time CLI progress on a text stream (stderr), for interactive terminals and CI logs.

The reporter is a :data:`~.progress.ProgressListener` plus phase control.
Events update a small state under a lock; a daemon heartbeat thread calls
:meth:`ConsoleProgress.tick` every second, so output continues while the main
thread is blocked in an HTTP request or a retry wait.

- ``tty`` mode: phase changes and warnings are permanent lines; one status line
  is redrawn in place (``\\r`` + clear to end of line).
- ``plain`` mode: phase changes, warnings, and at most one status line per
  ``interval`` seconds, for redirected output and CI logs.

Only counts, elapsed times, phase names, and TestRail endpoint paths are
printed. There is no ETA and no completion percentage: discovery totals are
unknown, and a finished run is reported as completed only when it was.
"""

from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from typing import Final, TextIO

from .progress import (
    BudgetExhausted,
    CollectionProgress,
    DiscoveryProgress,
    ProgressEvent,
    RequestFinished,
    RequestStarted,
    RetryScheduled,
    Stage,
    StageStarted,
)

PROGRESS_MODES: Final = ("auto", "tty", "plain", "off")
DEFAULT_INTERVAL_SECONDS: Final = 15
# A pending request is reported after this many seconds, then again every SLOW_REPEAT_SECONDS.
SLOW_REQUEST_SECONDS: Final = 15.0
SLOW_REPEAT_SECONDS: Final = 30.0
# Fractions of the request budget that trigger a warning.
BUDGET_WARNING_LEVELS: Final = (0.8, 0.95)
TICK_SECONDS: Final = 1.0
_SPINNER: Final = "|/-\\"
_CLEAR_LINE: Final = "\r\x1b[K"


class Outcome(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    NOTHING_TO_COLLECT = "nothing_to_collect"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ConsoleProgress:
    def __init__(
        self,
        stream: TextIO,
        phases: Sequence[str],
        *,
        tty: bool,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        stage_phases: Mapping[Stage, int] | None = None,
        clock: Callable[[], float] = time.monotonic,
        width: Callable[[], int] | None = None,
        tick_seconds: float = TICK_SECONDS,
    ) -> None:
        """``phases``: names in execution order. ``stage_phases``: 1-based phase entered on a stage event."""
        if not phases:
            raise ValueError("at least one phase is required")
        self._stream = stream
        self._phases = tuple(phases)
        self._tty = tty
        self._interval = interval
        self._stage_phases = dict(stage_phases or {})
        self._clock = clock
        self._width = width or (lambda: shutil.get_terminal_size((100, 24)).columns)
        self._tick_seconds = tick_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = clock()
        self._phase = 0
        self._phase_started_at = self._started_at
        self._detail = ""
        self._requests = 0
        self._budget: int | None = None
        self._budget_exhausted = False
        self._budget_warned: set[float] = set()
        self._pending: tuple[str, float] | None = None  # endpoint, sent at
        self._next_slow_warning = SLOW_REQUEST_SECONDS
        self._retry: tuple[RetryScheduled, float] | None = None  # event, retry at
        self._last_plain_status = self._started_at
        self._status_shown = False
        self._spin = 0
        self._closed = False

    @property
    def interactive(self) -> bool:
        """True in ``tty`` mode (status line redrawn in place)."""
        return self._tty

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start the heartbeat thread that keeps output alive during blocking calls."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._heartbeat, name="progress-heartbeat", daemon=True)
            self._thread.start()

    def finish(self, outcome: Outcome, detail: str | None = None) -> None:
        """Stop the heartbeat and print the final line; later events are ignored."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()
        with self._lock:
            if self._closed:
                return
            if outcome in (Outcome.COMPLETED, Outcome.PARTIAL) and self._phase:
                self._end_phase()
            self._line(self._final_text(outcome, detail))
            self._closed = True

    def phase(self, index: int) -> None:
        """Enter phase ``index`` (1-based); the previous phase is reported as ended."""
        with self._lock:
            self._enter(index)

    # -- events --------------------------------------------------------------

    def __call__(self, event: ProgressEvent) -> None:
        with self._lock:
            if self._closed:
                return
            now = self._clock()
            match event:
                case StageStarted(stage=stage):
                    index = self._stage_phases.get(stage)
                    if index is not None and index > self._phase:
                        self._enter(index)
                case RequestStarted():
                    self._pending = (event.endpoint, now)
                    self._next_slow_warning = SLOW_REQUEST_SECONDS
                    self._retry = None
                    self._requests, self._budget = event.requests_made, event.budget
                    self._check_budget()
                case RequestFinished():
                    self._pending = None
                case RetryScheduled():
                    self._pending = None
                    self._retry = (event, now + event.delay_seconds)
                    reason = event.reason[:1].upper() + event.reason[1:]
                    self._line(
                        f"{reason}: retrying {event.endpoint} in {_seconds(event.delay_seconds)} "
                        f"(attempt {event.attempt} of {event.max_attempts})"
                    )
                case BudgetExhausted():
                    self._pending = self._retry = None
                    self._requests, self._budget = event.requests_made, event.budget
                    if not self._budget_exhausted:
                        self._budget_exhausted = True
                        self._line(
                            f"Request budget exhausted ({event.requests_made}/{event.budget}); "
                            "remaining requests are skipped and coverage will be partial"
                        )
                case DiscoveryProgress():
                    self._detail = _discovery_text(event)
                case CollectionProgress():
                    self._detail = _collection_text(event)

    def tick(self) -> None:
        """Report slow requests and refresh the status (called by the heartbeat thread)."""
        with self._lock:
            if self._closed or not self._phase:
                return
            now = self._clock()
            if self._pending is not None:
                endpoint, sent_at = self._pending
                waited = now - sent_at
                if waited >= self._next_slow_warning:
                    self._line(f"Slow response: still waiting for TestRail ({endpoint}, {_duration(waited)})")
                    self._next_slow_warning += SLOW_REPEAT_SECONDS
            if self._tty:
                self._spin += 1
                self._draw()
            elif now - self._last_plain_status >= self._interval:
                self._line(self._status_text(now))

    # -- rendering -----------------------------------------------------------

    def _enter(self, index: int) -> None:
        if self._closed or not 1 <= index <= len(self._phases) or index == self._phase:
            return
        if self._phase:
            self._end_phase()
        self._phase = index
        self._phase_started_at = self._clock()
        self._detail = ""
        total = len(self._phases)
        name = self._phases[index - 1]
        self._line(f"Phase {index}/{total}: {name} ({index - 1} completed, {total - index} remaining after this)")

    def _end_phase(self) -> None:
        took = _duration(self._clock() - self._phase_started_at)
        note = " (request budget exhausted)" if self._budget_exhausted else ""
        self._line(f"Phase {self._phase}/{len(self._phases)} ended after {took}{note}")

    def _final_text(self, outcome: Outcome, detail: str | None) -> str:
        total = len(self._phases)
        elapsed = _duration(self._clock() - self._started_at)
        where = f"phase {self._phase}/{total} ({self._phases[self._phase - 1]})" if self._phase else "startup"
        api = f"; API requests {self._requests}/{self._budget}" if self._budget is not None else ""
        suffix = f": {detail}" if detail else ""
        match outcome:
            case Outcome.COMPLETED if self._phase == total:
                return f"Completed all {total} phases in {elapsed}{api}"
            case Outcome.COMPLETED:
                return f"Finished after {where} in {elapsed}{api}"
            case Outcome.PARTIAL:
                budget = "; request budget exhausted" if self._budget_exhausted else ""
                ran = f"all {total} phases ran" if self._phase == total else f"stopped after {where}"
                return (
                    f"Finished with PARTIAL coverage in {elapsed} ({ran}{budget}){api}; "
                    "see Limitations in the summary"
                )
            case Outcome.NOTHING_TO_COLLECT:
                return f"Stopped after {where} in {elapsed}: nothing to collect{api}"
            case Outcome.INTERRUPTED:
                return f"Interrupted during {where} after {elapsed}{api}"
            case _:
                return f"Failed during {where} after {elapsed}{suffix}{api}"

    def _status_text(self, now: float, *, name: bool = True) -> str:
        """Most important first (a narrow terminal truncates the end): phase, activity, budget, counts."""
        phase = f"Phase {self._phase}/{len(self._phases)}"
        parts = [f"{phase} {self._phases[self._phase - 1]}" if name else phase]
        parts.append(f"running {_duration(now - self._phase_started_at)}")
        if self._retry is not None:
            event, retry_at = self._retry
            parts.append(f"{event.reason}, retry in {_duration(max(retry_at - now, 0.0))}: {event.endpoint}")
        elif self._pending is not None:
            endpoint, sent_at = self._pending
            parts.append(f"waiting {_duration(now - sent_at)}: {endpoint}")
        if self._budget is not None:
            parts.append(f"API {self._requests}/{self._budget}")
        if self._detail:
            parts.append(self._detail)
        return " | ".join(parts)

    def _draw(self) -> None:
        now = self._clock()
        status = self._status_text(now, name=False)  # the phase name is on the permanent line above
        text = f"{_SPINNER[self._spin % len(_SPINNER)]} [{_duration(now - self._started_at)}] {status}"
        limit = max(self._width() - 1, 20)
        if len(text) > limit:
            text = text[: limit - 3] + "..."
        self._write(_CLEAR_LINE + text)
        self._status_shown = True

    def _line(self, text: str) -> None:
        now = self._clock()
        prefix = _CLEAR_LINE if self._tty and self._status_shown else ""
        self._write(f"{prefix}[{_duration(now - self._started_at)}] {text}\n")
        self._status_shown = False
        self._last_plain_status = now

    def _write(self, text: str) -> None:
        if self._closed:
            return
        try:
            self._stream.write(text)
            self._stream.flush()
        except (OSError, ValueError):  # closed or broken stream: progress is best effort
            self._closed = True

    def _check_budget(self) -> None:
        if not self._budget:
            return
        for level in BUDGET_WARNING_LEVELS:
            if level not in self._budget_warned and self._requests >= level * self._budget:
                self._budget_warned.add(level)
                self._line(
                    f"Request budget {level:.0%} used ({self._requests}/{self._budget}); "
                    "collection stops with partial coverage when it is exhausted (see --max-requests)"
                )

    def _heartbeat(self) -> None:
        while not self._stop.wait(self._tick_seconds):
            self.tick()


class NullProgress:
    """``--progress off``: the same interface, no output and no thread."""

    def __call__(self, event: ProgressEvent) -> None:
        pass

    def start(self) -> None:
        pass

    def phase(self, index: int) -> None:
        pass

    def tick(self) -> None:
        pass

    def finish(self, outcome: Outcome, detail: str | None = None) -> None:
        pass


def _discovery_text(event: DiscoveryProgress) -> str:
    parts = []
    if event.targets_total is not None:
        parts.append(f"{event.target_kind} {event.targets_done}/{event.targets_total}")
    if event.plans_listed is not None:
        parts.append(f"plans listed {event.plans_listed}")
    parts.append(f"plans fetched {event.plans_fetched}")
    parts.append(f"runs found {event.runs_found} (included {event.runs_included})")
    return ", ".join(parts)


def _collection_text(event: CollectionProgress) -> str:
    return (
        f"{event.step}: {event.processed}/{event.total} runs; runs with results {event.runs_with_results}, "
        f"skipped {event.runs_skipped}; result records {event.records}"
    )


def _duration(seconds: float) -> str:
    total = int(max(seconds, 0.0))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _seconds(seconds: float) -> str:
    return f"{seconds:g}s" if seconds < 60 else _duration(seconds)


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "PROGRESS_MODES",
    "ConsoleProgress",
    "NullProgress",
    "Outcome",
]
