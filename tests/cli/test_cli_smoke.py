from __future__ import annotations

import sys
from pathlib import Path

import typer.main
from typer.testing import CliRunner

from legal_corpus_ingester.cli import app
from tests.cli.helpers import strip_ansi

runner = CliRunner()


def test_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0


def test_init_creates_dirs(tmp_path: object, monkeypatch: object) -> None:  # type: ignore[type-arg]
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert (tmp_path / "config").is_dir()  # type: ignore[operator]
    assert (tmp_path / "out").is_dir()  # type: ignore[operator]
    assert (tmp_path / "state").is_dir()  # type: ignore[operator]


def test_sources_list_no_sources(tmp_path: object, monkeypatch: object) -> None:  # type: ignore[type-arg]
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    (tmp_path / "config" / "sources").mkdir(parents=True)  # type: ignore[operator]
    result = runner.invoke(app, ["sources", "list"])
    assert result.exit_code == 0
    assert "No sources" in result.output


def test_sources_list_help() -> None:
    result = runner.invoke(app, ["sources", "list", "--help"])
    assert result.exit_code == 0


def test_status_no_checkpoints(tmp_path: object, monkeypatch: object) -> None:  # type: ignore[type-arg]
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "No runs" in result.output


def test_fetch_dry_run_help() -> None:
    result = runner.invoke(app, ["fetch", "--help"])
    assert result.exit_code == 0
    # Assert on the registered parameters, not the help text: the fetch
    # docstring mentions --dry-run, so a text check passes without the option.
    commands = getattr(typer.main.get_command(app), "commands", {})
    assert "fetch" in commands, f"no fetch command: {sorted(commands)}"
    fetch = commands["fetch"]
    options = {opt for param in fetch.params for opt in getattr(param, "opts", [])}
    assert "--dry-run" in options, f"fetch has no --dry-run option: {sorted(options)}"


# ---------------------------------------------------------------------------
# validate-round-trip tests
# ---------------------------------------------------------------------------

_VALID_MANIFEST_YAML = """\
chunk_count: 10
chunker_version: v1.0.0-vendored-from-terms-analysis@abc123
corpus_version: '2026.07.0'
embedder_model: apertus-8b-instruct
embedder_revision: sha256:abc123def456
sources:
- eurlex
"""


def test_validate_round_trip_help() -> None:
    """validate-round-trip --help exits 0."""
    result = runner.invoke(app, ["validate-round-trip", "--help"])
    assert result.exit_code == 0


def test_validate_round_trip_missing_dir(tmp_path: Path) -> None:
    """Non-existent path exits 1."""
    missing = str(tmp_path) + "/no-such-bundle"
    result = runner.invoke(app, ["validate-round-trip", missing])
    assert result.exit_code == 1


def test_validate_round_trip_no_manifest(tmp_path: Path) -> None:
    """Existing dir without MANIFEST.yaml exits 1 and output contains X-Corpus-Mismatch."""
    bundle = tmp_path / "bundle-v1"
    bundle.mkdir()
    result = runner.invoke(app, ["validate-round-trip", str(bundle)])
    assert result.exit_code == 1
    assert "X-Corpus-Mismatch" in result.output


def test_validate_round_trip_valid_bundle(tmp_path: Path) -> None:
    """Valid bundle with MANIFEST.yaml + required subdirs exits 0."""
    bundle = tmp_path / "2026.07.0"
    bundle.mkdir()

    # Write a valid MANIFEST.yaml
    (bundle / "MANIFEST.yaml").write_text(_VALID_MANIFEST_YAML, encoding="utf-8")

    # Create required subdirectories
    for subdir in ("corpus", "index", "provenance"):
        (bundle / subdir).mkdir()

    result = runner.invoke(app, ["validate-round-trip", str(bundle)])
    # terms-analysis is not installed in the test env; it should skip gracefully
    assert result.exit_code == 0, result.output
    assert "VALID" in result.output


# ---------------------------------------------------------------------------
# prune tests
# ---------------------------------------------------------------------------


def _make_bundle(base: Path, name: str) -> Path:
    """Create a minimal bundle directory under base."""
    d = base / name
    d.mkdir(parents=True)
    return d


def test_prune_help() -> None:
    """prune --help exits 0 and mentions expected flags."""
    result = runner.invoke(app, ["prune", "--help"])
    assert result.exit_code == 0
    out = strip_ansi(result.output)
    assert "--dry-run" in out
    assert "--force" in out


def test_prune_no_flags_prints_guidance(tmp_path: Path, monkeypatch: object) -> None:
    """prune with no action flag prints guidance without deleting anything."""
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    out = tmp_path / "out"
    out.mkdir()
    result = runner.invoke(app, ["prune", "--out-dir", str(out)])
    assert result.exit_code == 0
    # With nothing to prune, output should reference "prune" or "bundle"
    assert "prune" in result.output.lower() or "bundle" in result.output.lower()


def test_prune_dry_run(tmp_path: Path, monkeypatch: object) -> None:
    """prune --dry-run lists bundles that would be pruned without deleting them."""
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    out = tmp_path / "out"
    out.mkdir()
    # Create 15 monthly bundles (2024.01 through 2025.03) so bundles outside the
    # 12-month window and not a quarterly anchor qualify for pruning.
    for year, month in [
        (2024, 1), (2024, 2), (2024, 3), (2024, 4),
        (2024, 5), (2024, 6), (2024, 7), (2024, 8),
        (2024, 9), (2024, 10), (2024, 11), (2024, 12),
        (2025, 1), (2025, 2), (2025, 3),
    ]:
        _make_bundle(out, f"{year}.{month:02d}.0")
    result = runner.invoke(app, ["prune", "--out-dir", str(out), "--dry-run"])
    assert result.exit_code == 0
    assert "would prune" in result.output


