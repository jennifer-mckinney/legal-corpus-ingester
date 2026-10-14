from __future__ import annotations

import importlib
import re
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


class StageClassError(Exception):
    """A pipeline class path in a source config does not name a class this package ships."""


# The subpackage each pipeline stage may load from (terms-analysis#93, ruling 8). The schema's
# dotted-path pattern admits any importable module; resolving only inside these subpackages of
# legal_corpus_ingester means a source config can never import code from elsewhere on sys.path.
STAGE_PACKAGES: dict[str, str] = {
    "fetcher": "fetchers",
    "cleaner": "cleaners",
    "chunker": "chunkers",
}
_PACKAGE = __name__.rsplit(".", 2)[0]  # "legal_corpus_ingester"
_MODULE_PART_RE = re.compile(r"[a-z][a-z0-9_]*")
_CLASS_PART_RE = re.compile(r"[A-Z][A-Za-z0-9]*")


def resolve_stage_class(stage: str, dotted: str | None) -> type:
    """Return the class *dotted* names for *stage*, loaded only from that stage's subpackage.

    *dotted* must be exactly "<subpackage>.<module>.<Class>" (for example
    "fetchers.eurlex.EurLexFetcher"), and the class must be defined in that module, not merely
    imported there. Nothing outside legal_corpus_ingester.<subpackage> is ever imported. The
    error message names the stage only, never the configured text (F8).
    """
    subpackage = STAGE_PACKAGES[stage]
    parts = dotted.split(".") if dotted else []
    if (
        len(parts) != 3
        or parts[0] != subpackage
        or not _MODULE_PART_RE.fullmatch(parts[1])
        or not _CLASS_PART_RE.fullmatch(parts[2])
    ):
        raise StageClassError(f"pipeline.{stage} must name a class in {subpackage}.<module>.<Class>")
    module_name = f"{_PACKAGE}.{subpackage}.{parts[1]}"
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise StageClassError(f"pipeline.{stage} names a module that does not exist") from exc
    cls = getattr(module, parts[2], None)
    if not isinstance(cls, type) or cls.__module__ != module_name:
        raise StageClassError(f"pipeline.{stage} names no class defined in its module")
    return cls
