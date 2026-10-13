"""Command-line interface: read-only historical collection with a sanitized summary.

Commands::

    testrail-stability-analyzer collect plan PLAN_ID [PLAN_ID ...] [--run-id RUN_ID ...]
    testrail-stability-analyzer collect run RUN_ID [RUN_ID ...] [--plan-id PLAN_ID ...]
    testrail-stability-analyzer collect sample

    ... [--xml-report PATH] [--xlsx-report PATH]

The CLI runs :class:`~.collection.HistoricalCollector` and prints counts,
TestRail IDs, configured mapping names, and status categories only. Plan and
run names, comments, defect identifiers, and credentials are never printed.

Nothing is written to disk unless a report is requested: ``--xml-report``
(Excel-compatible XML Spreadsheet 2003, standard library) and/or
``--xlsx-report`` (native XLSX, needs the optional ``openpyxl`` package). The
Feature / Test Group of each case is reconstructed from TestRail sections
(unless ``report.feature_group.enabled`` is false). Section metadata and case
types are fetched once per invocation and only for reports; collection on its
own makes no extra requests.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import os
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, TextIO

from .analysis import analyze
from .api import HttpTransport, TestRailClient, TestRailError, TestRailHTTPError, TestRailRequestBudgetExceeded
from .collection import (
    CaseMetadataCache,
    CollectionReport,
    DimensionState,
    DimensionValue,
    HistoricalCollector,
    ResolvedResult,
    RunDiscovery,
    SamplePlanSelection,
    enrich_feature_groups,
)
from .config import (
    ConfigurationError,
    CredentialError,
    Settings,
    credential_provider_from_settings,
    load_settings,
)
from .config.settings import CREDENTIAL_PROVIDER_TYPES
from .export import XlsxUnavailableError, build_report, render_spreadsheet, render_xlsx, xlsx_available
from .export.xlsx import INSTALL_HINT
from .models import StatusResolver

EXIT_OK: Final = 0
EXIT_UNEXPECTED: Final = 1
EXIT_INVALID_CONFIGURATION: Final = 2  # also used by argparse for invalid arguments
EXIT_PARTIAL: Final = 3
EXIT_API_ERROR: Final = 4
EXIT_NO_TARGET: Final = 5

SAMPLE_MAX_RESULTS: Final = 15
SAMPLE_MAX_REQUESTS: Final = 20
SAMPLE_MAX_CANDIDATES: Final = 5

PROG: Final = "testrail-stability-analyzer"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="Read-only TestRail stability analyzer.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    collect = commands.add_parser("collect", help="collect historical executions and print a summary")
    targets = collect.add_subparsers(dest="target", required=True, metavar="TARGET")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, help="TOML configuration file (keep it out of version control)")
    common.add_argument("--suite-id", type=_positive_int, help="override testrail.suite_id")
    common.add_argument("--credential-provider", choices=CREDENTIAL_PROVIDER_TYPES, help="credential source")
    common.add_argument("--keychain-service", help="macOS Keychain service name (not a secret)")
    common.add_argument("--keychain-account", help="macOS Keychain account name (not a secret)")
    common.add_argument("--max-results", type=_non_negative_int, help="result-record cap; 0 disables it")
    common.add_argument("--max-requests", type=_positive_int, help="maximum API requests, including retries")
    common.add_argument("--window-days", type=_positive_int, help="history window in days")
    common.add_argument("--lookback-days", type=_non_negative_int, help="lookback for results in days; 0 = no limit")
    common.add_argument(
        "--xml-report",
        type=Path,
        metavar="PATH",
        help="write an Excel-compatible XML Spreadsheet 2003 report (keep it out of version control)",
    )
    common.add_argument(
        "--xlsx-report",
        type=Path,
        metavar="PATH",
        help="write a native XLSX report; needs openpyxl (keep it out of version control)",
    )

    plan = targets.add_parser("plan", parents=[common], help="collect selected Test Plans")
    plan.add_argument("plan_ids", nargs="+", type=_positive_int, metavar="PLAN_ID")
    plan.add_argument("--run-id", dest="run_ids", action="append", type=_positive_int, default=[], metavar="RUN_ID")

    run = targets.add_parser("run", parents=[common], help="collect selected Test Runs")
    run.add_argument("run_ids", nargs="+", type=_positive_int, metavar="RUN_ID")
    run.add_argument("--plan-id", dest="plan_ids", action="append", type=_positive_int, default=[], metavar="PLAN_ID")

    sample = targets.add_parser(
        "sample",
        parents=[common],
        help="collect the newest eligible Test Plan "
        f"(defaults: --max-results {SAMPLE_MAX_RESULTS}, --max-requests {SAMPLE_MAX_REQUESTS})",
    )
    sample.add_argument(
        "--max-candidates",
        type=_positive_int,
        default=SAMPLE_MAX_CANDIDATES,
        help=f"maximum candidate plans to inspect (default {SAMPLE_MAX_CANDIDATES})",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    transport: HttpTransport | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            args = build_parser().parse_args(argv)
    except SystemExit as exc:  # argparse has printed usage or help
        return exc.code if isinstance(exc.code, int) else EXIT_INVALID_CONFIGURATION
    try:
        settings = load_settings(args.config, environ=environ, overrides=_overrides(args))
    except ConfigurationError as exc:
        err.write("invalid configuration:\n" + "".join(f"  - {problem}\n" for problem in exc.errors))
        return EXIT_INVALID_CONFIGURATION
    report_problem = _report_path_problem(args.xml_report) or _report_path_problem(args.xlsx_report)
    if _same_file(args.xml_report, args.xlsx_report):
        report_problem = "--xml-report and --xlsx-report must be different files"
    if report_problem is not None:
        err.write(f"invalid report path: {report_problem}\n")
        return EXIT_INVALID_CONFIGURATION
    if args.xlsx_report is not None and not xlsx_available():
        err.write(f"{INSTALL_HINT}\n")
        return EXIT_INVALID_CONFIGURATION
    try:
        return _collect(args, settings, out, err, environ, transport, clock)
    except CredentialError as exc:
        err.write(f"credential error: {exc}\n")
        return EXIT_API_ERROR
    except TestRailError as exc:
        # The exception message can contain untrusted server response text; print only local facts.
        status = f" (HTTP {exc.status})" if isinstance(exc, TestRailHTTPError) and isinstance(exc.status, int) else ""
        err.write(f"TestRail API error: {type(exc).__name__}{status}; response details are not shown\n")
        return EXIT_API_ERROR


def _collect(
    args: argparse.Namespace,
    settings: Settings,
    out: TextIO,
    err: TextIO,
    environ: Mapping[str, str] | None,
    transport: HttpTransport | None,
    clock: Callable[[], datetime],
) -> int:
    provider = credential_provider_from_settings(settings.credential_provider, environ=environ)
    client = TestRailClient(settings, provider, transport)
    now = clock()
    resolver = StatusResolver(client.get_statuses(), settings.mappings.statuses)
    plans = None
    sample = None
    if args.target == "sample":
        lookback_days = settings.history.lookback_limit_days
        lookback_start = None if lookback_days is None else now - timedelta(days=lookback_days)
        sample = RunDiscovery(client, settings).find_sample_plan(lookback_start, max_candidates=args.max_candidates)
        if sample.plan is None:
            out.write(_sample_lines(sample, client.requests_made))
            out.write("No eligible Test Plan found; nothing was collected.\n")
            return EXIT_PARTIAL if sample.budget_exhausted else EXIT_NO_TARGET
        collection = dataclasses.replace(settings.collection, plan_ids=(sample.plan.id,), run_ids=())
        settings = dataclasses.replace(settings, collection=collection)
        plans = {sample.plan.id: sample.plan}
    cache = CaseMetadataCache(client, settings.testrail.project_id)
    report = HistoricalCollector(client, settings, resolver, clock=lambda: now, plans=plans, case_cache=cache).collect()
    discovery = report.discovery
    if discovery.invalid_selections and not discovery.included:
        # No report is written: an empty one would look like a successful collection.
        if sample is not None:
            out.write(_sample_lines(sample, None))
        out.write(format_summary(report, settings))
        err.write("none of the selected plans or runs could be collected\n")
        return EXIT_API_ERROR
    outputs: list[tuple[str, Path, bytes]] = []
    if args.xml_report is not None or args.xlsx_report is not None:
        # Metadata is fetched once and shared by both formats.
        if settings.report.feature_group.enabled:
            report = enrich_feature_groups(report, client, settings, cache)
        notes: list[str] = []
        case_types = _case_types(client, notes)
        report = dataclasses.replace(report, requests_made=client.requests_made)
        analysis = analyze(report, settings)
        content = build_report(report, analysis, settings, case_types=case_types, notes=notes)
        if args.xml_report is not None:
            xml = render_spreadsheet(report, analysis, settings, content=content)
            outputs.append(("XML", args.xml_report, xml.encode("utf-8")))
        if args.xlsx_report is not None:
            try:
                outputs.append(("XLSX", args.xlsx_report, render_xlsx(report, analysis, settings, content=content)))
            except XlsxUnavailableError as exc:
                err.write(f"{exc}\n")
                return EXIT_INVALID_CONFIGURATION
    if sample is not None:
        out.write(_sample_lines(sample, None))
    out.write(format_summary(report, settings))
    for label, path, data in outputs:
        try:
            path.write_bytes(data)
        except OSError as exc:
            err.write(f"could not write the {label} report: {exc.strerror or type(exc).__name__}\n")
            return EXIT_UNEXPECTED
        out.write(f"{label} report written: {path}\n")
    return EXIT_PARTIAL if report.is_partial else EXIT_OK


def _report_path_problem(path: Path | None) -> str | None:
    """Checked before any request, so an unusable path does not waste the collection."""
    if path is None:
        return None
    if path.is_dir():
        return f"{path} is a directory"
    if not path.parent.is_dir():
        return f"directory {path.parent} does not exist"
    return None


def _same_file(first: Path | None, second: Path | None) -> bool:
    """True when both paths name one file, also via relative paths, symlinks, or hard links."""
    if first is None or second is None:
        return False
    if first.resolve() == second.resolve():
        return True
    try:
        return first.exists() and second.exists() and os.path.samefile(first, second)
    except OSError:
        return False


def _case_types(client: TestRailClient, notes: list[str]) -> dict[int, str] | None:
    """Case type names for the Test Type column; ``None`` (type IDs are shown) when they cannot be fetched."""
    try:
        return {case_type.id: case_type.name for case_type in client.get_case_types()}
    except TestRailRequestBudgetExceeded:
        notes.append("case types not fetched (request budget exhausted); Test Type shows TestRail type IDs")
    except TestRailError as exc:
        status = f" HTTP {exc.status}" if isinstance(exc, TestRailHTTPError) and isinstance(exc.status, int) else ""
        notes.append(f"case types could not be fetched ({type(exc).__name__}{status}); Test Type shows type IDs")
    return None


def _overrides(args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    overrides: dict[str, dict[str, Any]] = {}

    def put(section: str, key: str, value: Any) -> None:
        if value is not None:
            overrides.setdefault(section, {})[key] = value

    put("testrail", "suite_id", args.suite_id)
    put("credential_provider", "type", args.credential_provider)
    put("credential_provider", "keychain_service", args.keychain_service)
    put("credential_provider", "keychain_account", args.keychain_account)
    put("history", "window_days", args.window_days)
    put("history", "lookback_limit_days", args.lookback_days)
    sample = args.target == "sample"
    put("collection", "max_results", SAMPLE_MAX_RESULTS if sample and args.max_results is None else args.max_results)
    max_requests = SAMPLE_MAX_REQUESTS if sample and args.max_requests is None else args.max_requests
    put("http", "max_requests_per_execution", max_requests)
    # The command line selection replaces any selection in the configuration file.
    put("collection", "plan_ids", [] if sample else list(args.plan_ids))
    put("collection", "run_ids", [] if sample else list(args.run_ids))
    return overrides


# -- summary -------------------------------------------------------------------


def format_summary(report: CollectionReport, settings: Settings) -> str:
    """A sanitized, deterministic summary: counts, IDs, mapping names, and categories only."""
    discovery = report.discovery
    lines: list[str] = []
    add = lines.append
    mode = "targeted" if discovery.targeted else "project-wide"
    add(f"TestRail historical collection ({mode}, read-only)")
    add(f"Project ID: {settings.testrail.project_id}")
    if settings.testrail.suite_id is not None:
        add(f"Suite ID: {settings.testrail.suite_id}")
    if discovery.targeted:
        add(f"Selected plan IDs: {_ids(discovery.selected_plan_ids)}")
        add(f"Selected run IDs: {_ids(discovery.selected_run_ids)}")
    lookback = "none (no limit)" if report.lookback_start is None else f"{report.lookback_start:%Y-%m-%d}"
    add(f"Window start: {report.window_start:%Y-%m-%d}; lookback start: {lookback}")

    for invalid in discovery.invalid_selections:
        status = f" (HTTP {invalid.http_status})" if invalid.http_status is not None else ""
        add(f"Invalid selection: {invalid.kind} {invalid.id}: {invalid.reason}{status}")

    scans = {scan.run_id: scan for scan in report.scans}
    plan_ids = sorted({run.plan_id for run in discovery.runs if run.plan_id is not None})
    add(f"Plans inspected: {_ids(plan_ids)}")
    add(f"Runs inspected: {len(discovery.runs)} (included {len(discovery.included)})")
    for run in discovery.runs:
        parent = f"plan {run.plan_id}" if run.plan_id is not None else "standalone"
        c = run.classification
        add(
            f"  run {run.run_id} ({parent}): {run.decision}; application={_dimension(c.application)}, "
            f"platform={_dimension(c.platform)}, execution type={_dimension(c.execution_type)}"
        )
        scan = scans.get(run.run_id)
        if scan is not None:
            capped = ", capped" if scan.results_capped else ", skipped (cap reached)" if scan.skipped_by_cap else ""
            add(
                f"    results fetched: {_yes(scan.results_fetched)} (records: {scan.result_count}{capped}); "
                f"tests fetched: {_yes(scan.tests_fetched)}"
            )

    unresolved = [
        f"run {run.run_id} {label} {value.state}"
        for run in discovery.runs
        for label, value in (("application", run.classification.application), ("platform", run.classification.platform))
        if value.state is not DimensionState.RESOLVED
    ]
    add("Unresolved classification: " + ("; ".join(unresolved) if unresolved else "none"))

    outcomes = report.outcomes
    records: list[ResolvedResult] = [record for outcome in outcomes for record in outcome.results]
    records.extend(report.orphan_results)
    cap = report.max_results if report.max_results is not None else "none"
    add(f"Test instances examined: {len(outcomes)}")
    add(
        f"Result records fetched: {report.result_records_fetched} (cap: {cap}; cap reached: "
        f"{_yes(report.results_capped)})"
    )
    comment_only = sum(1 for record in records if record.is_comment_only)
    add(
        f"  status-bearing: {len(records) - comment_only}; comment-only: {comment_only}; "
        f"orphaned: {len(report.orphan_results)}"
    )
    add(f"  with comments: {sum(1 for r in records if _present(r.result.comment))}")
    add(f"  with linked defects: {sum(1 for r in records if _present(r.result.defects))}")
    with_final = [outcome for outcome in outcomes if outcome.final is not None]
    verified = sum(1 for outcome in with_final if outcome.is_verified_final)
    add(
        f"Final outcomes (one per test and run): {len(with_final)} "
        f"(verified: {verified}; from capped streams: {len(with_final) - verified}; "
        f"without a status-bearing record: {len(outcomes) - len(with_final)})"
    )
    retried = sum(1 for outcome in outcomes if len(outcome.results) > 1)
    add(f"  tests with retries or triage (more than one record in a run): {retried}")
    categories = Counter(str(o.category) if o.category is not None else "unknown" for o in with_final)
    add("Final status categories: " + _counts(categories))

    groups = Counter(
        f"{h.key.application.name} / {h.key.platform.name}" if h.key.is_identified else "unidentified (grouped per run)"
        for h in report.histories
    )
    add(f"Histories (case / application / platform): {len(report.histories)}")
    add("  groups: " + _counts(groups))
    add("  coverage: " + _counts(Counter(str(h.coverage) for h in report.histories)))
    add("  states: " + _counts(Counter(str(h.state) for h in report.histories)))

    if report.case_groups is not None:
        groups = report.case_groups.values()
        unknown = Counter(str(group.state) for group in groups if not group.is_resolved)
        resolved = sum(1 for group in groups if group.is_resolved)
        add(f"Feature / Test Groups: resolved={resolved}; unknown: {_counts(unknown)}")
    add(f"API requests: {report.requests_made} (budget: {settings.http.max_requests_per_execution})")
    add(f"Request budget exhausted: {_yes(report.budget_exhausted)}")
    if report.conflicts:
        add(f"Conflicting duplicate result records: {len(report.conflicts)}")
    add("Limitations:" if report.limitations else "Limitations: none")
    lines.extend(f"  - {note}" for note in report.limitations)
    add(f"Collection: {'PARTIAL' if report.is_partial else 'OK'}")
    return "\n".join(lines) + "\n"


def _sample_lines(sample: SamplePlanSelection, requests_made: int | None) -> str:
    chosen = f"plan {sample.plan.id}" if sample.plan is not None else "none"
    lines = [
        f"Sample selection: {chosen} (plans listed: {sample.plans_listed}; "
        f"inspected: {_ids(sample.inspected_plan_ids)})"
    ]
    lines.extend(f"  - {note}" for note in sample.notes)
    if requests_made is not None:
        lines.append(f"API requests: {requests_made}")
    return "\n".join(lines) + "\n"


def _dimension(value: DimensionValue) -> str:
    if value.state is DimensionState.RESOLVED:
        return f"{value.name}"
    if value.state is DimensionState.AMBIGUOUS:
        return f"ambiguous ({', '.join(value.candidates)})"
    return str(value.state)


def _counts(counter: Counter[str]) -> str:
    return ", ".join(f"{key}={counter[key]}" for key in sorted(counter)) or "none"


def _ids(values: Sequence[int]) -> str:
    return ", ".join(str(value) for value in values) or "none"


def _yes(flag: bool) -> str:
    return "yes" if flag else "no"


def _present(value: str | None) -> bool:
    return bool(value and value.strip())


def _positive_int(text: str) -> int:
    value = _int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer: {text!r}")
    return value


def _non_negative_int(text: str) -> int:
    value = _int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be a non-negative integer: {text!r}")
    return value


def _int(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None


def run() -> None:
    """Console-script entry point."""
    try:
        code = main()
    except KeyboardInterrupt:
        code = EXIT_UNEXPECTED
    sys.exit(code)


__all__ = [
    "EXIT_API_ERROR",
    "EXIT_INVALID_CONFIGURATION",
    "EXIT_NO_TARGET",
    "EXIT_OK",
    "EXIT_PARTIAL",
    "EXIT_UNEXPECTED",
    "build_parser",
    "format_summary",
    "main",
]
