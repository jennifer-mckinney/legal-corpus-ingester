from __future__ import annotations


def test_jurisdiction_codes_is_frozenset() -> None:
    from legal_corpus_ingester.jurisdictions import JURISDICTION_CODES
    assert isinstance(JURISDICTION_CODES, frozenset)


def test_jurisdiction_codes_nonempty() -> None:
    from legal_corpus_ingester.jurisdictions import JURISDICTION_CODES
    assert len(JURISDICTION_CODES) > 10


def test_jurisdiction_codes_contains_known_values() -> None:
    from legal_corpus_ingester.jurisdictions import JURISDICTION_CODES
    # These must always be present — they are in terms-analysis schemas.py
    for code in ("US-CA", "GDPR", "PIPEDA", "LGPD"):
        assert code in JURISDICTION_CODES, f"{code} missing from JURISDICTION_CODES"


def test_sync_script_is_idempotent() -> None:
    """Running sync_jurisdictions.py twice produces identical output."""
    import subprocess
    import sys
    from pathlib import Path
    script = Path("scripts/sync_jurisdictions.py")
    assert script.exists(), "sync_jurisdictions.py must exist"
    schemas = (
        script.resolve().parent.parent.parent
        / "terms-analysis"
        / "src"
        / "backend"
        / "app"
        / "schemas.py"
    )
    if not schemas.is_file():
        import pytest

        pytest.skip("requires the adjacent terms-analysis checkout")

    subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, check=True
    )
    content1 = Path("src/legal_corpus_ingester/jurisdictions.py").read_text()

    subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, check=True
    )
    content2 = Path("src/legal_corpus_ingester/jurisdictions.py").read_text()

    assert content1 == content2, "sync_jurisdictions.py is not idempotent"
