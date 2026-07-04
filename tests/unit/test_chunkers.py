from __future__ import annotations
from legal_corpus_ingester.types import CleanedDocument, ProvenanceRecord, Chunk

_PROV = ProvenanceRecord(
    source_url="https://example.com",
    fetch_timestamp="2026-07-04T00:00:00Z",
    license="CC-BY-4.0",
    upstream_version=None,
    content_sha256="d" * 64,
    source_name="test",
    fetcher_class="test.Fetcher",
)

_SECTIONED_DOC = CleanedDocument(
    text="## Article 1 — Scope\n\nThis regulation applies to all persons.\n\n## Article 2 — Definitions\n\nFor the purposes of this regulation, the following definitions apply.",
    has_sections=True,
    headers={"jurisdiction": "GDPR"},
    provenance=_PROV,
)

_PLAIN_DOC = CleanedDocument(
    text="This is a plain document without section markers. " * 30,
    has_sections=False,
    headers={"jurisdiction": "GDPR"},
    provenance=_PROV,
)


def test_sectioned_chunker_produces_chunks() -> None:
    from legal_corpus_ingester.chunkers.sectioned import SectionedChunker
    chunker = SectionedChunker()
    chunks = chunker.chunk(_SECTIONED_DOC)
    assert len(chunks) > 0
    for c in chunks:
        assert isinstance(c, Chunk)
        assert c.provenance == _PROV


def test_sectioned_chunker_preserves_section_in_text() -> None:
    from legal_corpus_ingester.chunkers.sectioned import SectionedChunker
    chunker = SectionedChunker()
    chunks = chunker.chunk(_SECTIONED_DOC)
    # Each chunk should contain content from the document
    all_text = " ".join(c.text for c in chunks)
    assert "Article 1" in all_text or "Scope" in all_text


def test_plain_chunker_produces_chunks() -> None:
    from legal_corpus_ingester.chunkers.plain import PlainChunker
    chunker = PlainChunker()
    chunks = chunker.chunk(_PLAIN_DOC)
    assert len(chunks) > 0
    for c in chunks:
        assert isinstance(c, Chunk)
        assert c.offset_start >= 0
        assert c.offset_end > c.offset_start


def test_factory_dispatches_by_has_sections() -> None:
    from legal_corpus_ingester.chunkers.factory import ChunkerFactory
    factory = ChunkerFactory()
    sectioned_chunks = factory.chunk(_SECTIONED_DOC)
    plain_chunks = factory.chunk(_PLAIN_DOC)
    assert len(sectioned_chunks) > 0
    assert len(plain_chunks) > 0


def test_chunk_offsets_non_negative() -> None:
    from legal_corpus_ingester.chunkers.plain import PlainChunker
    chunker = PlainChunker()
    chunks = chunker.chunk(_PLAIN_DOC)
    for c in chunks:
        assert c.offset_start >= 0
        assert c.offset_end >= c.offset_start
