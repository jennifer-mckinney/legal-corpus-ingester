from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from typer.testing import CliRunner

from legal_corpus_ingester.cli import app

runner = CliRunner()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_SOURCE_YAML = """\
name: test-src
jurisdiction: GDPR
base_url: https://example.com
license:
  spdx: CC-BY-4.0
  url: https://example.com/license
refresh:
  cadence: weekly
pipeline:
  fetcher: fetchers.eurlex.EurLexFetcher
"""

_LICENSE_TEXT = "This is the license text for testing."


def _make_mock_response(text: str = _LICENSE_TEXT) -> SimpleNamespace:
    """Return a minimal mock httpx.Response with raise_for_status as a no-op."""
    return SimpleNamespace(text=text, raise_for_status=lambda: None)


def _write_source_config(sources_dir: Path, content: str = _VALID_SOURCE_YAML) -> None:
    sources_dir.mkdir(parents=True, exist_ok=True)
    (sources_dir / "test-src.yaml").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_audit_license_help() -> None:
    """audit-license --help exits 0."""
    result = runner.invoke(app, ["audit-license", "--help"])
    assert result.exit_code == 0
    assert "audit-license" in result.output.lower() or "license" in result.output.lower()


def test_audit_license_unknown_source(tmp_path: Path, monkeypatch: Any) -> None:
    """Unknown source name exits 1."""
    monkeypatch.chdir(tmp_path)
    sources_dir = tmp_path / "config" / "sources"
    _write_source_config(sources_dir)

    result = runner.invoke(
        app,
        ["audit-license", "no-such-source", "--sources-dir", str(sources_dir)],
    )
    assert result.exit_code == 1
    assert "Unknown source" in result.output


def test_audit_license_no_sources_dir(tmp_path: Path) -> None:
    """Missing sources dir exits 1."""
    missing = tmp_path / "nonexistent"
    result = runner.invoke(
        app,
        ["audit-license", "test-src", "--sources-dir", str(missing)],
    )
    assert result.exit_code == 1


def test_audit_license_no_change(tmp_path: Path, monkeypatch: Any) -> None:
    """State file already contains matching hash -> status NO_CHANGE, exit 0."""
    monkeypatch.chdir(tmp_path)
    sources_dir = tmp_path / "config" / "sources"
    _write_source_config(sources_dir)

    # Pre-compute the hash that the CLI will produce
    from legal_corpus_ingester.provenance.license_audit import hash_content

    expected_hash = hash_content(_LICENSE_TEXT)

    # Write state file with matching hash + spdx
    state_file = tmp_path / "state" / "license-hashes.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        json.dumps({"test-src": {"hash": expected_hash, "spdx": "CC-BY-4.0"}}),
        encoding="utf-8",
    )

    mock_response = _make_mock_response()
    monkeypatch.setattr(
        "legal_corpus_ingester.cli.httpx.get",
        lambda url, **kw: mock_response,
    )

    result = runner.invoke(
        app,
        [
            "audit-license",
            "test-src",
            "--sources-dir",
            str(sources_dir),
            "--state-file",
            str(state_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "NO_CHANGE" in result.output


def test_audit_license_new_baseline(tmp_path: Path, monkeypatch: Any) -> None:
    """State file absent -> status NEW, exit 0."""
    monkeypatch.chdir(tmp_path)
    sources_dir = tmp_path / "config" / "sources"
    _write_source_config(sources_dir)

    state_file = tmp_path / "state" / "license-hashes.json"
    # Do NOT create the state file — it should not exist

    mock_response = _make_mock_response()
    monkeypatch.setattr(
        "legal_corpus_ingester.cli.httpx.get",
        lambda url, **kw: mock_response,
    )

    result = runner.invoke(
        app,
        [
            "audit-license",
            "test-src",
            "--sources-dir",
            str(sources_dir),
            "--state-file",
            str(state_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "NEW" in result.output


def test_audit_license_spdx_drift(tmp_path: Path, monkeypatch: Any) -> None:
    """State file has different SPDX -> status SPDX_DRIFT, exit 1."""
    monkeypatch.chdir(tmp_path)
    sources_dir = tmp_path / "config" / "sources"
    _write_source_config(sources_dir)

    from legal_corpus_ingester.provenance.license_audit import hash_content

    expected_hash = hash_content(_LICENSE_TEXT)

    state_file = tmp_path / "state" / "license-hashes.json"
    state_file.parent.mkdir(parents=True)
    # Store with a DIFFERENT spdx than the source config (CC-BY-4.0) to trigger SPDX_DRIFT
    state_file.write_text(
        json.dumps({"test-src": {"hash": expected_hash, "spdx": "MIT"}}),
        encoding="utf-8",
    )

    mock_response = _make_mock_response()
    monkeypatch.setattr(
        "legal_corpus_ingester.cli.httpx.get",
        lambda url, **kw: mock_response,
    )

    result = runner.invoke(
        app,
        [
            "audit-license",
            "test-src",
            "--sources-dir",
            str(sources_dir),
            "--state-file",
            str(state_file),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "SPDX_DRIFT" in result.output


def test_audit_license_update_baseline(tmp_path: Path, monkeypatch: Any) -> None:
    """--update-baseline with NEW status writes to state file."""
    monkeypatch.chdir(tmp_path)
    sources_dir = tmp_path / "config" / "sources"
    _write_source_config(sources_dir)

    state_file = tmp_path / "state" / "license-hashes.json"
    # No existing state file -> NEW status

    mock_response = _make_mock_response()
    monkeypatch.setattr(
        "legal_corpus_ingester.cli.httpx.get",
        lambda url, **kw: mock_response,
    )

    result = runner.invoke(
        app,
        [
            "audit-license",
            "test-src",
            "--sources-dir",
            str(sources_dir),
            "--state-file",
            str(state_file),
            "--update-baseline",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "NEW" in result.output
    assert "baseline updated" in result.output

    # Confirm state file was written with correct content
    assert state_file.exists(), "State file should have been created by --update-baseline"
    baseline = json.loads(state_file.read_text(encoding="utf-8"))
    assert "test-src" in baseline
    assert baseline["test-src"]["spdx"] == "CC-BY-4.0"

    from legal_corpus_ingester.provenance.license_audit import hash_content

    assert baseline["test-src"]["hash"] == hash_content(_LICENSE_TEXT)
