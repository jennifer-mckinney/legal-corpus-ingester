from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# terms-analysis backend on sys.path
# Allows `from app.services.legal_kb import LegalKnowledgeBase` without
# installing terms-analysis as a build-time dependency.
# parents[2] = repo root (legal-corpus-ingester/), parents[3] = 01_Claude_Projects/
# ---------------------------------------------------------------------------
_BACKEND_PATH = Path(__file__).resolve().parents[3] / "terms-analysis" / "src" / "backend"


def _ensure_backend_on_path() -> None:
    p = str(_BACKEND_PATH)
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------------------
# Inline fakes — no shared conftest dependency
# ---------------------------------------------------------------------------


class _FakeEmbedder:
    """Returns deterministic 1024-dim L2-normalised float32 vectors; no LocalAI."""

    _DIM = 1024

    async def embed(self, chunks: list[Any]) -> np.ndarray[Any, np.dtype[np.float32]]:
        n = len(chunks)
        raw = np.ones((n, self._DIM), dtype=np.float32)
        norms = np.linalg.norm(raw, axis=1, keepdims=True)
        return (raw / norms).astype(np.float32)

    def revision(self) -> str:
        return "fake-rev-abc123"


class _MockLocalAIClient:
    """Duck-typed stand-in for terms-analysis LocalAIClient.

    Returns a 1024-dim unit vector matching the FakeEmbedder's output dimension
    so LegalKnowledgeBase._retrieve() can compute cosine similarity without a
    real LocalAI server.
    """

    async def embed(self, text: str, model: str = "") -> list[float]:
        vec = np.ones(1024, dtype=np.float32)
        return (vec / float(np.linalg.norm(vec))).tolist()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CASSETTE = "tests/fixtures/cassettes/eurlex/gdpr_fetch.yaml"
_CELEX_ID = "32016R0679"


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_e2e_round_trip_gdpr(tmp_path: Path) -> None:
    """EUR-Lex VCR cassette → ingester pipeline → publish bundle → LegalKB retrieve.

    Verifies the full ingester→consumer contract:
      fetch (VCR replay) → clean → chunk → embed (fake) → publish (filesystem)
      → LegalKnowledgeBase.load_from_bundle → retrieve returns GDPR Art. 6 result.
    """
    import vcr as vcrpy

    from legal_corpus_ingester.cleaners.xml_akn import AKNXMLCleaner
    from legal_corpus_ingester.chunkers.factory import ChunkerFactory
    from legal_corpus_ingester.fetchers.eurlex import EurLexFetcher
    from legal_corpus_ingester.pipeline.manifest import Manifest
    from legal_corpus_ingester.publishers.base import PublishTarget
    from legal_corpus_ingester.publishers.filesystem import FilesystemPublisher
    from legal_corpus_ingester.types import Corpus

    # --- Step 1: Fetch GDPR via VCR cassette (replay only, no live network) ---
    with vcrpy.VCR().use_cassette(_CASSETTE, record_mode="none"):
        fetch_result = asyncio.run(EurLexFetcher().fetch(celex_id=_CELEX_ID))

    # --- Step 2: Clean with AKNXMLCleaner ------------------------------------
    # AKNXMLCleaner.clean() takes (raw_bytes, provenance) — not FetchResult —
    # so we call it directly rather than routing through the Orchestrator.
    doc = AKNXMLCleaner().clean(fetch_result.raw_bytes, fetch_result.provenance)
    assert len(doc.text) > 0, (
        "AKNXMLCleaner produced empty text — cassette XML may lack AKN namespace"
    )
    assert doc.headers.get("jurisdiction") == "GDPR", (
        f"Expected jurisdiction='GDPR', got {doc.headers.get('jurisdiction')!r}"
    )

    # --- Step 3: Chunk -------------------------------------------------------
    chunks = ChunkerFactory().chunk(doc)
    assert len(chunks) > 0, "No chunks produced from GDPR document"
    for chunk in chunks:
        assert chunk.metadata.get("jurisdiction") == "GDPR", (
            f"SectionedChunker did not propagate jurisdiction into metadata: "
            f"{chunk.metadata!r}"
        )

    # --- Step 4: Embed (fake — no real LocalAI needed) -----------------------
    embedder = _FakeEmbedder()
    embeddings = asyncio.run(embedder.embed(chunks))
    assert embeddings.shape == (len(chunks), 1024), (
        f"Unexpected embedding shape: {embeddings.shape}"
    )
    assert embeddings.dtype == np.float32

    # --- Step 5: Publish to a tmp bundle directory ---------------------------
    bundle_dir = tmp_path / "2026.07.0"
    manifest = Manifest(
        corpus_version="2026.07.0",
        chunker_version="v1.0.0-vendored",
        embedder_model="apertus-8b-instruct",
        embedder_revision=embedder.revision(),
        sources=["eurlex"],
        chunk_count=len(chunks),
    )
    corpus = Corpus(chunks=chunks, embeddings=embeddings, corpus_files=["eurlex"])
    FilesystemPublisher().publish(
        corpus,
        PublishTarget(kind="filesystem", path=bundle_dir),
        manifest,
    )
    assert (bundle_dir / "MANIFEST.yaml").exists(), "MANIFEST.yaml missing from bundle"
    assert (bundle_dir / "index" / "legal_kb.npy").exists(), "legal_kb.npy missing"
    assert (bundle_dir / "index" / "legal_kb_metadata.json").exists(), (
        "legal_kb_metadata.json missing"
    )

    # --- Step 6: Load into LegalKnowledgeBase (terms-analysis consumer) ------
    _ensure_backend_on_path()
    from app.services.legal_kb import LegalKnowledgeBase  # noqa: PLC0415

    kb = LegalKnowledgeBase()
    kb.load_from_bundle(bundle_dir)
    assert kb.chunk_count > 0, "LegalKnowledgeBase loaded 0 chunks — bundle may be empty"

    # --- Step 7: Retrieve with mock LocalAI client ---------------------------
    mock_client = _MockLocalAIClient()
    results = asyncio.run(kb.retrieve("consent lawful basis", mock_client))

    assert len(results) > 0, "retrieve() returned no results — index may be empty or scoring failed"

    # All results must carry jurisdiction=GDPR (provenance propagation check).
    for result in results:
        assert result.get("jurisdiction") == "GDPR", (
            f"Result jurisdiction expected 'GDPR', got {result.get('jurisdiction')!r}"
        )

    # At least one result must reference Article 6 / lawfulness / consent.
    # We check across all results (not just top-1) because the fake uniform
    # embedder produces identical dense vectors — cosine similarity is equal
    # for every chunk, so ranking is determined by BM25 fallback order (which
    # may place Article 5 first when rank_bm25 is not installed).  The
    # contract being tested here is *pipeline completeness* (all GDPR chunks
    # passed through fetch→clean→chunk→embed→publish→retrieve), not that the
    # retrieval model itself ranks Article 6 above Article 5.
    all_texts = " ".join(r.get("text", "") for r in results)
    assert (
        "Article 6" in all_texts
        or "Lawfulness" in all_texts
        or "consent" in all_texts.lower()
    ), (
        f"No result contains GDPR Art. 6 / lawfulness / consent reference. "
        f"texts={[r.get('text', '')[:100] for r in results]!r}"
    )
