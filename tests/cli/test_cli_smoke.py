from __future__ import annotations

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


def test_validate_round_trip_missing_dir(tmp_path: object) -> None:
    """Non-existent path exits 1."""
    missing = str(tmp_path) + "/no-such-bundle"  # type: ignore[operator]
    result = runner.invoke(app, ["validate-round-trip", missing])
    assert result.exit_code == 1


def test_validate_round_trip_no_manifest(tmp_path: object) -> None:
    """Existing dir without MANIFEST.yaml exits 1 and output contains X-Corpus-Mismatch."""
    bundle = tmp_path / "bundle-v1"  # type: ignore[operator]
    bundle.mkdir()  # type: ignore[union-attr]
    result = runner.invoke(app, ["validate-round-trip", str(bundle)])
    assert result.exit_code == 1
    assert "X-Corpus-Mismatch" in result.output


def test_validate_round_trip_valid_bundle(tmp_path: object) -> None:
    """Valid bundle with MANIFEST.yaml + required subdirs exits 0."""
    bundle = tmp_path / "2026.07.0"  # type: ignore[operator]
    bundle.mkdir()  # type: ignore[union-attr]

    # Write a valid MANIFEST.yaml
    (bundle / "MANIFEST.yaml").write_text(_VALID_MANIFEST_YAML, encoding="utf-8")  # type: ignore[operator]

    # Create required subdirectories
    for subdir in ("corpus", "index", "provenance"):
        (bundle / subdir).mkdir()  # type: ignore[operator]

    result = runner.invoke(app, ["validate-round-trip", str(bundle)])
    # terms-analysis is not installed in the test env; it should skip gracefully
    assert result.exit_code == 0, result.output
    assert "VALID" in result.output
