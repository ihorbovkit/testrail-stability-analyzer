"""Typed, validated application settings.

Settings are resolved from (lowest to highest precedence): built-in defaults,
an optional TOML file, and ``TESTRAIL_*`` environment variables for non-secret
values. Credentials are never part of settings; see :mod:`.auth`.
"""

from __future__ import annotations

import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

DEFAULT_HISTORY_WINDOW_DAYS: Final = 60
DEFAULT_MAX_HISTORY_RUNS: Final = 10
# Conclusive executions below which analysis confidence is low and "stable" is not assigned.
DEFAULT_MIN_EXECUTIONS: Final = 5
# Extended lookback for the latest known result; 0 in configuration means no limit.
DEFAULT_LOOKBACK_LIMIT_DAYS: Final[int | None] = 365
DEFAULT_CONNECT_TIMEOUT_SECONDS: Final = 10.0
DEFAULT_READ_TIMEOUT_SECONDS: Final = 30.0
DEFAULT_MAX_REQUESTS_PER_EXECUTION: Final = 1000
DEFAULT_MAX_RETRIES: Final = 3
DEFAULT_RETRY_BACKOFF_SECONDS: Final = 1.0
DEFAULT_MAX_RETRY_DELAY_SECONDS: Final = 60.0
DEFAULT_FEATURE_GROUP_SEPARATOR: Final = "/"
CREDENTIAL_PROVIDER_TYPES: Final = ("environment", "keychain")

# Non-secret settings that may be overridden from the environment.
ENVIRONMENT_VARIABLES: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        "TESTRAIL_BASE_URL": ("testrail", "base_url"),
        "TESTRAIL_PROJECT_ID": ("testrail", "project_id"),
        "TESTRAIL_SUITE_ID": ("testrail", "suite_id"),
        "TESTRAIL_HISTORY_WINDOW_DAYS": ("history", "window_days"),
        "TESTRAIL_HISTORY_MAX_RUNS": ("history", "max_runs"),
        "TESTRAIL_HISTORY_LOOKBACK_LIMIT_DAYS": ("history", "lookback_limit_days"),
        "TESTRAIL_ANALYSIS_MIN_EXECUTIONS": ("analysis", "min_executions"),
        "TESTRAIL_HTTP_CONNECT_TIMEOUT_SECONDS": ("http", "connect_timeout_seconds"),
        "TESTRAIL_HTTP_READ_TIMEOUT_SECONDS": ("http", "read_timeout_seconds"),
        "TESTRAIL_HTTP_MAX_REQUESTS": ("http", "max_requests_per_execution"),
        "TESTRAIL_HTTP_MAX_RETRIES": ("http", "max_retries"),
        "TESTRAIL_HTTP_RETRY_BACKOFF_SECONDS": ("http", "retry_backoff_seconds"),
        "TESTRAIL_HTTP_MAX_RETRY_DELAY_SECONDS": ("http", "max_retry_delay_seconds"),
        "TESTRAIL_MAX_RESULTS": ("collection", "max_results"),
        "TESTRAIL_CREDENTIAL_PROVIDER": ("credential_provider", "type"),
        "TESTRAIL_KEYCHAIN_SERVICE": ("credential_provider", "keychain_service"),
        "TESTRAIL_KEYCHAIN_ACCOUNT": ("credential_provider", "keychain_account"),
    }
)

# Keys that indicate someone is trying to put credentials into the config file.
_CREDENTIAL_KEYS: Final = frozenset(
    {"api_key", "apikey", "password", "token", "secret", "username", "user", "auth", "authorization", "credentials"}
)
_IDENTIFIER_RE: Final = re.compile(r"^[a-z][a-z0-9_]*$")


