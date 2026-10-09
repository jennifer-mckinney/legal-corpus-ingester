# approval expiry watcher

## Purpose

Sources with license risk require a signed APPROVAL.yaml before ingest can proceed
(HR9). Those approvals carry an expiry date. If an approval expires silently, the
next `ingester refresh` will gate-fail mid-run -- a worse outcome than catching the
expiry in advance. This watcher runs daily, checks every APPROVAL.yaml file in
`config/approvals/`, and opens a GitHub issue when any approval has expired or is
nearing expiry.

## Trigger

The workflow (`.github/workflows/approval-expiry.yml`) fires on:

- Cron schedule: `0 6 * * *` (6 AM UTC daily)
- `workflow_dispatch` (manual trigger from the Actions UI or `gh workflow run`)

Running daily keeps the warning window (60 days by default) actionable. An expiry
surfaced 60 days out gives the operator sufficient lead time to obtain renewed
approval before the next weekly refresh.

## What it checks

`scripts/check_approvals.py` walks every `*.yaml` file in `config/approvals/` and
classifies each as:

| Status | Condition |
|--------|-----------|
| `OK` | Expiry is more than 60 days away |
| `EXPIRING_SOON` | Expiry is within 60 days |
| `EXPIRED` | Today is on or past the expiry date |
| `ERROR` | YAML is malformed or the expiry field is missing / unparseable |

The script prints a markdown table to stdout with one row per approval file:

```
| source_id | status | expiry | days_remaining |
|-----------|--------|--------|----------------|
| sg-sso-terms | EXPIRING_SOON | 2026-08-01 | 28 |
```

Exit 0 when all approvals are OK or EXPIRING_SOON. Exit 1 when any approval is
EXPIRED or ERROR. Exit 2 (config problem, message on stderr) when there is nothing
to check: the approvals dir or the `--sources-dir` (default `config/sources`) is
missing or unreadable, a `*.yaml` entry is not a regular file, or the approvals dir
holds zero approval files. Zero approvals is never success, with or without source
configs (terms-analysis#173).

## What it produces

**GitHub issue** (conditional). When the check script exits non-zero (1 or 2), the workflow creates the
`approval-expiry` label if it is missing (`gh label create --force`, idempotent; a real
error fails the step). It then opens an issue titled `Approval expiry alert -- YYYY-MM-DD`
with that label. The
issue body instructs the operator to run `python scripts/check_approvals.py` locally
for the full status table and to update the affected APPROVAL.yaml files before the
next ingester refresh.

No issue is opened when all approvals are current. This keeps the issue tracker quiet
during normal operation.

## Failure mode

Before any check runs, a `Preflight` step fails the run with an `::error::`
annotation when the runner has no `gh` CLI or the `approval-expiry` issue label
cannot be read, and names the fix (`gh label create approval-expiry`). The label
is set once, as the job's `ISSUE_LABEL`, and the issue step files under it.

If `check_approvals.py` exits 1 (expired or invalid approval) or 2 (config
problem, see above), the `Run approval check` step is marked failed and the `Open issue on expiry` step fires. That step's
condition is `failure() && steps.approval_check.outcome == 'failure'`, so a
Preflight or install failure fails the job without filing an expiry issue. The workflow
job itself ends in failure, making the problem visible in the Actions summary without
requiring the operator to inspect logs proactively.

If checkout or `Install` fails, the approval check is skipped and no expiry issue is
opened, because that alert would be misleading. The run stays plain red, and GitHub
sends its standard failed-scheduled-workflow notification (terms-analysis#90).

If `config/approvals/` is missing, unreadable or holds no `*.yaml` approval files,
the script prints the reason to stderr and exits 2. The run goes red and the expiry
issue opens; its body is generic, so read the step log for the exact cause. The job
never passes silently on an empty or absent approvals dir (terms-analysis#173).

## Escalation

When an issue fires:

1. Open the issue and read the source_id in the alert.
2. Run `python scripts/check_approvals.py` locally to see the full table.
3. For each EXPIRED entry:
   a. Contact the approving party to obtain a renewed approval.
   b. Update `config/approvals/<source>.yaml` with the new expiry date and
      `signed_artifact_sha256` if the artifact changed.
   c. Re-run the script to confirm the approval is now OK.
4. Close the issue once all expired approvals are resolved.
5. If renewal is not immediately possible, set the affected source to
   `publish_blocked: true` in its source config and add an entry to `ALERTS.md`
   to prevent silent corpus corruption on the next refresh.

## Where it runs

The `approval-expiry` job runs on the project's self-hosted runner, matched by label
`[self-hosted, legal-corpus-ingester]`. This satisfies HR4 (local-only data). The
approval files and any associated artifact hashes remain on-machine.

## How to run manually

```bash
python scripts/check_approvals.py --approvals-dir config/approvals
```

To customise the warning window:

```bash
python scripts/check_approvals.py --approvals-dir config/approvals --warn-days 90
```

To trigger the GitHub Actions run manually:

```bash
gh workflow run approval-expiry.yml
gh run list --workflow=approval-expiry.yml --limit 5
```

## CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `--approvals-dir DIR` | `config/approvals` | Directory containing APPROVAL.yaml files |
| `--warn-days INT` | `60` | Days before expiry at which to emit EXPIRING_SOON |

## Relationship to other automations

| Automation | Relationship |
|------------|--------------|
| `refresh` | Fires Sunday 3 AM UTC; approval-expiry fires daily at 6 AM so expired approvals are caught before the weekly refresh |
| `ci-pr` | Validates APPROVAL.yaml structure on every PR that touches `config/approvals/` |
| `health-check` | Checks source freshness independently; does not inspect approval state |
