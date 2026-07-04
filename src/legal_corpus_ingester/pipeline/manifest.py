from __future__ import annotations
import re
from dataclasses import dataclass, asdict
from pathlib import Path
import yaml

MANIFEST_FILENAME = "MANIFEST.yaml"

# YYYY.MM.PATCH calver — rejects path traversal strings like "../../../tmp/evil".
_CALVER_RE = re.compile(r"^\d{4}\.\d{1,2}\.\d+$")


class ManifestError(Exception):
    """Raised when MANIFEST.yaml is missing or malformed."""


@dataclass
class Manifest:
    corpus_version: str          # YYYY.MM.PATCH calver
    chunker_version: str         # e.g. "v1.0.0-vendored-from-terms-analysis@<sha>"
    embedder_model: str          # e.g. "apertus-8b-instruct"
    embedder_revision: str       # SHA256 of model identifier per L6
    sources: list[str]           # source names included in this bundle
    chunk_count: int             # total chunks across all sources

    def __post_init__(self) -> None:
        if not _CALVER_RE.fullmatch(self.corpus_version):
            raise ValueError(
                f"corpus_version {self.corpus_version!r} is not a valid calver "
                "(expected YYYY.MM.PATCH, e.g. '2026.07.0')"
            )

    def write(self, bundle_dir: Path) -> None:
        """Write MANIFEST.yaml to bundle_dir."""
        bundle_dir.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        manifest_path = bundle_dir / MANIFEST_FILENAME
        manifest_path.write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=True),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, bundle_dir: Path) -> "Manifest":
        """Load MANIFEST.yaml from bundle_dir."""
        manifest_path = bundle_dir / MANIFEST_FILENAME
        if not manifest_path.exists():
            raise ManifestError(f"MANIFEST.yaml not found in {bundle_dir}")
        try:
            data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            return cls(**data)
        except (yaml.YAMLError, TypeError, KeyError) as exc:
            raise ManifestError(f"Failed to parse {manifest_path}: {exc}") from exc
