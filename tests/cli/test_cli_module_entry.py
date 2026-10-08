"""`python -m legal_corpus_ingester.cli` must run the Typer app (terms-analysis#173).

Today cli.py has no `__main__` guard and the package has no `__main__.py`, so
`python -m legal_corpus_ingester.cli <anything>` imports the module, defines the
app, never calls it, and exits 0 having done nothing.

The subprocess tests prove `python -m`; the in-process main() tests below measure
its branches and pin the argv=None path (folded in from a separate file, #173).
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
import tomllib
import typer
from typer.testing import CliRunner

from legal_corpus_ingester.cli import EXIT_USAGE, app, main
from legal_corpus_ingester.pipeline.state import STAGES, CheckpointState, CheckpointStore
from legal_corpus_ingester.sources.registry import load_all

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
_FIXTURE_SOURCE = _REPO_ROOT / "tests" / "fixtures" / "sources" / "eurlex.yaml"


def _run_module(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run `python -m legal_corpus_ingester.cli` against THIS checkout's src/."""
    env = dict(os.environ)
    # Import this worktree's code even when the venv's editable install points elsewhere.
    env["PYTHONPATH"] = str(_SRC) + os.pathsep + env.get("PYTHONPATH", "")
    # Plain output so the comparison with CliRunner is not affected by rich/terminal width.
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    env["COLUMNS"] = "120"
    return subprocess.run(
        [sys.executable, "-m", "legal_corpus_ingester.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_python_dash_m_cli_without_subcommand_exits_nonzero(tmp_path: Path) -> None:
    result = _run_module(cwd=tmp_path)
    assert result.returncode == EXIT_USAGE, (
        f"expected usage exit {EXIT_USAGE}, got {result.returncode}; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # Usage goes to stderr so a caller capturing stdout cannot mistake it for output.
    assert "Usage:" in result.stderr


def test_python_dash_m_cli_runs_subcommand(tmp_path: Path) -> None:
    # One real source config plus its checkpoint, so `status` has real work to show.
    # Not an EMPTY sources/state pair: `status` exiting 0 on nothing is the silent-green
    # path tracked by ingester/TA #190, and this test must not bless it.
    sources = tmp_path / "sources"
    state = tmp_path / "state"
    sources.mkdir()
    (sources / _FIXTURE_SOURCE.name).write_text(_FIXTURE_SOURCE.read_text())
    (name,) = load_all(sources)  # source name from the config, not a literal
    CheckpointStore(state).save(CheckpointState(source_name=name, stage=STAGES[-1], corpus_version="v-test"))
    status_line = f"{name}: {STAGES[-1]}"
    args = ["status", "--sources-dir", str(sources), "--state-dir", str(state)]

    expected = CliRunner().invoke(app, args)
    assert status_line in expected.stdout, "control: in-process `status` did not show the checkpoint"

    result = _run_module(*args, cwd=tmp_path)
    assert result.returncode == expected.exit_code, result.stderr
    # Same observable output as the console-script entry point: proves the app ran.
    assert result.stdout == expected.stdout


# ---------------------------------------------------------------------------
# main() in-process
# ---------------------------------------------------------------------------


def test_cli_exit_usage_matches_typer_usage_error_code() -> None:
    # Checked against what Typer itself returns for a usage error, not a literal restated
    # here. Typer >= 0.27 bundles click privately and no longer installs it, so the code
    # comes from behaviour: a throwaway app (not ours) given an option it does not have.
    probe = typer.Typer()

    @probe.command()
    def first() -> None: ...

    @probe.command()
    def second() -> None: ...

    result = CliRunner().invoke(probe, ["--no-such-option"])
    assert "No such option" in result.output, "control: the probe did not hit a usage error"
    assert result.exit_code == EXIT_USAGE


def test_console_script_without_args_prints_usage_to_stderr_and_exits_usage(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Resolve the `ingester` console script exactly as pyproject.toml declares it, so the
    # installed entry point and `python -m` cannot drift apart again (terms-analysis#173).
    scripts = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    module_name, _, attr = scripts["ingester"].partition(":")
    entry = getattr(importlib.import_module(module_name), attr)
    monkeypatch.setattr(sys, "argv", ["ingester"])
    with pytest.raises(SystemExit) as info:
        entry()
    captured = capsys.readouterr()
    assert info.value.code == EXIT_USAGE
    assert captured.out == "", "console script wrote help to stdout"
    assert "Usage:" in captured.err


def test_main_without_args_prints_usage_to_stderr_and_exits_usage(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["ingester"])
    with pytest.raises(SystemExit) as info:
        main()
    captured = capsys.readouterr()
    assert info.value.code == EXIT_USAGE
    assert captured.out == ""
    assert "Usage:" in captured.err
    assert captured.err.rstrip().endswith("Error: Missing command.")


def test_main_with_args_runs_the_app(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # One real source config plus its checkpoint, so `status` has real work to show.
    # Not an EMPTY sources/state pair: `status` exiting 0 on nothing is the silent-green
    # path tracked by ingester/TA #190, and this test must not bless it.
    sources = tmp_path / "sources"
    state = tmp_path / "state"
    sources.mkdir()
    (sources / _FIXTURE_SOURCE.name).write_text(_FIXTURE_SOURCE.read_text())
    (name,) = load_all(sources)  # source name from the config, not a literal
    CheckpointStore(state).save(CheckpointState(source_name=name, stage=STAGES[-1], corpus_version="v-test"))
    status_line = f"{name}: {STAGES[-1]}"
    with pytest.raises(SystemExit) as info:
        main(["status", "--sources-dir", str(sources), "--state-dir", str(state)])
    captured = capsys.readouterr()
    assert info.value.code == 0
    # The checkpoint's stage is shown, which only happens when the command really ran.
    assert status_line in captured.out.splitlines()
    assert "Usage:" not in captured.out, "main() showed help instead of running the command"


def test_main_unknown_command_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["no-such-command"])
    assert info.value.code == EXIT_USAGE
    assert "no-such-command" in capsys.readouterr().err
