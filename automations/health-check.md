# health-check automation

## Purpose

Nightly freshness audit for all configured legal corpus sources. The workflow
confirms that each source has been ingested recently and produces a human-readable
markdown report for review.

## Trigger

The workflow (`.github/workflows/health.yml`) fires on:

- Cron schedule: `0 3 * * *` (3 AM UTC daily)
- `workflow_dispatch` (manual trigger from the Actions UI or `gh workflow run`)

## What it does

The `health` job runs two checks in this order. Neither step has `if:` or
`continue-on-error`, so if the first fails the second does not run:

1. `Run health check` (`python scripts/health_check.py`) - reads
   `config/sources/*.yaml` to enumerate sources, inspects
   `<state dir>/<source>.checkpoint.json` for last-run time and stage, and writes
   a markdown table to `out/health/YYYY-MM-DD.md`. It runs first so that an
   invalid checkpoint surfaces as exit 4 (#17) and fails the job before
   `ingester status` can crash on the same file.
2. `Run ingester status` (`ingester status`) - the CLI's built-in status command.
   It runs only after the health check passed or warned with exit 3. It prints a
   `<name>: <stage>` line only for configured sources that have a checkpoint;
   configured sources with no checkpoint print nothing. When no configured
   source has a checkpoint it prints "No runs recorded." and exits 0. A sources
   dir that loads but holds no source exits 5 (`EXIT_NO_SOURCES`). A
   `--sources-dir` path that does not exist or is not a directory exits
   6 (`EXIT_SOURCES_DIR_MISSING`, #108; it exited 1 before). A sources dir that
   exists but cannot be loaded (unreadable, or a config that fails to parse or
   validate) exits 1 (`EXIT_LOAD_ERROR`). Exit 2 is a command-line usage error
   only. The codes are listed in `ingester status --help` and `ingester --help`
   (terms-analysis#190). It is not masked; a crash fails the job
   (terms-analysis#90).

Both steps pass `--state-dir "$INGESTER_STATE_DIR"`, the directory refresh writes,
declared with the same value in the workflow `env` of `health.yml` and `refresh.yml`
and outside the checkout (`actions/checkout` cleans ignored files, which wipes the
gitignored `state/`). Before them, `actions/cache/restore` restores the state the newest
refresh run saved (ingester #71); health never saves. On a miss (no refresh has run
yet, or the cache was evicted after 7 days unused) the directory is empty and the
health check fails every source as never-run: a red run, not a green one. See
`automations/refresh.md`, "State between runs".

## What it produces

`out/health/YYYY-MM-DD.md` - a markdown table with one row per source:

```markdown
# Legal Corpus Health -- YYYY-MM-DD

| Source | Last Run (UTC) | Stage | Lag (days) | Status |
|--------|----------------|-------|-----------|--------|
| eurlex | 2026-07-04T12:00:00Z | done | 0.5 | fresh |
| cfpb   | never          | -     | -         | never-run |
```

The `out/health/` directory is uploaded as a GitHub Actions artifact named
`health-report-<run_id>` on every run, even if earlier steps fail.

## Freshness threshold

A source is considered stale if its checkpoint mtime is 8 days or older.
`health_verdict()` in `scripts/health_check.py` is the one place the exit code is
decided:

| Exit | Meaning | Workflow result |
|------|---------|-----------------|
| 0 | Every source is fresh | green |
| 1 | A source is stale, its last checkpoint stage is not the success stage `done` (status `failed`: `not_approved`, any `*_failed`, an unfinished or unknown stage, however recent), or it is never-run while refresh is wired | red |
| 2 | Config problem: `config/sources/` missing, unreadable or holding zero source configs, a non-file `*.yaml` entry, or an empty / non-mapping source YAML | red |
| 3 (`EXIT_NOT_WIRED`) | The only problem is never-run sources and `cli.REFRESH_WIRED` is False | green with a "Refresh not wired" warning annotation |
| 4 (`EXIT_CHECKPOINT_INVALID`) | A checkpoint file exists but is not a valid `CheckpointState` for its source (see below). Outranks 1 and 3 (stale or never-run sources do not mask it), wired or not | red |

Exit 3 was the known unwired state: no checkpoint could exist while `ingester
refresh` did not run the pipeline. Refresh is wired since terms-analysis#93 and
`REFRESH_WIRED` is True, so a never-run source now exits 1 and the run is red until
every configured source has a fresh checkpoint. The step still turns exactly code 3
into a warning (the code is reserved) and fails on every other non-zero code,
including a crash. A stale checkpoint is never excused.

A checkpoint is valid only when it is a regular file (a symlink to one is
followed) holding strict UTF-8 JSON that `CheckpointStore.load` accepts: an
object with exactly the `CheckpointState` fields (`pipeline/state.py`), every
field without a default present, no unknown keys, each field a string, and
`source_name` naming its own source. The rule is `checkpoint_problem` in
`pipeline/state.py`, shared with the store, so a checkpoint that passes the
health check never crashes `ingester status` (#17). Anything else at the checkpoint path, including a directory, a FIFO
or a dangling symlink, is invalid: the source's row shows `checkpoint-invalid`, the report names the file
and the reason without echoing its contents, and the script exits 4 (#17). A
path with nothing at it is still never-run.

The threshold is configurable:

```bash
python scripts/health_check.py --stale-days 14
```

## Failure mode

If `health_check.py` exits 1 (stale or failed source, or never-run once refresh is wired),
2 (config problem: `config/sources/` missing, unreadable or holding zero source
configs, a `*.yaml` entry that is not a regular file, or a source YAML that is
empty or not a mapping; terms-analysis#90, #173), 4 (an invalid checkpoint;
#17) or any other code except 3,
the `Run health check` step is marked failed. None of these is masked
(terms-analysis#90). The artifact upload still runs because it has
`if: always()`. No issue is auto-opened - this is a monitoring report, not an
alerting trigger. Alerting comes from VCR drift canary failures or publish
pipeline failures, which are separate workflows.

## Where it runs

The `health` job runs on GitHub-hosted `ubuntu-latest` (ADR-016). It reads
tracked source configs and run checkpoints, not corpus data.

## How to run manually

```bash
source .venv/bin/activate
python scripts/health_check.py --stale-days 14
cat out/health/$(date +%Y-%m-%d).md
```

To trigger the GitHub Actions run manually:

```bash
gh workflow run health.yml
gh run list --workflow=health.yml --limit 5
```

## CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `--config-dir DIR` | `config/sources` | Directory with per-source YAML files |
| `--state-dir DIR` | `state` | Directory with checkpoint JSON files (the workflow passes the XDG state dir above) |
| `--out-dir DIR` | `out` | Output directory for bundles and health reports |
| `--stale-days INT` | `8` | Days before a source is considered stale |
