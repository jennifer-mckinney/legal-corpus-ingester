#!/usr/bin/env bash
# regen-manifest.sh
# Regenerate .claude/_governance-manifest.json with current SHA256 hashes for
# the four tracked governance files. Use ONLY after an intentional principle
# or governance change that has been reviewed via PR.
#
# Usage:
#   scripts/governance/regen-manifest.sh          # interactive confirm
#   scripts/governance/regen-manifest.sh --yes    # non-interactive
#
# Exit codes:
#   0 - manifest written
#   1 - user declined or a tracked file is missing
#   2 - required tool unavailable

set -euo pipefail

if command -v sha256sum >/dev/null 2>&1; then
    SHA_CMD=(sha256sum)
elif command -v shasum >/dev/null 2>&1; then
    SHA_CMD=(shasum -a 256)
else
    echo "ERROR: neither sha256sum nor shasum available on PATH" >&2
    exit 2
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 required to emit JSON manifest" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MANIFEST="${REPO_ROOT}/.claude/_governance-manifest.json"

# Tracked entries. Each row: <manifest_path>|<resolved_filesystem_path>|<note>
# $HOME entries are recorded literally in the manifest and resolved at verify time.
ENTRIES=(
    ".claude/CLAUDE.md|${REPO_ROOT}/.claude/CLAUDE.md|Project governance charter."
    ".claude/library/LIB-PRINCIPLES.md|${REPO_ROOT}/.claude/library/LIB-PRINCIPLES.md|LIB-PRINCIPLES P8 v2 baseline; single source of truth for role principles."
    "\$HOME/.claude/CLAUDE.md|${HOME}/.claude/CLAUDE.md|Global user CLAUDE.md; changes here affect every project session."
    "\$HOME/.claude/library/PEAS.md|${HOME}/.claude/library/PEAS.md|PEAS agent design framework reference."
)

AUTO_YES=0
for arg in "$@"; do
    case "${arg}" in
        --yes|-y) AUTO_YES=1 ;;
        *) ;;
    esac
done

if [ "${AUTO_YES}" -ne 1 ]; then
    echo "This overwrites _governance-manifest.json - only run after an intentional governance change was reviewed. Continue? [y/N]"
    read -r reply
    case "${reply}" in
        y|Y|yes|YES) ;;
        *) echo "Aborted."; exit 1 ;;
    esac
fi

# Verify every tracked file exists before we overwrite anything.
for row in "${ENTRIES[@]}"; do
    IFS='|' read -r _mpath fpath _note <<< "${row}"
    if [ ! -f "${fpath}" ]; then
        echo "ERROR: tracked file missing on disk: ${fpath}" >&2
        exit 1
    fi
done

TS="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

# Compute each entry's hash + size in shell, then hand (mpath, hash, size, note)
# tuples straight to python3 as CLI args. python3 emits valid JSON — no risk of
# a note containing a literal tab breaking the round-trip.
PY_ARGS=("${MANIFEST}" "${TS}")
for row in "${ENTRIES[@]}"; do
    IFS='|' read -r mpath fpath note <<< "${row}"
    hash="$("${SHA_CMD[@]}" "${fpath}" | awk '{print $1}')"
    size="$(wc -c < "${fpath}" | tr -d ' ')"
    PY_ARGS+=("${mpath}" "${hash}" "${size}" "${note}")
done

python3 - "${PY_ARGS[@]}" <<'PY'
import json
import sys

manifest_path = sys.argv[1]
ts = sys.argv[2]
rest = sys.argv[3:]
if len(rest) % 4 != 0:
    print(
        "regen-manifest.sh: internal error — entry args not a multiple of 4",
        file=sys.stderr,
    )
    sys.exit(2)

entries = []
for i in range(0, len(rest), 4):
    mpath, sha, size, note = rest[i], rest[i + 1], rest[i + 2], rest[i + 3]
    entries.append(
        {
            "path": mpath,
            "sha256": sha,
            "size_bytes": int(size),
            "recorded_at": ts,
            "note": note,
        }
    )

doc = {
    "schema_version": 1,
    "generated_at": ts,
    "note": (
        "Baseline captured after governance change. Paths beginning with "
        "$HOME/ are resolved at verify time via shell expansion; "
        "project-relative paths are resolved from the repo root."
    ),
    "entries": entries,
}

with open(manifest_path, "w") as f:
    json.dump(doc, f, indent=2)
    f.write("\n")
PY

count="${#ENTRIES[@]}"
echo "MANIFEST REGENERATED: ${count} entries"
exit 0
