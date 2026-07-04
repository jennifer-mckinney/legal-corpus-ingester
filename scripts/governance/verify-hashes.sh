#!/usr/bin/env bash
# verify-hashes.sh
# Verify that governance files match the SHA256 hashes recorded in
# .claude/_governance-manifest.json.
#
# Exit codes:
#   0 - all hashes match
#   1 - hash drift detected on one or more tracked files
#   2 - manifest file missing
#   3 - tracked file missing on disk
#
# Output is intentionally compact so a Critic pass can read it without noise.

set -euo pipefail

# Detect sha256 tool: prefer sha256sum (Linux/coreutils), fall back to shasum -a 256 (macOS default).
if command -v sha256sum >/dev/null 2>&1; then
    SHA_CMD=(sha256sum)
elif command -v shasum >/dev/null 2>&1; then
    SHA_CMD=(shasum -a 256)
else
    echo "ERROR: neither sha256sum nor shasum available on PATH" >&2
    exit 2
fi

# Locate repo root: this script lives at <repo>/scripts/governance/verify-hashes.sh.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MANIFEST="${REPO_ROOT}/.claude/_governance-manifest.json"

if [ ! -f "${MANIFEST}" ]; then
    echo "MANIFEST MISSING: ${MANIFEST}"
    exit 2
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 required to parse manifest" >&2
    exit 2
fi

# Extract entries via python3 (no jq dependency). Output one line per entry:
#   <path>|<expected_hash>
ENTRIES="$(python3 - "${MANIFEST}" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
for e in data.get("entries", []):
    print(f"{e['path']}|{e['sha256']}")
PY
)"

if [ -z "${ENTRIES}" ]; then
    echo "MANIFEST MISSING: no entries found in ${MANIFEST}"
    exit 2
fi

# Resolve a manifest-recorded path into an absolute filesystem path.
# - $HOME/... entries expand via shell HOME.
# - Anything else is treated as repo-relative.
resolve_path() {
    local raw="$1"
    case "${raw}" in
        \$HOME/*)
            echo "${HOME}/${raw#\$HOME/}"
            ;;
        /*)
            echo "${raw}"
            ;;
        *)
            echo "${REPO_ROOT}/${raw}"
            ;;
    esac
}

# Compute SHA256 of a file using whichever tool we detected.
compute_hash() {
    local f="$1"
    "${SHA_CMD[@]}" "${f}" | awk '{print $1}'
}

# Compare current file size against recorded size as a drift hint.
# If size differs we report byte delta; if size matches but hash differs
# we report "content differs, same length".
line_delta_note() {
    local current_file="$1"
    local expected_size="$2"
    if [ -z "${expected_size}" ]; then
        echo "expected size unknown"
        return
    fi
    local current_size
    current_size="$(wc -c < "${current_file}" | tr -d ' ')"
    if [ "${current_size}" = "${expected_size}" ]; then
        echo "same size, content differs"
    else
        local delta=$(( current_size - expected_size ))
        if [ ${delta} -gt 0 ]; then
            echo "+${delta} bytes vs manifest"
        else
            echo "${delta} bytes vs manifest"
        fi
    fi
}

# Also expose the expected size from the manifest for each entry.
SIZES="$(python3 - "${MANIFEST}" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
for e in data.get("entries", []):
    print(f"{e['path']}|{e['size_bytes']}")
PY
)"

get_expected_size() {
    local target="$1"
    echo "${SIZES}" | while IFS='|' read -r p s; do
        if [ "${p}" = "${target}" ]; then
            echo "${s}"
            return
        fi
    done
}

drift_lines=""
missing_lines=""
verified=0

while IFS='|' read -r raw_path expected_hash; do
    [ -z "${raw_path}" ] && continue
    resolved="$(resolve_path "${raw_path}")"
    if [ ! -f "${resolved}" ]; then
        missing_lines="${missing_lines}${raw_path} -> ${resolved}"$'\n'
        continue
    fi
    actual_hash="$(compute_hash "${resolved}")"
    if [ "${actual_hash}" = "${expected_hash}" ]; then
        verified=$(( verified + 1 ))
    else
        expected_size="$(get_expected_size "${raw_path}")"
        delta_note="$(line_delta_note "${resolved}" "${expected_size}")"
        exp_short="${expected_hash:0:12}"
        got_short="${actual_hash:0:12}"
        drift_lines="${drift_lines}${raw_path}: expected ${exp_short} got ${got_short} (${delta_note})"$'\n'
    fi
done <<< "${ENTRIES}"

if [ -n "${missing_lines}" ]; then
    # Report only the first missing file (compact output) and exit 3.
    first_missing="$(printf '%s' "${missing_lines}" | head -n1)"
    echo "TRACKED FILE MISSING: ${first_missing}"
    exit 3
fi

if [ -n "${drift_lines}" ]; then
    echo "HASH DRIFT:"
    printf '%s' "${drift_lines}"
    exit 1
fi

echo "HASHES OK: ${verified} files verified"
exit 0
