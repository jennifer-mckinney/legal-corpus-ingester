from __future__ import annotations
import hashlib
import json


def test_record_produces_provenance_record() -> None:
    from legal_corpus_ingester.provenance.tracker import ProvenanceTracker
    from legal_corpus_ingester.types import ProvenanceRecord
    tracker = ProvenanceTracker(fetcher_class="fetchers.eurlex.EurLexFetcher")
    record = tracker.record(
        source_name="eurlex",
        source_url="https://eur-lex.europa.eu/eli/reg/2016/679/oj",
        license_spdx="CC-BY-4.0",
        upstream_version="20160504",
        content=b"GDPR full text",
    )
    assert isinstance(record, ProvenanceRecord)
    assert record.source_name == "eurlex"
    assert record.license == "CC-BY-4.0"
    assert record.upstream_version == "20160504"


def test_record_timestamp_is_utc_iso8601() -> None:
    from legal_corpus_ingester.provenance.tracker import ProvenanceTracker
    tracker = ProvenanceTracker(fetcher_class="test.Fetcher")
    record = tracker.record(
        source_name="test",
        source_url="https://example.com",
        license_spdx="MIT",
        upstream_version=None,
        content=b"content",
    )
    # ISO-8601 UTC: ends with Z
    assert record.fetch_timestamp.endswith("Z")
    # parseable
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(record.fetch_timestamp.rstrip("Z")).replace(tzinfo=timezone.utc)
    assert dt.tzinfo is not None


def test_record_content_sha256() -> None:
    from legal_corpus_ingester.provenance.tracker import ProvenanceTracker
    content = b"test legal content"
    tracker = ProvenanceTracker(fetcher_class="test.Fetcher")
    record = tracker.record(
        source_name="test",
        source_url="https://example.com",
        license_spdx="MIT",
        upstream_version=None,
        content=content,
    )
    expected_sha = hashlib.sha256(content).hexdigest()
    assert record.content_sha256 == expected_sha


def test_record_roundtrip_json() -> None:
    from legal_corpus_ingester.provenance.tracker import ProvenanceTracker
    from legal_corpus_ingester.types import ProvenanceRecord
    import dataclasses
    tracker = ProvenanceTracker(fetcher_class="test.Fetcher")
    record = tracker.record(
        source_name="test",
        source_url="https://example.com",
        license_spdx="MIT",
        upstream_version="v1",
        content=b"content",
    )
    # Serialize to JSON and back
    as_dict = dataclasses.asdict(record)
    json_str = json.dumps(as_dict, sort_keys=True)
    loaded = json.loads(json_str)
    restored = ProvenanceRecord(**loaded)
    assert restored == record


def test_same_content_deterministic_sha() -> None:
    from legal_corpus_ingester.provenance.tracker import ProvenanceTracker
    content = b"deterministic content"
    t1 = ProvenanceTracker(fetcher_class="test.Fetcher")
    t2 = ProvenanceTracker(fetcher_class="test.Fetcher")
    r1 = t1.record("src", "https://x.com", "MIT", None, content)
    r2 = t2.record("src", "https://x.com", "MIT", None, content)
    # SHA must match
    assert r1.content_sha256 == r2.content_sha256
    # Timestamps may differ by milliseconds — that's fine
