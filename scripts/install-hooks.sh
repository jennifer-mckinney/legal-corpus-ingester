#!/usr/bin/env bash
# Idempotent installer for the P9 pre-push gate.
# Sets core.hooksPath to .githooks, marks hook scripts executable,
# and ensures .git/reviews/ exists for signoff files.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "${REPO_ROOT}"

git config core.hooksPath .githooks
find .githooks -maxdepth 1 -type f -exec chmod +x {} \;
mkdir -p .git/reviews

echo "Git hooks installed: core.hooksPath=.githooks"
echo "Signoff directory ready: .git/reviews/"
