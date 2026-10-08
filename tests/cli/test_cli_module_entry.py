"""`python -m legal_corpus_ingester.cli` must run the Typer app (terms-analysis#173).

Today cli.py has no `__main__` guard and the package has no `__main__.py`, so
`python -m legal_corpus_ingester.cli <anything>` imports the module, defines the
app, never calls it, and exits 0 having done nothing.

The subprocess tests prove `python -m`; the in-process main() tests below measure
its branches and pin the argv=None path (folded in from a separate file, #173).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from legal_corpus_ingester.cli import EXIT_USAGE, app, main

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
# Usage errors use click's conventional exit code 2 (the same code the
# `ingester` console script returns with no arguments).
_EXIT_USAGE = 2


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
    assert result.returncode == _EXIT_USAGE, (
        f"expected usage exit {_EXIT_USAGE}, got {result.returncode}; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # Usage goes to stderr so a caller capturing stdout cannot mistake it for output.
    assert "Usage:" in result.stderr


def test_python_dash_m_cli_runs_subcommand(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    state = tmp_path / "state"
    sources.mkdir()
    state.mkdir()
    args = ["status", "--sources-dir", str(sources), "--state-dir", str(state)]

    expected = CliRunner().invoke(app, args)
    assert expected.stdout.strip(), "control: in-process `status` printed nothing"

    result = _run_module(*args, cwd=tmp_path)
    assert result.returncode == expected.exit_code, result.stderr
    # Same observable output as the console-script entry point: proves the app ran.
    assert result.stdout == expected.stdout


# ---------------------------------------------------------------------------
# main() in-process
# ---------------------------------------------------------------------------


def test_cli_exit_usage_matches_click_convention() -> None:
    assert EXIT_USAGE == _EXIT_USAGE


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
    sources = tmp_path / "sources"
    state = tmp_path / "state"
    sources.mkdir()
    state.mkdir()
    with pytest.raises(SystemExit) as info:
        main(["status", "--sources-dir", str(sources), "--state-dir", str(state)])
    captured = capsys.readouterr()
    assert info.value.code == 0
    assert captured.out.strip()
    assert "Usage:" not in captured.out, "main() showed help instead of running the command"


def test_main_unknown_command_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["no-such-command"])
    assert info.value.code == EXIT_USAGE
    assert "no-such-command" in capsys.readouterr().err
