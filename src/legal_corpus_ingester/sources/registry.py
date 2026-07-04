from __future__ import annotations
from pathlib import Path
import yaml
from pydantic import ValidationError
from legal_corpus_ingester.sources.schema import SourceConfig


class RegistryLoadError(Exception):
    """Raised when a source YAML fails to load or validate."""


def load_all(sources_dir: Path) -> dict[str, SourceConfig]:
    """Load all *.yaml files from sources_dir, returning dict keyed by source name.

    Raises RegistryLoadError with the filename on any parse or validation failure.
    Class paths (pipeline.fetcher) are stored as strings — lazy import on use.
    """
    if not sources_dir.is_dir():
        raise RegistryLoadError(f"Sources directory not found: {sources_dir}")

    registry: dict[str, SourceConfig] = {}
    for path in sorted(sources_dir.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            cfg = SourceConfig.model_validate(raw)
            registry[cfg.name] = cfg
        except (yaml.YAMLError, ValidationError, TypeError) as exc:
            raise RegistryLoadError(
                f"Failed to load source config {path.name!r}: {exc}"
            ) from exc
    return registry
