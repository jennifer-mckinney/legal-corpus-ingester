from __future__ import annotations
import hashlib
import json
import re
from enum import Enum
from pathlib import Path

DEFAULT_STATE_FILE = Path("state/license-hashes.json")


class DriftStatus(str, Enum):
    NEW = "NEW"
    NO_CHANGE = "NO_CHANGE"
    DRIFT = "DRIFT"
    SPDX_DRIFT = "SPDX_DRIFT"


def hash_content(content: str) -> str:
    """SHA256 of whitespace-normalized content."""
    normalized = re.sub(r"\s+", " ", content).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def check_license_drift(
    source_name: str,
    current_hash: str,
    current_spdx: str,
    state_file: Path = DEFAULT_STATE_FILE,
) -> DriftStatus:
    """Compare current license hash+SPDX against baseline in state_file."""
    if not state_file.exists():
        return DriftStatus.NEW

    baseline = json.loads(state_file.read_text(encoding="utf-8"))
    if source_name not in baseline:
        return DriftStatus.NEW

    entry = baseline[source_name]
    if current_spdx != entry.get("spdx"):
        return DriftStatus.SPDX_DRIFT
    if current_hash != entry.get("hash"):
        return DriftStatus.DRIFT
    return DriftStatus.NO_CHANGE


def record_license_baseline(
    source_name: str,
    current_hash: str,
    current_spdx: str,
    state_file: Path = DEFAULT_STATE_FILE,
) -> None:
    """Write or update the baseline for source_name in state_file."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    baseline: dict[str, dict[str, str]] = {}
    if state_file.exists():
        baseline = json.loads(state_file.read_text(encoding="utf-8"))
    baseline[source_name] = {"hash": current_hash, "spdx": current_spdx}
    state_file.write_text(
        json.dumps(baseline, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
