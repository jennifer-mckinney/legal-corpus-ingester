from __future__ import annotations

import numpy as np
import pytest

from legal_corpus_ingester.errors import EmbedEndpointError, UpstreamNotFoundError
from legal_corpus_ingester.pipeline.manifest import Manifest
from legal_corpus_ingester.pipeline.state import CheckpointStore
from legal_corpus_ingester.publishers.base import PublishReceipt, PublishTarget
from legal_corpus_ingester.types import (
    Chunk,
    CleanedDocument,
    Corpus,
    FetchResult,
    ProvenanceRecord,
)

# ---------------------------------------------------------------------------
# Shared fake data
# ---------------------------------------------------------------------------

_FAKE_PROVENANCE = ProvenanceRecord(
    source_url="https://example.com/tos",
    fetch_timestamp="2026-07-04T00:00:00Z",
    license="CC-BY-4.0",
    upstream_version="1.0",
    content_sha256="deadbeef",
    source_name="test-source",
    fetcher_class="FakeFetcher",
)

_FAKE_RESULT = FetchResult(
    source_name="test-source",
    raw_bytes=b"Terms of service text.",
    mime_type="text/plain",
    upstream_metadata={},
    provenance=_FAKE_PROVENANCE,
)

_FAKE_DOC = CleanedDocument(
    text="Terms of service text.",
    has_sections=False,
    headers={},
    provenance=_FAKE_PROVENANCE,
)

_FAKE_CHUNKS = [
    Chunk(
        text="Terms of service text.",
        section="main",
        metadata={},
        provenance=_FAKE_PROVENANCE,
        offset_start=0,
        offset_end=22,
    )
]


# ---------------------------------------------------------------------------
# Fake injectable components
# ---------------------------------------------------------------------------


class FakeFetcher:
    async def fetch(self, source_config: object) -> FetchResult:
        return _FAKE_RESULT


class FailingFetcher:
    async def fetch(self, source_config: object) -> FetchResult:
        raise UpstreamNotFoundError("upstream 404")


class FakeCleaner:
    def clean(self, result: FetchResult) -> CleanedDocument:
        return _FAKE_DOC


class FakeChunker:
    def chunk(self, doc: CleanedDocument) -> list[Chunk]:
        return _FAKE_CHUNKS


class FakeEmbedder:
    async def embed(self, chunks: list[Chunk]) -> np.ndarray:  # type: ignore[type-arg]
        n = len(chunks)
        raw = np.ones((n, 1024), dtype=np.float32)
        norms = np.linalg.norm(raw, axis=1, keepdims=True)
        return (raw / norms).astype(np.float32)

    def revision(self) -> str:
        return "abc123"


class FailingEmbedder:
    async def embed(self, chunks: list[Chunk]) -> np.ndarray:  # type: ignore[type-arg]
        raise EmbedEndpointError("embed endpoint down")

    def revision(self) -> str:
        return "abc123"


class FakePublisher:
    def publish(self, corpus: Corpus, target: PublishTarget, manifest: Manifest) -> PublishReceipt:
        return PublishReceipt(
            chunk_count=len(corpus.chunks),
            matrix_sha256="aaa",
            metadata_sha256="bbb",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orchestrator(
    tmp_path,
    fetcher=None,
    embedder=None,
):
    from legal_corpus_ingester.pipeline.orchestrator import Orchestrator

    return Orchestrator(
        fetcher=fetcher or FakeFetcher(),
        cleaner=FakeCleaner(),
        chunker=FakeChunker(),
        embedder=embedder or FakeEmbedder(),
        publisher=FakePublisher(),
        publish_target=PublishTarget(kind="filesystem", path=tmp_path / "output"),
        state_dir=tmp_path / "state",
        corpus_version="2026.07.0",
        chunker_version="v1.0.0",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_orchestrator_happy_path(tmp_path):
    """Successful run returns a receipt with expected chunk_count and writes 'done' checkpoint."""
    orch = _make_orchestrator(tmp_path)
    receipt = orch.run("test-source", object())

    assert receipt.chunk_count == 1

    store = CheckpointStore(tmp_path / "state")
    checkpoint = store.load("test-source")
    assert checkpoint is not None
    assert checkpoint.stage == "done"


def test_fetch_failure_records_checkpoint(tmp_path):
    """Fetcher error persists fetch_failed checkpoint with error message."""
    orch = _make_orchestrator(tmp_path, fetcher=FailingFetcher())

    with pytest.raises(UpstreamNotFoundError):
        orch.run("test-source", object())

    store = CheckpointStore(tmp_path / "state")
    checkpoint = store.load("test-source")
    assert checkpoint is not None
    assert checkpoint.stage == "fetch_failed"
    assert "upstream 404" in checkpoint.error


def test_embed_failure_records_checkpoint(tmp_path):
    """EmbedEndpointError persists embed_failed checkpoint."""
    orch = _make_orchestrator(tmp_path, embedder=FailingEmbedder())

    with pytest.raises(EmbedEndpointError):
        orch.run("test-source", object())

    store = CheckpointStore(tmp_path / "state")
    checkpoint = store.load("test-source")
    assert checkpoint is not None
    assert checkpoint.stage == "embed_failed"


def test_done_checkpoint_is_cleared_on_rerun(tmp_path):
    """If checkpoint is already 'done', rerun clears it and succeeds (idempotent)."""
    orch = _make_orchestrator(tmp_path)

    # First run
    receipt1 = orch.run("test-source", object())
    assert receipt1.chunk_count == 1

    store = CheckpointStore(tmp_path / "state")
    assert store.load("test-source").stage == "done"

    # Second run — must still succeed
    receipt2 = orch.run("test-source", object())
    assert receipt2.chunk_count == 1
    assert store.load("test-source").stage == "done"
