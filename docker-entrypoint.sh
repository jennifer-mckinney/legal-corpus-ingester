#!/usr/bin/env bash
# G7: fail fast when the `ingester` console script is not installed.
# Without pyproject.toml (Phase 0.0 grace path), the builder stage creates
# an empty venv and the image builds green but has no CLI. Rather than
# `exec: "ingester": executable file not found` at runtime, print a clear
# diagnostic and exit non-zero.
set -euo pipefail
if ! command -v ingester >/dev/null 2>&1; then
    echo "ingester command not installed in this image." >&2
    echo "This may be a Phase 0.0 grace-path build with no pyproject.toml." >&2
    echo "Install: rebuild after Task 2 lands the pyproject." >&2
    exit 1
fi
exec ingester "$@"
