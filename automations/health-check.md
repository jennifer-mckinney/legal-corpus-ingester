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
   sources, inspects `state/<source>.checkpoint.json` for last-run time and stage,
   and writes a markdown table to `out/health/YYYY-MM-DD.md`.

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
The script exits 1 if any source is stale or has never run, causing the
`Run health check` step to fail. The artifact is still uploaded.

The threshold is configurable:

```bash
python scripts/health_check.py --stale-days 14
```

## Failure mode

If `health_check.py` exits 1 (stale or never-run source) or 2 (config problem:
`config/sources/` missing, unreadable or holding zero source configs, a `*.yaml`
entry that is not a regular file, or a source YAML that is empty or not a mapping;
terms-analysis#90, #173), the `Run health check` step is marked failed.
Neither code is masked (terms-analysis#90). The artifact upload still runs because it has
`if: always()`. No issue is auto-opened - this is a monitoring report, not an
alerting trigger. Alerting comes from VCR drift canary failures or publish
pipeline failures, which are separate workflows.

## Where it runs

The `health` job runs on the project's self-hosted runner, matched by label
`[self-hosted, legal-corpus-ingester]`. This satisfies HR4 (local-only data).
No corpus data leaves the runner.

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
| `--state-dir DIR` | `state` | Directory with checkpoint JSON files |
| `--out-dir DIR` | `out` | Output directory for bundles and health reports |
| `--stale-days INT` | `8` | Days before a source is considered stale |
