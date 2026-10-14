"""Report output paths: automatic unique file names and collision-safe writing.

A report option names either a file (used as given) or a directory (an
existing one, or a path ending with a separator), in which the file name is
generated::

    stability-{scope}-p{project_id}[-s{suite_id}]-{window}d-{YYYYMMDD-HHMMSS}[-N].{xlsx|xml}

The timestamp is UTC. Formats written together into directories share one
base name and suffix, differing only in the extension. Files are created
exclusively (``O_CREAT | O_EXCL``), so an existing report, including one
written by another process in the same second, is never overwritten: an
automatic name gets the next free ``-2``, ``-3``, ... suffix, and an explicit
file name fails unless overwriting was requested, in which case the file is
replaced atomically. New files are readable by the owner only.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

PREFIX: Final = "stability"
MAX_SUFFIX: Final = 10_000
# Scopes list at most this many IDs per kind in a file name; more are summarized as "+<n>more".
MAX_NAMED_IDS: Final = 3
_FILE_MODE: Final = 0o600
_SEPARATORS: Final = tuple({os.sep, "/"} | ({os.altsep} if os.altsep else set()))


class ReportFileError(Exception):
    """A report file could not be created; the message holds local path facts only."""

    def __init__(self, message: str, written: Sequence[tuple[str, Path]] = ()) -> None:
        super().__init__(message)
        self.written = tuple(written)  # (label, path) of reports written before the failure


@dataclass(frozen=True, slots=True)
class ReportTarget:
    label: str  # "XML" or "XLSX"
    path: Path
    extension: str  # ".xml" or ".xlsx", used for automatic names
    directory_requested: bool = False  # the path was given with a trailing separator

    @classmethod
    def parse(cls, label: str, raw: str, extension: str) -> ReportTarget:
        """From the command-line text; ``Path`` itself drops a trailing separator."""
        return cls(label, Path(raw), extension, raw.endswith(_SEPARATORS))

    @property
    def automatic(self) -> bool:
        """True when the path names a directory, so the file name is generated."""
        return self.directory_requested or self.path.is_dir()


def report_scope(
    target: str, plan_ids: Sequence[int] = (), run_ids: Sequence[int] = (), sample_plan_id: int | None = None
) -> str:
    """``project``, ``plan-<ids>[-run-<ids>]``, ``run-<ids>[-plan-<ids>]``, or ``sample[-plan-<id>]``."""
    if target == "project":
        return "project"
    if target == "sample":
        return "sample" if sample_plan_id is None else f"sample-plan-{sample_plan_id}"
    kinds = (("plan", plan_ids), ("run", run_ids))
    parts = [f"{kind}-{_ids(ids)}" for kind, ids in (kinds if target == "plan" else kinds[::-1]) if ids]
    return "-".join(parts) or target


def report_basename(scope: str, project_id: int, suite_id: int | None, window_days: int, moment: datetime) -> str:
    suite = "" if suite_id is None else f"-s{suite_id}"
    stamp = moment.astimezone(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{PREFIX}-{scope}-p{project_id}{suite}-{window_days}d-{stamp}"


def target_problem(target: ReportTarget, *, overwrite: bool) -> str | None:
    """Checked before any TestRail request, so an unusable path does not waste the collection."""
    path = target.path
    if target.automatic:
        if not path.is_dir():
            return f"directory {path} does not exist"
        return None if os.access(path, os.W_OK | os.X_OK) else f"directory {path} is not writable"
    if not path.parent.is_dir():
        return f"directory {path.parent} does not exist"
    if not os.access(path.parent, os.W_OK | os.X_OK):
        return f"directory {path.parent} is not writable"
    if (path.exists() or path.is_symlink()) and not overwrite:
        return f"{path} already exists; pass --overwrite-report to replace it"
    return None


def write_reports(
    reports: Sequence[tuple[ReportTarget, bytes]], basename: str, *, overwrite: bool
) -> list[tuple[str, Path]]:
    """Write every report; returns ``(label, absolute path)`` per written file, in order.

    Raises :class:`ReportFileError` at the first file that cannot be written;
    files written before it are kept, reserved automatic files not yet written
    are removed.
    """
    reserved = _reserve([target for target, _ in reports if target.automatic], basename)
    written: list[tuple[str, Path]] = []
    try:
        for target, data in reports:
            if target.automatic:
                fd, path = reserved.pop(target)
                _write_new(fd, path, data)
            elif overwrite:
                path = Path(os.path.abspath(target.path))
                _write_replace(path, data)
            else:
                path = Path(os.path.abspath(target.path))
                try:
                    fd = _create(path)
                except FileExistsError:  # created after the check before collection
                    raise ReportFileError(f"{path} already exists; pass --overwrite-report to replace it") from None
                _write_new(fd, path, data)
            written.append((target.label, path))
    except ReportFileError as exc:
        raise ReportFileError(f"could not write the {target.label} report: {exc}", written) from None
    finally:
        for fd, path in reserved.values():
            os.close(fd)
            path.unlink(missing_ok=True)
    return written


def _reserve(targets: Sequence[ReportTarget], basename: str) -> dict[ReportTarget, tuple[int, Path]]:
    """Exclusively create one empty file per target with the first suffix free for all of them."""
    if not targets:
        return {}
    for number in range(1, MAX_SUFFIX + 1):
        name = basename if number == 1 else f"{basename}-{number}"
        created: dict[ReportTarget, tuple[int, Path]] = {}
        try:
            for target in targets:
                path = Path(os.path.abspath(target.path / f"{name}{target.extension}"))
                created[target] = (_create(path), path)
            return created
        except (FileExistsError, ReportFileError) as exc:
            for fd, path in created.values():
                os.close(fd)
                path.unlink(missing_ok=True)
            if isinstance(exc, ReportFileError):
                raise
    raise ReportFileError(f"no free file name for {basename} after {MAX_SUFFIX} attempts")


def _create(path: Path) -> int:
    """Create ``path`` exclusively; :class:`FileExistsError` when it exists (never follows a symlink)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        return os.open(path, flags, _FILE_MODE)
    except FileExistsError:
        raise
    except OSError as exc:
        raise ReportFileError(f"{path}: {exc.strerror or type(exc).__name__}") from None


def _write_new(fd: int, path: Path, data: bytes) -> None:
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise ReportFileError(f"{path}: {exc.strerror or type(exc).__name__}") from None


def _write_replace(path: Path, data: bytes) -> None:
    """Atomic replacement: write a temporary file in the same directory, then rename it over ``path``."""
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as file:
            temporary = file.name
            file.write(data)
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
        raise ReportFileError(f"{path}: {exc.strerror or type(exc).__name__}") from None


def _ids(ids: Sequence[int]) -> str:
    unique = list(dict.fromkeys(ids))
    shown = "_".join(str(item) for item in unique[:MAX_NAMED_IDS])
    return shown if len(unique) <= MAX_NAMED_IDS else f"{shown}+{len(unique) - MAX_NAMED_IDS}more"


__all__ = [
    "ReportFileError",
    "ReportTarget",
    "report_basename",
    "report_scope",
    "target_problem",
    "write_reports",
]
