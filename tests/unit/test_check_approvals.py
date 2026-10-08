"""Unit tests for scripts/check_approvals.py."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

# scripts/ is not a package; inject it into the path.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

import check_approvals
from check_approvals import YamlDirError, check_approvals_dir, classify_approval  # noqa: E402

# Valid 64-char lowercase hex sha256 used by tests that need a passing sha256.
_VALID_SHA256 = "a" * 64


# ---------------------------------------------------------------------------
# classify_approval
# ---------------------------------------------------------------------------


class TestClassifyApproval:
    def test_ok_when_far_from_expiry(self):
        """Today is well before expiry — should be OK."""
        today = date(2026, 1, 1)
        expiry = date(2026, 6, 1)  # 151 days away
        assert classify_approval(expiry, today, warn_days=60) == "OK"

    def test_expiring_soon_within_warn_window(self):
        """Today is within warn_days of expiry — should be EXPIRING_SOON."""
        today = date(2026, 1, 1)
        expiry = date(2026, 2, 1)  # 31 days away, within 60-day window
        assert classify_approval(expiry, today, warn_days=60) == "EXPIRING_SOON"

    def test_expiring_soon_at_exact_boundary(self):
        """Exactly warn_days before expiry — should be EXPIRING_SOON (inclusive boundary)."""
        today = date(2026, 1, 1)
        expiry = date(2026, 3, 2)  # 60 days away exactly
        assert classify_approval(expiry, today, warn_days=60) == "EXPIRING_SOON"

    def test_expired_on_expiry_day(self):
        """today == expiry — boundary condition should be EXPIRED."""
        today = date(2026, 6, 1)
        expiry = date(2026, 6, 1)
        assert classify_approval(expiry, today, warn_days=60) == "EXPIRED"

    def test_expired_past_expiry(self):
        """today is past expiry — should be EXPIRED."""
        today = date(2026, 7, 1)
        expiry = date(2026, 6, 1)
        assert classify_approval(expiry, today, warn_days=60) == "EXPIRED"

    def test_ok_with_zero_warn_days(self):
        """warn_days=0 means EXPIRING_SOON only fires on expiry day itself."""
        today = date(2026, 1, 1)
        expiry = date(2026, 1, 2)  # 1 day away
        # With warn_days=0, (expiry-today).days == 1 > 0, so should be OK.
        assert classify_approval(expiry, today, warn_days=0) == "OK"


# ---------------------------------------------------------------------------
# check_approvals_dir — directory-level scenarios
# ---------------------------------------------------------------------------


class TestCheckApprovalsDir:
    def test_no_directory_fails_closed(self, tmp_path):
        """A missing dir raises; it is never "nothing expired" (terms-analysis#173, grumpy 4)."""
        missing = tmp_path / "no_such_dir"
        with pytest.raises(YamlDirError, match="does not exist"):
            check_approvals_dir(missing, date.today(), warn_days=60)

    def test_empty_directory_is_not_success(self, tmp_path):
        """Zero approval files must not read as "all OK" for any caller (terms-analysis#173)."""
        # Grumpy r2-1: one contract only. The walk raises, with the shared empty-dir wording.
        with pytest.raises(YamlDirError) as raised:
            check_approvals_dir(tmp_path, date.today(), warn_days=60)
        assert str(raised.value) == check_approvals._empty_dir_problem(tmp_path, "approvals dir")

    def test_valid_expired_approval(self, tmp_path):
        """An expired APPROVAL.yaml yields any_expired=True and an EXPIRED row."""
        approval = tmp_path / "singapore_sso.yaml"
        approval.write_text(
            f"source_id: singapore_sso\n"
            f"expiry: 2020-01-01\n"
            f"signed_artifact_sha256: {_VALID_SHA256}\n",
            encoding="utf-8",
        )
        rows, any_expired = check_approvals_dir(tmp_path, date(2026, 7, 4), warn_days=60)
        assert any_expired is True
        assert len(rows) == 1
        assert rows[0]["status"] == "EXPIRED"
        assert rows[0]["source_id"] == "singapore_sso"
        # days_remaining should be the absolute days past expiry (positive).
        assert isinstance(rows[0]["days_remaining"], int)
        assert rows[0]["days_remaining"] >= 0

    def test_valid_ok_approval(self, tmp_path):
        """A current APPROVAL.yaml yields any_expired=False and an OK row."""
        approval = tmp_path / "eurlex.yaml"
        approval.write_text(
            f"source_id: eurlex\n"
            f"expiry: 2030-12-31\n"
            f"signed_artifact_sha256: {_VALID_SHA256}\n",
            encoding="utf-8",
        )
        rows, any_expired = check_approvals_dir(tmp_path, date(2026, 7, 4), warn_days=60)
        assert any_expired is False
        assert len(rows) == 1
        assert rows[0]["status"] == "OK"

    def test_malformed_yaml_yields_error_row(self, tmp_path):
        """Unparseable YAML yields an ERROR row and any_expired=True (HR9: block ingest)."""
        bad = tmp_path / "bad_source.yaml"
        bad.write_text(": : this is not valid yaml\n", encoding="utf-8")
        rows, any_expired = check_approvals_dir(tmp_path, date(2026, 7, 4), warn_days=60)
        assert any_expired is True
        assert len(rows) == 1
        assert rows[0]["status"] == "ERROR"
        assert rows[0]["source_id"] == "bad_source"

    def test_missing_expiry_field_yields_error_row(self, tmp_path):
        """YAML without 'expiry' key yields an ERROR row and any_expired=True."""
        no_expiry = tmp_path / "no_expiry.yaml"
        no_expiry.write_text(
            f"source_id: no_expiry\n"
            f"signed_artifact_sha256: {_VALID_SHA256}\n",
            encoding="utf-8",
        )
        rows, any_expired = check_approvals_dir(tmp_path, date(2026, 7, 4), warn_days=60)
        assert any_expired is True
        assert len(rows) == 1
        assert rows[0]["status"] == "ERROR"
        assert "expiry" in rows[0]["expiry"]  # error message mentions the missing field

    def test_invalid_date_format_yields_error_row(self, tmp_path):
        """YAML with a malformed date string yields an ERROR row and any_expired=True."""
        bad_date = tmp_path / "bad_date.yaml"
        bad_date.write_text(
            "source_id: bad_date\n"
            "expiry: not-a-date\n",
            encoding="utf-8",
        )
        rows, any_expired = check_approvals_dir(tmp_path, date(2026, 7, 4), warn_days=60)
        assert any_expired is True
        assert rows[0]["status"] == "ERROR"

    def test_mixed_ok_and_expired(self, tmp_path):
        """Mix of OK and EXPIRED files: any_expired=True, both rows present."""
        ok_file = tmp_path / "ok_source.yaml"
        ok_file.write_text(
            f"source_id: ok_source\nexpiry: 2030-01-01\nsigned_artifact_sha256: {_VALID_SHA256}\n",
            encoding="utf-8",
        )
        expired_file = tmp_path / "expired_source.yaml"
        expired_file.write_text(
            f"source_id: expired_source\nexpiry: 2020-01-01\nsigned_artifact_sha256: {_VALID_SHA256}\n",
            encoding="utf-8",
        )
        rows, any_expired = check_approvals_dir(tmp_path, date(2026, 7, 4), warn_days=60)
        assert any_expired is True
        statuses = {r["source_id"]: r["status"] for r in rows}
        assert statuses["ok_source"] == "OK"
        assert statuses["expired_source"] == "EXPIRED"


# ---------------------------------------------------------------------------
# check_approvals_dir — HR9 signed_artifact_sha256 validation
# ---------------------------------------------------------------------------


class TestCheckApprovalsDirSha256:
    def test_missing_sha256_yields_error_row(self, tmp_path):
        """YAML with valid expiry but missing signed_artifact_sha256 yields ERROR."""
        no_sha = tmp_path / "no_sha.yaml"
        no_sha.write_text(
            "source_id: no_sha\n"
            "expiry: 2030-12-31\n",
            encoding="utf-8",
        )
        rows, any_expired = check_approvals_dir(tmp_path, date(2026, 7, 4), warn_days=60)
        assert any_expired is True
        assert len(rows) == 1
        assert rows[0]["status"] == "ERROR"
        assert rows[0]["source_id"] == "no_sha"

    def test_empty_sha256_yields_error_row(self, tmp_path):
        """YAML with empty-string signed_artifact_sha256 yields ERROR."""
        empty_sha = tmp_path / "empty_sha.yaml"
        empty_sha.write_text(
            "source_id: empty_sha\n"
            "expiry: 2030-12-31\n"
            "signed_artifact_sha256: ''\n",
            encoding="utf-8",
        )
        rows, any_expired = check_approvals_dir(tmp_path, date(2026, 7, 4), warn_days=60)
        assert any_expired is True
        assert rows[0]["status"] == "ERROR"

    def test_whitespace_only_sha256_yields_error_row(self, tmp_path):
        """YAML with whitespace-only signed_artifact_sha256 yields ERROR."""
        ws_sha = tmp_path / "ws_sha.yaml"
        ws_sha.write_text(
            "source_id: ws_sha\n"
            "expiry: 2030-12-31\n"
            "signed_artifact_sha256: '   '\n",
            encoding="utf-8",
        )
        rows, any_expired = check_approvals_dir(tmp_path, date(2026, 7, 4), warn_days=60)
        assert any_expired is True
        assert rows[0]["status"] == "ERROR"

    def test_present_sha256_proceeds_to_expiry_check(self, tmp_path):
        """YAML with valid 64-char lowercase hex sha256 and valid future expiry yields OK."""
        good = tmp_path / "good.yaml"
        good.write_text(
            f"source_id: good\n"
            f"expiry: 2030-12-31\n"
            f"signed_artifact_sha256: {_VALID_SHA256}\n",
            encoding="utf-8",
        )
        rows, any_expired = check_approvals_dir(tmp_path, date(2026, 7, 4), warn_days=60)
        assert any_expired is False
        assert rows[0]["status"] == "OK"

    def test_invalid_sha256_format_yields_error_row(self, tmp_path):
        """SHA256 that is not 64 lowercase hex chars yields ERROR."""
        bad_sha = tmp_path / "bad_sha.yaml"
        bad_sha.write_text(
            "source_id: bad_sha\n"
            "expiry: 2030-12-31\n"
            "signed_artifact_sha256: APPROVED\n",
            encoding="utf-8",
        )
        rows, any_expired = check_approvals_dir(tmp_path, date(2026, 7, 4), warn_days=60)
        assert any_expired is True
        assert rows[0]["status"] == "ERROR"

    def test_uppercase_hex_sha256_yields_error_row(self, tmp_path):
        """Uppercase hex SHA256 (not lowercase) yields ERROR (hashlib.hexdigest is always lowercase)."""
        upper_sha = tmp_path / "upper_sha.yaml"
        upper_sha.write_text(
            "source_id: upper_sha\n"
            "expiry: 2030-12-31\n"
            "signed_artifact_sha256: 'DEADBEEFDEADBEEFDEADBEEFDEADBEEFDEADBEEFDEADBEEFDEADBEEFDEADBEEF'\n",
            encoding="utf-8",
        )
        rows, any_expired = check_approvals_dir(tmp_path, date(2026, 7, 4), warn_days=60)
        assert any_expired is True
        assert rows[0]["status"] == "ERROR"
