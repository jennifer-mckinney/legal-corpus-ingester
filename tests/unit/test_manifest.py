from __future__ import annotations
import tempfile
from pathlib import Path


def test_manifest_write_and_load_roundtrip() -> None:
    from legal_corpus_ingester.pipeline.manifest import Manifest
    with tempfile.TemporaryDirectory() as tmp:
        bundle_dir = Path(tmp)
        m = Manifest(
            corpus_version="2026.07.0",
            chunker_version="v1.0.0-vendored-from-terms-analysis",
            embedder_model="apertus-8b-instruct",
            embedder_revision="sha256:abc123",
            sources=["eurlex", "coe"],
            chunk_count=42,
        )
        m.write(bundle_dir)
        loaded = Manifest.load(bundle_dir)
        assert loaded.corpus_version == "2026.07.0"
        assert loaded.chunker_version == m.chunker_version
        assert loaded.chunk_count == 42
        assert "eurlex" in loaded.sources


def test_manifest_calver_format() -> None:
    import re
    from legal_corpus_ingester.pipeline.manifest import Manifest
    with tempfile.TemporaryDirectory() as tmp:
        m = Manifest(
            corpus_version="2026.07.0",
            chunker_version="v1",
            embedder_model="apertus-8b-instruct",
            embedder_revision="sha256:abc",
            sources=["test"],
            chunk_count=1,
        )
        m.write(Path(tmp))
        loaded = Manifest.load(Path(tmp))
        assert re.match(r"^\d{4}\.\d{2}\.\d+$", loaded.corpus_version), \
            f"corpus_version {loaded.corpus_version!r} not YYYY.MM.PATCH calver"


def test_manifest_missing_required_field_raises() -> None:
    from legal_corpus_ingester.pipeline.manifest import Manifest
    import pytest
    with pytest.raises((TypeError, Exception)):
        # Missing embedder_model — should raise at construction
        Manifest(  # type: ignore[call-arg]
            corpus_version="2026.07.0",
            chunker_version="v1",
            embedder_revision="sha256:abc",
            sources=["test"],
            chunk_count=0,
            # embedder_model missing
        )


def test_manifest_load_missing_file_raises() -> None:
    import pytest
    from legal_corpus_ingester.pipeline.manifest import Manifest, ManifestError
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ManifestError):
            Manifest.load(Path(tmp))  # no MANIFEST.yaml here