def test_prune_force_deletes(tmp_path: Path, monkeypatch: object) -> None:
    """prune --force actually removes prunable bundles and keeps the required ones."""
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    out = tmp_path / "out"
    out.mkdir()
    # Same 15-month dataset — bundles outside the retention window will be deleted.
    for year, month in [
        (2024, 1), (2024, 2), (2024, 3), (2024, 4),
        (2024, 5), (2024, 6), (2024, 7), (2024, 8),
        (2024, 9), (2024, 10), (2024, 11), (2024, 12),
        (2025, 1), (2025, 2), (2025, 3),
    ]:
        _make_bundle(out, f"{year}.{month:02d}.0")
    result = runner.invoke(app, ["prune", "--out-dir", str(out), "--force"])
    assert result.exit_code == 0
    assert "pruned" in result.output
    # At least 4 bundles must remain (the 4-most-recent recency rule)
    remaining = [d for d in out.iterdir() if d.is_dir()]
    assert len(remaining) >= 4


# ---------------------------------------------------------------------------
# validate-round-trip — C1: retrieve returns [None, None] exits 1 (Issue #6)
# ---------------------------------------------------------------------------


def test_cli_validate_round_trip_retrieve_returns_none_list_exits_1(tmp_path: Path, monkeypatch: object) -> None:
    """retrieve() returning [None, None] should exit 1 (garbage result)."""
    import types

    # Build a minimal valid bundle directory
    bundle = tmp_path / "2026.07.001"
    bundle.mkdir()
    (bundle / "corpus").mkdir()
    (bundle / "index").mkdir()
    (bundle / "provenance").mkdir()

    # MANIFEST.yaml uses corpus_version (not bundle_version) and requires
    # corpus_version, chunker_version, embedder_model, embedder_revision,
    # sources, chunk_count — matching the Manifest dataclass exactly.
    manifest_yaml = (
        "corpus_version: '2026.07.001'\n"
        "embedder_model: apertus-8b\n"
        "embedder_revision: abc123\n"
        "chunker_version: 1.0.0\n"
        "chunk_count: 0\n"
        "sources: []\n"
    )
    (bundle / "MANIFEST.yaml").write_text(manifest_yaml, encoding="utf-8")

    # Stub a legal_kb module whose retrieve() returns [None, None]
    stub_mod = types.ModuleType("backend")
    stub_app = types.ModuleType("backend.app")
    stub_services = types.ModuleType("backend.app.services")
    stub_legal_kb = types.ModuleType("backend.app.services.legal_kb")

    class _KB:
        def load_from_bundle(self, bundle_dir: Path) -> None:
            pass

        def retrieve(self, query: str) -> list[object]:
            return [None, None]

    stub_legal_kb.LegalKnowledgeBase = _KB  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "backend", stub_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "backend.app", stub_app)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "backend.app.services", stub_services)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "backend.app.services.legal_kb", stub_legal_kb)  # type: ignore[attr-defined]

    result = runner.invoke(app, ["validate-round-trip", str(bundle)])
    assert result.exit_code == 1
    assert "empty result" in result.output


# ---------------------------------------------------------------------------
# audit-license --offline (Issue #7 / Fix C2)
# ---------------------------------------------------------------------------

# Minimal valid source config matching the current SourceConfig schema:
# - name must be lowercase/digits/hyphens/underscores only (no "EUR-Lex")
# - refresh and pipeline are required fields
_EURLEX_YAML = (
    "name: eurlex\n"
    "jurisdiction: EU\n"
    "base_url: https://eur-lex.europa.eu/\n"
    "license:\n"
    "  spdx: CC-BY-4.0\n"
    "  url: https://eur-lex.europa.eu/content/legal-notice/legal-notice.html\n"
    "refresh:\n"
    "  cadence: weekly\n"
    "pipeline:\n"
    "  fetcher: fetchers.eurlex.EurLexFetcher\n"
)


def test_cli_audit_license_offline_reads_baseline(tmp_path: Path) -> None:
    """--offline flag should read stored baseline and skip live fetch."""
    import json

    # Set up minimal sources dir with a source that has a license URL
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir()
    (sources_dir / "eurlex.yaml").write_text(_EURLEX_YAML, encoding="utf-8")

    # Pre-populate a state file
    state_file = tmp_path / "license-hashes.json"
    state_file.write_text(
        json.dumps({
            "eurlex": {
                "hash": "deadbeef12345678",
                "spdx": "CC-BY-4.0",
                "recorded_at": "2026-07-04T00:00:00Z",
            }
        }),
        encoding="utf-8",
    )

    result = runner.invoke(app, [
        "audit-license", "eurlex",
        "--sources-dir", str(sources_dir),
        "--state-file", str(state_file),
        "--offline",
    ])
    assert result.exit_code == 0
    assert "BASELINE_ONLY" in result.output
    assert "deadbeef1234" in result.output


def test_cli_audit_license_offline_no_baseline(tmp_path: Path) -> None:
    """--offline with no stored baseline should exit 0 with NO_BASELINE message."""
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir()
    (sources_dir / "eurlex.yaml").write_text(_EURLEX_YAML, encoding="utf-8")

    state_file = tmp_path / "empty-hashes.json"
    state_file.write_text("{}", encoding="utf-8")

    result = runner.invoke(app, [
        "audit-license", "eurlex",
        "--sources-dir", str(sources_dir),
        "--state-file", str(state_file),
        "--offline",
    ])
    assert result.exit_code == 0
    assert "NO_BASELINE" in result.output
