from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

# Ordered pipeline stages; "fetch_failed" / "embed_failed" are error states.
STAGES = ("fetch", "clean", "chunk", "embed", "publish", "done")


@dataclass
class CheckpointState:
    source_name: str
    stage: str          # one of STAGES or an error variant
    corpus_version: str
    error: str = ""


class CheckpointStore:
    """Write-ahead checkpoint log backed by per-source JSON files."""

    def __init__(self, state_dir: Path) -> None:
        self._dir = state_dir

    def _path(self, source_name: str) -> Path:
        return self._dir / f"{source_name}.checkpoint.json"

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
