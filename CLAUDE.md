# CLAUDE.md

Project instructions for Claude Code.

## Project purpose

`testrail-stability-analyzer` is a reusable, customer-independent Python application that:

- Connects to TestRail API v2 (read-only).
- Retrieves test plans, runs, cases, statuses, and historical results.
- Identifies the latest result for each test.
- Analyzes execution history across distinct runs.
- Calculates stability and flakiness metrics.
- Distinguishes application defects from automation maintenance issues.
- Exports structured XML, JSON, and Excel reports.

It must work with any TestRail instance and must not be tied to a specific customer, automation framework, application, or TestRail project.

## Architecture principles

- Python 3.13.
- Modular, maintainable design with typed Python models.
- Separate modules for API access, data collection, analysis, and export.
- TestRail instances and project settings are configurable.
- No hardcoded customer-specific identifiers, status IDs, or status names.
- No TestRail write operations — the API client is read-only.
- Prefer small, reusable components.
- Statistics must be deterministic and reproducible.
- AI may assist with interpreting failures but must never invent evidence.

## Security

- Never print, log, commit, or expose API credentials, tokens, or auth headers.
- Never include real customer API keys or data in fixtures, tests, or documentation.
- Read credentials only from approved secure sources (environment variables, local untracked config, OS keychain).
- Avoid retaining raw TestRail comments, session IDs, or asset URLs unless necessary.
- Never upload customer data to external services without approval.
- Keep customer-specific configuration outside version control.
- `.claude/` is git-ignored because it contains local credentials. Do not read or display secret values from it.

## Git rules

- Never stage files, commit, push, create Pull Requests, or merge Pull Requests. These operations are exclusively the user's responsibility.
- Never create or switch branches, reset, clean, or rewrite Git history without explicit approval.
- Never modify Git remotes or Git configuration without approval.
- Never initialize another repository.
- Check `git status` before making changes and preserve existing user changes.
- Never force-push.
- Never commit generated reports or credentials.
- Claude may use read-only Git operations (`git status`, `git diff`, `git log`, `git show`) for investigation and code review.
- After implementation, provide a change summary, test results, and Git status. Leave all staging, committing, pushing, and PR management to the user.

## Development workflow

- Present a brief implementation plan before changes.
- Ask for approval before creating or modifying files.
- Ask before installing dependencies.
- Implement only the approved scope.
- Write unit tests for new logic, using mocked API responses.
- Keep live API tests separate and explicitly opt-in.
- Report what changed, what was tested, and any limitations.
- Stop after each agreed implementation phase.

## TestRail reporting rules

- Preserve intermediate retry records for traceability.
- History window is configurable; default is 60 days.
- Maximum distinct historical runs per case/application/platform is configurable; default is 10.
- Retrieve the latest known result even when it is older than the history window.
- Application and platform mapping is configurable.
- Distinguish Not Applicable, Not Run, Blocked, Failed, Passed, and automation maintenance statuses.
- Do not automatically classify every failure as an application defect.
- Report insufficient historical data explicitly.

### Execution history and stability rules

- Each distinct Test Run represents one historical execution of a test case for a specific application and platform.
- Multiple results within the same Test Run (retries, failures, manual triage) must not be counted as separate historical executions.
- Use the latest result within each Test Run as its final outcome.
- Determine the latest known test status using the actual result timestamp, not only the Test Plan creation date.
- Calculate stability separately for each test case, application, and platform combination.
- Track the automation-issue rate (configurable status, e.g. "AU Issue") separately from application defect failures.
- Do not automatically classify consistently failing tests as flaky.
- Do not classify a test as stable when insufficient historical evidence is available.
- Preserve execution dates, application versions, and source TestRail IDs (plan, run, test, case, result) for auditability.

## Documentation

- Keep instructions concise and practical; avoid duplicating implementation details.
- Do not include customer-specific project information.
- Document configuration and usage in `README.md` once implementation begins.
