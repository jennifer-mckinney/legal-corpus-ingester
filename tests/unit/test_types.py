from __future__ import annotations
import dataclasses
import numpy as np
from legal_corpus_ingester.types import (
    Chunk,
    CleanedDocument,
    Corpus,
    FetchResult,
    ProvenanceRecord,
)


_PROV = ProvenanceRecord(
    source_url="https://example.com/law.html",
    fetch_timestamp="2026-07-04T00:00:00Z",
    license="CC-BY-4.0",
    upstream_version=None,
    content_sha256="b" * 64,
    source_name="test",
    fetcher_class="test.TestFetcher",
)


def test_provenance_record_requires_all_fields() -> None:
    p = ProvenanceRecord(
        source_url="https://eur-lex.europa.eu/eli/reg/2016/679/oj",
        fetch_timestamp="2026-07-04T12:00:00Z",
        license="CC-BY-4.0",
        upstream_version="20160504",
        content_sha256="a" * 64,
        source_name="eurlex",
        fetcher_class="fetchers.eurlex.EurLexFetcher",
    )
    assert p.license == "CC-BY-4.0"
    assert len(p.content_sha256) == 64


def test_provenance_record_is_frozen() -> None:
    p = ProvenanceRecord(
        source_url="x", fetch_timestamp="x", license="x",
        upstream_version=None, content_sha256="x", source_name="x",
        fetcher_class="x",
    )
    try:
        p.license = "MIT"  # type: ignore[misc]
        raise AssertionError("Expected FrozenInstanceError")
    except dataclasses.FrozenInstanceError:
        pass


def test_fetch_result_valid_mime_type() -> None:
    fr = FetchResult(
        source_name="eurlex",
        raw_bytes=b"<html>GDPR text</html>",
        mime_type="text/html",
        upstream_metadata={"title": "GDPR"},
        provenance=_PROV,
    )
    assert fr.mime_type == "text/html"


def test_fetch_result_invalid_mime_type_raises() -> None:
    try:
        FetchResult(
            source_name="eurlex",
            raw_bytes=b"",
            mime_type="application/json",  # not allowed
            upstream_metadata={},
            provenance=_PROV,
        )
        raise AssertionError("Expected ValueError")
    except ValueError:
        pass


def test_cleaned_document_has_jurisdiction_header() -> None:
    doc = CleanedDocument(
        text="Article 1. General provisions.",
        has_sections=True,
        headers={"jurisdiction": "EU", "title": "GDPR"},
        provenance=_PROV,
    )
    assert "jurisdiction" in doc.headers


def test_chunk_offsets_non_negative() -> None:
    c = Chunk(
        text="Article 1 text",
        section="Article 1",
        metadata={"chunk_index": 0},
        provenance=_PROV,
        offset_start=0,
        offset_end=14,
    )
    assert c.offset_start >= 0
    assert c.offset_end >= 0


def test_corpus_embeddings_shape_and_dtype() -> None:
    n = 3
    dim = 1024
    # Create L2-normalized float32 embeddings
    raw = np.random.default_rng(42).random((n, dim)).astype(np.float32)
    norms = np.linalg.norm(raw, axis=1, keepdims=True)
    embeddings = raw / norms
    chunks = [
        Chunk(
            text=f"chunk {i}",
            section="s1",
            metadata={},
            provenance=_PROV,
            offset_start=i * 10,
            offset_end=(i + 1) * 10,
        )
        for i in range(n)
    ]
    corpus = Corpus(chunks=chunks, embeddings=embeddings, corpus_files=[])
    assert corpus.embeddings.dtype == np.float32
    assert corpus.embeddings.shape == (n, dim)
    norms_check = np.linalg.norm(corpus.embeddings, axis=1)
    assert np.allclose(norms_check, 1.0, atol=1e-5)
