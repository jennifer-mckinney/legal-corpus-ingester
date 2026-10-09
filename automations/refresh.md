# weekly corpus refresh

## Purpose

Runs a full `ingester refresh --all` on a weekly schedule to keep every configured
source current. When the bundle target changes, the workflow opens a GitHub issue
prompting the operator to validate consumer compatibility before updating the
terms-analysis consumer.

## Trigger

The workflow (`.github/workflows/refresh.yml`) fires on:

- Cron schedule: `0 3 * * 0` (Sunday 3 AM UTC)
- `workflow_dispatch` (manual trigger from the Actions UI or `gh workflow run`)

Only one refresh job can run at a time. `cancel-in-progress: false` ensures a
running refresh is never cancelled mid-bundle -- a partial write would produce an
invalid bundle.

## What it does

The `refresh` job runs six steps in order:

1. **Preflight.** Fails the run with an `::error::` annotation when the runner has
   no `gh` CLI or the `corpus-refresh` issue label cannot be read, and names the
   fix (`gh label create corpus-refresh`). The label is set once, as the job's
   `ISSUE_LABEL`, and step 5 files under it.

2. **Record pre-refresh bundle target.** Reads `readlink out/current` before the
   refresh starts and saves the result to `GITHUB_OUTPUT` as `target`. If
   `out/current` does not yet exist (fresh install), `target` is set to the empty
   string.

3. **Run refresh.** Calls `ingester refresh --all` inside the virtualenv. If the
   command exits non-zero because no sources are configured or because of a transient
   error that `ingester` already handled, the step swallows the failure and exits 0
   to keep the workflow green. Hard failures (Python exceptions, missing config) are
   still surfaced through the step's stderr output.

4. **Detect bundle change.** Reads `readlink out/current` again and compares it to
   the pre-refresh value. Also runs `git status --porcelain out/ state/` to catch
   any uncommitted changes (new checkpoint files, updated manifest) that indicate
   ingest activity even when the symlink did not move. Sets `changed=true` if either
   check sees a difference.

5. **Open issue on corpus change.** Runs only when `changed=true`. Creates a GitHub
   issue titled `Weekly corpus refresh -- YYYY-MM-DD` with the bundle version and run
   ID in the body. The issue instructs the operator to run
   `ingester validate-round-trip out/current` before updating the terms-analysis
   consumer.

6. **Upload health report.** Uploads `out/health/` as a GitHub Actions artifact named
   `health-report-<run_id>`. This step runs with `if: always()` so the artifact is
   preserved even when earlier steps fail.

## What it produces

**GitHub issue** (conditional). When the bundle target changes, an issue is opened
with label `corpus-refresh`. The issue body contains:

- The new bundle version (the symlink target path, e.g. `out/2026.07.0`)
- The Actions run ID for traceability
- The event name that triggered the run
- The instruction to run `ingester validate-round-trip out/current`

No issue is opened when the refresh runs but produces no new bundle (all sources
returned the same content as the prior run). This avoids noise on weeks when nothing
changes.

**Artifact.** `out/health/` is uploaded on every run as `health-report-<run_id>`.
If the health check script wrote a report during the refresh, it lands in the
artifact.

## Failure mode

If `ingester refresh` exits non-zero, the step prints "Refresh failed or no sources
configured" and exits 0. The workflow continues, the health report artifact is still
uploaded, and no issue is opened (because no bundle change is detected). The failure
is visible in the step's stdout in the Actions run log.

Operators who want to investigate should:

1. Open the Actions run for the failed refresh.
2. Check the `Run refresh` step output for error messages.
3. Run `ingester status` locally to inspect checkpoint state.
4. If sources were partially written, check `ALERTS.md` for any source that set
   `publish_blocked: true`.

## Escalation

The GitHub issue opened on corpus change is the primary escalation artifact. An
operator should:

1. Read the issue body to identify the new bundle version.
2. Run `ingester validate-round-trip out/current` to confirm the bundle is
   structurally sound and consumer-compatible.
3. If validation passes, update the terms-analysis consumer to point at the new
   bundle and close the issue.
4. If validation fails, inspect the bundle, check `ALERTS.md`, and resolve any
   source-level errors before re-running the refresh.

The issue label `corpus-refresh` can be used to filter open refresh issues across
all weekly runs.

## Where it runs

The `refresh` job runs on the project's self-hosted runner, matched by label
`[self-hosted, legal-corpus-ingester]`. This satisfies HR4 (local-only data). No
corpus data leaves the runner.

## How to run manually

```bash
source .venv/bin/activate
ingester refresh --all
ingester validate-round-trip out/current
```

To trigger the GitHub Actions run manually:

```bash
gh workflow run refresh.yml
gh run list --workflow=refresh.yml --limit 5
```

## Relationship to other automations

| Automation | Relationship |
|------------|--------------|
| `health-check` | Health report uploaded as artifact on every refresh run |
| `vcr-drift` | Fires on a separate Sunday 4 AM schedule; detects upstream schema drift that could affect the next refresh |
| `approval-expiry` | Fires daily at 6 AM; expired approvals block ingest for gated sources before refresh runs |
| `ci-pr` | Validates changes to source config or pipeline code before they reach the refresh workflow |
