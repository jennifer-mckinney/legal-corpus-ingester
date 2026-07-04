# pre-commit hook

## Purpose

Enforce ruff + mypy + pytest unit checks before every local commit lands. It is a fast local gate that catches lint, typing, and unit-test regressions before they leave the workstation. It complements the pre-push gate rather than replacing it.

## Trigger

Runs automatically on `git commit`, after `core.hooksPath` is pointed at `.githooks/` by `scripts/install-hooks.sh`.

## Behavior in Phase 0.0

Phase 0.0 is bootstrapping. The hook exits 0 silently under any of these conditions:

- `pyproject.toml` is not present in the repo root
- `.venv/` is present but `tests/unit/` is empty or missing

This grace period exists because `pyproject.toml` lands in Task 1 and the first unit tests land shortly after. Attempting to enforce lint or tests before those artifacts exist would block every commit for no reason.

If `pyproject.toml` is present but `.venv/` is missing, the hook fails loudly with a pointer to the bootstrap command:

```
python3 -m venv .venv && source .venv/bin/activate && pip install -e .[dev]
```

## Behavior once `pyproject.toml` lands (Phase 0.1 Task 2)

The hook runs three checks in order:

1. `ruff check .` - blocking. A ruff violation blocks the commit.
2. `mypy src/` - non-blocking during Phase 0.0, blocking after Task 1. A soft-fail during Phase 0.0 prints a warning but does not block. Once Task 1 removes the `|| { ... }` guard, mypy failures will block.
3. `pytest tests/unit -q` - blocking once tests exist. Output is tail-truncated to the last 20 lines so the commit output stays readable.

## How to install

Run once per clone:

```bash
bash scripts/install-hooks.sh
```

The installer is idempotent. It sets `core.hooksPath=.githooks`, chmods every file under `.githooks/` (so both pre-commit and pre-push get the executable bit), and ensures `.git/reviews/` exists for the pre-push signoff artifacts.

Re-run `install-hooks.sh` after adding any new hook file to `.githooks/`. The chmod loop only runs at install time, so a freshly-cloned repo will not have the bit set on a hook that was added after the last install.

## How to bypass (and why not to)

`git commit --no-verify` disables the hook for a single commit. This is a P7 violation if used to bypass fixes. It is only defensible in a pre-approved emergency situation - for example, a merge conflict resolution where the working tree is temporarily inconsistent and the fix lands in a follow-up commit reviewed by the user.

The pre-push hook still runs even if pre-commit was bypassed, so `--no-verify` on commit does not let unreviewed code reach `origin`.

## Grace during Phase 0.0

The hook is deliberately permissive while the project is bootstrapping:

| Condition | Behavior |
|-----------|----------|
| No `pyproject.toml` | exit 0, skip everything |
| No `.venv/` | exit 1, print bootstrap instructions |
| `tests/unit/` missing or empty | exit 0 after ruff + mypy |
| ruff fails | exit non-zero, block commit |
| mypy fails | print warning, continue (Phase 0.0 only) |
| pytest fails with tests present | exit non-zero, block commit |

## Relationship to pre-push

The pre-commit hook gates local commits with fast checks (lint, types, unit tests). The pre-push hook gates remote pushes with the P9 independent-review workflow (security-engineer + grumpy-developer signoff). They are independent - passing pre-commit does not satisfy pre-push, and bypassing pre-commit does not disable pre-push.

## Failure mode

If `.venv` exists but `ruff`, `mypy`, or `pytest` are not installed inside it, the hook will fail when the corresponding command is invoked. The remedy is:

```bash
source .venv/bin/activate
pip install -e '.[dev]'
```

This installs the project in editable mode along with the dev extras (ruff, mypy, pytest, and pytest plugins).

The hook also refuses to source `.venv/bin/activate` if the file is a symlink or is tracked in git (SecF6). `.venv/` is gitignored, but `git add -f .venv/bin/activate` could force-track a malicious activate script. The two guards run before `source` and abort the commit with a clear message. If a legitimate workflow ever needs a tracked activate script, remove the guards deliberately in a reviewed change rather than working around them.

## Auditability

`--no-verify` skips the hook entirely, and the pre-push hook cannot tell whether pre-commit ran. To make bypasses observable, the hook appends a timestamp to `.git/pre-commit.log` at every successful completion (SecF7). The log lives inside `.git/`, so it is untracked and per-clone.

To verify pre-commit ran for a given commit, check `.git/pre-commit.log`. The last timestamp should be within seconds of the commit's authored time. A gap indicates a `--no-verify` bypass.

```bash
tail .git/pre-commit.log
```

This is a material auditability improvement, not a cryptographic proof. A stronger scheme would use a `prepare-commit-msg` hook to inject a `Verified-by: pre-commit` commit trailer that ties the audit record to the commit hash. That is tracked as a follow-up.

## Known Phase 0.0 gaps

- **G3 mypy silent-swallow**: the `mypy src/` block is wrapped in `|| { echo ... }` so failures print a warning but do not block. A `TODO(task-1)` marker sits directly above the block. Task 1 removes the guard so mypy failures block commits.
