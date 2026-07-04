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
