from __future__ import annotations
import dataclasses
from legal_corpus_ingester.types import ProvenanceRecord


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
