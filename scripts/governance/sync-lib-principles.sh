#!/usr/bin/env bash
# sync-lib-principles.sh
# Detect drift between this project's LIB-PRINCIPLES.md and the authoritative
# copy in the sibling terms-analysis project.
#
# Usage:
#   scripts/governance/sync-lib-principles.sh
#
# Exit codes:
#   0 - files are identical (in sync)
#   1 - diff detected (drift)
#   3 - sibling path does not exist

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

LOCAL="${REPO_ROOT}/.claude/library/LIB-PRINCIPLES.md"
SIBLING_REPO="$(cd "${REPO_ROOT}/../terms-analysis" 2>/dev/null && pwd)" || true

if [ -z "${SIBLING_REPO}" ] || [ ! -d "${SIBLING_REPO}" ]; then
    echo "ERROR: sibling project not found at $(dirname "${REPO_ROOT}")/terms-analysis" >&2
    echo "       Ensure terms-analysis is checked out as a sibling directory." >&2
    exit 3
fi

AUTHORITATIVE="${SIBLING_REPO}/.claude/library/LIB-PRINCIPLES.md"

if [ ! -f "${AUTHORITATIVE}" ]; then
    echo "ERROR: authoritative file missing: ${AUTHORITATIVE}" >&2
    exit 3
fi

if [ ! -f "${LOCAL}" ]; then
    echo "ERROR: local file missing: ${LOCAL}" >&2
    exit 3
fi

DIFF_OUTPUT="$(diff "${LOCAL}" "${AUTHORITATIVE}")"

if [ -z "${DIFF_OUTPUT}" ]; then
    echo "LIB-PRINCIPLES in sync with terms-analysis"
    exit 0
fi

# Drift detected — show diff then instructions.
printf '%s\n' "${DIFF_OUTPUT}"
echo ""
echo "DRIFT DETECTED: .claude/library/LIB-PRINCIPLES.md differs from terms-analysis."
echo "Options:"
echo "  1. Accept mirror: cp ${AUTHORITATIVE} ${LOCAL}"
echo "  2. Justify divergence: open an ADR in docs/adr/ explaining why they should differ"
exit 1
