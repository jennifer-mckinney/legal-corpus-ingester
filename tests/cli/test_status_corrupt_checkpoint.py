"""Tests for status command handling of corrupt or malformed checkpoints (#102)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from legal_corpus_ingester.cli import EXIT_CORRUPT_STATE, app
from legal_corpus_ingester.pipeline.state import (
    EXIT_CORRUPT_STATE as STATE_EXIT_CORRUPT_STATE,
    STAGES,
    CheckpointState,
    CheckpointStore,
)
from legal_corpus_ingester.sources.registry import load_all

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_SOURCE = _REPO_ROOT / "tests" / "fixtures" / "sources" / "eurlex.yaml"

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path, str]:
    """Create a workspace with one configured source."""
    sources = tmp_path / "sources"
    state = tmp_path / "state"
    sources.mkdir()
    state.mkdir()
    (sources / _FIXTURE_SOURCE.name).write_text(_FIXTURE_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    (name,) = load_all(sources)
    return sources, state, name


def test_exit_corrupt_state_constant() -> None:
    """EXIT_CORRUPT_STATE is exported and equals 4."""
    assert EXIT_CORRUPT_STATE == 4
    assert STATE_EXIT_CORRUPT_STATE == 4


def test_status_valid_checkpoint_succeeds(workspace: tuple[Path, Path, str]) -> None:
    """Control test: valid checkpoint outputs stage and exits 0."""
    sources, state, name = workspace
    store = CheckpointStore(state)
    store.save(CheckpointState(source_name=name, stage=STAGES[-1], corpus_version="v-test"))

    result = runner.invoke(app, ["status", "--sources-dir", str(sources), "--state-dir", str(state)])
    assert result.exit_code == 0
    assert f"{name}: {STAGES[-1]}" in result.stdout


def test_status_truncated_json_checkpoint(workspace: tuple[Path, Path, str]) -> None:
    """Truncated JSON checkpoint exits EXIT_CORRUPT_STATE with clean error and no traceback."""
    sources, state, name = workspace
    checkpoint_file = state / f"{name}.checkpoint.json"
    checkpoint_file.write_text('{"source_name": "' + name + '", "stage": "done"', encoding="utf-8")

    result = runner.invoke(app, ["status", "--sources-dir", str(sources), "--state-dir", str(state)])
    assert result.exit_code == EXIT_CORRUPT_STATE
    output = result.stdout + result.stderr
    assert "Checkpoint file is corrupt" in output
    assert f"{name}.checkpoint.json" in output
    assert "traceback" not in output.lower()


def test_status_invalid_json_checkpoint(workspace: tuple[Path, Path, str]) -> None:
    """Invalid JSON content exits EXIT_CORRUPT_STATE with clean error pointing to checkpoint path."""
    sources, state, name = workspace
    checkpoint_file = state / f"{name}.checkpoint.json"
    checkpoint_file.write_text("<<< NOT JSON CONTENT >>>", encoding="utf-8")

    result = runner.invoke(app, ["status", "--sources-dir", str(sources), "--state-dir", str(state)])
    assert result.exit_code == EXIT_CORRUPT_STATE
    output = result.stdout + result.stderr
    assert "Checkpoint file is corrupt" in output
    assert f"{name}.checkpoint.json" in output
    assert "traceback" not in output.lower()


def test_status_malformed_schema_checkpoint(workspace: tuple[Path, Path, str]) -> None:
    """Valid JSON with malformed checkpoint schema exits EXIT_CORRUPT_STATE cleanly."""
    sources, state, name = workspace
    checkpoint_file = state / f"{name}.checkpoint.json"
    checkpoint_file.write_text(json.dumps({"unknown_field": "bad_data"}), encoding="utf-8")

    result = runner.invoke(app, ["status", "--sources-dir", str(sources), "--state-dir", str(state)])
    assert result.exit_code == EXIT_CORRUPT_STATE
    output = result.stdout + result.stderr
    assert "Checkpoint file is corrupt" in output
    assert f"{name}.checkpoint.json" in output
    assert "traceback" not in output.lower()
