# testrail-stability-analyzer

A reusable, read-only Python application for analyzing test stability in any
TestRail instance. It is not tied to a specific customer, project, automation
framework, or TestRail configuration.

> **Status: Milestone 1 (project foundation).** Configuration, credential
> handling, and a minimal read-only API client are implemented. Historical
> collection, stability analysis, and report export are **not** implemented
> yet. See [Implementation status](#implementation-status).

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
| `history.lookback_limit_days` (optional cap; `0` = no limit) | no limit | `TESTRAIL_HISTORY_LOOKBACK_LIMIT_DAYS` |
| `http.connect_timeout_seconds` | `10.0` | `TESTRAIL_HTTP_CONNECT_TIMEOUT_SECONDS` |
| `http.read_timeout_seconds` | `30.0` | `TESTRAIL_HTTP_READ_TIMEOUT_SECONDS` |
| `http.max_requests_per_execution` | `1000` | `TESTRAIL_HTTP_MAX_REQUESTS` |
| `naming.plan_name_patterns` / `naming.run_name_patterns` | `[]` (include all) | — |
| `mappings.applications` / `mappings.platforms` | `[]` | — |
| `mappings.custom_fields` | `{}` | — |
| `mappings.statuses` | `{}` | — |

### Configuration principles

- **No assumptions about your instance.** Application and platform names are
  free-form and matched by regular expressions against plan names, run names,
  or configuration names. Projects may use Test Plans or standalone Test Runs,
  single or multiple suites.
- **Custom statuses are optional.** `mappings.statuses` maps analyzer
  categories (`passed`, `failed`, `blocked`, `not_run`, `retest`,
  `not_applicable`, `automation_issue`) to TestRail status system names or IDs.
- **The latest known result is always searched for**, even when it is older
  than the history window. `history.lookback_limit_days` is an optional cap
  for limiting API usage; results older than the cap will not be found.
- **Validation reports every problem at once**, with the setting path, e.g.
  `history.window_days must be >= 1`. Unknown keys are rejected to catch typos.

## Security

- The API client is **read-only**: it only issues HTTP GET requests and only
  accepts `get_*` endpoints. It has no add/update/delete methods.
- Credentials are never part of configuration. Config files containing keys
  such as `api_key`, `password`, or `token` are rejected, as are base URLs with
  embedded credentials.
- Credentials are read on demand through a `CredentialProvider`. The built-in
  `EnvironmentCredentialProvider` reads `TESTRAIL_USERNAME` and
  `TESTRAIL_API_KEY`; other sources (e.g. macOS Keychain) can be added by
  implementing the same interface.
- `Credentials` objects redact both fields from `repr()`/`str()`; error
  messages never include credential values or the Authorization header.
- HTTPS is required unless `allow_insecure_http = true` is set explicitly.
  Redirects are not followed, so credentials are never sent to another host.
- A per-execution request budget (`http.max_requests_per_execution`) prevents
  runaway API usage.

TestRail credentials are entirely separate from any other tool's credentials.

## Project layout

```text
src/testrail_stability_analyzer/
├── config/      settings.py (typed settings, validation), auth.py (credential providers)
├── api/         client.py (read-only client), transport.py (HTTP), errors.py
├── models/      planned: typed TestRail entities
├── collection/  planned: historical data collection
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
  - URL construction for `index.php?/api/v2/...` endpoints and query parameters,
  - injectable HTTP transport (standard-library `http.client` by default) with
    separate connect/read timeouts,
  - distinct errors for authentication (401), authorization (403), rate limit
    (429, with `Retry-After`), other HTTP errors, network/timeout failures, and
    invalid responses,
  - pagination over `_links.next` (TestRail 6.7+) and legacy list responses,
  - request budget enforcement.

### Not implemented yet

- Endpoint-specific methods (projects, plans, runs, tests, results, statuses).
- Typed domain models.
- Historical data collection and latest-result resolution.
- Status resolution from `get_statuses` and status-category mapping logic.
- Application/platform matching logic (only the configuration exists).
- Stability, flakiness, and automation-issue metrics.
- XML, JSON, and Excel export.
- Command-line interface.
- Automatic retry/backoff on rate limits.
- macOS Keychain or other credential providers.
- Live integration tests.
