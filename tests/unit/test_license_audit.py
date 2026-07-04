from __future__ import annotations
import json
import tempfile
from pathlib import Path


def test_hash_license_page_sha256_of_normalized_content() -> None:
    from legal_corpus_ingester.provenance.license_audit import hash_content
    content = "  CC-BY-4.0   License\n\nSome   text  "
    h = hash_content(content)
    # normalized: whitespace collapsed
    import hashlib
    import re
    normalized = re.sub(r"\s+", " ", content).strip()
    expected = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    assert h == expected


def test_check_drift_new_source() -> None:
    from legal_corpus_ingester.provenance.license_audit import check_license_drift, DriftStatus
    with tempfile.TemporaryDirectory() as tmp:
        state_file = Path(tmp) / "license-hashes.json"
        result = check_license_drift(
            source_name="eurlex",
            current_hash="abc123",
            current_spdx="CC-BY-4.0",
            state_file=state_file,
        )
        assert result == DriftStatus.NEW


def test_check_drift_no_change() -> None:
    from legal_corpus_ingester.provenance.license_audit import check_license_drift, DriftStatus
    with tempfile.TemporaryDirectory() as tmp:
        state_file = Path(tmp) / "license-hashes.json"
        state_file.write_text(json.dumps({
            "eurlex": {"hash": "abc123", "spdx": "CC-BY-4.0"}
        }))
        result = check_license_drift(
            source_name="eurlex",
            current_hash="abc123",
            current_spdx="CC-BY-4.0",
            state_file=state_file,
        )
        assert result == DriftStatus.NO_CHANGE


def test_check_drift_spdx_changed() -> None:
    from legal_corpus_ingester.provenance.license_audit import check_license_drift, DriftStatus
    with tempfile.TemporaryDirectory() as tmp:
        state_file = Path(tmp) / "license-hashes.json"
        state_file.write_text(json.dumps({
            "eurlex": {"hash": "abc123", "spdx": "CC-BY-4.0"}
        }))
        result = check_license_drift(
            source_name="eurlex",
            current_hash="abc123",
            current_spdx="CC-BY-NC-4.0",  # SPDX changed
            state_file=state_file,
        )
        assert result == DriftStatus.SPDX_DRIFT


def test_check_drift_hash_changed() -> None:
    from legal_corpus_ingester.provenance.license_audit import check_license_drift, DriftStatus
    with tempfile.TemporaryDirectory() as tmp:
        state_file = Path(tmp) / "license-hashes.json"
        state_file.write_text(json.dumps({
            "eurlex": {"hash": "abc123", "spdx": "CC-BY-4.0"}
        }))
        result = check_license_drift(
            source_name="eurlex",
            current_hash="def456",  # hash changed
            current_spdx="CC-BY-4.0",
            state_file=state_file,
        )
        assert result == DriftStatus.DRIFT
