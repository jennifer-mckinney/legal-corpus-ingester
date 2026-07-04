from __future__ import annotations

import hashlib
import tarfile
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
    """Build a Corpus with n chunks and (n, 1024) L2-normalised float32 embeddings."""
    rng = np.random.default_rng(7)
    raw = rng.random((n, 1024)).astype(np.float32)
    norms = np.linalg.norm(raw, axis=1, keepdims=True)
    embeddings = raw / norms
    chunks = [
        Chunk(
            text=f"Section {i} text",
            section=f"Section {i}",
            metadata={"chunk_index": i},
            provenance=_PROV,
            offset_start=i * 50,
            offset_end=(i + 1) * 50,
        )
        for i in range(n)
    ]
    return Corpus(chunks=chunks, embeddings=embeddings, corpus_files=["gdpr.html"])


def _make_manifest(version: str = "2026.07.0"):
    from legal_corpus_ingester.pipeline.manifest import Manifest

    return Manifest(
        corpus_version=version,
        chunker_version="v1.0.0-vendored",
        embedder_model="apertus-8b-instruct",
        embedder_revision="sha256:abc123",
        sources=["gdpr"],
        chunk_count=2,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_tarball_created(tmp_path: Path) -> None:
    """publish() creates a .tar.gz tarball in the bundle directory."""
    from legal_corpus_ingester.publishers.base import PublishTarget
    from legal_corpus_ingester.publishers.tarball import TarballPublisher

    out_dir = tmp_path / "out"
    corpus = _make_corpus()
    manifest = _make_manifest("2026.07.0")

    TarballPublisher().publish(corpus, PublishTarget("tarball", out_dir), manifest)

    tarball = out_dir / "2026.07.0" / "legal-corpus-2026.07.0.tar.gz"
    assert tarball.exists(), f"Tarball not found at {tarball}"
    assert tarball.stat().st_size > 0, "Tarball is empty"


def test_tarball_contains_all_bundle_files(tmp_path: Path) -> None:
    """The tarball contains both legal_kb.npy and MANIFEST.yaml."""
    from legal_corpus_ingester.publishers.base import PublishTarget
    from legal_corpus_ingester.publishers.tarball import TarballPublisher

    out_dir = tmp_path / "out"
    corpus = _make_corpus()
    manifest = _make_manifest("2026.07.0")

    TarballPublisher().publish(corpus, PublishTarget("tarball", out_dir), manifest)

    tarball = out_dir / "2026.07.0" / "legal-corpus-2026.07.0.tar.gz"
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()

    with tarfile.open(str(tarball), "r:gz") as tf:
        tf.extractall(str(extract_dir))

    # legal_kb.npy must be present somewhere in the extracted tree
    npy_files = list(extract_dir.rglob("legal_kb.npy"))
    assert npy_files, "legal_kb.npy not found in tarball"

    # MANIFEST.yaml must also be present
    manifest_files = list(extract_dir.rglob("MANIFEST.yaml"))
    assert manifest_files, "MANIFEST.yaml not found in tarball"


def test_tarball_sha256_in_receipt(tmp_path: Path) -> None:
    """receipt.tarball_sha256 is non-empty and matches the actual tarball bytes."""
    from legal_corpus_ingester.publishers.base import PublishTarget
    from legal_corpus_ingester.publishers.tarball import TarballPublisher

    out_dir = tmp_path / "out"
    corpus = _make_corpus()
    manifest = _make_manifest("2026.07.0")

    receipt = TarballPublisher().publish(corpus, PublishTarget("tarball", out_dir), manifest)

    assert receipt.tarball_sha256 != "", "tarball_sha256 must not be empty"

    tarball = out_dir / "2026.07.0" / "legal-corpus-2026.07.0.tar.gz"
    expected_sha256 = hashlib.sha256(tarball.read_bytes()).hexdigest()
    assert receipt.tarball_sha256 == expected_sha256, (
        f"SHA256 mismatch: receipt={receipt.tarball_sha256!r}, "
        f"file={expected_sha256!r}"
    )


def test_round_trip_npy(tmp_path: Path) -> None:
    """Extract the tarball and reload legal_kb.npy — shape and dtype must match."""
    from legal_corpus_ingester.publishers.base import PublishTarget
    from legal_corpus_ingester.publishers.tarball import TarballPublisher

    n = 3
    out_dir = tmp_path / "out"
    corpus = _make_corpus(n)
    manifest = _make_manifest("2026.07.0")
    manifest.chunk_count = n  # keep manifest consistent with n

    TarballPublisher().publish(corpus, PublishTarget("tarball", out_dir), manifest)

    tarball = out_dir / "2026.07.0" / "legal-corpus-2026.07.0.tar.gz"
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()

    with tarfile.open(str(tarball), "r:gz") as tf:
        tf.extractall(str(extract_dir))

    npy_path = next(extract_dir.rglob("legal_kb.npy"))
    loaded = np.load(str(npy_path))
    assert loaded.shape == (n, 1024), f"Shape mismatch: {loaded.shape}"
    assert loaded.dtype == np.float32, f"Dtype mismatch: {loaded.dtype}"


def test_tarball_does_not_contain_itself(tmp_path: Path) -> None:
    """The tarball must not include itself (no recursive self-inclusion)."""
    from legal_corpus_ingester.publishers.base import PublishTarget
    from legal_corpus_ingester.publishers.tarball import TarballPublisher

    out_dir = tmp_path / "out"
    corpus = _make_corpus()
    manifest = _make_manifest("2026.07.0")

    TarballPublisher().publish(corpus, PublishTarget("tarball", out_dir), manifest)

    tarball_name = "legal-corpus-2026.07.0.tar.gz"
    tarball = out_dir / "2026.07.0" / tarball_name

    with tarfile.open(str(tarball), "r:gz") as tf:
        members = [m.name for m in tf.getmembers()]

    # No member should be the tarball file itself
    assert not any(tarball_name in m for m in members), (
        f"Tarball contains itself — member names: {members}"
    )
