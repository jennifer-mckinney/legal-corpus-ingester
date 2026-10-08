"""`python -m legal_corpus_ingester.cli` must run the Typer app (terms-analysis#173).

Today cli.py has no `__main__` guard and the package has no `__main__.py`, so
`python -m legal_corpus_ingester.cli <anything>` imports the module, defines the
app, never calls it, and exits 0 having done nothing.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from legal_corpus_ingester.cli import app

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
