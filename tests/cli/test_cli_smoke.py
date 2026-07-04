from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from legal_corpus_ingester.cli import app

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
    assert "dry-run" in result.output.lower() or "dry_run" in result.output.lower()


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
    assert "dry-run" in result.output or "force" in result.output


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
