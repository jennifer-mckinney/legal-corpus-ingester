# weekly corpus refresh

## Purpose

Runs a full `ingester refresh --all` on a weekly schedule to keep every configured
source current. When the published bundle changes, the workflow opens a GitHub issue
prompting the operator to validate consumer compatibility before updating the
terms-analysis consumer.

## Trigger

The workflow (`.github/workflows/refresh.yml`) fires on:

- Cron schedule: `0 3 * * 0` (Sunday 3 AM UTC)
- `workflow_dispatch` (manual trigger from the Actions UI or `gh workflow run`). Its one
  input, `seed_bundle_record` (boolean, default `false`), seeds the first bundle record;
  see "State between runs" below.

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

   **Restore ingester state.** `actions/cache/restore` restores `INGESTER_STATE_DIR`
   from the newest earlier run's save (see "State between runs"). A miss does not fail
   this step; step 3 makes it a red run.

2. **Run refresh.** Calls `ingester refresh --all` inside the virtualenv, under the
   workflow-level `bash -eo pipefail` shell. Any non-zero exit fails the job, including
   5 (`EXIT_NO_SOURCES`: `config/sources/` is tracked, so an empty registry means a
   broken checkout), 6 (`EXIT_SOURCES_DIR_MISSING`: `config/sources/` does not exist
   or is not a directory, also a broken checkout; #108), 7 (`EXIT_NO_APPROVED_SOURCES`:
   no source holds a valid approval, so nothing ran; every repo approval is a
   placeholder until the owner signs real hashes, so the run is red until then),
   8 (`EXIT_SOURCE_FAILED`: at least one source failed; each is named on stderr and
   checkpointed, and the remaining sources still run; terms-analysis#93) and 9
   (`EXIT_LICENSE_BASELINE_MISSING`: the licence baseline file is missing and the run
   does not seed it, so nothing ran and nothing was written; see "State between runs").
   The step passes `--seed-license-baseline` only on a dispatch with
   `seed_bundle_record` ticked; a scheduled run never seeds. A source
   without a valid approval, while another source has one, is such a failure: it is
   not run, it is named on stderr and it is checkpointed as `not_approved`, so a
   partly signed run is red, never a silent partial success. `--dry-run` exits with
   the same code and writes nothing. The full list is in `ingester --help`.
   When every source succeeds, refresh publishes one combined bundle
   `out/<YYYY.M.patch>/` and repoints the relative link `out/current` at it. A run with
   a failed source publishes no bundle and leaves `out/current` alone. A run whose
   content equals the bundle `out/current` already points at publishes nothing (exit
   0), so step 3 reports no change. `out/` is not cached, so on a hosted runner there
   is no `out/current` at run start: refresh also keeps a producer version record,
   `$INGESTER_STATE_DIR/producer-version-record.json` (the version it last published
   and that bundle's step 3 fingerprint), written only after a full publish. Content
   equal to the record's is republished under the recorded version, even in a later
   month, so step 3 reports no change. New content takes the next patch after the
   highest of the record's version and the bundle dirs in `out/` that month (`.0` in a
   new month), so a version name is never reused for different content. A missing
   record (first run or cache eviction) falls back to the dirs in `out/` alone; a
   malformed, unreadable or linked record exits 1 with its type only and runs nothing.
   The file is separate from step 3's `last-published-bundle.json`.
   `out/<source>/` is per-source staging; nothing reads it.

3. **Detect bundle change.** Runs `scripts/detect_bundle_change.py detect`. It reads
   the bundle `out/current` points at and fingerprints it (SHA256 of its
   `checksums.txt` lines, leaving out run-metadata `MANIFEST.yaml`). It compares the
   version and fingerprint with the record of the last announced bundle, kept in
   `$INGESTER_STATE_DIR/last-published-bundle.json`. Sets `changed=true` on a new
   version or new content. `--require-state` turns a missing record (the first run
   was not seeded, or the cache was evicted) into exit 2, a red run that announces
   nothing. The record lives outside the checkout because
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

   **Save ingester state.** `actions/cache/save` saves `INGESTER_STATE_DIR` with
   `if: always()`, so the failure checkpoints and licence baseline of a red run persist
   too, under a key unique to the run and attempt (a cache key is immutable).

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

The `refresh` job runs on GitHub-hosted `ubuntu-latest` (ADR-016). The runner
filesystem is discarded after each run, so `out/` does not survive between runs.

## State between runs

Checkpoints, the refresh licence baseline (`refresh-license-baseline.json`), the
`audit-license` baseline (`license-hashes.json`) and the bundle record
(`last-published-bundle.json`) all live in one directory, `INGESTER_STATE_DIR`.
`refresh.yml` and `health.yml` declare it with the same value in their workflow
`env`: the parent of the workspace, outside the checkout (the `runner` context is not
available in workflow or job `env`, so `runner.temp` cannot be used there). It
persists between hosted runners through the `actions/cache/restore` and
`actions/cache/save` pair (ingester #71, option 1): refresh restores the newest save
by the `ingester-state-<os>-` prefix and saves under
`ingester-state-<os>-<run_id>-<run_attempt>`; health restores the same path and never
saves.

The first run has neither the bundle record nor the refresh licence baseline
(`refresh-license-baseline.json`), so an unseeded refresh exits 9 before any source runs
and writes nothing. Seed both once: run the workflow by hand with `seed_bundle_record`
ticked (`gh workflow run refresh.yml -f seed_bundle_record=true`). That run passes
`--seed-license-baseline` to refresh, which records the licence (SPDX) each source
declares today in `refresh-license-baseline.json`: seeding accepts the current SPDX as
the baseline, with no drift check against an earlier one, so confirm each source's
current licence before you tick the box. The seed step then records the bundle that
run published as already announced, before detect reads it, so that run announces
nothing and later runs compare against it.

A cache unused for 7 days is evicted. The next scheduled run then finds no
`refresh-license-baseline.json` and exits 9 (and, were the baseline present, a missing
bundle record still exits 2 in detect), red until it is seeded again: never a silent
re-baseline of the licence baseline or the bundle record. The seed flag only replaces a
missing `refresh-license-baseline.json`; a source whose SPDX changed against an existing
baseline is still blocked as `license_failed`, seed or not. A baseline file with no
entry for a newly approved source is not missing: that source is new and runs.

## How to run manually

```bash
source .venv/bin/activate
ingester refresh --all
# First run, or the state dir was lost: confirm each source's licence, then seed the
# licence baseline (otherwise refresh exits 9 and writes nothing).
ingester refresh --all --seed-license-baseline
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
