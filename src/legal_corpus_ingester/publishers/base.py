from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from legal_corpus_ingester.pipeline.manifest import Manifest
    from legal_corpus_ingester.types import Corpus


@dataclass
class PublishTarget:
    """Describes where a publish operation should write its output."""

    kind: str  # "filesystem" | "terms_analysis" | "tarball"
    path: Path


@dataclass(frozen=True)
class PublishReceipt:
    """Returned by every publisher after a successful publish call."""

    chunk_count: int
    matrix_sha256: str       # SHA256 hex of legal_kb.npy bytes
    metadata_sha256: str     # SHA256 hex of legal_kb_metadata.json bytes
    tarball_sha256: str = field(default="")  # only set by TarballPublisher


class Publisher(Protocol):
    """Structural protocol satisfied by any publisher class."""

    def publish(
        self,
        corpus: Corpus,
        target: PublishTarget,
        manifest: Manifest,
    ) -> PublishReceipt:
        """Write corpus to target and return a receipt."""
        ...