class ConfigurationError(ValueError):
    """Raised when settings are missing or invalid. Lists every problem found."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("Invalid configuration:\n" + "\n".join(f"  - {e}" for e in errors))


class StatusCategory(StrEnum):
    """Analyzer-level outcome categories that TestRail statuses map onto."""

    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    NOT_RUN = "not_run"
    RETEST = "retest"
    NOT_APPLICABLE = "not_applicable"
    AUTOMATION_ISSUE = "automation_issue"
    # Assigned when no mapping applies; cannot be configured.
    UNKNOWN = "unknown"


class MatchSource(StrEnum):
    """TestRail attributes that application/platform patterns may match against."""

    PLAN_NAME = "plan_name"
    RUN_NAME = "run_name"
    CONFIGURATION = "configuration"


@dataclass(frozen=True, slots=True)
class TestRailSettings:
    base_url: str
    project_id: int
    suite_id: int | None = None
    allow_insecure_http: bool = False


@dataclass(frozen=True, slots=True)
class HistorySettings:
    window_days: int = DEFAULT_HISTORY_WINDOW_DAYS
    max_runs: int = DEFAULT_MAX_HISTORY_RUNS
    # Extended lookback (days) for the latest known result; None means no limit.
    # Results older than a configured limit will not be found.
    lookback_limit_days: int | None = DEFAULT_LOOKBACK_LIMIT_DAYS


@dataclass(frozen=True, slots=True)
class AnalysisSettings:
    # Conclusive executions (passed, failed, automation issue) needed for medium
    # confidence and for a "stable" diagnosis. Must not exceed history.max_runs.
    min_executions: int = DEFAULT_MIN_EXECUTIONS


@dataclass(frozen=True, slots=True)
class HttpSettings:
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS
    max_requests_per_execution: int = DEFAULT_MAX_REQUESTS_PER_EXECUTION
    # Retries for rate limits (429), temporary server errors (502/503/504) and
    # network failures. Every attempt counts against max_requests_per_execution.
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS
    max_retry_delay_seconds: float = DEFAULT_MAX_RETRY_DELAY_SECONDS


@dataclass(frozen=True, slots=True)
class DimensionMapping:
    """Maps TestRail plan/run/configuration names onto a named application or platform."""

    name: str
    patterns: tuple[re.Pattern[str], ...]
    sources: tuple[MatchSource, ...] = tuple(MatchSource)
    # Applications only: values of the ``applicability`` case field (raw IDs or
    # strings) that mark a case as applicable to this application.
    applicability_values: tuple[str | int, ...] = ()


@dataclass(frozen=True, slots=True)
class NamingSettings:
    """Optional include filters for Test Plan and Test Run names."""

    plan_name_patterns: tuple[re.Pattern[str], ...] = ()
    run_name_patterns: tuple[re.Pattern[str], ...] = ()


@dataclass(frozen=True, slots=True)
class FilterSettings:
    """Optional include filters by mapped name; empty tuples include everything."""

    applications: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    execution_types: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.applications or self.platforms or self.execution_types)


@dataclass(frozen=True, slots=True)
class MappingSettings:
    applications: tuple[DimensionMapping, ...] = ()
    platforms: tuple[DimensionMapping, ...] = ()
    # Logical field name -> TestRail field system name.
    custom_fields: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    # Category -> TestRail status system names or numeric IDs.
    statuses: Mapping[StatusCategory, tuple[str | int, ...]] = field(default_factory=lambda: MappingProxyType({}))
    # Appended last to keep the positional order of the fields above.
    execution_types: tuple[DimensionMapping, ...] = ()


@dataclass(frozen=True, slots=True)
class CollectionSettings:
    """Optional targeted selection and result-record cap.

    With no plan or run IDs, runs are discovered project-wide. ``max_results``
    caps the result records retrieved per execution (``None``: no cap); it is a
    validation aid, separate from the history window and ``history.max_runs``.
    """

    plan_ids: tuple[int, ...] = ()
    run_ids: tuple[int, ...] = ()
    max_results: int | None = None

    @property
    def targeted(self) -> bool:
        return bool(self.plan_ids or self.run_ids)


@dataclass(frozen=True, slots=True)
class FeatureGroupSettings:
    """Feature / Test Group of each case, reconstructed from its TestRail section hierarchy.

    Section metadata is only fetched when a report is generated and ``enabled``
    is true; collection on its own never fetches it.
    """

    enabled: bool = True
    separator: str = DEFAULT_FEATURE_GROUP_SEPARATOR
    # Section levels shown in the label, from the top; 0 shows the full hierarchy.
    max_depth: int = 0
    include_suite_name: bool = False


@dataclass(frozen=True, slots=True)
class ReportSettings:
    feature_group: FeatureGroupSettings = FeatureGroupSettings()


@dataclass(frozen=True, slots=True)
class CredentialProviderSettings:
    """Which credential source to use. Holds names of secrets' locations, never secret values."""

    type: str = "environment"
    keychain_service: str | None = None
    keychain_account: str | None = None
    # Optional environment variable holding the username; defaults to the Keychain account.
    username_variable: str | None = None


