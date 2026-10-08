"""health_check.py must not report success when the registry is empty (terms-analysis#173).

Kept in its own file, apart from tests/unit/test_health_check.py, because
feat/g0-1-unwired-exit-nonzero (terms-analysis#90) edits that file for the
MISSING config dir case. This file covers an EXISTING dir that yields zero
usable source configs, plus the QUALITY-BAR attack list A for that input.

Contract pinned here: exit 2 (config problem), message on stderr.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

# scripts/ is not a package; inject it into the path.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

from health_check import main

_EXIT_CONFIG = 2
_FIXTURE_SOURCE = Path(__file__).resolve().parents[1] / "fixtures" / "sources" / "eurlex.yaml"
# A directory name that tries to forge terminal output: clear-screen escape,
# a newline followed by a fake status line, and a bidi override.
_HOSTILE_NAME = "hostile-cfg\x1b[2J\nFORGED: all sources fresh\u202e"


def _run(config_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    rc = main([
        "--config-dir", str(config_dir),
        "--state-dir", str(tmp_path / "state"),
        "--out-dir", str(tmp_path / "out"),
    ])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


@contextmanager
def _unreadable(path: Path) -> Iterator[None]:
    """Make `path` unreadable; fail (not skip) under CI if the OS ignores it (QUALITY-BAR D)."""
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):
            msg = "cannot make a directory unreadable here (running as root?)"
            if os.environ.get("CI"):
                pytest.fail(msg)
            pytest.skip(msg)
        yield
    finally:
        path.chmod(stat.S_IRWXU)


# ---------------------------------------------------------------------------
# Card spec
# ---------------------------------------------------------------------------


def test_empty_config_dir_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    rc, _out, err = _run(config_dir, tmp_path, capsys)
    assert rc == _EXIT_CONFIG
    assert "No sources configured" in err


# ---------------------------------------------------------------------------
# Attack list A: dirs that exist but hold zero source configs
# ---------------------------------------------------------------------------


def _only_non_yaml(d: Path) -> None:
    (d / "README.md").write_text("# sources\n")
    (d / "notes.txt").write_text("eurlex\n")
    (d / "eurlex.json").write_text("{}\n")


def _only_hidden(d: Path) -> None:
    (d / ".gitkeep").write_text("")
    (d / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")


def _yaml_named_directory(d: Path) -> None:
    (d / "eurlex.yaml").mkdir()


@pytest.mark.parametrize(
    "populate",
    [_only_non_yaml, _only_hidden, _yaml_named_directory],
    ids=["only-non-yaml", "only-hidden-files", "yaml-named-directory"],
)
def test_dir_with_no_source_files_exits_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], populate: object
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    populate(config_dir)  # type: ignore[operator]
    rc, _out, err = _run(config_dir, tmp_path, capsys)
    assert rc == _EXIT_CONFIG
    assert "No sources configured" in err


def test_symlink_to_empty_dir_exits_config_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    real = tmp_path / "real-empty"
    real.mkdir()
    link = tmp_path / "config"
    link.symlink_to(real, target_is_directory=True)
    rc, _out, err = _run(link, tmp_path, capsys)
    assert rc == _EXIT_CONFIG
    assert "No sources configured" in err


@pytest.mark.parametrize(
    "content",
    ["", "# only a comment\n# and another\n", "---\n", "~\n"],
    ids=["empty-file", "comments-only", "bare-document-marker", "yaml-null"],
)
def test_yaml_that_parses_to_nothing_exits_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "ghost.yaml").write_text(content)
    rc, _out, err = _run(config_dir, tmp_path, capsys)
    assert rc == _EXIT_CONFIG
    # The message names the offending file so the operator can fix it.
    assert "ghost.yaml" in err


def test_unreadable_config_dir_exits_config_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_dir = tmp_path / "locked-config"
    config_dir.mkdir()
    (config_dir / "eurlex.yaml").write_text(_FIXTURE_SOURCE.read_text())
    with _unreadable(config_dir):
        rc, _out, err = _run(config_dir, tmp_path, capsys)
    assert rc == _EXIT_CONFIG
    assert "locked-config" in err
    # Honest reason: the dir holds a config, so "no sources" would be a false diagnosis.
    assert "No sources configured" not in err


def test_hostile_config_dir_name_is_sanitised_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_dir = tmp_path / _HOSTILE_NAME
    config_dir.mkdir()
    rc, out, err = _run(config_dir, tmp_path, capsys)
    assert rc == _EXIT_CONFIG
    # The dir is still identified...
    assert "hostile-cfg" in err
    # ...but its control, escape and bidi bytes never reach the terminal or log.
    for text in (out, err):
        assert "\x1b" not in text
        assert "\u202e" not in text
    assert not any(line.startswith("FORGED") for line in err.splitlines())


# ---------------------------------------------------------------------------
# Control: a real, fresh source still exits 0 (the fix must not fail everything)
# ---------------------------------------------------------------------------


def test_fresh_valid_source_still_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "eurlex.yaml").write_text(_FIXTURE_SOURCE.read_text())
    state = tmp_path / "state"
    state.mkdir()
    (state / "eurlex.checkpoint.json").write_text(json.dumps({"stage": "published"}))
    rc, out, _err = _run(config_dir, tmp_path, capsys)
    assert rc == 0
    assert "| eurlex |" in out
