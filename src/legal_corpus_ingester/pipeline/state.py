from __future__ import annotations

import json
import typing
from dataclasses import MISSING, asdict, dataclass, fields
from pathlib import Path

# Ordered pipeline stages; "fetch_failed" / "embed_failed" are error states.
STAGES = ("fetch", "clean", "chunk", "embed", "publish", "done")
# The one success terminal stage: scripts/health_check.py treats only this stage as fresh.
SUCCESS_STAGE = STAGES[-1]
# Error state written by the CLI for a requested source with no valid approval: it was never
# fetched (terms-analysis#93, lead rulings B and D).
STAGE_NOT_APPROVED = "not_approved"


@dataclass
class CheckpointState:
    source_name: str
    stage: str          # one of STAGES or an error variant
    corpus_version: str
    error: str = ""


# The checkpoint schema, derived from the dataclass so it is stated once (#17). These are
# exactly the rules CheckpointState(**data) in CheckpointStore.load enforces at
# construction (keys) plus the declared field types, which the dataclass does not check.
CHECKPOINT_FIELD_TYPES: dict[str, type] = typing.get_type_hints(CheckpointState)
CHECKPOINT_REQUIRED_FIELDS: frozenset[str] = frozenset(
    f.name for f in fields(CheckpointState)
    if f.default is MISSING and f.default_factory is MISSING
)


def checkpoint_problem(data: object) -> str | None:
    """Return why parsed checkpoint JSON is not a loadable CheckpointState, or None.

    None means CheckpointStore.load would build a CheckpointState from *data* without
    error and every field has its declared type. Messages name fields only from the
    dataclass, never from *data* (an unknown key is untrusted text).
    """
    if not isinstance(data, dict):
        return "not a JSON object"
    if not set(data) <= CHECKPOINT_FIELD_TYPES.keys():
        return "has an unknown field"
    for name, expected in CHECKPOINT_FIELD_TYPES.items():
        if name in data and not isinstance(data[name], expected):
            return f'field "{name}" is not of type {expected.__name__}'
    for name in sorted(CHECKPOINT_REQUIRED_FIELDS):
        if name not in data:
            return f'field "{name}" is missing'
    return None


class CheckpointStore:
    """Write-ahead checkpoint log backed by per-source JSON files."""

    def __init__(self, state_dir: Path) -> None:
        self._dir = state_dir

    def _path(self, source_name: str) -> Path:
        # Resolve before returning to catch any path traversal attempts via
        # source_name values like "../../../etc/passwd".
        p = (self._dir / f"{source_name}.checkpoint.json").resolve()
        if not p.is_relative_to(self._dir.resolve()):
            raise ValueError(
                f"source_name {source_name!r} would escape state_dir; path traversal blocked"
            )
        return p

    def save(self, state: CheckpointState) -> None:
        """Persist *state* to disk, creating the state directory if needed."""
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path(state.source_name).write_text(
            json.dumps(asdict(state), indent=2) + "\n",
            encoding="utf-8",
        )

    def load(self, source_name: str) -> CheckpointState | None:
        """Return the persisted CheckpointState for *source_name*, or None if absent."""
        p = self._path(source_name)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return CheckpointState(**data)

    def clear(self, source_name: str) -> None:
        """Delete the checkpoint file for *source_name* (idempotent)."""
        self._path(source_name).unlink(missing_ok=True)
