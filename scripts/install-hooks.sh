#!/usr/bin/env bash
# Idempotent installer for the P9 pre-push gate (terms-analysis#175).
# Sets core.hooksPath to .githooks (relative, so every checkout, including
# linked worktrees, runs its own tracked .githooks/), marks hook scripts
# executable, and ensures <git-common-dir>/reviews/ exists for signoffs.
# Safe to run from the main checkout or from any `git worktree`.
#
# Canonical file: byte-identical in terms-analysis and legal-corpus-ingester
# (pinned by tests in each repo). Edit both copies together.
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

# The common git dir is shared by the main checkout and all worktrees; in a
# worktree `.git` is a file, so `.git/reviews` cannot be created there.
GIT_COMMON_DIR_RAW="$(git rev-parse --git-common-dir)"
GIT_COMMON_DIR="$(cd -- "${GIT_COMMON_DIR_RAW}" && pwd -P)"

# Replace any other hooksPath value (e.g. an absolute <repo>/.git/hooks that
# contains no pre-push and silently disables the gate) and say so.
CURRENT_HOOKS_PATH="$(git config --get core.hooksPath || true)"
if [ -n "${CURRENT_HOOKS_PATH}" ] && [ "${CURRENT_HOOKS_PATH}" != "${EXPECTED_HOOKS_PATH}" ]; then
    echo "install-hooks: replacing core.hooksPath=${CURRENT_HOOKS_PATH} with ${EXPECTED_HOOKS_PATH} (the previous value bypasses the tracked P9 gate)" >&2
fi
git config core.hooksPath "${EXPECTED_HOOKS_PATH}"

# Verify the effective value: a higher-precedence scope (e.g. a per-worktree
# config.worktree) could still shadow the value just written.
EFFECTIVE_HOOKS_PATH="$(git config --get core.hooksPath || true)"
if [ "${EFFECTIVE_HOOKS_PATH}" != "${EXPECTED_HOOKS_PATH}" ]; then
    echo "install-hooks: core.hooksPath is still '${EFFECTIVE_HOOKS_PATH}' after install; find the shadowing scope with: git config --show-origin --show-scope --get-all core.hooksPath" >&2
    exit 1
fi

# Hook scripts only; the .sha256 pin files are data, not executables.
find "${EXPECTED_HOOKS_PATH}" -maxdepth 1 -type f ! -name '*.sha256' -exec chmod +x {} \;
mkdir -p "${GIT_COMMON_DIR}/reviews"

echo "Git hooks installed: core.hooksPath=${EXPECTED_HOOKS_PATH}"
echo "Signoff directory ready: ${GIT_COMMON_DIR}/reviews/"
