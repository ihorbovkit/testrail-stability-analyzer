# testrail-stability-analyzer

A reusable, read-only Python application for analyzing test stability in any
TestRail instance. It is not tied to a specific customer, project, automation
framework, or TestRail configuration.

> **Status: Milestone 6.1 (XLSX and XML reports).** Configuration,
> credential handling (environment variables or macOS Keychain), a read-only
> API client, status resolution, a historical execution collector with
> targeted plan/run selection and a result-record cap, stability analysis,
> a command-line interface, and native XLSX and Excel-compatible XML
> Spreadsheet 2003 reports with section-based Feature / Test Groups are
> implemented. JSON export is **not** implemented yet. See
> [Implementation status](#implementation-status).

## Purpose

The goal is to connect to TestRail API v2 (read-only), collect execution history
for test cases, and report:

- the latest known result for each test,
- stability and flakiness per test case, application, and platform,
- automation-maintenance issues separately from application defects,
- explicit "insufficient data" results when history is too short,

exported as XML, JSON, and Excel.

## Requirements

- Python 3.13 (`>=3.13,<4.0`)
- [Poetry](https://python-poetry.org/) 2.x for dependency management (optional
  for running unit tests)

Collection, analysis, and XML reports use **only the standard library**.
Native XLSX reports need the optional `openpyxl` package (extra `xlsx`).
`pytest` and `openpyxl` are declared as development dependencies.

## Installation

```bash
# Creates a project-local .venv/ (see poetry.toml) and installs dev dependencies.
poetry install
# Runtime only, with XLSX support:
poetry install --only main --extras xlsx
# Or into an existing virtual environment:
pip install "openpyxl>=3.1,<4"
```

`openpyxl` is imported only when an XLSX report is written. Without it,
`--xlsx-report` stops with an installation hint before any TestRail request;
everything else works unchanged.

## Running unit tests

Unit tests use mocked HTTP responses. They need no credentials, network access,
or TestRail instance.

With the standard library only (no installation needed):

```bash
PYTHONPATH=src python3.13 -m unittest discover -s tests -t .
```

With pytest (after `poetry install`):

```bash
poetry run pytest
```

Tests marked `live` (none exist yet) are excluded by default and will only run
when explicitly selected with `-m live` and credentials are provided.

## Configuration

Settings are resolved in this order (later wins):

1. Built-in defaults
2. An optional TOML file (see [`config/config.example.toml`](config/config.example.toml))
3. `TESTRAIL_*` environment variables for non-secret values

Keep instance-specific files out of version control, e.g. in
`config/local/config.toml` (git-ignored).

```python
from testrail_stability_analyzer.config import load_settings

settings = load_settings("config/local/config.toml")
```

### Settings

| Setting | Default | Environment variable |
|---|---|---|
| `testrail.base_url` (https required) | required | `TESTRAIL_BASE_URL` |
| `testrail.project_id` | required | `TESTRAIL_PROJECT_ID` |
| `testrail.suite_id` (multi-suite projects) | none | `TESTRAIL_SUITE_ID` |
| `testrail.allow_insecure_http` | `false` | — |
| `history.window_days` | `60` | `TESTRAIL_HISTORY_WINDOW_DAYS` |
| `history.max_runs` (distinct runs per case/application/platform) | `10` | `TESTRAIL_HISTORY_MAX_RUNS` |
| `history.lookback_limit_days` (extended lookback for the latest known result; `0` = no limit) | `365` | `TESTRAIL_HISTORY_LOOKBACK_LIMIT_DAYS` |
| `analysis.min_executions` (conclusive executions for medium confidence and `stable`; ≤ `max_runs`) | `5` | `TESTRAIL_ANALYSIS_MIN_EXECUTIONS` |
| `http.connect_timeout_seconds` | `10.0` | `TESTRAIL_HTTP_CONNECT_TIMEOUT_SECONDS` |
| `http.read_timeout_seconds` | `30.0` | `TESTRAIL_HTTP_READ_TIMEOUT_SECONDS` |
| `http.max_requests_per_execution` (includes retries) | `1000` | `TESTRAIL_HTTP_MAX_REQUESTS` |
| `http.max_retries` (`0` disables retries) | `3` | `TESTRAIL_HTTP_MAX_RETRIES` |
| `http.retry_backoff_seconds` | `1.0` | `TESTRAIL_HTTP_RETRY_BACKOFF_SECONDS` |
| `http.max_retry_delay_seconds` (≥ backoff) | `60.0` | `TESTRAIL_HTTP_MAX_RETRY_DELAY_SECONDS` |
| `naming.plan_name_patterns` / `naming.run_name_patterns` | `[]` (include all) | — |
| `mappings.applications` / `mappings.platforms` / `mappings.execution_types` | `[]` | — |
| `mappings.applications[].applicability_values` | `[]` | — |
| `filters.applications` / `filters.platforms` / `filters.execution_types` | `[]` (include all) | — |
| `mappings.custom_fields` (logical names: `application_version`, `applicability`, `class_name`) | `{}` | — |
| `mappings.statuses` | `{}` | — |
| `collection.plan_ids` / `collection.run_ids` (targeted collection) | `[]` (project-wide) | — |
| `collection.max_results` (result-record cap; `0` = no cap) | `0` | `TESTRAIL_MAX_RESULTS` |
| `credential_provider.type` (`environment` or `keychain`) | `environment` | `TESTRAIL_CREDENTIAL_PROVIDER` |
| `credential_provider.keychain_service` / `keychain_account` | none (required for `keychain`) | `TESTRAIL_KEYCHAIN_SERVICE` / `TESTRAIL_KEYCHAIN_ACCOUNT` |
| `credential_provider.username_variable` (environment variable holding the username) | none | — |
| `report.feature_group.enabled` (fetch section metadata when a report is generated) | `true` | — |
| `report.feature_group.separator` | `"/"` | — |
| `report.feature_group.max_depth` (section levels from the top; `0` = full hierarchy) | `0` | — |
| `report.feature_group.include_suite_name` (one extra `get_suites` request) | `false` | — |
| `report.blocked_reason.enabled` (Blocked Reason column and Overview note line) | `true` | — |
| `report.blocked_reason.section_headers` (comment section headers recognized at the start of a line, case-insensitive) | `["BLOCKED REASON", "SKIPPED REASON"]` | — |
| `report.blocked_reason.max_length` (1–1000 characters) | `160` | — |
| `report.applicability.all_label` (Applicability label when every configured application matches, e.g. `"Both"`; requires `mappings.custom_fields.applicability`) | none (application names are listed) | — |

### Configuration principles

- **No assumptions about your instance.** Application and platform names are
  free-form and matched by regular expressions against plan names, run names,
  or configuration names. Projects may use Test Plans or standalone Test Runs,
  single or multiple suites.
- **Custom statuses are optional.** `mappings.statuses` maps analyzer
  categories (`passed`, `failed`, `blocked`, `not_run`, `retest`,
  `not_applicable`, `automation_issue`) to TestRail status system names or IDs.
  Statuses that match no rule are reported as `unknown` (see
  [Status resolution](#status-resolution)); `unknown` itself cannot be mapped.
- **The latest known result is searched for beyond the history window**, up
  to `history.lookback_limit_days` (default 365). `0` removes the limit, at
  the cost of scanning the whole project history.
- **Validation reports every problem at once**, with the setting path, e.g.
  `history.window_days must be >= 1`. Unknown keys are rejected to catch typos.

## Using the API client

```python
from datetime import UTC, datetime

from testrail_stability_analyzer.api import TestRailClient
from testrail_stability_analyzer.config import EnvironmentCredentialProvider, load_settings
from testrail_stability_analyzer.models import StatusResolver

settings = load_settings("config/local/config.toml")
client = TestRailClient(settings, EnvironmentCredentialProvider())

resolver = StatusResolver(client.get_statuses(), settings.mappings.statuses)
since = datetime(2024, 1, 1, tzinfo=UTC)
for run in client.get_runs(settings.testrail.project_id, created_after=since, include_plan_runs=True):
    for result in client.get_results_for_run(run.id):
        status = resolver.resolve(result.status_id) if result.status_id is not None else None
```

### Authentication

TestRail API access must be enabled for the instance (Administration → Site
Settings → API). The client sends HTTP Basic authentication with the
username (email) and an API key (or password) obtained from the configured
`CredentialProvider`. Credentials are fetched lazily on the first request.

### Credential providers

Select the source in `[credential_provider]` (or with `--credential-provider`).
Only the *location* of credentials is configured, never their values.

| `type` | Username | API key |
|---|---|---|
| `environment` (default) | `TESTRAIL_USERNAME` (or `username_variable`) | `TESTRAIL_API_KEY` |
| `keychain` (macOS only) | the Keychain account (or `username_variable`) | password of the generic Keychain item |

```python
from testrail_stability_analyzer.config import credential_provider_from_settings

provider = credential_provider_from_settings(settings.credential_provider)
```

**macOS Keychain prerequisites**

- macOS with the system `/usr/bin/security` tool.
- A *generic password* item whose service and account names you configure
  locally (no defaults exist), e.g. created once with Keychain Access, or
  `security add-generic-password -s <service> -a <account> -w` (prompts for
  the key; avoid passing it as an argument).
- The analyzer only reads the item (`security find-generic-password … -w`);
  macOS may ask once to allow access. A missing item (exit status 44), a
  denied or cancelled prompt, or another platform raises `CredentialError`
  without echoing tool output. A configured `username_variable` is checked
  before the Keychain is read, so a missing username never triggers a prompt.

### Endpoints

All methods issue HTTP GET only and return typed models (`testrail_stability_analyzer.models`).

| Method | TestRail endpoint | Returns | Filters |
|---|---|---|---|
| `get_project(project_id)` | `get_project` | `Project` | — |
| `get_suites(project_id)` | `get_suites` | `list[Suite]` | — |
| `get_sections(project_id, ...)` | `get_sections` | `Iterator[Section]` | `suite_id` |
| `get_cases(project_id, ...)` | `get_cases` | `Iterator[Case]` | `suite_id`, `section_id`, `created_after/before/by`, `updated_after/before/by`, `milestone_id`, `priority_id`, `template_id`, `type_id` |
| `get_case_fields()` | `get_case_fields` | `list[CustomField]` | — |
| `get_case_types()` | `get_case_types` | `list[CaseType]` | — |
| `get_statuses()` | `get_statuses` | `list[Status]` | — |
| `get_result_fields()` | `get_result_fields` | `list[CustomField]` | — |
| `get_plans(project_id, ...)` | `get_plans` | `Iterator[Plan]` (no entries) | `created_after/before/by`, `is_completed`, `milestone_id` |
| `get_plan(plan_id)` | `get_plan` | `Plan` with entries and runs | — |
| `get_runs(project_id, ...)` | `get_runs` (+ `get_plans`/`get_plan`) | `Iterator[Run]` | `created_after/before/by`, `is_completed`, `milestone_id`, `suite_id`, `include_plan_runs` |
| `get_run(run_id)` | `get_run` | `Run` | — |
| `get_tests(run_id, ...)` | `get_tests` | `Iterator[Test]` | `status_id` |
| `get_results_for_run(run_id, ...)` | `get_results_for_run` | `Iterator[Result]` | `created_after/before/by`, `status_id` |
| `get_results_for_case(run_id, case_id, ...)` | `get_results_for_case` | `Iterator[Result]` | `status_id` |

- Date filters accept a timezone-aware `datetime` or a Unix timestamp; naive
  datetimes are rejected. ID filters accept one ID or a collection of IDs.
- Only filters documented by TestRail are sent. Invalid arguments raise
  `ValueError` before any request is made.
- TestRail's `get_runs` returns only standalone runs. `include_plan_runs=True`
  is a **client-side** option (not an API parameter): it also lists plans and
  fetches each plan to yield its runs, applying the same filters per run. This
  costs one extra request per plan; only `created_before` is used to
  pre-filter plans, since other filters (including `is_completed`: runs in an
  active plan can be closed independently) could exclude matching runs.
- Prefer bulk methods (`get_tests`, `get_results_for_run`) over per-case calls.
- Raw JSON access remains available via `get(endpoint, params)` and
  `get_paginated(endpoint, collection_key, params)`.

### Pagination

Collection methods return lazy iterators: arguments are validated immediately,
requests are made while iterating. The client follows `_links.next`
(TestRail 6.7+) and also accepts legacy responses that return a plain JSON
array. Pagination links are validated to stay on the configured instance and on
read-only endpoints. Malformed pages raise `TestRailResponseError`.

### Retries and rate limiting

- Retried: HTTP 429, 502, 503, 504, and network failures/timeouts (safe because
  every request is a read-only GET).
- Never retried: 401, 403, and other 4xx/5xx errors.
- A valid `Retry-After` header (429/503), either delay-seconds or an
  HTTP-date, is honoured; negative or malformed values are ignored. If it exceeds
  `http.max_retry_delay_seconds`, the client fails immediately instead of
  waiting. Without the header, delays are `retry_backoff_seconds × 2ⁿ`, capped
  at `max_retry_delay_seconds`.
- At most `http.max_retries` retries per request. Every attempt counts against
  `http.max_requests_per_execution`.
- On exhaustion the original error type is raised (e.g.
  `TestRailRateLimitError`) with `attempts` set and an explanatory note. If the
  request budget runs out while retrying, `TestRailRequestBudgetExceeded` is
  raised, chained to the last error.

### Models

Models are frozen dataclasses built with `<Model>.from_api(payload)`:
`Project`, `Suite`, `Plan`, `PlanEntry`, `Run`, `Case`, `Test`, `Result`,
`Status`, `CaseType`, `CustomField` (aliases `CaseField`, `ResultField`) and
`CustomFieldConfig`.

- Source TestRail IDs and relationships are preserved:
  `Plan.entries → PlanEntry.runs → Run`, `Test.case_id/run_id`,
  `Result.test_id` (plus `run_id`/`case_id` from the request context).
  Standalone runs have `plan_id = None` (`Run.is_standalone`).
- Timestamps are converted to timezone-aware UTC `datetime` values.
- Missing optional fields become `None`; malformed payloads raise
  `TestRailResponseError` naming the endpoint and field.
- `custom_*` fields are kept in `custom_fields` keyed by system name; other
  unrecognized fields are kept in `extra`. Status counts
  (`passed_count`, `custom_status1_count`, ...) are kept in `status_counts`.
- A run's results include retries and comment-only updates
  (`status_id = None`); all are preserved.
- `Result.comment` is kept for later failure analysis but excluded from
  `repr()` (as are custom/extra values) so it is not logged by accident.

### Status resolution

`StatusResolver(client.get_statuses(), settings.mappings.statuses)` maps each
status ID to a `ResolvedStatus` (original ID, system name, label, category,
and how it was resolved), in order of precedence:

1. configured `mappings.statuses` (by system name or ID),
2. TestRail's built-in system statuses (`passed`, `failed`, `blocked`,
   `retest`, `untested`), identified by system name,
3. statuses flagged `is_untested` → `not_run`,
4. otherwise `unknown`. Custom statuses are never guessed from their labels.

IDs that are not defined on the instance resolve to `unknown`. Configured
identifiers that do not exist on the instance (e.g. an optional custom status)
are listed in `resolver.unmatched_mappings` instead of failing.

## Historical collection

```python
from testrail_stability_analyzer.collection import HistoricalCollector

report = HistoricalCollector(client, settings, resolver).collect()
for history in report.histories:
    history.key            # case ID + application + platform
    history.window         # final outcomes inside the history window, newest first, capped
    history.latest_known   # newest final outcome found, possibly older than the window
    history.state          # result_available, untested, not_included_in_run, ...
    history.coverage       # complete, lookback_limited, or partial
```

`report` also carries the discovery decisions for every run, per-run scan
status, orphan and conflicting result records, the number of API requests
made, and a list of coverage limitations. Nothing is logged.

### Supported project structures

Test Plans (with one or many child runs and configurations), standalone Test
Runs, or both. Runs listed by both `get_runs` and a plan are deduplicated by
run ID, keeping the plan context. Each run keeps its plan ID, run ID, name,
creation date, and completion state.

With a lookback start `L` (now − `lookback_limit_days`), discovery lists:

- runs and plans created after `L`,
- runs and plans created before `L` that are still open, and the child runs
  of such plans that are open or created after `L`.

`L` itself is inclusive: `created_after` is sent as the last whole second
before `L`, so nothing at `L` is lost if TestRail compares strictly (at most
one extra second is returned otherwise). Results are fetched the same way.

Completed runs and plans created before `L` are not listed; results they may
have received after `L` are therefore not found. This is reported as a
limitation. With `lookback_limit_days = 0` everything is listed (one
`get_plan` request per plan).

### Application, platform, and execution type

Each run is classified with `mappings.applications`, `mappings.platforms`, and
`mappings.execution_types`: regular expressions matched against the plan
name, run name, and/or configuration names (`sources`). A dimension is
`resolved` when exactly one mapping matches, `unresolved` when none matches,
`ambiguous` when several match, and `not_configured` when no mappings exist.
Nothing is guessed.

- Runs whose application or platform is unresolved, ambiguous, or not
  configured are kept, but each forms its own history group
  (`HistoryKey.run_id` is set), so unrelated executions are never merged.
  Only a resolved mapping groups runs together. A project with a single
  application or platform can say so explicitly with a catch-all mapping,
  e.g. `{ name = "Product", patterns = [".*"] }`.
- `[filters]` restricts collection to mapped names. Runs resolved to other
  names are `excluded_by_filter`; runs that cannot be classified for an active
  filter are `unclassified`: reported for review, but their results are not
  downloaded. Without filters, every discovered run is collected.
- `naming.*_name_patterns` and `testrail.suite_id` exclusions are recorded
  with a reason, never silently dropped. The suite is filtered client-side
  for standalone and plan runs alike, so runs from other suites are still
  listed (as `excluded_by_filter`).

### Final outcome per run

A test can have several results in one run (retries, triage). All records are
kept in chronological order: by result timestamp, then result ID. The final
outcome is the **latest record that carries a status**:

- Comment-only records (no status) are kept as evidence but never replace a
  status.
- Records without a timestamp sort before timestamped ones; if no record has a
  timestamp, `executed_on` stays `None` (never taken from the run or plan).
- Statuses are resolved with `StatusResolver`. Failed stays failed (not an
  application defect by default), a configured automation-issue status stays
  distinct, blocked stays blocked, not applicable is distinct from not run, and
  unmapped statuses stay `unknown`.
- A test without status-bearing records has `final = None`; its current
  TestRail status is kept in `current_status`.
- The application version comes from the final result's `version` (or the
  result field configured as `mappings.custom_fields.application_version`).
- Comments are kept on the result models but excluded from `repr()`.
  `ResolvedResult.failure_summary()` returns a sanitized, truncated summary
  with HTML, URLs, email addresses, tokens, session IDs, long hex IDs/UUIDs,
  and file paths replaced by placeholders. Values of credential-like keys
  (`password`, `token`, `api_key`, `secret`, `session…`, …) are redacted in
  `key=value`, `key: value`, and quoted JSON/attribute forms. Markup (`<`
  followed by a letter, `/`, `!`, or `?`) is removed both before and after
  HTML entities are decoded, so encoded tags such as `&lt;img …&gt;` do not
  survive, while literal comparisons such as `<200>` are kept. The summary is **plain text**, not
  HTML: it can still contain `<`, `>`, or `&` (e.g. `expected <200>`), so
  HTML reports must escape it. Redaction is best-effort; summaries must still
  not be sent to external services.

### Grouping and history window

Outcomes are grouped by **case ID + application + platform**. Each distinct
run contributes one outcome per case, with its retries attached. Outcomes are
sorted by the actual final-result timestamp, newest first.

- `history.outcomes`: every collected outcome for the key.
- `history.window`: outcomes whose final result falls within
  `history.window_days`, capped at `history.max_runs`; `window_total` is the
  count before the cap.
- `history.window_complete` is false when runs that may hold results inside
  the window were not scanned (request budget, incomplete discovery), so the
  window can miss executions even when `latest_known` is fully backed. A note
  is added to `coverage_notes`. `history.window_interrupted` is true when
  this is caused by interrupted collection rather than only by a targeted
  selection.
- Exact duplicate result records (e.g. across pages) are merged; records with
  the same ID but different content are listed in `report.conflicts`.

### Collection order and latest known result

The latest known result is determined from **result timestamps**, never from
plan or run creation or completion dates. Closed runs are not assumed to be
immutable.

1. Results (`get_results_for_run` with `created_after = L`) and tests are
   fetched for open runs and runs created within the window.
2. Results are fetched for every other discovered run, so a late result or
   triage update in an old (even completed) run is found.
3. Tests are fetched for every remaining run with results in the lookback,
   newest actual result first. Results carry no case ID, so this is needed to
   find every case executed within the lookback; if the budget runs out, the
   runs with the oldest results are the ones left unscanned.

`history.coverage` states how far `latest_known` is backed by the scan:

| Coverage | Meaning |
|---|---|
| `complete` | No lookback limit, discovery complete, and every run that could hold a newer result for this key was scanned. `is_verified_latest` is true only here. |
| `lookback_limited` | As complete, but only within the lookback period (see discovery). |
| `selection_limited` | Targeted collection, identified keys: complete within the selected runs; other runs were not scanned. |
| `partial` | Incomplete discovery, invalid selected IDs, relevant runs not scanned (request budget), or records cut by the result-record cap. |

Even `complete` is limited to the runs TestRail returns for the project and
to the application/platform attribution of each run.

### Missing-result states

| State | Meaning |
|---|---|
| `result_available` | A final result exists (window or lookback). |
| `untested` | The case is in a collected run but has no status-bearing result. |
| `not_applicable` | The final status is not applicable, the test's current status is not applicable (without results in the lookback), or a resolved applicability value excludes the case. |
| `not_included_in_run` | Runs for the application/platform were collected; the case is not in them. |
| `unknown_applicability` | As above, but applicability is configured and the case's value is missing, unmapped, or could not be read. |
| `no_run_for_platform` | No run for this application/platform was found; this is not a failure. |
| `no_result_within_lookback` | The case was executed (per its current status) or runs exist, but no result record lies within the lookback. |
| `results_incomplete` | The result-record cap cut or skipped the relevant run(s) before a status-bearing record was retrieved. |

Keys are created for each case on every platform observed in the collected
runs, so missing platforms are visible. No execution records are created for
missing tests.

**Applicability** is used only when configured: set
`mappings.custom_fields.applicability` to a case field system name and list
the matching raw values (IDs or strings) in `applicability_values` for each
application. The value is read from `get_tests`; only cases lacking it there
trigger one bulk `get_cases` request per suite. Values are compared as text,
so `3` and `"3"` match. A dropdown option meaning "both" is simply listed
under every application it covers:

```toml
[mappings.custom_fields]
applicability = "custom_target_applications"

[[mappings.applications]]
name = "Application A"
patterns = ['(?i)\bapp-a\b']
applicability_values = [1, 3]   # option 3 covers both applications

[[mappings.applications]]
name = "Application B"
patterns = ['(?i)\bapp-b\b']
applicability_values = [2, 3]
```

A case is **excluded** from an application only when its value was read, is
set, lists none of that application's values, and contains no value that is
unmapped (listed by no configured application). An empty, unmapped, or
unread value never establishes an exclusion; the case is then
`unknown_applicability`. An actual result always takes precedence over the
case value.

### API request budget

- No per-case requests are made (`get_case`, `get_results_for_case`).
- Requests: discovery (2 `get_runs` + 2 `get_plans` listings plus one
  `get_plan` per plan), one `get_results_for_run` per run, and one
  `get_tests` per run that is open, created in the window, or has results in
  the lookback (more when paginated). Runs without results in the lookback
  cost no `get_tests` request.
- Because closed runs may still receive results, every discovered run's
  results are checked, and every result-bearing run's tests are fetched: a
  long lookback over a busy project can need about two requests per run.
  Increase `http.max_requests_per_execution` or shorten the lookback.
- If the budget runs out, collection stops, keeps what was fully collected,
  sets `report.budget_exhausted`, and marks affected histories `partial`.
- Targeted collection makes no listing requests: one `get_plan` per selected
  plan, one `get_run` per selected run not already covered by a selected
  plan, and one `get_plan` per distinct unselected parent plan (for its
  name), then results and tests as above.

### Targeted collection

Set `collection.plan_ids` and/or `collection.run_ids` (or use the
[CLI](#command-line-interface)) to collect only those plans and runs:

- Plans resolve to their child runs, which keep the plan ID and name. Selected
  runs may be standalone or part of a plan; a plan run's parent plan is
  fetched for its name, so plan-name mappings still apply.
- Runs selected both through a plan and directly are collected once, with
  their plan context. Duplicate IDs are ignored.
- IDs answered with HTTP 400/403/404 or belonging to another project are
  listed in `report.discovery.invalid_selections` (kind, ID, reason); the
  remaining IDs are still collected and histories are `partial`. HTTP 401,
  rate limiting, and server errors stop the collection.
- Selected runs are never excluded for their age; the suite, naming
  patterns, and filters still apply. Results are still fetched from the
  lookback start, so use `lookback_limit_days = 0` for old runs.
- Histories of identified case/application/platform keys are at best
  `selection_limited`, and their `window_complete` is false: unselected runs
  may hold newer results or executions in the window. Unidentified keys
  consist of one run, which unselected runs cannot change, so they keep the
  usual `complete`/`lookback_limited`/`partial` coverage; their window is
  incomplete only when that run's result records were capped.
- If the request budget runs out while fetching a selected run's parent plan,
  the run is kept without a plan name (as for an inaccessible parent) and
  discovery is marked incomplete.

### Result-record limit

`collection.max_results` caps the number of result **records** retrieved per
execution, a validation aid that is separate from `history.window_days`,
`history.max_runs` (distinct runs per history), and the request budget.

- Enforced during retrieval: each `get_results_for_run` request asks for at
  most the remaining number of records (`limit`, at most 250), and iteration
  stops at the cap, so no further page or run is requested.
- Every record counts, including retries and comment-only records.
  `report.result_records_fetched` is the record count; `report.outcomes` are
  the distinct per-run outcomes.
- Runs are processed in a deterministic order (open runs first, then newest
  created). The run in which the cap is reached is marked `results_capped`
  (also when its stream happens to end exactly at the cap); later runs are
  `skipped_by_cap` and not fetched.
- TestRail returns results newest first, so a capped stream may lack a
  test's older records, or contain only a trailing comment-only record and
  miss the status before it. Such outcomes have `results_complete = False`,
  are never `is_verified_final`, and their histories are `partial` (state
  `results_incomplete` when no status was retrieved). `report.results_capped`
  is set and `report.is_partial` is true.

## Stability analysis

`analyze(report, settings)` turns a `CollectionReport` into an
`AnalysisReport`. It is a pure function of collected data and makes no
TestRail requests.

```python
from testrail_stability_analyzer.analysis import analyze

analysis = analyze(report, settings)
for item in analysis.analyses:  # one per case / application / platform
    print(item.key.case_id, item.diagnosis, item.confidence, item.metrics.pass_rate.value)
```

Each `KeyAnalysis` keeps objective **metrics** separate from the diagnostic
**interpretation** (`diagnosis`, `confidence`, `reasons`), and links back to
the collected `history` (source plan, run, test, and result IDs).

### Result records versus distinct runs

A test can have several result records in one run (automated retries, manual
triage, comment-only updates). They are one execution: the run's final
status-bearing record is its outcome. Stability metrics count **distinct
runs** in `history.window` (final result within `history.window_days`, at most
`history.max_runs`). Result records only feed the retry/triage metrics and the
defect aggregation.

### Metrics and denominators

| Metric | Definition |
|---|---|
| `executions` | distinct runs in the window (after the cap); `window_total` is before the cap |
| `passed`, `failed`, `blocked`, `automation_issue`, `not_applicable`, `retest`, `not_run`, `unknown` | final category of each execution |
| `evidence_count` | conclusive executions: `passed + failed + automation_issue` |
| `pass_rate` | `passed / evidence_count` |
| `failure_rate` | `failed / evidence_count` |
| `automation_issue_rate` | `automation_issue / evidence_count`, tracked separately from failures |
| `retry_triage_count` / `retry_triage_rate` | executions with more than one result record / `executions` |
| `status_changed_within_run` | executions with a category change between consecutive status-bearing records (e.g. failed → passed, or failed → passed → failed); comment-only records are ignored |
| `transitions` | changes of final category between consecutive executions, oldest to newest |
| `latest_*` | latest known result, which may be older than the window |

Blocked, retest, not run, not applicable, and unknown executions are counted
but excluded from the rates. Rates are exact `Rate(numerator, denominator)`
values; `value` is `None` when the denominator is zero.

### Diagnosis

Rules are applied in order; the first match wins (P = passed, F = failed,
A = automation issue, E = P + F + A, n = `analysis.min_executions`):

| # | Diagnosis | Rule |
|---|---|---|
| 1 | `not_applicable` | final status or applicability metadata says not applicable |
| 2 | `partial` | coverage is `partial`, results are incomplete, or the window was interrupted (budget, cap, unscanned runs) |
| 3 | `unresolved_applicability` | not executed, and applicability metadata is missing |
| 4 | `not_run_in_window` | no execution in the window, but an earlier one is known |
| 4 | `not_run` | no execution is known |
| 5 | `insufficient_history` | E = 0 (only blocked, retest, not run, or unknown) |
| 6 | `stable` | P = E, E ≥ n, and no unknown status in the window; otherwise `insufficient_history` |
| 7 | `inconsistent` | P > 0 and F > 0 |
| 8 | `automation_issue_evidence` | A > 0 and F = 0 |
| 9 | `application_failure_evidence` | P = 0, F > 0, A = 0 |
| 9 | `mixed_failure_evidence` | P = 0, F > 0, A > 0 |

- **Labels describe evidence, not causes.** Mixed results do not prove a test
  is flaky; consistent failures are never called inconsistent.
  `application_failure_evidence` means executions ended as failed, not that an
  application defect is confirmed.
- **Linked defects do not affect the diagnosis.** A defect reference records
  that someone linked a ticket to a result; the ticket may describe a test,
  environment, or data problem, may be a duplicate, or may be rejected. The
  analyzer does not read the tracker, so it cannot establish an application
  failure from it.
- **A single passing execution never establishes stability**: `stable`
  needs at least `analysis.min_executions` (default 5) conclusive executions.

### Confidence

| Level | Rule |
|---|---|
| `none` | no conclusive execution, or a diagnosis that does not rest on execution evidence (`not_applicable`, `partial`, `unresolved_applicability`, `not_run_in_window`, `not_run`); metrics are still reported unchanged |
| `low` | fewer than `min_executions` conclusive executions, or an unidentified application/platform |
| `medium` | at least `min_executions`, but fewer than `history.max_runs`, conclusive executions; or `max_runs` with a window that may be incomplete (e.g. `selection_limited`) |
| `high` | `history.max_runs` conclusive executions, `complete` or `lookback_limited` coverage, and a complete window |

### Coverage limitations

Each analysis carries the history's `coverage` and `coverage_notes`;
`AnalysisReport.limitations` adds collection limitations. Targeted collection
(selected plans or runs) is `selection_limited`: it is not project-wide
historical coverage, so it can never reach `high` confidence, and a single
selected plan normally yields `insufficient_history`; `not_run` and
`not_run_in_window` reasons then state that only the selected runs were
searched. Partial collection,
selection limits, insufficient evidence, and not-applicable cases are always
reported as distinct values.

### Case-level defect aggregation

`AnalysisReport.case_defects` lists, per **Test Case ID**, every defect
referenced by its result records within the reporting period (the history
window), across all applications and platforms. The same `CaseDefects`
object is attached to every analysis of the case.

- Every collected record counts: final results, automated retries, manual
  triage, and comment-only records. The `max_runs` cap does not apply.
- Each record is placed by its **own** timestamp. Records without one are
  excluded and counted (`undated_records_with_defects`).
- The `defects` field is split on commas, trimmed, and deduplicated
  case-sensitively. Identifiers are kept exactly as entered; no tracker URLs
  are derived.
- Each `DefectReference` lists its sources: result, test, run, and plan IDs,
  timestamp, status category, whether it is the run's final record,
  application, platform, and the version recorded on that result itself
  (read from the configured version field during collection; `None` if it
  has none; the final result's version is not attributed to retries or
  triage records). Comments are not included.
- Case and test `refs` fields are not execution defects and are ignored.
- `coverage` (`complete`, `selection_limited`, `partial`) is report-wide,
  since a run that was not scanned may hold records of any case:
  - `partial`: runs within the collection scope may be missing (request
    budget, record cap, incomplete discovery, unscanned included runs, or
    runs that could not be classified for the configured filters);
  - `selection_limited`: everything in scope was scanned, but the scope
    excludes runs (targeted selection, naming patterns, or filters);
  - `complete`: every discovered run that may hold records was scanned.
  Excluded runs of another suite are ignored when suite IDs are known.
  An empty list with `complete` coverage means no defect was linked;
  otherwise it means none was found in the scanned runs. `records_examined`
  shows how many records were checked.
- Records of tests no longer in their run (orphans) have no known case; their
  count is reported in `orphan_records_with_defects` and the limitations,
  without downgrading every case.

## Reports

Two formats are built from the same content and can be written in one run
(`--xlsx-report` and `--xml-report` together fetch the metadata only once):

| | XLSX (`--xlsx-report PATH`) | XML (`--xml-report PATH`) |
|---|---|---|
| Format | native Office Open XML workbook | XML Spreadsheet 2003 (SpreadsheetML) |
| Dependency | `openpyxl` (optional extra) | standard library only |
| Opens in | Excel, Google Sheets, LibreOffice, Google Drive preview | Excel, LibreOffice (Google Drive shows raw XML) |
| Worksheets | Stability Overview, Stability Details, Report Info | Stability (= Stability Details), Report Info |
| Overview notes, text wrapping | yes | no |

Both have frozen header rows, AutoFilter, set column widths, numeric
percentage and date cells (UTC), and the colours below.

**`Stability Overview`** (XLSX only, first worksheet): one row per Test Case
ID with the case columns below and **one compact column per application ×
platform** combination (header `<application> <platform>`). Each cell shows
the latest status of that combination, or a state:

| Cell | When |
|---|---|
| No History | the case was not in any collected run of the combination (the note says whether its applicability applies or is unresolved) |
| Not Applicable | diagnosis or latest status is not applicable, or, without any history, a resolved applicability value excludes the application |
| Partial | collection was incomplete for the combination |
| Applicability Unresolved | not executed, and applicability metadata is missing |
| Not Run | no execution, or none within the history window |
| Passed, Failed, Blocked, Automation Issue, Retest, Unknown Status | otherwise: the latest known status, also when history is insufficient |

Colour follows the shown status (green Passed, red Failed, yellow Automation
Issue, grey otherwise); an `Inconsistent` diagnosis turns the cell yellow
while keeping the status. **Green Passed means the latest result passed, not
that stability is confirmed**: the cell's note shows the diagnosis,
confidence, executions, pass rate, coverage, and latest result date, and,
for a latest status of Blocked, `Blocked reason: <reason>` (see
[Blocked reason](#blocked-reason)).

**`Stability Details`** (the XML `Stability` worksheet): one row per Test Case
ID, sorted by ID.

| Column | Source |
|---|---|
| Test Case ID | TestRail case ID |
| Test Case Title | the case title from the case's newest test instance with one (`get_tests`, no extra request); `Title unavailable` (with a `Report Info` limitation) when none was returned. Never derived from Class Name |
| Feature / Test Group | the case's section hierarchy (see below); omitted when `report.feature_group.enabled = false` |
| Class Name | `mappings.custom_fields.class_name`, read from the case's newest test instance; omitted when not configured |
| Test Type | case type name (`get_case_types`, one request); the type ID if it cannot be fetched |
| Defects | defect IDs shared across all applications/platforms (see [Case-level defect aggregation](#case-level-defect-aggregation)) |
| Applicability | only when `mappings.custom_fields.applicability` is configured: the configured applications the case applies to, by mapped name and in configuration order (e.g. `Application A, Application B`, or `report.applicability.all_label` when every application matches). `Not set` (no value), `Unmapped` (a value no application lists; appended to matched names), `Unknown (not fetched)` (request budget), `Unknown (case not found)` (not returned by `get_cases`). TestRail option labels are not used |

Then, for every configured application × platform combination (limited by
`filters.*` when set), in configuration order: Latest Status, Latest Result
(UTC), Executions, Pass Rate, Failure Rate, Automation Issue Rate, Diagnosis,
Confidence, Coverage, and Blocked Reason (when `report.blocked_reason.enabled`).
Each combination is analyzed independently.
`No History` means the case was not in any collected run of that
combination; when a resolved applicability value excludes the application,
`N/A` is shown instead. Histories of runs with an unidentified application or platform
cannot be placed in a combination column; they are counted in `Report Info`.

| Colour | Diagnosis / latest status |
|---|---|
| Green | Stable / Passed |
| Yellow | Inconsistent, Automation Issue / Automation Issue |
| Red | Failure Evidence, Failure + Automation Issue / Failed. Failed executions do **not** confirm an application defect. |
| Neutral | N/A, Not Run, Not Run in Window, Insufficient History, Partial, Applicability Unresolved, No History |

**`Report Info` worksheet**: project ID, collection mode and selection,
window and lookback, collection status (OK/PARTIAL), API requests, case and
combination counts, coverage, diagnosis, and Overview status counts, test
case applicability counts and the number of combinations shown as Not
Applicable without history, Blocked reason counts, explicit
notes for selection-limited, lookback-limited, and partial coverage, defect
coverage, Feature / Test Group summary, every limitation, and a legend
(colours, Overview, confidence levels).

### Blocked reason

For a combination whose **latest known result is a verified Blocked final**
(its run's result stream was not cut by the result-record cap), a short
reason is taken from that result's comment, or, if it has none, from
comment-only triage notes added after it in the same run. Earlier retries,
older runs, and combinations whose latest status is not Blocked are never
used, so a historical Blocked reason does not appear once a newer result
exists.

Only text following a configured header at the start of a line is used,
e.g.:

```text
BLOCKED REASON: Payment sandbox unavailable
```

Headers may be decorated with markdown or HTML (`**SKIPPED REASON:**`,
`### Blocked reason`, `<p>BLOCKED REASON</p>`), and the text may start on the
next line. The section ends at a blank line, another labelled section
(`STEPS:`, `**Expected Result:**`, a heading), a code block, or the first log
or stack-trace line (timestamps, log levels, `Traceback`, stack frames,
qualified exception names). The section is then stripped of images,
attachment and file references, and markdown link targets; URLs, emails,
credentials, session and other identifiers, tokens, and paths are replaced
with placeholders; and the text is collapsed to one line and truncated to
`max_length`. A comment without a recognized section, or one that leaves
almost nothing but placeholders, yields `Reason unavailable`; the comment
itself is **never** exported. An unverified Blocked latest result also shows
`Reason unavailable`.

The reason is shown in the Overview note and the `Blocked Reason` column of
Stability Details (also in the XML report). It never changes the latest
status, diagnosis, or confidence.

```toml
[report.blocked_reason]
enabled = true
section_headers = ["BLOCKED REASON", "SKIPPED REASON"]
max_length = 160
```

### Google Sheets

Upload the `.xlsx` to Google Drive and open it with Google Sheets. Values,
colours, number formats, frozen rows, and filters are kept; cell notes appear
as notes (hover the cell). Column widths are approximate. The XML format is
not supported by Google Sheets.

### Feature / Test Group

The group is reconstructed generically from TestRail sections: the case's
`section_id` is followed through `parent_id` links up to the top-level
section, and the names are joined top level first with
`report.feature_group.separator` (e.g. `Top Section/Child Section`). No
section names are assumed. The original names are kept as a tuple
(`CaseGroup.path`); a name that contains the separator is not escaped in the
label.

`get_tests` does not return `section_id`, so section metadata is fetched
**only when a report is generated**, at most once per distinct suite of the
collected runs. A case is looked up first in the suites of its own runs
(newest first), then in the other collected suites, so a case moved between
collected suites is shown under its current group; sections are fetched only
for suites where a case was found:

| Request | Count per suite |
|---|---|
| `get_cases` (shared with the applicability fallback; never requested twice) | ⌈cases / 250⌉ pages |
| `get_sections` | ⌈sections / 250⌉ pages |
| `get_suites` (only with `include_suite_name`) | 1 per report |

Collection without a report option makes none of these requests. Metadata is
fetched after collection, so an exhausted request budget or a failed request
never changes stability results; the affected groups are reported as unknown,
with a limitation:

| State | Meaning |
|---|---|
| `resolved` | full path reconstructed |
| `case_not_found` | the case is not returned by `get_cases` for any collected suite (deleted, or moved to a suite not covered by the collected runs) |
| `section_not_found` | the case has no section, or its section is not in the suite |
| `broken_hierarchy` | a parent section is missing |
| `cyclic_hierarchy` | parent links form a cycle |
| `inaccessible` | a metadata request failed (e.g. HTTP 403) |
| `not_fetched` | the request budget was exhausted |

### Report safety

Reports contain only these fields:

- **Case columns:** Test Case ID, Test Case Title, Feature / Test Group
  (section names), Class Name (the raw value of the configured custom field),
  Test Type, Defects (defect IDs as entered in results), and Applicability
  (configured application names).
- **Combination columns:** analyzed values only (latest status and date,
  execution count, rates, diagnosis, confidence, coverage) and, for a latest
  Blocked status, the sanitized Blocked reason excerpt.
- **Report Info:** project, suite, selected plan and run IDs, collection
  settings and counts, limitations, and the legend.

Result comments (beyond the sanitized excerpt of a configured Blocked reason
section), case steps and other case content, plan and run names, session IDs,
the TestRail base URL, and credentials are never written.

Text fields copied from TestRail (titles, section names, Class Name, defect IDs) are
exported as entered: if they contain URL text, that text appears in the
report as plain text. The exporter never creates hyperlinks, external links,
or links to TestRail or defect trackers; XLSX values are stored as text
cells, never formulas.

Every string is stripped of characters XML cannot carry, escaped, truncated
to Excel's 32,767-character cell limit, and prefixed with `'` when it starts
with `=`, `+`, `-`, `@`, tab, or carriage return, so spreadsheets do not
evaluate it as a formula (identically in both formats; defect IDs are
otherwise kept exactly as entered). Reports contain customer data such as
section names and defect IDs: write them outside the repository or to a
git-ignored directory (`reports/`, `output/`).

## Command-line interface

```bash
# With Poetry: poetry run testrail-stability-analyzer ...
PYTHONPATH=src python3.13 -m testrail_stability_analyzer collect plan 1234 --config config/local/config.toml
PYTHONPATH=src python3.13 -m testrail_stability_analyzer collect plan 1234 --config config/local/config.toml \
    --xlsx-report reports/stability.xlsx
# Both formats in one run (metadata is fetched once):
PYTHONPATH=src python3.13 -m testrail_stability_analyzer collect plan 1234 --config config/local/config.toml \
    --xlsx-report reports/stability.xlsx --xml-report reports/stability.xml
```

| Command | Collects |
|---|---|
| `collect plan PLAN_ID [PLAN_ID ...] [--run-id RUN_ID ...]` | the selected plans (and runs) |
| `collect run RUN_ID [RUN_ID ...] [--plan-id PLAN_ID ...]` | the selected runs (and plans) |
| `collect sample [--max-candidates N]` | the newest eligible plan; defaults `--max-results 15 --max-requests 20` |

Options for all commands (each overrides the configuration file; all values
are validated like the file):

| Option | Setting |
|---|---|
| `--config PATH` | TOML file with URL, project ID, mappings (keep it untracked) |
| `--suite-id N` | `testrail.suite_id` |
| `--credential-provider {environment,keychain}` | `credential_provider.type` |
| `--keychain-service NAME` / `--keychain-account NAME` | Keychain item names (never the secret) |
| `--max-results N` | `collection.max_results` (`0` = no cap) |
| `--max-requests N` | `http.max_requests_per_execution` |
| `--window-days N` / `--lookback-days N` | `history.window_days` / `history.lookback_limit_days` |
| `--xlsx-report PATH` | also write the [XLSX report](#reports) (needs `openpyxl`); the directory must exist |
| `--xml-report PATH` | also write the [XML report](#reports); the directory must exist |

The command-line selection replaces `collection.plan_ids`/`run_ids` from the
configuration file. Secrets cannot be passed as arguments.

`collect sample` lists the project's plans created within the lookback
(paginated, newest first), skips plans whose name does not match
`naming.plan_name_patterns` without further requests, and fetches up to
`--max-candidates` (default 5) remaining plans until one has a run included
by the configured suite, naming patterns, and filters. If none qualifies,
nothing is collected; an unmatched plan is never selected.

The CLI runs `HistoricalCollector` and prints a summary to standard output:
project, selected and inspected plan/run IDs, per-run decision and
application/platform/execution type (configured mapping names or state),
unresolved classifications, test instances, result records (status-bearing,
comment-only, with comments, with linked defects), final outcomes (verified
or from capped streams), status categories, grouping, coverage, states, API
requests, and limitations (with a report option, also Feature / Test Group
counts). Plan and run names, section names, comment text, defect
identifiers, URLs, and credentials are never printed. No files are written
unless `--xlsx-report` or `--xml-report` is given.

| Exit code | Meaning |
|---|---|
| `0` | Collection complete (within its selection and lookback) |
| `1` | Unexpected error or interruption |
| `2` | Invalid arguments or configuration, unusable report path, or `--xlsx-report` without `openpyxl` |
| `3` | Partial collection: result-record cap, request budget, or some invalid IDs |
| `4` | API or credential error, or no selected plan/run could be collected (no report is written) |
| `5` | `collect sample` found no eligible plan |

## Security

- The API client is **read-only**: it only issues HTTP GET requests and only
  accepts `get_*` endpoints. It has no add/update/delete methods.
- Credentials are never part of configuration. Config files containing keys
  such as `api_key`, `password`, or `token` are rejected, as are base URLs with
  embedded credentials.
- Credentials are read on demand through a `CredentialProvider`. The built-in
  `EnvironmentCredentialProvider` reads `TESTRAIL_USERNAME` and
  `TESTRAIL_API_KEY`; `KeychainCredentialProvider` reads a macOS Keychain
  item (read-only; the secret is read from the tool's output, never passed as
  an argument, and tool output is never included in errors). Other sources
  can be added by implementing the same interface.
- XLSX and XML reports contain only analyzed values (see [Report safety](#report-safety)).
- The CLI prints only counts, IDs, mapping names, and status categories. API
  errors are reported by exception type and HTTP status only; TestRail's
  response text is never printed.
- `Credentials` objects redact both fields from `repr()`/`str()`; error
  messages never include credential values or the Authorization header.
- Result comments are excluded from model `repr()` and never logged. Use
  `ResolvedResult.failure_summary()` for a sanitized summary.
- HTTPS is required unless `allow_insecure_http = true` is set explicitly.
  Redirects are not followed, so credentials are never sent to another host.
- A per-execution request budget (`http.max_requests_per_execution`) prevents
  runaway API usage.

TestRail credentials are entirely separate from any other tool's credentials
(including Anthropic/Claude credentials).

## Project layout

```text
src/testrail_stability_analyzer/
├── cli.py       command-line interface (also: python -m testrail_stability_analyzer)
├── config/      settings.py (typed settings, validation), auth.py (credential providers)
├── api/         client.py (read-only client), transport.py (HTTP), errors.py
├── models/      metadata.py, execution.py (typed entities), statuses.py (status resolution)
├── collection/  discovery.py, classification.py, outcomes.py, history.py,
│                collector.py, sanitize.py, case_metadata.py, sections.py
│                (historical execution collection, Feature / Test Groups)
├── analysis/    metrics.py, classification.py, defects.py, analyzer.py
│                (stability analysis and case-level defect aggregation)
└── export/      report_data.py (shared report content), xlsx.py (XLSX, optional openpyxl),
                 spreadsheet.py (Excel-compatible XML Spreadsheet 2003)
config/config.example.toml
tests/unit/          mocked unit tests
tests/integration/   reserved for opt-in live tests
```

## Implementation status

### Implemented

- Python 3.13 package with `src/` layout, Poetry/PEP 621 metadata, pytest config.
- Typed, validated settings from TOML and environment variables.
- Credential-provider interface with an environment-variable implementation.
- Read-only API client:
  - typed methods for 15 read-only endpoints (see [Endpoints](#endpoints)),
  - bounded retries for rate limits, temporary server errors, and network failures,
  - URL construction for `index.php?/api/v2/...` endpoints and query parameters,
  - injectable HTTP transport (standard-library `http.client` by default) with
    separate connect/read timeouts,
  - distinct errors for authentication (401), authorization (403), rate limit
    (429, with `Retry-After`), other HTTP errors, network/timeout failures, and
    invalid responses,
  - pagination over `_links.next` (TestRail 6.7+) and legacy list responses,
  - request budget enforcement (including retries).
- Typed models for TestRail entities with custom and unknown field preservation.
- Status resolution with configurable category mappings and explicit `unknown`.
- Historical execution collection: plan and standalone run discovery,
  application/platform/execution-type classification and filters, final
  outcome per run with retained retries, grouping per case/application/
  platform, history window and cap, latest known result with coverage, missing
  and not-applicable states, sanitized failure summaries.
- Targeted collection by Test Plan and Test Run IDs without project-wide
  discovery, `selection_limited` coverage, and invalid-ID reporting.
- Result-record cap enforced during retrieval, with incomplete outcomes
  marked and reported as partial.
- macOS Keychain credential provider and provider selection in configuration.
- Command-line interface (`collect plan`, `collect run`, `collect sample`)
  with a sanitized summary and exit codes.
- Stability analysis per case/application/platform: distinct-run metrics,
  documented pass-rate denominator, retry/triage and transition metrics,
  deterministic diagnosis and confidence, and coverage limitations.
- Case-level defect aggregation across applications, platforms, retries,
  and triage records, with provenance.
- Native XLSX report (`--xlsx-report`, optional `openpyxl`) with a compact
  Stability Overview, Stability Details, and Report Info.
- Excel-compatible XML Spreadsheet 2003 report (`--xml-report`) with dynamic
  application/platform columns, colour coding, report-level coverage and
  limitations, and formula-injection protection.
- Feature / Test Group per case from the TestRail section hierarchy
  (`get_sections`), fetched once per suite only for reports.

### Not implemented yet

- JSON export, and analysis output in the CLI summary.
- Live integration tests.
- Credential providers other than environment variables and macOS Keychain.

### Limitations

- Model fields follow the TestRail API v2 documentation; fields added by newer
  TestRail versions are preserved in `extra` but not typed.
- Custom field values are kept as returned (e.g. dropdown IDs); decoding them
  via `CustomFieldConfig.items()` is left to later milestones.
- `include_plan_runs` filtering by creation date uses each run's `created_on`;
  it costs one request per plan in the project (after pre-filtering).
- `get_results_for_case` supports only `status_id`, matching the TestRail API.
- The result-record cap marks a run as capped when its stream reaches the cap
  exactly, since confirming the end would need one more record.
- `collect sample` only considers plans created within the lookback; with a
  narrow lookback or many non-matching plans it may find none (it never
  widens the search silently).
- Targeted collection cannot confirm that a newer result does not exist in
  other runs, so its histories are never `complete`.
- Retry delays are deterministic (no jitter); intended for a single process.
- **Old completed runs receiving recent results.** It has not been verified
  whether TestRail can add results to completed runs, so the collector does
  not assume it cannot. Every discovered run's results are checked, which
  costs at least one request per run. Completed runs and plans **created
  before the lookback start** are not discovered at all, so a recent result
  added to such a run is not found. Every history is then marked
  `lookback_limited` (never `complete`), and the report lists this
  limitation. Set `history.lookback_limit_days = 0` to remove it, at the cost
  of scanning the whole project.
- A case appears only if it is in a collected run: one that is open, created
  in the window, or has results in the lookback. A case never executed within
  the lookback has no history.
- Results created before the lookback start are not fetched, also for open
  runs created earlier (`RunOutcome.evidence_truncated`).
- Window membership of an execution uses its **final** result's timestamp,
  while defect aggregation uses each record's own timestamp; for a run whose
  records straddle the window start, the two can differ.
- Defect coverage is report-wide: one unscanned or unclassified run marks
  every case's defect list `partial`, and one run excluded by naming patterns
  or filters marks it `selection_limited`, whether or not that run contains
  the case (finding out would need extra requests).
- Discovery relies on TestRail's `created_after`/`created_before`/
  `is_completed` filters; their boundary-second semantics are handled by
  overlapping queries.
- Custom field values (e.g. dropdown IDs) are not decoded; applicability
  values are matched as configured and shown as application names, and Class
  Name shows the raw value.
- Applicability can only mark combinations of cases that appear in at least
  one collected run; cases never executed in the collected scope are not
  listed.
- Feature / Test Group is the case's **current** section at report time; for
  a case moved since, older executions are shown under its new group.
- In reports, histories with an unidentified application or platform, or
  with one outside the configured filters, are only counted in `Report Info`
  (separately), not shown in combination columns.
- The Overview colour follows the latest status, except for `Inconsistent`
  (yellow). A history diagnosed `automation_issue_evidence` whose latest
  result passed shows a green Passed; its note and Stability Details show the
  diagnosis.
- A defect ID that starts with `=`, `+`, `-`, or `@` and is the first in its
  cell is shown with a leading `'` (formula-injection protection).
- XLSX column widths are approximations of the shared point widths.
- Blocked reasons are recognized only in sections whose header starts a line;
  redaction is pattern-based and best-effort, so an unusual secret format
  inside a reason section could remain. Reports must not be shared outside
  the approved audience.
