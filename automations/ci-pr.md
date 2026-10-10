# CI workflow

Continuous integration for `legal-corpus-ingester`. Runs lint, type checking, and the full test suite on every pull request and push to main.

## Trigger

The workflow (`.github/workflows/ci.yml`) fires on:

- `pull_request` (any branch)
- `push` to `main`

## Where it runs

Jobs run on GitHub-hosted `ubuntu-latest` runners (ADR-016). There is no
runner to provision. If GitHub's hosted capacity is unavailable, jobs stay in
the `queued` state rather than failing loudly; see the failure-mode note below.

## Steps

The single `test` job runs these steps sequentially. Every step runs under the workflow-level `shell: bash -eo pipefail {0}`, so a failing
command piped into `tail` still fails the step (terms-analysis#90).

1. `actions/checkout@v4` - pulls the ref under test.
2. `Set up Python` - `actions/setup-python` installs the interpreter named in `.python-version` (the single Python pin) from the hosted runner's tool cache.
3. `Install` - creates `.venv` and runs `pip install -e '.[dev]'`. Output is tail-truncated to 5 lines.
4. `Lint` - runs `ruff check .` against the full codebase. Failures block the job.
5. `Type check` - runs `mypy src/`. Failures block the job.
6. `Install actionlint` - downloads the pinned actionlint release for the runner's platform, verifies its SHA-256 and version, and puts it on `PATH`. An unpinned platform, a checksum or version mismatch, or a failed download fails the job.
7. `Unit tests` - runs `pytest tests/unit tests/snapshot tests/cli` with coverage collection. This includes `tests/unit/test_workflow_validation.py`, which fails if any file in `.github/workflows/` does not parse or fails actionlint (config: `.github/actionlint.yaml`), so a broken workflow fails PR CI. Coverage XML is written for the upload step. Output is tail-truncated to 30 lines.
8. `Integration tests` - runs `pytest tests/integration`. Output is tail-truncated to 30 lines.
9. `E2E tests` - runs `pytest tests/e2e`. Output is tail-truncated to 20 lines.
10. `Upload coverage` - uploads `coverage.xml` as a GitHub Actions artifact named `coverage-report-<run_id>`. Runs even if earlier steps fail (`if: always()`).

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

- **Job stays `queued`**: GitHub-hosted capacity is unavailable, or `runs-on`
  names a label GitHub does not host. Check https://www.githubstatus.com/ and
  confirm the job's `runs-on` is `ubuntu-latest`.

- **Job fails on lint or type check**: ruff or mypy found a violation. Read the
  step log for the exact file and line number, fix locally, and push again.

- **Job fails on a test step**: the step log shows the pytest summary. Run the
  same pytest command locally with `.venv` activated to reproduce.

- **Cancelled by concurrency**: expected if a newer commit lands on the same
  ref while a run is active. Rerun manually only if the newer run also
  failed for an unrelated reason.
