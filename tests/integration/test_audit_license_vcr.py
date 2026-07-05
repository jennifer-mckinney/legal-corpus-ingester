"""Integration test for audit-license using a hand-crafted httpx response.

This test exercises the full audit_license() code path without a live network call,
using the same response shape that the EUR-Lex legal notice page returns.
The cassette file (eurlex_legal_notice.yaml) is the authoritative source for the
response body — both tests and the cassette stay in sync automatically.

httpx.Response requires an attached httpx.Request before raise_for_status() can be
called. The _make_response() helper always attaches one so our mocks match what the
real httpx.get() returns.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from legal_corpus_ingester.cli import app

runner = CliRunner()

# Absolute path to the cassette file, resolved relative to this test file
_EURLEX_LEGAL_NOTICE_YAML = (
    Path(__file__).parent.parent
    / "fixtures"
    / "cassettes"
    / "eurlex"
    / "eurlex_legal_notice.yaml"
)

# License URL used by the eurlex source config (must match sources YAML below)
_LICENSE_URL = "https://eur-lex.europa.eu/content/legal-notice/legal-notice.html"


def _load_cassette_body() -> str:
    """Extract the response body string from the VCR cassette YAML."""
    import yaml  # noqa: PLC0415

    cassette = yaml.safe_load(_EURLEX_LEGAL_NOTICE_YAML.read_text(encoding="utf-8"))
    return cassette["interactions"][0]["response"]["body"]["string"]


def _make_response(status_code: int, content: bytes, content_type: str) -> httpx.Response:
    """Build an httpx.Response with an attached Request so raise_for_status() works.

    httpx.Response raises RuntimeError on raise_for_status() when no Request is set.
    Real httpx.get() always populates response.request; we replicate that here.
    """
    request = httpx.Request("GET", _LICENSE_URL)
    return httpx.Response(
        status_code,
        content=content,
        headers={"Content-Type": content_type},
        request=request,
    )


@pytest.fixture()
def eurlex_sources_dir(tmp_path: Path) -> Path:
    """Minimal sources dir with a eurlex entry pointing to the legal notice URL."""
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "eurlex.yaml").write_text(
        "name: eurlex\n"
        "jurisdiction: EU\n"
        "base_url: https://eur-lex.europa.eu/\n"
        "license:\n"
        "  spdx: CC-BY-4.0\n"
        f"  url: {_LICENSE_URL}\n"
        "refresh:\n"
        "  cadence: weekly\n"
        "pipeline:\n"
        "  fetcher: fetchers.eurlex.EurLexFetcher\n",
        encoding="utf-8",
    )
    return sources


class TestAuditLicenseVcr:
    def test_audit_license_new_source_exits_0(
        self, eurlex_sources_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """audit-license on a source with no baseline exits 0 (NEW status)."""
        cassette_body = _load_cassette_body()

        def _mock_get(url: str, **kwargs: object) -> httpx.Response:
            return _make_response(200, cassette_body.encode("utf-8"), "text/html; charset=UTF-8")

        monkeypatch.setattr(httpx, "get", _mock_get)  # type: ignore[attr-defined]

        state_file = tmp_path / "license-hashes.json"
        result = runner.invoke(
            app,
            [
                "audit-license",
                "eurlex",
                "--sources-dir",
                str(eurlex_sources_dir),
                "--state-file",
                str(state_file),
            ],
        )
        assert result.exit_code == 0, f"unexpected output: {result.output}"
        assert "NEW" in result.output

    def test_audit_license_unchanged_exits_0(
        self, eurlex_sources_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """audit-license with matching baseline exits 0 (NO_CHANGE status)."""
        from legal_corpus_ingester.provenance.license_audit import hash_content

        cassette_body = _load_cassette_body()

        def _mock_get(url: str, **kwargs: object) -> httpx.Response:
            return _make_response(200, cassette_body.encode("utf-8"), "text/html; charset=UTF-8")

        monkeypatch.setattr(httpx, "get", _mock_get)  # type: ignore[attr-defined]

        # Pre-populate baseline with the hash that matches the cassette body
        current_hash = hash_content(cassette_body)
        state_file = tmp_path / "license-hashes.json"
        state_file.write_text(
            json.dumps({"eurlex": {"hash": current_hash, "spdx": "CC-BY-4.0"}}),
            encoding="utf-8",
        )

        result = runner.invoke(
            app,
            [
                "audit-license",
                "eurlex",
                "--sources-dir",
                str(eurlex_sources_dir),
                "--state-file",
                str(state_file),
            ],
        )
        assert result.exit_code == 0, f"unexpected output: {result.output}"
        assert "NO_CHANGE" in result.output

    def test_audit_license_content_drift_exits_1(
        self, eurlex_sources_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """audit-license with different content exits 1 (DRIFT status)."""
        cassette_body = _load_cassette_body()

        def _mock_get(url: str, **kwargs: object) -> httpx.Response:
            return _make_response(200, cassette_body.encode("utf-8"), "text/html; charset=UTF-8")

        monkeypatch.setattr(httpx, "get", _mock_get)  # type: ignore[attr-defined]

        # Baseline with a DIFFERENT hash to simulate content drift
        state_file = tmp_path / "license-hashes.json"
        state_file.write_text(
            json.dumps({"eurlex": {"hash": "oldhashvalue", "spdx": "CC-BY-4.0"}}),
            encoding="utf-8",
        )

        result = runner.invoke(
            app,
            [
                "audit-license",
                "eurlex",
                "--sources-dir",
                str(eurlex_sources_dir),
                "--state-file",
                str(state_file),
            ],
        )
        assert result.exit_code == 1, f"unexpected output: {result.output}"
        assert "DRIFT" in result.output

    def test_audit_license_network_error_exits_1(
        self, eurlex_sources_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """audit-license on network error exits 1 with error message."""

        def _mock_get_fail(url: str, **kwargs: object) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        monkeypatch.setattr(httpx, "get", _mock_get_fail)  # type: ignore[attr-defined]

        state_file = tmp_path / "license-hashes.json"
        result = runner.invoke(
            app,
            [
                "audit-license",
                "eurlex",
                "--sources-dir",
                str(eurlex_sources_dir),
                "--state-file",
                str(state_file),
            ],
        )
        assert result.exit_code == 1, f"unexpected output: {result.output}"
        assert "Error" in result.output

    def test_audit_license_http_error_exits_1(
        self, eurlex_sources_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """audit-license on HTTP 404 exits 1 with error message."""

        def _mock_get_404(url: str, **kwargs: object) -> httpx.Response:
            # Build a 404 response with a Request attached so raise_for_status() works
            resp = _make_response(404, b"Not Found", "text/plain")
            resp.raise_for_status()
            return resp

        monkeypatch.setattr(httpx, "get", _mock_get_404)  # type: ignore[attr-defined]

        state_file = tmp_path / "license-hashes.json"
        result = runner.invoke(
            app,
            [
                "audit-license",
                "eurlex",
                "--sources-dir",
                str(eurlex_sources_dir),
                "--state-file",
                str(state_file),
            ],
        )
        assert result.exit_code == 1, f"unexpected output: {result.output}"
