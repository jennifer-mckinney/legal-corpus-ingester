# weekly corpus refresh

## Purpose

Runs a full `ingester refresh --all` on a weekly schedule to keep every configured
source current. When the published bundle changes, the workflow opens a GitHub issue
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

The `refresh` job runs these steps in order. Checkout uses
`persist-credentials: false` and no job-level token is set; only the Preflight and
issue steps get `GH_TOKEN` (terms-analysis#90).

1. **Preflight.** Fails the run with an `::error::` annotation when the runner has
   no `gh` CLI or the `corpus-refresh` issue label cannot be read, and names the
   fix (`gh label create corpus-refresh`). The label is set once, as the job's
   `ISSUE_LABEL`, and step 4 files under it.

2. **Run refresh.** Calls `ingester refresh --all` inside the virtualenv, under the
   workflow-level `bash -eo pipefail` shell. Any non-zero exit fails the job, including
   2 (`EXIT_NO_SOURCES`: `config/sources/` is tracked, so an empty registry means a
   broken checkout) and 3 (`EXIT_NOT_WIRED`: intentionally red until G2 wires the
   orchestrator; terms-analysis#90).

3. **Detect bundle change.** Runs `scripts/detect_bundle_change.py detect`. It reads
   the bundle `out/current` points at and fingerprints it (SHA256 of its
   `checksums.txt` lines, leaving out run-metadata `MANIFEST.yaml`). It compares the
   version and fingerprint with the record of the last announced bundle, kept in
   `$XDG_STATE_HOME/legal-corpus-ingester/last-published-bundle.json` (default
   `~/.local/state/...`) on the runner. Sets `changed=true` on a first publish, a new
   version or new content. The record lives outside the checkout because
   `actions/checkout`'s clean deletes the gitignored `out/`. That is why the old
   "readlink before refresh" comparison always saw an empty value and reported every
   run as a change. If refresh succeeded but `out/current` is missing, or the version
   or `checksums.txt` is invalid, the step exits 1. It does not report "no change".

4. **Open issue on corpus change.** Runs only when `changed=true`. Creates the
   `corpus-refresh` label first (`gh label create --force`, idempotent; a real error
   fails the step). Then creates a GitHub issue titled
   `Weekly corpus refresh -- YYYY-MM-DD` with the bundle version and run ID in the
   body. The issue tells the operator to run `ingester validate-round-trip out/current`
   from an environment with terms-analysis installed, which exits 4 otherwise, before
   updating the terms-analysis consumer. If a `corpus-refresh` issue is already open, no
   duplicate is created; the step comments on it naming the new bundle version instead.

5. **Record announced bundle.** Runs `scripts/detect_bundle_change.py record` only
   after the issue step succeeded. A failed alert is therefore retried on the next run.

6. **Upload health report.** Uploads `out/health/` as a GitHub Actions artifact named
   `health-report-<run_id>`. This step runs with `if: always()` so the artifact is
   preserved even when earlier steps fail.

## What it produces

**GitHub issue** (conditional). When the published bundle's version or content fingerprint differs from the last announced one, an issue is opened
with label `corpus-refresh`. If one is already open, a comment naming the new bundle version is added to it instead. The issue body contains:

- The new bundle version (the version `out/current` resolves to, e.g. `2026.07.0`)
- The Actions run ID for traceability
- The event name that triggered the run
- The instruction to run `ingester validate-round-trip out/current` from an
  environment with terms-analysis installed (exits 4 if the consumer is not importable)

No issue is opened when the refresh publishes a bundle whose version and content
fingerprint match the last announced one. This avoids noise on weeks when nothing
changes.

**Artifact.** `out/health/` is uploaded on every run as `health-report-<run_id>`.
If the health check script wrote a report during the refresh, it lands in the
artifact.

## Failure mode

If `ingester refresh` exits non-zero, the `Run refresh` step fails and the job goes
red. The later steps are skipped, the health report artifact is still uploaded
(`if: always()`), and no issue is opened. Nothing is masked (terms-analysis#90).

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
2. Run `ingester validate-round-trip out/current` from an environment where
   terms-analysis is importable, to confirm the bundle is structurally sound and
   consumer-compatible. If the consumer cannot be imported it exits 4
   (`EXIT_CONSUMER_SKIPPED`) instead of printing VALID; `--allow-missing-consumer`
   runs the structural checks only and prints `VALID (consumer check skipped)`.
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
# The consumer check imports backend.app.services.legal_kb from terms-analysis.
# Point PYTHONPATH at its src/ directory, or the command exits 4 (EXIT_CONSUMER_SKIPPED).
PYTHONPATH=<terms-analysis>/src ingester validate-round-trip out/current
# Structural checks only, no consumer: prints "VALID (consumer check skipped)"
ingester validate-round-trip out/current --allow-missing-consumer
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
