"""In-process tests for cli.main(), the `python -m legal_corpus_ingester.cli` entry (terms-analysis#173).

tests/cli/test_cli_module_entry.py proves the subprocess behaviour; these run
main() in-process so its branches are measured and the argv=None path is pinned.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from legal_corpus_ingester.cli import EXIT_USAGE, main


def test_main_without_args_prints_usage_to_stderr_and_exits_usage(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["ingester"])
    with pytest.raises(SystemExit) as info:
        main()
    captured = capsys.readouterr()
    assert info.value.code == EXIT_USAGE == 2
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
