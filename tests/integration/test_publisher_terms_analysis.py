from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

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


def _make_corpus(n: int = 2, version: str = "2026.07.0") -> Corpus:
    """Build a small Corpus with n chunks and (n, 1024) L2-normalised float32 embeddings."""
    rng = np.random.default_rng(42)
    raw = rng.random((n, 1024)).astype(np.float32)
    norms = np.linalg.norm(raw, axis=1, keepdims=True)
    embeddings = raw / norms
    chunks = [
        Chunk(
            text=f"Article {i} body",
            section=f"Article {i}",
            metadata={"chunk_index": i},
            provenance=_PROV,
            offset_start=i * 100,
            offset_end=(i + 1) * 100,
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


def test_publishes_to_versioned_dir(tmp_path: Path) -> None:
    """publish() creates a versioned bundle directory and a 'current' symlink."""
    from legal_corpus_ingester.publishers.base import PublishTarget
    from legal_corpus_ingester.publishers.terms_analysis import TermsAnalysisPublisher

    out_dir = tmp_path / "out"
    corpus = _make_corpus()
    manifest = _make_manifest("2026.07.0")

    TermsAnalysisPublisher().publish(corpus, PublishTarget("terms_analysis", out_dir), manifest)

    # Versioned bundle directory must contain the index artefact
    npy_path = out_dir / "2026.07.0" / "index" / "legal_kb.npy"
    assert npy_path.exists(), f"NPY not found at {npy_path}"

    # 'current' must be a symlink pointing to the versioned bundle
    current = out_dir / "current"
    assert current.is_symlink(), "'current' is not a symlink"
    assert current.readlink() == Path("2026.07.0"), (
        f"'current' points to {current.readlink()!r}, expected '2026.07.0'"
    )


def test_hot_reload_copies_exist(tmp_path: Path) -> None:
    """publish() copies index files into out_dir root for hot-reload safety."""
    from legal_corpus_ingester.publishers.base import PublishTarget
    from legal_corpus_ingester.publishers.terms_analysis import TermsAnalysisPublisher

    out_dir = tmp_path / "out"
    corpus = _make_corpus()
    manifest = _make_manifest("2026.07.0")

    TermsAnalysisPublisher().publish(corpus, PublishTarget("terms_analysis", out_dir), manifest)

    assert (out_dir / "legal_kb.npy").exists(), "Hot-reload copy legal_kb.npy missing"
    assert (out_dir / "legal_kb_metadata.json").exists(), (
        "Hot-reload copy legal_kb_metadata.json missing"
    )


def test_rollback_repoints_symlink(tmp_path: Path) -> None:
    """Republishing repoints 'current' symlink; manual repoint via atomic_symlink also works."""
    from legal_corpus_ingester.publishers.base import PublishTarget
    from legal_corpus_ingester.publishers.latest_symlink import atomic_symlink
    from legal_corpus_ingester.publishers.terms_analysis import TermsAnalysisPublisher

    out_dir = tmp_path / "out"
    publisher = TermsAnalysisPublisher()

    # Publish v1
    publisher.publish(
        _make_corpus(), PublishTarget("terms_analysis", out_dir), _make_manifest("2026.07.0")
    )
    assert (out_dir / "current").readlink() == Path("2026.07.0")

    # Publish v2 — 'current' must repoint to new version
    publisher.publish(
        _make_corpus(), PublishTarget("terms_analysis", out_dir), _make_manifest("2026.07.1")
    )
    assert (out_dir / "current").readlink() == Path("2026.07.1")

    # Manual rollback via atomic_symlink
    atomic_symlink("2026.07.0", out_dir / "current")
    assert (out_dir / "current").readlink() == Path("2026.07.0")


def test_sighup_called_with_pid(tmp_path: Path) -> None:
    """publish(pid=12345) calls reload_signal exactly once with that pid."""
    from legal_corpus_ingester.publishers.base import PublishTarget
    from legal_corpus_ingester.publishers.terms_analysis import TermsAnalysisPublisher

    out_dir = tmp_path / "out"
    corpus = _make_corpus()
    manifest = _make_manifest("2026.07.0")

    with patch(
        "legal_corpus_ingester.publishers.terms_analysis.reload_signal"
    ) as mock_reload:
        TermsAnalysisPublisher().publish(
            corpus,
            PublishTarget("terms_analysis", out_dir),
            manifest,
            pid=12345,
        )

    mock_reload.assert_called_once_with(12345)


def test_sighup_not_called_without_pid(tmp_path: Path) -> None:
    """publish() without pid (default pid=0) must NOT call reload_signal."""
    from legal_corpus_ingester.publishers.base import PublishTarget
    from legal_corpus_ingester.publishers.terms_analysis import TermsAnalysisPublisher

    out_dir = tmp_path / "out"
    corpus = _make_corpus()
    manifest = _make_manifest("2026.07.0")

    with patch(
        "legal_corpus_ingester.publishers.terms_analysis.reload_signal"
    ) as mock_reload:
        TermsAnalysisPublisher().publish(
            corpus,
            PublishTarget("terms_analysis", out_dir),
            manifest,
        )

    mock_reload.assert_not_called()
