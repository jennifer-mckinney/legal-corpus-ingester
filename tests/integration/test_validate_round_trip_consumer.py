"""Integration tests exercising the terms-analysis consumer import path in validate-round-trip.

The terms-analysis LegalKnowledgeBase is not installed in the test environment. These tests
stub it via sys.modules so the retrieve() call path and result-truthiness check are exercised.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from legal_corpus_ingester import cli
from legal_corpus_ingester.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bundle(tmp_path: Path) -> Path:
    """Build a valid bundle directory with MANIFEST.yaml and required subdirs."""
    bundle = tmp_path / "2026.7.1"
    bundle.mkdir()
    for subdir in ("corpus", "index", "provenance"):
        (bundle / subdir).mkdir()
    # Manifest.load() expects MANIFEST.yaml with fields matching the Manifest dataclass:
    # corpus_version, chunker_version, embedder_model, embedder_revision, sources, chunk_count
    manifest = {
        "corpus_version": "2026.7.1",
        "chunker_version": "v1.0.0-vendored-from-terms-analysis@abc123",
        "embedder_model": "apertus-8b-instruct",
        "embedder_revision": "sha256:abc123def456",
        "sources": ["eurlex"],
        "chunk_count": 5,
    }
    (bundle / "MANIFEST.yaml").write_text(
        yaml.dump(manifest, default_flow_style=False, sort_keys=True),
        encoding="utf-8",
    )
    return bundle


def _stub_legal_kb(
    monkeypatch: pytest.MonkeyPatch,
    retrieve_return: object,
    load_raises: Exception | None = None,
) -> None:
    """Install a stub backend.app.services.legal_kb in sys.modules.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        retrieve_return: The value to return from retrieve("test query").
        load_raises: If not None, load_from_bundle() raises this exception instead.
    """
    stub_mod = types.ModuleType("backend")
    stub_app = types.ModuleType("backend.app")
    stub_services = types.ModuleType("backend.app.services")
    stub_legal_kb_mod = types.ModuleType("backend.app.services.legal_kb")

    _load_raises = load_raises  # capture for closure

    class _KB:
        def load_from_bundle(self, bundle_dir: Path) -> None:
            if _load_raises is not None:
                raise _load_raises

        def retrieve(self, query: str) -> object:
            return retrieve_return

    stub_legal_kb_mod.LegalKnowledgeBase = _KB  # type: ignore[attr-defined]
    for key, val in [
        ("backend", stub_mod),
        ("backend.app", stub_app),
        ("backend.app.services", stub_services),
        ("backend.app.services.legal_kb", stub_legal_kb_mod),
    ]:
        monkeypatch.setitem(sys.modules, key, val)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestValidateRoundTripConsumer:
    def test_valid_retrieve_exits_0(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """retrieve() returning real text objects exits 0 with VALID in output."""

        class _Chunk:
            text = "Article 5 — Principles relating to processing."

        bundle = _make_bundle(tmp_path)
        _stub_legal_kb(monkeypatch, retrieve_return=[_Chunk(), _Chunk()])

        result = runner.invoke(app, ["validate-round-trip", str(bundle)])
        assert result.exit_code == 0, result.output
        assert "VALID" in result.output
        assert "consumer check skipped" not in result.output
        assert "terms-analysis round-trip: OK" in result.output

    def test_retrieve_returns_none_list_exits_1(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """retrieve() returning [None, None] exits 1 (C1 fix — weak truthiness)."""
        bundle = _make_bundle(tmp_path)
        _stub_legal_kb(monkeypatch, retrieve_return=[None, None])

        result = runner.invoke(app, ["validate-round-trip", str(bundle)])
        assert result.exit_code == 1
        assert "empty result" in result.output

    def test_retrieve_returns_empty_list_exits_1(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """retrieve() returning [] exits 1."""
        bundle = _make_bundle(tmp_path)
        _stub_legal_kb(monkeypatch, retrieve_return=[])

        result = runner.invoke(app, ["validate-round-trip", str(bundle)])
        assert result.exit_code == 1
        assert "empty result" in result.output

    def test_retrieve_returns_blank_text_exits_1(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """retrieve() returning objects with blank .text exits 1."""

        class _BlankChunk:
            text = ""

        bundle = _make_bundle(tmp_path)
        _stub_legal_kb(monkeypatch, retrieve_return=[_BlankChunk(), _BlankChunk()])

        result = runner.invoke(app, ["validate-round-trip", str(bundle)])
        assert result.exit_code == 1
        assert "empty result" in result.output

    def test_load_failure_exits_1(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """load_from_bundle() raising RuntimeError exits 1 with terms-analysis error message."""
        bundle = _make_bundle(tmp_path)
        _stub_legal_kb(
            monkeypatch,
            retrieve_return=[],
            load_raises=RuntimeError("bundle index corrupted"),
        )

        result = runner.invoke(app, ["validate-round-trip", str(bundle)])
        assert result.exit_code == 1
        assert "terms-analysis error" in result.output

    @staticmethod
    def _make_consumer_missing(monkeypatch: pytest.MonkeyPatch) -> None:
        """Make the terms-analysis consumer import raise ModuleNotFoundError."""
        # Remove any stubs that may have been installed in prior tests
        for key in (
            "backend",
            "backend.app",
            "backend.app.services",
            "backend.app.services.legal_kb",
        ):
            monkeypatch.delitem(sys.modules, key, raising=False)

        # Patch importlib.import_module directly — importlib.import_module does not
        # go through builtins.__import__, so patching builtins.__import__ would have
        # no effect here.
        original_import_module = importlib.import_module

        def _raise_for_legal_kb(name: str, *args: object, **kwargs: object) -> object:
            if name == "backend.app.services.legal_kb":
                raise ModuleNotFoundError(f"No module named {name!r}")
            return original_import_module(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(importlib, "import_module", _raise_for_legal_kb)

    def test_module_not_found_exits_consumer_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing consumer is not a pass: exits EXIT_CONSUMER_SKIPPED, never prints VALID."""
        bundle = _make_bundle(tmp_path)
        self._make_consumer_missing(monkeypatch)

        result = runner.invoke(app, ["validate-round-trip", str(bundle)])
        assert result.exit_code == cli.EXIT_CONSUMER_SKIPPED, result.output
        assert result.exit_code not in (0, 1)
        assert "VALID" not in result.stdout
        assert "NOT checked" in result.stderr

    def test_module_not_found_with_opt_out_says_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--allow-missing-consumer exits 0 but labels the verdict as consumer-skipped."""
        bundle = _make_bundle(tmp_path)
        self._make_consumer_missing(monkeypatch)

        result = runner.invoke(
            app, ["validate-round-trip", str(bundle), "--allow-missing-consumer"]
        )
        assert result.exit_code == 0, result.output
        assert "not installed" in result.output
        assert "VALID (consumer check skipped)" in result.output
