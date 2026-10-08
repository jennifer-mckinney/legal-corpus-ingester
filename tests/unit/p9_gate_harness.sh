#!/usr/bin/env bash
# P9 pre-push gate test harness (G0-5, terms-analysis#175).
#
# Builds a throwaway sandbox that exercises this repo's REAL .githooks/pre-push
# and scripts/install-hooks.sh without touching the real repository's config:
#
#   <sandbox>/home/.gitconfig   isolated global config (identity, default branch)
#   <sandbox>/remote.git        local bare remote
#   <sandbox>/main              main checkout (copies of the repo's hook files)
#   <sandbox>/wt                linked worktree of <sandbox>/main (branch wt-branch)
#
# Usage: p9_gate_harness.sh <repo_root> <sandbox_dir>
# The caller (pytest) must export HOME, GIT_CONFIG_GLOBAL and GIT_CONFIG_NOSYSTEM
# pointing into the sandbox so no user/system git config leaks in.
set -euo pipefail
export GIT_TERMINAL_PROMPT=0
unset CDPATH

REPO_ROOT="${1:?usage: p9_gate_harness.sh <repo_root> <sandbox_dir>}"
SANDBOX="${2:?usage: p9_gate_harness.sh <repo_root> <sandbox_dir>}"

# Refuse to run against anything but an isolated global config.
case "${GIT_CONFIG_GLOBAL:-}" in
    "${SANDBOX}"/*) ;;
    *) echo "harness: GIT_CONFIG_GLOBAL must point inside the sandbox" >&2; exit 2 ;;
esac

mkdir -p "${SANDBOX}/home"
SANDBOX_REAL="$(cd "${SANDBOX}" && pwd -P)"
# Every git command below must run inside the sandbox, never the real repo.
in_sandbox() {
    case "$(pwd -P)" in
        "${SANDBOX_REAL}"|"${SANDBOX_REAL}"/*) ;;
        *) echo "harness: cwd $(pwd -P) is outside the sandbox; refusing" >&2; exit 2 ;;
    esac
}
cd "${SANDBOX_REAL}"
in_sandbox
cat > "${GIT_CONFIG_GLOBAL}" <<'EOF'
[user]
    name = P9 Harness
    email = p9-harness@example.invalid
[init]
    defaultBranch = main
[commit]
    gpgsign = false
[advice]
    detachedHead = false
EOF

git init -q --bare "${SANDBOX}/remote.git"
git init -q "${SANDBOX}/main"

# Copy only the files under test: the P9 pre-push hook and the installer.
# (Other hooks such as pre-commit carry repo-specific guards that are out of scope.)
mkdir -p "${SANDBOX}/main/.githooks" "${SANDBOX}/main/scripts"
cp "${REPO_ROOT}/.githooks/pre-push" "${SANDBOX}/main/.githooks/pre-push"
cp "${REPO_ROOT}/scripts/install-hooks.sh" "${SANDBOX}/main/scripts/install-hooks.sh"
chmod +x "${SANDBOX}/main/.githooks/pre-push" "${SANDBOX}/main/scripts/install-hooks.sh"

cd "${SANDBOX_REAL}/main"
in_sandbox
echo "seed" > README
git add -A
git commit -q -m "seed"
git remote add origin "${SANDBOX}/remote.git"
# The only remote is the local bare repo in the sandbox (never github.com).
[ "$(git remote get-url origin)" = "${SANDBOX}/remote.git" ] || { echo "harness: unexpected origin" >&2; exit 2; }
# Seed the remote before any hook is installed so the gate is not involved.
git push -q origin main

git worktree add -q "${SANDBOX}/wt" -b wt-branch
echo "harness ready: ${SANDBOX}"
