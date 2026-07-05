from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from legal_corpus_ingester.errors import LegalReviewGateError
from legal_corpus_ingester.pipeline.approval_gate import check_approval

# Canonical SHA256 value used in the fixture files.
VALID_SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

# Locate the fixture directory relative to this test file.
_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "approvals"


@pytest.fixture()
def approval_fixture() -> Path:
    """Return the path to the valid sg_sso_terms.yaml fixture."""
    return _FIXTURE_DIR / "sg_sso_terms.yaml"


@pytest.fixture()
def expired_fixture() -> Path:
    """Return the path to the expired.yaml fixture."""
    return _FIXTURE_DIR / "expired.yaml"


def test_passes_with_valid_approval(approval_fixture: Path) -> None:
    """check_approval raises nothing for a well-formed, unexpired approval."""
    check_approval(approval_fixture, "sg-sso-terms", VALID_SHA, today=date(2026, 7, 4))


def test_blocks_on_missing_file(tmp_path: Path) -> None:
    """check_approval raises LegalReviewGateError when the file does not exist."""
    with pytest.raises(LegalReviewGateError, match="not found"):
        check_approval(tmp_path / "missing.yaml", "x", "y")


def test_blocks_on_expired(expired_fixture: Path) -> None:
    """check_approval raises LegalReviewGateError when today >= expiry date."""
    with pytest.raises(LegalReviewGateError, match="expired"):
        check_approval(expired_fixture, "sg-sso-terms", VALID_SHA, today=date(2026, 7, 4))


def test_blocks_on_sha256_mismatch(approval_fixture: Path) -> None:
    """check_approval raises LegalReviewGateError when the SHA256 does not match."""
    with pytest.raises(LegalReviewGateError, match="mismatch"):
        check_approval(approval_fixture, "sg-sso-terms", "wrong-sha", today=date(2026, 7, 4))


def test_blocks_on_source_id_mismatch(approval_fixture: Path) -> None:
    """check_approval raises LegalReviewGateError when source_id does not match."""
    with pytest.raises(LegalReviewGateError, match="source_id"):
        check_approval(approval_fixture, "wrong-source", VALID_SHA, today=date(2026, 7, 4))


def test_gate_blocks_on_missing_expiry(tmp_path: Path) -> None:
    """check_approval raises LegalReviewGateError when 'expiry' field is absent."""
    approval_file = tmp_path / "no_expiry.yaml"
    approval_file.write_text(
        "source_id: sg-sso-terms\n"
        f"signed_artifact_sha256: {VALID_SHA}\n",
        encoding="utf-8",
    )
    with pytest.raises(LegalReviewGateError, match="expiry"):
        check_approval(approval_file, "sg-sso-terms", VALID_SHA, today=date(2026, 7, 4))


def test_gate_blocks_on_invalid_expiry_format(tmp_path: Path) -> None:
    """check_approval raises LegalReviewGateError when 'expiry' is not ISO date format."""
    approval_file = tmp_path / "bad_expiry.yaml"
    approval_file.write_text(
        "source_id: sg-sso-terms\n"
        f"signed_artifact_sha256: {VALID_SHA}\n"
        "expiry: not-a-date\n",
        encoding="utf-8",
    )
    with pytest.raises(LegalReviewGateError, match="not a valid ISO date"):
        check_approval(approval_file, "sg-sso-terms", VALID_SHA, today=date(2026, 7, 4))


def test_check_approval_rejects_placeholder_sha256(tmp_path: Path) -> None:
    """check_approval raises LegalReviewGateError when signed_artifact_sha256 is the all-zeros placeholder."""
    from datetime import date, timedelta

    approval = tmp_path / "test.yaml"
    future = (date.today() + timedelta(days=365)).isoformat()
    approval.write_text(yaml.dump({
        "source_id": "test-source",
        "signed_artifact_sha256": "0" * 64,
        "expiry": future,
        "approved_by": "test@example.com",
        "approved_at": "2026-01-01",
    }))
    with pytest.raises(LegalReviewGateError, match="not yet signed"):
        check_approval(approval, "test-source", "0" * 64)


def test_warns_within_60_days(tmp_path: Path) -> None:
    """check_approval emits a UserWarning when expiry is within EXPIRY_WARNING_DAYS."""
    # Expiry 30 days from our 'today'
    today = date(2026, 7, 4)
    expiry = date(today.year, today.month, today.day)
    from datetime import timedelta
    expiry = today + timedelta(days=30)

    approval_path = tmp_path / "near_expiry.yaml"
    approval_path.write_text(
        yaml.dump({
            "source_id": "sg-sso-terms",
            "signed_artifact_sha256": VALID_SHA,
            "expiry": expiry.isoformat(),
            "approved_by": "legal@example.com",
            "approved_at": "2026-06-01",
        }),
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match="expires in"):
        check_approval(approval_path, "sg-sso-terms", VALID_SHA, today=today)
