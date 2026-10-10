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

The `health` job runs two checks in order:

1. `ingester status` - calls the CLI's built-in status command. It exits 0 on a
   healthy system (including "No runs recorded."), so it is not masked; a crash
   fails the job (terms-analysis#90).
2. `python scripts/health_check.py` - reads `config/sources/*.yaml` to enumerate
   sources, inspects `<state dir>/<source>.checkpoint.json` for last-run time and
   stage, and writes a markdown table to `out/health/YYYY-MM-DD.md`.

Both steps pass `--state-dir "${XDG_STATE_HOME:-$HOME/.local/state}/legal-corpus-ingester/state"`,
outside the checkout. `actions/checkout` cleans ignored files, which wipes the
gitignored `state/`, so reading `./state` would always report every source as
never-run. On a GitHub-hosted runner the whole filesystem is new each run, so
this directory is empty too until state persistence is designed (ADR-016). When
refresh is wired it must write its checkpoints to this same directory and
persist them across runs.

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
| 1 | A source is stale, or never-run while refresh is wired | red |
| 2 | Config problem: `config/sources/` missing, unreadable or holding zero source configs, a non-file `*.yaml` entry, or an empty / non-mapping source YAML | red |
| 3 (`EXIT_NOT_WIRED`) | The only problem is never-run sources and `cli.REFRESH_WIRED` is False | green with a "Refresh not wired" warning annotation |

Exit 3 is the known unwired state: `ingester refresh` does not run the pipeline
yet, so no checkpoint can exist. The step turns exactly that code into a warning
and fails on every other non-zero code, including a crash. A stale checkpoint is
never excused. The PR that wires refresh flips `REFRESH_WIRED` to True (a unit
test fails until it does), after which never-run exits 1.

The threshold is configurable:

```bash
python scripts/health_check.py --stale-days 14
```

## Failure mode

If `health_check.py` exits 1 (stale source, or never-run once refresh is wired),
2 (config problem: `config/sources/` missing, unreadable or holding zero source
configs, a `*.yaml` entry that is not a regular file, or a source YAML that is
empty or not a mapping; terms-analysis#90, #173) or any other code except 3,
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
