# CI workflow

Continuous integration for `legal-corpus-ingester`. Runs lint, type checking, and the full test suite on every pull request and push to main.

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

The single `test` job runs these steps sequentially:

1. `actions/checkout@v4` - pulls the ref under test.
2. `Set up Python` - runs `python3 --version` to confirm the runner's Python is reachable.
3. `Install` - creates `.venv` and runs `pip install -e '.[dev]'`. Output is tail-truncated to 5 lines.
4. `Lint` - runs `ruff check .` against the full codebase. Failures block the job.
5. `Type check` - runs `mypy src/`. Failures block the job.
6. `Unit tests` - runs `pytest tests/unit tests/snapshot tests/cli` with coverage collection. Coverage XML is written for the upload step. Output is tail-truncated to 30 lines.
7. `Integration tests` - runs `pytest tests/integration`. Output is tail-truncated to 30 lines.
8. `E2E tests` - runs `pytest tests/e2e`. Output is tail-truncated to 20 lines.
9. `Upload coverage` - uploads `coverage.xml` as a GitHub Actions artifact named `coverage-report-<run_id>`. Runs even if earlier steps fail (`if: always()`).

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

- **Job fails on lint or type check**: ruff or mypy found a violation. Read the
  step log for the exact file and line number, fix locally, and push again.

- **Job fails on a test step**: the step log shows the pytest summary. Run the
  same pytest command locally with `.venv` activated to reproduce.

- **Cancelled by concurrency**: expected if a newer commit lands on the same
  ref while a run is active. Rerun manually only if the newer run also
  failed for an unrelated reason.
