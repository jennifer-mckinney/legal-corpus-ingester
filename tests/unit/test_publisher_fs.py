from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from legal_corpus_ingester.types import Chunk, Corpus, ProvenanceRecord


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_PROV = ProvenanceRecord(
    source_url="https://example.com/gdpr.html",
    fetch_timestamp="2026-07-04T00:00:00Z",
    license="CC-BY-4.0",
    upstream_version="20160504",
    content_sha256="a" * 64,
    source_name="gdpr",
    fetcher_class="test.TestFetcher",
)


def _make_corpus(n: int = 2) -> Corpus:
    """Build a tiny Corpus with n chunks and (n, 1024) L2-normalised float32 embeddings."""
    rng = np.random.default_rng(0)
    raw = rng.random((n, 1024)).astype(np.float32)
    norms = np.linalg.norm(raw, axis=1, keepdims=True)
    embeddings = raw / norms

    chunks = [
        Chunk(
            text=f"Article {i} text content",
            section=f"Article {i}",
            metadata={"chunk_index": i},
            provenance=_PROV,
            offset_start=i * 100,
            offset_end=(i + 1) * 100,
        )
        for i in range(n)
    ]
    return Corpus(chunks=chunks, embeddings=embeddings, corpus_files=["gdpr.html"])


def _make_manifest():
    from legal_corpus_ingester.pipeline.manifest import Manifest

    return Manifest(
        corpus_version="2026.07.0",
        chunker_version="v1.0.0-vendored",
        embedder_model="apertus-8b-instruct",
        embedder_revision="sha256:abc123",
        sources=["gdpr"],
        chunk_count=2,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_publish_writes_npy(tmp_path: Path) -> None:
    """publish() writes index/legal_kb.npy with correct shape, dtype, and metadata json."""
    from legal_corpus_ingester.publishers.filesystem import FilesystemPublisher
    from legal_corpus_ingester.publishers.base import PublishTarget

    corpus = _make_corpus(2)
    manifest = _make_manifest()
    publisher = FilesystemPublisher()
    target = PublishTarget(kind="filesystem", path=tmp_path)

    receipt = publisher.publish(corpus, target, manifest)

    # NPY exists with correct shape + dtype
    npy_path = tmp_path / "index" / "legal_kb.npy"
    assert npy_path.exists(), "index/legal_kb.npy not created"
    loaded = np.load(str(npy_path))
    assert loaded.shape == (2, 1024)
    assert loaded.dtype == np.float32

    # Metadata JSON exists and parses to 2 dicts
    meta_path = tmp_path / "index" / "legal_kb_metadata.json"
    assert meta_path.exists(), "index/legal_kb_metadata.json not created"
    meta = json.loads(meta_path.read_bytes())
    assert isinstance(meta, list)
    assert len(meta) == 2

    # MANIFEST.yaml present
    assert (tmp_path / "MANIFEST.yaml").exists(), "MANIFEST.yaml not created"

    # checksums.txt present
    assert (tmp_path / "checksums.txt").exists(), "checksums.txt not created"

    # receipt has correct chunk count
    assert receipt.chunk_count == 2


def test_publish_is_atomic_on_npy(tmp_path: Path) -> None:
    """After publish(), no tmp files remain in target.path (atomic write leaves nothing)."""
    from legal_corpus_ingester.publishers.filesystem import FilesystemPublisher
    from legal_corpus_ingester.publishers.base import PublishTarget

    corpus = _make_corpus(2)
    manifest = _make_manifest()
    publisher = FilesystemPublisher()
    target = PublishTarget(kind="filesystem", path=tmp_path)

    publisher.publish(corpus, target, manifest)

    # Walk all files: none should have a tmp_ prefix pattern
    for p in tmp_path.rglob("*"):
        if p.is_file():
            assert not p.name.startswith(".tmp"), (
                f"Temporary file left behind after publish: {p}"
            )


def test_publish_receipt_sha256_matches_file(tmp_path: Path) -> None:
    """receipt.matrix_sha256 matches sha256 of the written legal_kb.npy bytes."""
    from legal_corpus_ingester.publishers.filesystem import FilesystemPublisher
    from legal_corpus_ingester.publishers.base import PublishTarget

    corpus = _make_corpus(2)
    manifest = _make_manifest()
    publisher = FilesystemPublisher()
    target = PublishTarget(kind="filesystem", path=tmp_path)

    receipt = publisher.publish(corpus, target, manifest)

    npy_bytes = (tmp_path / "index" / "legal_kb.npy").read_bytes()
    expected_sha256 = hashlib.sha256(npy_bytes).hexdigest()
    assert receipt.matrix_sha256 == expected_sha256


def test_publish_metadata_sha256_matches_file(tmp_path: Path) -> None:
    """receipt.metadata_sha256 matches sha256 of the written legal_kb_metadata.json bytes."""
    from legal_corpus_ingester.publishers.filesystem import FilesystemPublisher
    from legal_corpus_ingester.publishers.base import PublishTarget

    corpus = _make_corpus(2)
    manifest = _make_manifest()
    publisher = FilesystemPublisher()
    target = PublishTarget(kind="filesystem", path=tmp_path)

    receipt = publisher.publish(corpus, target, manifest)

    meta_bytes = (tmp_path / "index" / "legal_kb_metadata.json").read_bytes()
    expected_sha256 = hashlib.sha256(meta_bytes).hexdigest()
    assert receipt.metadata_sha256 == expected_sha256


def test_publish_chunk_txt_files_written(tmp_path: Path) -> None:
    """Chunk .txt files are written with the expected header format."""
    from legal_corpus_ingester.publishers.filesystem import FilesystemPublisher
    from legal_corpus_ingester.publishers.base import PublishTarget

    corpus = _make_corpus(2)
    manifest = _make_manifest()
    publisher = FilesystemPublisher()
    target = PublishTarget(kind="filesystem", path=tmp_path)

    publisher.publish(corpus, target, manifest)

    # Corpus dir should contain text files for source "gdpr"
    corpus_dir = tmp_path / "corpus" / "gdpr"
    assert corpus_dir.exists(), "corpus/gdpr/ directory not created"
    txt_files = list(corpus_dir.glob("*.txt"))
    assert len(txt_files) == 2, f"Expected 2 .txt files, got {len(txt_files)}"

    # Each file must start with the expected header line
    for txt_file in txt_files:
        content = txt_file.read_text(encoding="utf-8")
        assert content.startswith("# source: gdpr"), (
            f"Header missing in {txt_file}: {content[:60]!r}"
        )


def test_publish_checksums_covers_all_files(tmp_path: Path) -> None:
    """checksums.txt has one line per written file."""
    from legal_corpus_ingester.publishers.filesystem import FilesystemPublisher
    from legal_corpus_ingester.publishers.base import PublishTarget

    corpus = _make_corpus(2)
    manifest = _make_manifest()
    publisher = FilesystemPublisher()
    target = PublishTarget(kind="filesystem", path=tmp_path)

    publisher.publish(corpus, target, manifest)

    checksums_path = tmp_path / "checksums.txt"
    lines = [
        line.strip()
        for line in checksums_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # Each line: "<sha256>  <relative_path>"
    for line in lines:
        parts = line.split("  ", 1)
        assert len(parts) == 2, f"Malformed checksum line: {line!r}"
        sha_hex, rel_path = parts
        assert len(sha_hex) == 64, f"SHA256 should be 64 hex chars: {sha_hex!r}"
        file_path = tmp_path / rel_path
        assert file_path.exists(), f"Checksummed file missing: {file_path}"
