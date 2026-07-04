# CI workflow

Baseline continuous integration for `legal-corpus-ingester`. Kept intentionally
minimal at this stage; steps activate as the relevant scaffolding lands.

## Trigger

The workflow (`.github/workflows/ci.yml`) fires on:

- `pull_request` (any branch)
- `push` to `main`

## Where it runs

Jobs execute on the project's self-hosted runner, matched by label set
`[self-hosted, legal-corpus-ingester]`. That runner is provisioned per Task P2
and lives outside the repo. If the labels drift or the runner is offline, jobs
stay in the `queued` state rather than failing loudly; see the failure-mode
note below.

## Steps

The single `test` job runs sequentially:

1. `actions/checkout@v4` pulls the ref under test.
2. `python3 --version` confirms the runner's Python is reachable.
3. Conditional install: if `pyproject.toml` is present, create `.venv` and
   `pip install -e '.[dev]'`. Otherwise the step logs that install is skipped.
4. Conditional lint: if `pyproject.toml` contains a `[tool.ruff]` section, run
   `ruff check .`. Otherwise skipped.
5. Conditional test: if `tests/` exists and is non-empty, run
   `pytest --cov-fail-under=0`. Otherwise skipped.

The conditionals let the workflow pass green before code exists. As each
gating file appears in later tasks, the corresponding step becomes active on
its own; no workflow edit is required to turn steps on.

## Adding steps as the codebase grows

Edit `.github/workflows/ci.yml` directly. Two patterns are worth keeping:

- Keep new steps guarded by a file-existence check until their prerequisite
  lands, so intermediate commits do not break CI.
- Preserve the `runs-on: [self-hosted, legal-corpus-ingester]` label pair.
  Adding or removing labels here without matching the runner registration
  will leave jobs queued indefinitely.

## Concurrency

The workflow declares `concurrency.group: ci-${{ github.ref }}` with
`cancel-in-progress: true`. New pushes or PR updates on the same ref cancel
any prior in-flight run for that ref, so only the latest commit's result
counts.

## How to see results

```bash
# List recent CI runs
gh run list --workflow=ci.yml --limit 10

# Inspect a specific run (get the ID from the list above)
gh run view <run-id>

# Full log tail for a run
gh run view <run-id> --log
```

For a live tail while a run is in progress, add `--watch` to `gh run view`.

## Failure modes

- **Job stays `queued`**: usually a `runs-on` label mismatch or an offline
  runner. Check runner registration and status with:

  ```bash
  gh api /repos/jennifer-mckinney/legal-corpus-ingester/actions/runners
  ```

  Look for the runner in the response and confirm its `status` is `online`
  and its `labels` include both `self-hosted` and `legal-corpus-ingester`.

- **Job fails on a conditional step**: read the step log; the first line of
  each conditional step logs whether it ran or skipped, which narrows the
  investigation quickly.

- **Cancelled by concurrency**: expected if a newer commit lands on the same
  ref while a run is active. Rerun manually only if the newer run also
  failed for an unrelated reason.
