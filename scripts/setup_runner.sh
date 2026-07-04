#!/usr/bin/env bash
# setup_runner.sh
#
# Idempotent installer for the self-hosted GitHub Actions runner that
# powers the legal-corpus-ingester repo. Codifies Task P2.
#
# Rationale (HR4): corpus fetches and artifacts stay on this machine.
# GitHub-hosted runners would move that data through Azure.
#
# Safe to re-run: exits early if the runner is already configured.
# Requires: gh (authenticated), curl, tar. No sudo.

set -euo pipefail

REPO="jennifer-mckinney/legal-corpus-ingester"
RUNNER_DIR="${HOME}/actions-runner-legal-corpus"
RUNNER_VERSION="2.335.1"
RUNNER_ASSET="actions-runner-osx-arm64-${RUNNER_VERSION}.tar.gz"
RUNNER_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${RUNNER_ASSET}"
LABELS="legal-corpus-ingester,self-hosted,macos"

log() { printf '[setup_runner] %s\n' "$*"; }

# Guard: already configured
if [ -f "${RUNNER_DIR}/.runner" ]; then
    log "runner already configured at ${RUNNER_DIR}/.runner; skipping registration"
    log "service status:"
    (cd "${RUNNER_DIR}" && ./svc.sh status || true)
    exit 0
fi

# Preconditions
command -v gh >/dev/null || { log "gh CLI not found"; exit 1; }
command -v curl >/dev/null || { log "curl not found"; exit 1; }
command -v tar >/dev/null || { log "tar not found"; exit 1; }

# Step 1: registration token (60 min expiry, used immediately)
log "requesting registration token from GitHub"
TOKEN="$(gh api -X POST "/repos/${REPO}/actions/runners/registration-token" -q .token)"
if [ -z "${TOKEN}" ]; then
    log "empty token from GitHub; aborting"
    exit 1
fi

# Step 2: fetch + extract runner binary (outside the git repo)
mkdir -p "${RUNNER_DIR}"
cd "${RUNNER_DIR}"

if [ ! -f config.sh ]; then
    log "downloading ${RUNNER_ASSET}"
    curl -fSL -o actions-runner.tar.gz "${RUNNER_URL}"
    tar xzf actions-runner.tar.gz
fi

# Step 3: register runner
log "registering runner with repo ${REPO}"
./config.sh \
    --url "https://github.com/${REPO}" \
    --token "${TOKEN}" \
    --labels "${LABELS}" \
    --unattended

# Step 4: install + start LaunchAgent (user-level, no sudo)
log "installing LaunchAgent"
./svc.sh install
./svc.sh start
./svc.sh status

# Step 5: verify runner is online in GitHub
log "waiting 5s for GitHub to see the runner"
sleep 5
gh api "/repos/${REPO}/actions/runners" \
    -q '.runners[] | {name, status, labels: [.labels[].name]}'

log "done. runner directory: ${RUNNER_DIR}"
