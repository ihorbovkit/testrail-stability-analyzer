# testrail-stability-analyzer

A reusable, read-only Python application for analyzing test stability in any
TestRail instance. It is not tied to a specific customer, project, automation
framework, or TestRail configuration.

> **Status: Milestone 3.1 (targeted collection, Keychain authentication,
> CLI).** Configuration, credential handling (environment variables or macOS
> Keychain), a read-only API client, status resolution, a historical
> execution collector with targeted plan/run selection and a result-record
> cap, and a command-line interface are implemented. Stability/flakiness
> metrics and report export are **not** implemented yet. See
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

The runtime has **no third-party dependencies**; it uses only the standard
library. `pytest` is declared as an optional development dependency.

## Installation

```bash
# Creates a project-local .venv/ (see poetry.toml) and installs dev dependencies.
poetry install
```

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
| `mappings.custom_fields` | `{}` | — |
| `mappings.statuses` | `{}` | — |
| `collection.plan_ids` / `collection.run_ids` (targeted collection) | `[]` (project-wide) | — |
| `collection.max_results` (result-record cap; `0` = no cap) | `0` | `TESTRAIL_MAX_RESULTS` |
| `credential_provider.type` (`environment` or `keychain`) | `environment` | `TESTRAIL_CREDENTIAL_PROVIDER` |
| `credential_provider.keychain_service` / `keychain_account` | none (required for `keychain`) | `TESTRAIL_KEYCHAIN_SERVICE` / `TESTRAIL_KEYCHAIN_ACCOUNT` |
| `credential_provider.username_variable` (environment variable holding the username) | none | — |

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
  is added to `coverage_notes`.
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
| `selection_limited` | Targeted collection: complete within the selected runs; other runs were not scanned. |
| `partial` | Incomplete discovery, invalid selected IDs, relevant runs not scanned (request budget), or records cut by the result-record cap. |

Even `complete` is limited to the runs TestRail returns for the project and
to the application/platform attribution of each run.

### Missing-result states

| State | Meaning |
|---|---|
| `result_available` | A final result exists (window or lookback). |
| `untested` | The case is in a collected run but has no status-bearing result. |
| `not_applicable` | The final status is not applicable, the test's current status is not applicable (without results in the lookback), or configured applicability metadata excludes the case. |
| `not_included_in_run` | Runs for the application/platform were collected; the case is not in them. |
| `unknown_applicability` | As above, but applicability is configured and the case's value is missing. |
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
trigger one bulk `get_cases` request per suite.

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
- Histories are at best `selection_limited`: newer results may exist in runs
  outside the selection. For the same reason, `window_complete` is false for
  identified case/application/platform keys (unselected runs may hold
  executions in the window); unidentified keys consist of one run and are
  unaffected.
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

## Command-line interface

```bash
# With Poetry: poetry run testrail-stability-analyzer ...
PYTHONPATH=src python3.13 -m testrail_stability_analyzer collect plan 1234 --config config/local/config.toml
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
requests, and limitations. Plan and run names, comment text, defect
identifiers, URLs, and credentials are never printed, and no files are
written.

| Exit code | Meaning |
|---|---|
| `0` | Collection complete (within its selection and lookback) |
| `1` | Unexpected error or interruption |
| `2` | Invalid arguments or configuration |
| `3` | Partial collection: result-record cap, request budget, or some invalid IDs |
| `4` | API or credential error, or no selected plan/run could be collected |
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
- The CLI prints only counts, IDs, mapping names, and status categories.
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
│                collector.py, sanitize.py (historical execution collection)
├── analysis/    planned: stability and flakiness metrics
└── export/      planned: XML / JSON / Excel reports
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
  - typed methods for 14 read-only endpoints (see [Endpoints](#endpoints)),
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

### Not implemented yet

- Stability, flakiness, and automation-issue metrics.
- XML, JSON, and Excel export.
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
- Discovery relies on TestRail's `created_after`/`created_before`/
  `is_completed` filters; their boundary-second semantics are handled by
  overlapping queries.
- Custom field values (e.g. dropdown IDs) are not decoded; applicability
  values are matched as configured.
