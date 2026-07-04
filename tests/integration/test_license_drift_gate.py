from __future__ import annotations

import json
import warnings

import numpy as np
import pytest

from legal_corpus_ingester.errors import LicenseDriftError
from legal_corpus_ingester.pipeline.manifest import Manifest
from legal_corpus_ingester.publishers.base import PublishReceipt, PublishTarget
from legal_corpus_ingester.types import (
    Chunk,
    CleanedDocument,
    Corpus,
    FetchResult,
    ProvenanceRecord,
)


# ---------------------------------------------------------------------------
# Helpers to build fake data with a configurable license SPDX
# ---------------------------------------------------------------------------


def _make_provenance(license_spdx: str = "CC-BY-4.0") -> ProvenanceRecord:
    return ProvenanceRecord(
        source_url="https://example.com/tos",
        fetch_timestamp="2026-07-04T00:00:00Z",
        license=license_spdx,
        upstream_version="1.0",
        content_sha256="deadbeef",
        source_name="test-source",
        fetcher_class="FakeFetcher",
    )


def _make_fetch_result(license_spdx: str = "CC-BY-4.0", text: str = "Terms of service.") -> FetchResult:
    prov = _make_provenance(license_spdx)
    return FetchResult(
        source_name="test-source",
        raw_bytes=text.encode(),
        mime_type="text/plain",
        upstream_metadata={},
        provenance=prov,
    )


def _make_doc(text: str = "Terms of service.", license_spdx: str = "CC-BY-4.0") -> CleanedDocument:
    return CleanedDocument(
        text=text,
        has_sections=False,
        headers={},
        provenance=_make_provenance(license_spdx),
    )


# ---------------------------------------------------------------------------
# Fake injectable components (mirrored from test_orchestrator.py)
# ---------------------------------------------------------------------------


class FakeFetcherWithLicense:
    """Returns a FetchResult with a configurable license SPDX."""

    def __init__(self, license_spdx: str = "CC-BY-4.0", text: str = "Terms of service.") -> None:
        self._license = license_spdx
        self._text = text

    async def fetch(self, source_config: object) -> FetchResult:
        return _make_fetch_result(self._license, self._text)


class FakeCleanerWithText:
    """Returns a CleanedDocument that mirrors the fetcher's license + text."""

    def __init__(self, license_spdx: str = "CC-BY-4.0", text: str = "Terms of service.") -> None:
        self._license = license_spdx
        self._text = text

    def clean(self, result: FetchResult) -> CleanedDocument:
        return _make_doc(self._text, self._license)


class FakeChunker:
    def chunk(self, doc: CleanedDocument) -> list[Chunk]:
        return [
            Chunk(
                text=doc.text,
                section="main",
                metadata={},
                provenance=doc.provenance,
                offset_start=0,
                offset_end=len(doc.text),
            )
        ]


class FakeEmbedder:
    async def embed(self, chunks: list[Chunk]) -> np.ndarray:  # type: ignore[type-arg]
        n = len(chunks)
        raw = np.ones((n, 1024), dtype=np.float32)
        norms = np.linalg.norm(raw, axis=1, keepdims=True)
        return (raw / norms).astype(np.float32)

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
# Helper: build orchestrator with license_state_file
# ---------------------------------------------------------------------------


def _make_orch(
    tmp_path,
    license_spdx: str = "CC-BY-4.0",
    text: str = "Terms of service.",
    license_state_file=None,
):
    from legal_corpus_ingester.pipeline.orchestrator import Orchestrator

    return Orchestrator(
        fetcher=FakeFetcherWithLicense(license_spdx, text),
        cleaner=FakeCleanerWithText(license_spdx, text),
        chunker=FakeChunker(),
        embedder=FakeEmbedder(),
        publisher=FakePublisher(),
        publish_target=PublishTarget(kind="filesystem", path=tmp_path / "output"),
        state_dir=tmp_path / "state",
        corpus_version="2026.07.0",
        chunker_version="v1.0.0",
        license_state_file=license_state_file,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_spdx_drift_blocks_publish(tmp_path):
    """When the SPDX changes, LicenseDriftError is raised and ALERTS.md is written."""
    license_state_file = tmp_path / "license-hashes.json"

    # Establish baseline with CC-BY-4.0
    baseline = {"test-source": {"hash": "somehash", "spdx": "CC-BY-4.0"}}
    license_state_file.write_text(json.dumps(baseline))

    # Now run with CC-BY-NC — SPDX drift
    orch = _make_orch(
        tmp_path,
        license_spdx="CC-BY-NC",
        text="Terms of service.",
        license_state_file=license_state_file,
    )

    with pytest.raises(LicenseDriftError):
        orch.run("test-source", object())

    # ALERTS.md must exist at the publish target path
    alerts_path = (tmp_path / "output") / "ALERTS.md"
    assert alerts_path.exists(), "ALERTS.md not written on SPDX drift"
    assert "LICENSE DRIFT ALERT" in alerts_path.read_text()


def test_content_hash_drift_warns(tmp_path):
    """When only the hash changes (same SPDX), a UserWarning is issued but run succeeds."""
    license_state_file = tmp_path / "license-hashes.json"

    # Establish baseline with a hash that won't match current content
    from legal_corpus_ingester.provenance.license_audit import hash_content

    original_text = "Original text."
    original_hash = hash_content(original_text)
    baseline = {"test-source": {"hash": original_hash, "spdx": "CC-BY-4.0"}}
    license_state_file.write_text(json.dumps(baseline))

    # Run with different text but same SPDX → content hash drift
    orch = _make_orch(
        tmp_path,
        license_spdx="CC-BY-4.0",
        text="Different text now.",
        license_state_file=license_state_file,
    )

    with pytest.warns(UserWarning, match="License text changed"):
        receipt = orch.run("test-source", object())

    assert receipt.chunk_count == 1


def test_no_drift_records_baseline(tmp_path):
    """First run (NEW status) records baseline; second run (NO_CHANGE) succeeds silently."""
    license_state_file = tmp_path / "license-hashes.json"

    # First run — no baseline yet (NEW)
    orch1 = _make_orch(
        tmp_path,
        license_spdx="CC-BY-4.0",
        text="Terms of service.",
        license_state_file=license_state_file,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning = test failure
        receipt1 = orch1.run("test-source", object())

    assert receipt1.chunk_count == 1
    assert license_state_file.exists(), "Baseline not recorded after first run"

    baseline = json.loads(license_state_file.read_text())
    assert "test-source" in baseline

    # Second run — NO_CHANGE, no warning, succeeds
    orch2 = _make_orch(
        tmp_path / "run2",
        license_spdx="CC-BY-4.0",
        text="Terms of service.",
        license_state_file=license_state_file,
    )
    (tmp_path / "run2").mkdir(parents=True, exist_ok=True)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        receipt2 = orch2.run("test-source", object())

    assert receipt2.chunk_count == 1
