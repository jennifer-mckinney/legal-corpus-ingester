from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Optional
import numpy as np

# Allowed MIME types for raw fetched documents
VALID_MIME_TYPES: frozenset[str] = frozenset({
    "text/html",
    "application/pdf",
    "application/xml",
    "text/plain",
})


@dataclass(frozen=True)
class ProvenanceRecord:
    source_url: str
    fetch_timestamp: str
    license: str
    upstream_version: Optional[str]
    content_sha256: str
    source_name: str
    fetcher_class: str


@dataclass(frozen=True)
class FetchResult:
    source_name: str
    raw_bytes: bytes
    mime_type: str
    upstream_metadata: dict[str, Any]
    provenance: ProvenanceRecord

    def __post_init__(self) -> None:
        if self.mime_type not in VALID_MIME_TYPES:
            raise ValueError(
                f"mime_type {self.mime_type!r} not in {VALID_MIME_TYPES}"
            )


@dataclass(frozen=True)
class CleanedDocument:
    text: str
    has_sections: bool
    headers: dict[str, str]
    provenance: ProvenanceRecord


@dataclass(frozen=True)
class Chunk:
    text: str
    section: str
    metadata: dict[str, Any]
    provenance: ProvenanceRecord
    offset_start: int
    offset_end: int


@dataclass
class Corpus:
    """Corpus is not frozen because numpy arrays are mutable."""
    chunks: list[Chunk]
    embeddings: np.ndarray[Any, np.dtype[np.float32]]  # float32, shape (n, 1024), L2-normalized
    corpus_files: list[str]
