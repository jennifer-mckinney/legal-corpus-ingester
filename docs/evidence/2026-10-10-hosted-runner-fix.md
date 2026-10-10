# Green log: CI on GitHub-hosted ubuntu-latest runners (ingester #19)

Date: 2026-10-10. Branch: `pr69/hosted-workflows`, on top of red commit 49d8f86.
Scope: checklist section A (`docs/evidence/2026-10-10-hosted-runner-move-checklist.md` on the docs/PR #69 line).

## Changes

| File | Change |
|---|---|
| ci.yml, health.yml, refresh.yml, vcr-drift.yml, approval-expiry.yml | `runs-on: ubuntu-latest` (plain string) |
| .github/actionlint.yaml | Kept, because `test_workflow_validation.py` passes it as `-config-file`. `legal-corpus-ingester` label removed; `self-hosted-runner.labels: []`; the comment now says why there are no labels |
| vcr-drift.yml | Stash comment: the "shared /tmp on the self-hosted runner" wording is gone, and it now says GNU cp on ubuntu-latest |
| p9-review.yml | Header: every workflow runs on GitHub-hosted ubuntu-latest |
| refresh.yml, approval-expiry.yml | Preflight error: gh ships on the ubuntu-latest image, so a missing gh means the image changed |
| ci.yml | Both actionlint SHA-256 pins kept (darwin_arm64, linux_amd64). The step comment does not name a platform, so it is unchanged |
| tests/unit/test_python_version_pin.py, test_p9_review_workflow.py, test_vcr_canary_contract.py | Stale self-hosted/macOS comments corrected. The BSD cp model is kept, and its docstring now says where GNU differs |
| scripts/detect_bundle_change.py | Docstring: the record lives under the runner's home and persists only as long as that home does |

`git diff --name-status` before this file was added: 11 x `M`, no `A`.

## Gates

```
pytest tests/unit/test_hosted_runner.py -q --no-cov
75 passed

pytest tests/unit tests/snapshot tests/cli -q -rs --cov=src/legal_corpus_ingester --cov-fail-under=0
TOTAL 1170 264 77%
SKIPPED [2] tests/unit/test_workflow_validation.py:106: actionlint not on PATH; CI installs the pinned release (ci.yml)
1232 passed, 2 skipped, 1 xfailed   rc=0

pytest tests/unit -q
1199 passed, 2 skipped, 1 xfailed

ruff check .        All checks passed!  rc=0
mypy src/           Success: no issues found in 39 source files  rc=0
```

`ruff format --check` is not a CI step. The four touched .py files were already unformatted at the base and still are, so this change makes no format difference.

### actionlint (the 2 local skips, run for real)

The pinned actionlint 1.7.12 darwin_arm64 release was fetched into a scratch dir. Its SHA-256 `aba9ced2...6953f` matches the ci.yml pin.

```
PATH=<scratch>:$PATH pytest tests/unit/test_workflow_validation.py -q --no-cov
29 passed
actionlint -config-file .github/actionlint.yaml .github/workflows/*.yml   rc=0, no output
```

Probe: in a scratch copy of health.yml, `runs-on` was set back to `[self-hosted, legal-corpus-ingester]`. actionlint now rejects it with `label "legal-corpus-ingester" is unknown [runner-label]`, so the emptied config bites. A bare `self-hosted` label is built into actionlint and still passes it. `test_no_runs_on_context_under_github_names_a_self_hosted_label` covers that case.

## Sweep (R3)

- `git grep -nF "restart the runner service"` across docs automations .claude .github scripts src tests README*: 0 hits.
- `self-hosted` under .github/: the actionlint.yaml comment and key (allowed by the red contract), plus `.github/p9/security-engineer.md:57`, which is owned by the docs branch (checklist B) and was not touched.

## Open item for a card (not fixed here; out of scope)

`refresh.yml` keeps the last-announced bundle record under `$XDG_STATE_HOME` / `~/.local/state`. A GitHub-hosted runner starts every run with a fresh home, so `detect_bundle_change.py detect` finds no record (`previous is None`) and will report every successful refresh as a change. This is dormant today, because `ingester refresh` exits 3 (EXIT_NOT_WIRED) until G2. The state needs a durable home (for example actions/cache, an artifact, or a repo variable) before G2 wires refresh.
