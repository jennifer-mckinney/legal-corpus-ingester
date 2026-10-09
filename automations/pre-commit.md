# pre-commit hook

## Purpose

Enforce ruff + license-hashes audit + mypy + pytest unit checks before every local commit lands. It is a fast local gate that catches lint, license drift, typing, and unit-test regressions before they leave the workstation. It complements the pre-push gate rather than replacing it.

## Trigger

Runs automatically on `git commit`, after `core.hooksPath` is pointed at `.githooks/` by `scripts/install-hooks.sh`.

## Behavior

The hook runs four checks in order:

1. `ruff check .` - blocking. A ruff violation blocks the commit.
2. License-hashes audit - blocking if `state/license-hashes.json` exists. Reads the file and checks every source entry for `status` of `DRIFT` or `SPDX_DRIFT`. If any drifted source is found, the commit is blocked with a per-source message. If the file does not exist, this step is silently skipped.
3. `mypy src/` - blocking. A mypy failure blocks the commit.
4. `pytest tests/unit -q` - blocking once tests exist. Output is tail-truncated to the last 20 lines so the commit output stays readable. If `tests/unit/` is missing or empty, the hook exits 0 after the above checks.

If `pyproject.toml` is not present in the repo root, the hook exits 0 silently. If `.venv/` is missing, the hook fails loudly with a pointer to the bootstrap command:

```
python3 -m venv .venv && source .venv/bin/activate && pip install -e .[dev]
```

## Behavior table

| Condition | Behavior |
|-----------|----------|
| No `pyproject.toml` | exit 0, skip everything |
| No `.venv/` | exit 1, print bootstrap instructions |
| `tests/unit/` missing or empty | exit 0 after ruff + license audit + mypy |
| ruff fails | exit non-zero, block commit |
| license drift detected in `state/license-hashes.json` | exit non-zero, block commit |
| mypy fails | exit non-zero, block commit |
| pytest fails with tests present | exit non-zero, block commit |

## How to install

Run once per clone:

```bash
bash scripts/install-hooks.sh
```

The installer is idempotent. It sets `core.hooksPath=.githooks` and chmods every file under `.githooks/`. It no longer creates `.git/reviews/`: the P9 review runs in CI (`automations/p9-pre-push.md`).

Re-run `install-hooks.sh` after adding any new hook file to `.githooks/`. The chmod loop only runs at install time, so a freshly-cloned repo will not have the bit set on a hook that was added after the last install.

## How to bypass (and why not to)

`git commit --no-verify` disables the hook for a single commit. This is a P7 violation if used to bypass fixes. It is only defensible in a pre-approved emergency situation - for example, a merge conflict resolution where the working tree is temporarily inconsistent and the fix lands in a follow-up commit reviewed by the user.

Bypassing pre-commit does not bypass P9: every PR to `main` still runs the `security-review` and `grumpy-review` CI jobs.

## Relationship to P9 review

The pre-commit hook gates local commits with fast checks (lint, license audit, types, unit tests). The P9 independent review (security-engineer + grumpy-developer) runs as CI jobs on every PR to `main` (`.github/workflows/p9-review.yml`). They are independent - passing pre-commit does not satisfy P9, and bypassing pre-commit does not disable it. There is no local pre-push hook.

## Failure mode

If `.venv` exists but `ruff`, `mypy`, or `pytest` are not installed inside it, the hook will fail when the corresponding command is invoked. The remedy is:

```bash
source .venv/bin/activate
pip install -e '.[dev]'
```

This installs the project in editable mode along with the dev extras (ruff, mypy, pytest, and pytest plugins).

The hook also refuses to source `.venv/bin/activate` if the file is a symlink or is tracked in git (SecF6). `.venv/` is gitignored, but `git add -f .venv/bin/activate` could force-track a malicious activate script. The two guards run before `source` and abort the commit with a clear message. If a legitimate workflow ever needs a tracked activate script, remove the guards deliberately in a reviewed change rather than working around them.

## Auditability

`--no-verify` skips the hook entirely, and nothing downstream can tell whether pre-commit ran. To make bypasses observable, the hook appends a timestamp to `.git/pre-commit.log` at every successful completion (SecF7). The log lives inside `.git/`, so it is untracked and per-clone.

To verify pre-commit ran for a given commit, check `.git/pre-commit.log`. The last timestamp should be within seconds of the commit's authored time. A gap indicates a `--no-verify` bypass.

```bash
tail .git/pre-commit.log
```

This is a material auditability improvement, not a cryptographic proof. A stronger scheme would use a `prepare-commit-msg` hook to inject a `Verified-by: pre-commit` commit trailer that ties the audit record to the commit hash. That is tracked as a follow-up.
