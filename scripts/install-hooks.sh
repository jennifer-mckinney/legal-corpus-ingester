#!/usr/bin/env bash
# Idempotent installer for the tracked git hooks (.githooks/pre-commit).
# Sets core.hooksPath to .githooks (relative, so every checkout, including
# linked worktrees, runs its own tracked .githooks/) and marks the hook
# scripts executable. Safe to run from the main checkout or any worktree.
#
# P9 review is no longer a local hook: it runs as the CI jobs in
# .github/workflows/p9-review.yml (terms-analysis#191).
set -euo pipefail
# An exported CDPATH makes `cd <relative>` search elsewhere and echo the
# result, which would corrupt the resolved paths below.
unset CDPATH

EXPECTED_HOOKS_PATH=".githooks"

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd -- "${REPO_ROOT}"

if [ ! -d "${EXPECTED_HOOKS_PATH}" ]; then
    echo "install-hooks: ${REPO_ROOT}/${EXPECTED_HOOKS_PATH} not found; run from a checkout that tracks ${EXPECTED_HOOKS_PATH}/" >&2
    exit 1
fi

# Replace any other hooksPath value (e.g. an absolute <repo>/.git/hooks that
# has none of the tracked hooks and silently disables them) and say so.
CURRENT_HOOKS_PATH="$(git config --get core.hooksPath || true)"
if [ -n "${CURRENT_HOOKS_PATH}" ] && [ "${CURRENT_HOOKS_PATH}" != "${EXPECTED_HOOKS_PATH}" ]; then
    echo "install-hooks: replacing core.hooksPath=${CURRENT_HOOKS_PATH} with ${EXPECTED_HOOKS_PATH} (the previous value bypasses the tracked hooks)" >&2
fi
git config core.hooksPath "${EXPECTED_HOOKS_PATH}"

# Verify the effective value: a higher-precedence scope (e.g. a per-worktree
# config.worktree) could still shadow the value just written.
EFFECTIVE_HOOKS_PATH="$(git config --get core.hooksPath || true)"
if [ "${EFFECTIVE_HOOKS_PATH}" != "${EXPECTED_HOOKS_PATH}" ]; then
    echo "install-hooks: core.hooksPath is still '${EFFECTIVE_HOOKS_PATH}' after install; find the shadowing scope with: git config --show-origin --show-scope --get-all core.hooksPath" >&2
    exit 1
fi

find "${EXPECTED_HOOKS_PATH}" -maxdepth 1 -type f -exec chmod +x {} \;

echo "Git hooks installed: core.hooksPath=${EXPECTED_HOOKS_PATH}"