@dataclass(frozen=True, slots=True)
class Settings:
    testrail: TestRailSettings
    history: HistorySettings = HistorySettings()
    http: HttpSettings = HttpSettings()
    naming: NamingSettings = NamingSettings()
    mappings: MappingSettings = MappingSettings()
    filters: FilterSettings = FilterSettings()
    collection: CollectionSettings = CollectionSettings()
    credential_provider: CredentialProviderSettings = CredentialProviderSettings()
    analysis: AnalysisSettings = AnalysisSettings()
    report: ReportSettings = ReportSettings()


def load_settings(
    config_path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> Settings:
    """Load settings from an optional TOML file, then apply environment overrides.

    ``overrides`` (section -> key -> value, e.g. from command-line options) are
    applied last and validated like the file contents.
    """
    data: dict[str, Any] = {}
    if config_path is not None:
        path = Path(config_path)
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except FileNotFoundError:
            raise ConfigurationError([f"configuration file not found: {path}"]) from None
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationError([f"configuration file {path} is not valid TOML: {exc}"]) from None
    merged = _apply_environment(data, os.environ if environ is None else environ)
    for section, values in (overrides or {}).items():
        target = merged.setdefault(section, {})
        if isinstance(target, dict):
            target.update(values)
    return settings_from_mapping(merged)


def settings_from_mapping(data: Mapping[str, Any]) -> Settings:
    """Validate a raw mapping (e.g. parsed TOML) and build :class:`Settings`."""
    return _Parser().parse(data)


def _apply_environment(data: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, Any]:
    merged = {key: dict(value) if isinstance(value, Mapping) else value for key, value in data.items()}
    for variable, (section, key) in ENVIRONMENT_VARIABLES.items():
        value = environ.get(variable)
        if value is None or not value.strip():
            continue
        target = merged.setdefault(section, {})
        if isinstance(target, dict):
            target[key] = value.strip()
    return merged


def _join(path: str, key: object) -> str:
    return f"{path}.{key}" if path else str(key)


class _Parser:
    """Collects every validation error so users can fix them in one pass."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def parse(self, data: Mapping[str, Any]) -> Settings:
        if not isinstance(data, Mapping):
            raise ConfigurationError(["configuration root must be a table"])
        self._check_keys(
            data,
            "",
            {
                "testrail",
                "history",
                "http",
                "naming",
                "mappings",
                "filters",
                "collection",
                "credential_provider",
                "analysis",
                "report",
            },
        )

        testrail = self._section(data, "testrail", {"base_url", "project_id", "suite_id", "allow_insecure_http"})
        allow_http = self._boolean(testrail, "allow_insecure_http", "testrail", default=False)
        base_url = self._base_url(testrail, allow_http)
        project_id = self._integer(testrail, "project_id", "testrail", default=None, minimum=1, required=True)
        suite_id = self._integer(testrail, "suite_id", "testrail", default=None, minimum=1)

        history = self._section(data, "history", {"window_days", "max_runs", "lookback_limit_days"})
        window_days = self._integer(history, "window_days", "history", default=DEFAULT_HISTORY_WINDOW_DAYS, minimum=1)
        max_runs = self._integer(history, "max_runs", "history", default=DEFAULT_MAX_HISTORY_RUNS, minimum=1)
        lookback = self._integer(
            history, "lookback_limit_days", "history", default=DEFAULT_LOOKBACK_LIMIT_DAYS, minimum=0
        )
        lookback = lookback or None  # 0 disables the limit.
        if lookback is not None and window_days is not None and lookback < window_days:
            self.errors.append("history.lookback_limit_days must be 0 (no limit) or >= history.window_days")

        analysis = self._section(data, "analysis", {"min_executions"})
        min_executions = self._integer(analysis, "min_executions", "analysis", default=None, minimum=1)
        if min_executions is None:
            # Unset: the default, but never above a smaller history.max_runs.
            min_executions = min(DEFAULT_MIN_EXECUTIONS, max_runs or DEFAULT_MAX_HISTORY_RUNS)
        elif max_runs is not None and min_executions > max_runs:
            self.errors.append("analysis.min_executions must be <= history.max_runs")

        http = self._section(
            data,
            "http",
            {
                "connect_timeout_seconds",
                "read_timeout_seconds",
                "max_requests_per_execution",
                "max_retries",
                "retry_backoff_seconds",
                "max_retry_delay_seconds",
            },
        )
        connect_timeout = self._positive_number(http, "connect_timeout_seconds", "http", DEFAULT_CONNECT_TIMEOUT_SECONDS)
        read_timeout = self._positive_number(http, "read_timeout_seconds", "http", DEFAULT_READ_TIMEOUT_SECONDS)
        max_requests = self._integer(
            http, "max_requests_per_execution", "http", default=DEFAULT_MAX_REQUESTS_PER_EXECUTION, minimum=1
        )
        max_retries = self._integer(http, "max_retries", "http", default=DEFAULT_MAX_RETRIES, minimum=0)
        backoff = self._positive_number(http, "retry_backoff_seconds", "http", DEFAULT_RETRY_BACKOFF_SECONDS)
        max_delay = self._positive_number(http, "max_retry_delay_seconds", "http", DEFAULT_MAX_RETRY_DELAY_SECONDS)
        if max_delay < backoff:
            self.errors.append("http.max_retry_delay_seconds must be >= http.retry_backoff_seconds")

        naming = self._section(data, "naming", {"plan_name_patterns", "run_name_patterns"})
        plan_patterns = self._patterns(naming, "plan_name_patterns", "naming")
        run_patterns = self._patterns(naming, "run_name_patterns", "naming")

        mappings = self._section(
            data, "mappings", {"applications", "platforms", "execution_types", "custom_fields", "statuses"}
        )
        applications = self._dimensions(mappings, "applications")
        platforms = self._dimensions(mappings, "platforms")
        execution_types = self._dimensions(mappings, "execution_types")
        custom_fields = self._custom_fields(mappings)
        statuses = self._statuses(mappings)
        if any(app.applicability_values for app in applications) and "applicability" not in custom_fields:
            self.errors.append(
                "mappings.applications[].applicability_values requires mappings.custom_fields.applicability"
            )

        filters = self._section(data, "filters", {"applications", "platforms", "execution_types"})
        filter_values = {
            key: self._filter(filters, key, defined)
            for key, defined in (
                ("applications", applications),
                ("platforms", platforms),
                ("execution_types", execution_types),
            )
        }

        collection = self._collection(data)
        credential_provider = self._credential_provider(data)
        report = self._report(data)

        if self.errors:
            raise ConfigurationError(self.errors)
        assert base_url is not None and project_id is not None  # guaranteed when no errors
        return Settings(
            testrail=TestRailSettings(base_url, project_id, suite_id, allow_http),
            history=HistorySettings(window_days, max_runs, lookback),
            http=HttpSettings(connect_timeout, read_timeout, max_requests, max_retries, backoff, max_delay),
            naming=NamingSettings(plan_patterns, run_patterns),
            mappings=MappingSettings(
                applications=applications,
                platforms=platforms,
                custom_fields=custom_fields,
                statuses=statuses,
                execution_types=execution_types,
            ),
            filters=FilterSettings(**filter_values),
            collection=collection,
            credential_provider=credential_provider,
            analysis=AnalysisSettings(min_executions),
            report=report,
        )

    # -- collection and credential provider ---------------------------------

    def _collection(self, data: Mapping[str, Any]) -> CollectionSettings:
        section = self._section(data, "collection", {"plan_ids", "run_ids", "max_results"})
        max_results = self._integer(section, "max_results", "collection", default=0, minimum=0)
        return CollectionSettings(
            plan_ids=self._id_list(section, "plan_ids", "collection"),
            run_ids=self._id_list(section, "run_ids", "collection"),
            max_results=max_results or None,  # 0 means no cap.
        )

    def _report(self, data: Mapping[str, Any]) -> ReportSettings:
        report = self._section(data, "report", {"feature_group"})
        path = "report.feature_group"
        group = report.get("feature_group", {})
        if not isinstance(group, Mapping):
            self.errors.append(f"{path} must be a table")
            group = {}
        self._check_keys(group, path, {"enabled", "separator", "max_depth", "include_suite_name"})
        separator = group.get("separator", DEFAULT_FEATURE_GROUP_SEPARATOR)
        # Not stripped: a separator such as " / " is kept as written.
        if not isinstance(separator, str) or not separator or any(ord(char) < 32 for char in separator):
            self.errors.append(f"{path}.separator must be a non-empty string without control characters")
            separator = DEFAULT_FEATURE_GROUP_SEPARATOR
        max_depth = self._integer(group, "max_depth", path, default=0, minimum=0)
        return ReportSettings(
            FeatureGroupSettings(
                enabled=self._boolean(group, "enabled", path, default=True),
                separator=separator,
                max_depth=max_depth if max_depth is not None else 0,
                include_suite_name=self._boolean(group, "include_suite_name", path, default=False),
            )
        )

    def _credential_provider(self, data: Mapping[str, Any]) -> CredentialProviderSettings:
        path = "credential_provider"
        section = self._section(data, path, {"type", "keychain_service", "keychain_account", "username_variable"})
        provider = self._string(section, "type", path) or "environment"
        if provider not in CREDENTIAL_PROVIDER_TYPES:
            self.errors.append(f"{path}.type must be one of: {', '.join(CREDENTIAL_PROVIDER_TYPES)}")
        service = self._string(section, "keychain_service", path)
        account = self._string(section, "keychain_account", path)
        username_variable = self._string(section, "username_variable", path)
        if provider == "keychain":
            for key, value in (("keychain_service", service), ("keychain_account", account)):
                if value is None and key not in section:
                    self.errors.append(f"{path}.{key} is required when {path}.type is 'keychain'")
        return CredentialProviderSettings(provider, service, account, username_variable)

    def _id_list(self, data: Mapping[str, Any], key: str, path: str) -> tuple[int, ...]:
        values = data.get(key, [])
        valid = isinstance(values, list | tuple) and all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 1 for value in values
        )
        if not valid:
            self.errors.append(f"{_join(path, key)} must be a list of positive integer IDs")
            return ()
        return tuple(dict.fromkeys(values))  # duplicates removed, order kept

    # -- structure ---------------------------------------------------------

    def _check_keys(self, data: Mapping[str, Any], path: str, allowed: set[str]) -> None:
        for key in data:
            if str(key).lower() in _CREDENTIAL_KEYS:
                self.errors.append(
                    f"{_join(path, key)}: credentials must not be stored in configuration; "
                    "provide them through a credential provider (e.g. TESTRAIL_USERNAME / TESTRAIL_API_KEY)"
                )
            elif key not in allowed:
                self.errors.append(f"{_join(path, key)}: unknown setting")

    def _section(self, data: Mapping[str, Any], key: str, allowed: set[str]) -> Mapping[str, Any]:
        value = data.get(key, {})
        if not isinstance(value, Mapping):
            self.errors.append(f"{key} must be a table")
            return {}
        self._check_keys(value, key, allowed)
        return value

    # -- scalars -----------------------------------------------------------

    def _string(self, data: Mapping[str, Any], key: str, path: str, *, required: bool = False) -> str | None:
        if key not in data:
            if required:
                self.errors.append(f"{_join(path, key)} is required")
            return None
        value = data[key]
        if not isinstance(value, str) or not value.strip():
            self.errors.append(f"{_join(path, key)} must be a non-empty string")
            return None
        return value.strip()

    def _boolean(self, data: Mapping[str, Any], key: str, path: str, *, default: bool) -> bool:
        value = data.get(key, default)
        if not isinstance(value, bool):
            self.errors.append(f"{_join(path, key)} must be true or false")
            return default
        return value

    def _integer(
        self, data: Mapping[str, Any], key: str, path: str, *, default: int | None, minimum: int, required: bool = False
    ) -> int | None:
        if key not in data:
            if required:
                self.errors.append(f"{_join(path, key)} is required")
            return default
        value = data[key]
        if isinstance(value, str):
            try:
                value = int(value.strip())
            except ValueError:
                pass
        if isinstance(value, bool) or not isinstance(value, int):
            self.errors.append(f"{_join(path, key)} must be an integer")
            return default
        if value < minimum:
            self.errors.append(f"{_join(path, key)} must be >= {minimum}")
            return default
        return value

    def _positive_number(self, data: Mapping[str, Any], key: str, path: str, default: float) -> float:
        value = data.get(key, default)
        if isinstance(value, str):
            try:
                value = float(value.strip())
            except ValueError:
                pass
        if isinstance(value, bool) or not isinstance(value, int | float) or not (value > 0 and math.isfinite(value)):
            self.errors.append(f"{_join(path, key)} must be a finite positive number")
            return default
        return float(value)

    def _base_url(self, data: Mapping[str, Any], allow_http: bool) -> str | None:
        raw = self._string(data, "base_url", "testrail", required=True)
        if raw is None:
            return None
        # Never echo the raw URL: it could contain embedded credentials.
        try:
            parts = urlsplit(raw)
            parts.port  # noqa: B018 - validates the port
        except ValueError:
            self.errors.append("testrail.base_url is not a valid URL")
            return None
        schemes = {"https", "http"} if allow_http else {"https"}
        problems = []
        if parts.scheme not in schemes:
            problems.append("must use https" if not allow_http else "must use http or https")
        if not parts.hostname:
            problems.append("must include a host")
        if parts.username or parts.password:
            problems.append("must not contain credentials")
        if parts.query or parts.fragment:
            problems.append("must not contain a query string or fragment")
        if problems:
            self.errors.extend(f"testrail.base_url {problem}" for problem in problems)
            return None
        return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))

    # -- collections -------------------------------------------------------

    def _string_list(self, data: Mapping[str, Any], key: str, path: str) -> list[str]:
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            self.errors.append(f"{_join(path, key)} must be a list of non-empty strings")
            return []
        return value

    def _patterns(self, data: Mapping[str, Any], key: str, path: str) -> tuple[re.Pattern[str], ...]:
        compiled = []
        for index, pattern in enumerate(self._string_list(data, key, path)):
            try:
                compiled.append(re.compile(pattern))
            except re.error as exc:
                self.errors.append(f"{_join(path, key)}[{index}] is not a valid regular expression: {exc}")
        return tuple(compiled)

    def _dimensions(self, data: Mapping[str, Any], key: str) -> tuple[DimensionMapping, ...]:
        path = _join("mappings", key)
        entries = data.get(key, [])
        if not isinstance(entries, list):
            self.errors.append(f"{path} must be a list of tables")
            return ()
        result: list[DimensionMapping] = []
        seen: set[str] = set()
        for index, entry in enumerate(entries):
            entry_path = f"{path}[{index}]"
            if not isinstance(entry, Mapping):
                self.errors.append(f"{entry_path} must be a table")
                continue
            allowed = {"name", "patterns", "sources"} | ({"applicability_values"} if key == "applications" else set())
            self._check_keys(entry, entry_path, allowed)
            name = self._string(entry, "name", entry_path, required=True)
            if name is not None and name in seen:
                self.errors.append(f"{entry_path}.name '{name}' is duplicated")
            patterns = self._patterns(entry, "patterns", entry_path)
            if "patterns" not in entry:
                self.errors.append(f"{entry_path}.patterns is required")
            elif entry["patterns"] == []:
                self.errors.append(f"{entry_path}.patterns must not be empty")
            sources = self._sources(entry, entry_path)
            applicability = self._applicability_values(entry, entry_path)
            if name is not None:
                seen.add(name)
                result.append(DimensionMapping(name, patterns, sources, applicability_values=applicability))
        return tuple(result)

    def _applicability_values(self, entry: Mapping[str, Any], path: str) -> tuple[str | int, ...]:
        values = entry.get("applicability_values", [])
        valid = isinstance(values, list) and all(
            (isinstance(v, str) and v.strip()) or (isinstance(v, int) and not isinstance(v, bool)) for v in values
        )
        if not valid:
            self.errors.append(f"{path}.applicability_values must be a list of non-empty strings or integers")
            return ()
        return tuple(v.strip() if isinstance(v, str) else v for v in values)

    def _filter(self, data: Mapping[str, Any], key: str, defined: tuple[DimensionMapping, ...]) -> tuple[str, ...]:
        names = self._string_list(data, key, "filters")
        known = {mapping.name for mapping in defined}
        for name in names:
            if name not in known:
                self.errors.append(f"filters.{key}: '{name}' is not defined in mappings.{key}")
        return tuple(names)

    def _sources(self, entry: Mapping[str, Any], path: str) -> tuple[MatchSource, ...]:
        if "sources" not in entry:
            return tuple(MatchSource)
        sources = []
        for value in self._string_list(entry, "sources", path):
            try:
                sources.append(MatchSource(value))
            except ValueError:
                allowed = ", ".join(source.value for source in MatchSource)
                self.errors.append(f"{path}.sources: unknown source '{value}' (allowed: {allowed})")
        return tuple(sources)

    def _custom_fields(self, data: Mapping[str, Any]) -> Mapping[str, str]:
        value = data.get("custom_fields", {})
        if not isinstance(value, Mapping):
            self.errors.append("mappings.custom_fields must be a table")
            return MappingProxyType({})
        result = {}
        for logical, system_name in value.items():
            if str(logical).lower() in _CREDENTIAL_KEYS:
                self.errors.append(
                    f"mappings.custom_fields.{logical}: credentials must not be stored in configuration; "
                    "provide them through a credential provider (e.g. TESTRAIL_USERNAME / TESTRAIL_API_KEY)"
                )
            elif not _IDENTIFIER_RE.match(str(logical)):
                self.errors.append(f"mappings.custom_fields.{logical}: name must be lowercase snake_case")
            elif not isinstance(system_name, str) or not _IDENTIFIER_RE.match(system_name):
                self.errors.append(f"mappings.custom_fields.{logical} must be a TestRail field system name")
            else:
                result[str(logical)] = system_name
        return MappingProxyType(result)

    def _statuses(self, data: Mapping[str, Any]) -> Mapping[StatusCategory, tuple[str | int, ...]]:
        value = data.get("statuses", {})
        if not isinstance(value, Mapping):
            self.errors.append("mappings.statuses must be a table")
            return MappingProxyType({})
        result: dict[StatusCategory, tuple[str | int, ...]] = {}
        owners: dict[str | int, StatusCategory] = {}
        for raw_category, identifiers in value.items():
            path = f"mappings.statuses.{raw_category}"
            try:
                category = StatusCategory(raw_category)
            except ValueError:
                category = StatusCategory.UNKNOWN
            if category is StatusCategory.UNKNOWN:
                allowed = ", ".join(c.value for c in StatusCategory if c is not StatusCategory.UNKNOWN)
                self.errors.append(f"{path}: unknown status category (allowed: {allowed})")
                continue
            if not isinstance(identifiers, list):
                self.errors.append(f"{path} must be a list of status system names or IDs")
                continue
            accepted: list[str | int] = []
            for identifier in identifiers:
                if isinstance(identifier, str) and identifier.strip():
                    identifier = identifier.strip()
                elif isinstance(identifier, bool) or not isinstance(identifier, int) or identifier < 1:
                    self.errors.append(f"{path}: {identifier!r} is not a status system name or positive ID")
                    continue
                if identifier in owners:
                    self.errors.append(f"{path}: status {identifier!r} is already mapped to '{owners[identifier]}'")
                    continue
                owners[identifier] = category
                accepted.append(identifier)
            result[category] = tuple(accepted)
        return MappingProxyType(result)
