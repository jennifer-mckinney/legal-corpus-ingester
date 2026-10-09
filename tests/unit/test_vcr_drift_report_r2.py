"""Round-2 Critic LOW fixes for the VCR drift canary (terms-analysis#92)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import vcr_drift_report as vdr  # noqa: E402


def _cassette(body: Any) -> dict[str, Any]:
    return {
        "interactions": [
            {
                "request": {"uri": "http://x.com", "method": "GET"},
                "response": {"status": {"code": 200}, "body": body},
            }
        ]
    }


def test_trailer_uses_real_run_id(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.delenv("RUN_URL", raising=False)
    out = vdr._truncation_trailer("cut")
    assert "vcr-drift-report-12345" in out
    assert "<run_id>" not in out


def test_trailer_omits_artifact_name_without_run_id(monkeypatch):
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monkeypatch.delenv("RUN_URL", raising=False)
    out = vdr._truncation_trailer("cut")
    assert "<run_id>" not in out
    assert "vcr-drift-report-" not in out
    assert "cut" in out


def test_bare_string_body_baseline_only():
    s = vdr.diff_cassette(_cassette("old"), _cassette({"string": "new"}))
    assert s["body_drift"] == 1


def test_bare_string_body_current_only():
    s = vdr.diff_cassette(_cassette({"string": "old"}), _cassette("new"))
    assert s["body_drift"] == 1


def test_bare_string_body_both_sides_equal_and_different():
    assert vdr.diff_cassette(_cassette("same"), _cassette("same"))["body_drift"] == 0
    assert vdr.diff_cassette(_cassette("a"), _cassette("b"))["body_drift"] == 1


def test_bare_string_equals_mapping_string_form():
    s = vdr.diff_cassette(_cassette("same"), _cassette({"string": "same"}))
    assert s["body_drift"] == 0


def test_non_mapping_non_string_body_does_not_crash():
    s = vdr.diff_cassette(_cassette(None), _cassette(["x"]))
    assert isinstance(s["body_drift"], int)
