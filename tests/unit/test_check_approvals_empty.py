"""check_approvals.py must not report success when it had nothing to check (terms-analysis#173).

Today a missing or empty approvals dir prints "No approvals to check." and
exits 0, so the daily approval-expiry job passes silently. Card decision:
zero approvals is a config error (exit 2, message on stderr naming the dir)
whenever source configs exist, AND when there are zero configs and zero
approvals. A new `--sources-dir` option (default `config/sources`) tells the
script where the source configs live.

Kept in a separate file from tests/unit/test_check_approvals.py so it merges
cleanly with feat/g0-1-unwired-exit-nonzero (terms-analysis#90).
"""
from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

# scripts/ is not a package; inject it into the path.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

from check_approvals import main

_EXIT_CONFIG = 2
_EXIT_EXPIRED_OR_INVALID = 1
_FIXTURE_SOURCE = Path(__file__).resolve().parents[1] / "fixtures" / "sources" / "eurlex.yaml"
_VALID_APPROVAL = (
    'source_id: "eurlex"\n'
    f'signed_artifact_sha256: "{"a" * 64}"\n'
    'expiry: "2099-01-01"\n'
)
# A directory name that tries to forge log output: clear-screen escape,
# a newline followed by a fake status line, and a bidi override.
_HOSTILE_NAME = "hostile-appr\x1b[2J\nFORGED: all approvals OK\u202e"


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    """Call main(); an argparse rejection is reported as such, not mistaken for exit 2."""
    try:
        rc = main(argv)
    except SystemExit as exc:
        err = capsys.readouterr().err
        pytest.fail(f"main() raised SystemExit({exc.code}) instead of returning; stderr={err!r}")
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _sources(d: Path, *, configs: int = 1) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    if configs:
        (d / "eurlex.yaml").write_text(_FIXTURE_SOURCE.read_text())
    return d


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


def test_empty_approvals_dir_with_source_configs_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sources = _sources(tmp_path / "srcs")
    approvals = tmp_path / "empty-approvals"
    approvals.mkdir()
    rc, _out, err = _run(["--sources-dir", str(sources), "--approvals-dir", str(approvals)], capsys)
    assert rc == _EXIT_CONFIG
    assert "empty-approvals" in err


def test_missing_approvals_dir_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Default --sources-dir (config/sources) holds one config; the approvals dir is absent.
    monkeypatch.chdir(tmp_path)
    _sources(tmp_path / "config" / "sources")
    rc, _out, err = _run(["--approvals-dir", "no-such-approvals"], capsys)
    assert rc == _EXIT_CONFIG
    assert "no-such-approvals" in err


def test_empty_registry_and_no_approvals_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Zero source configs and zero approvals, all defaults, exactly as the scheduled job runs.
    monkeypatch.chdir(tmp_path)
    _sources(tmp_path / "config" / "sources", configs=0)
    (tmp_path / "config" / "approvals").mkdir()
    rc, _out, err = _run([], capsys)
    assert rc == _EXIT_CONFIG
    assert "config/approvals" in err


# ---------------------------------------------------------------------------
# Attack list A: approvals dirs that exist but hold zero approval files
# ---------------------------------------------------------------------------


def _only_non_yaml(d: Path) -> None:
    (d / "README.md").write_text("# approvals\n")
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
def test_approvals_dir_with_no_approval_files_exits_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, populate: object
) -> None:
    monkeypatch.chdir(tmp_path)
    _sources(tmp_path / "config" / "sources")
    approvals = tmp_path / "hollow-approvals"
    approvals.mkdir()
    populate(approvals)  # type: ignore[operator]
    rc, _out, err = _run(["--approvals-dir", str(approvals)], capsys)
    assert rc == _EXIT_CONFIG
    assert "hollow-approvals" in err


def test_symlink_to_empty_approvals_dir_exits_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _sources(tmp_path / "config" / "sources")
    real = tmp_path / "real-empty"
    real.mkdir()
    link = tmp_path / "linked-approvals"
    link.symlink_to(real, target_is_directory=True)
    rc, _out, err = _run(["--approvals-dir", str(link)], capsys)
    assert rc == _EXIT_CONFIG
    assert "linked-approvals" in err


def test_unreadable_approvals_dir_exits_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _sources(tmp_path / "config" / "sources")
    approvals = tmp_path / "locked-approvals"
    approvals.mkdir()
    (approvals / "eurlex.yaml").write_text(_VALID_APPROVAL)
    with _unreadable(approvals):
        rc, _out, err = _run(["--approvals-dir", str(approvals)], capsys)
    assert rc == _EXIT_CONFIG
    assert "locked-approvals" in err


@pytest.mark.parametrize(
    "content",
    ["", "# only a comment\n", "---\n"],
    ids=["empty-file", "comments-only", "bare-document-marker"],
)
def test_approval_yaml_that_parses_to_nothing_is_reported_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    # Already fail-closed on main (ERROR row, exit 1); pinned so the empty-dir fix keeps it.
    monkeypatch.chdir(tmp_path)
    _sources(tmp_path / "config" / "sources")
    approvals = tmp_path / "approvals"
    approvals.mkdir()
    (approvals / "ghost.yaml").write_text(content)
    rc, out, _err = _run(["--approvals-dir", str(approvals)], capsys)
    assert rc == _EXIT_EXPIRED_OR_INVALID
    assert "| ghost | ERROR |" in out


# ---------------------------------------------------------------------------
# Attack list A: the new --sources-dir input
# ---------------------------------------------------------------------------


def test_missing_sources_dir_exits_config_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Approvals are valid, so the only problem is the sources dir that does not exist.
    approvals = tmp_path / "approvals"
    approvals.mkdir()
    (approvals / "eurlex.yaml").write_text(_VALID_APPROVAL)
    missing = tmp_path / "no-such-sources"
    rc, _out, err = _run(["--sources-dir", str(missing), "--approvals-dir", str(approvals)], capsys)
    assert rc == _EXIT_CONFIG
    assert "no-such-sources" in err


def test_unreadable_sources_dir_exits_config_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    approvals = tmp_path / "approvals"
    approvals.mkdir()
    (approvals / "eurlex.yaml").write_text(_VALID_APPROVAL)
    sources = _sources(tmp_path / "locked-sources")
    with _unreadable(sources):
        rc, _out, err = _run(["--sources-dir", str(sources), "--approvals-dir", str(approvals)], capsys)
    assert rc == _EXIT_CONFIG
    assert "locked-sources" in err


# ---------------------------------------------------------------------------
# Attack list A: output safety for an untrusted dir name
# ---------------------------------------------------------------------------


def test_hostile_approvals_dir_name_is_sanitised_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _sources(tmp_path / "config" / "sources")
    approvals = tmp_path / _HOSTILE_NAME  # missing on purpose: the message must name it
    rc, out, err = _run(["--approvals-dir", str(approvals)], capsys)
    assert rc == _EXIT_CONFIG
    assert "hostile-appr" in err
    for text in (out, err):
        assert "\x1b" not in text
        assert "\u202e" not in text
    assert not any(line.startswith("FORGED") for line in err.splitlines())


# ---------------------------------------------------------------------------
# Controls: real work still exits 0 (the fix must not fail everything)
# ---------------------------------------------------------------------------


def test_valid_approval_with_explicit_sources_dir_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sources = _sources(tmp_path / "srcs")
    approvals = tmp_path / "approvals"
    approvals.mkdir()
    (approvals / "eurlex.yaml").write_text(_VALID_APPROVAL)
    rc, out, _err = _run(["--sources-dir", str(sources), "--approvals-dir", str(approvals)], capsys)
    assert rc == 0
    assert "| eurlex | OK |" in out


def test_valid_approval_with_default_dirs_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _sources(tmp_path / "config" / "sources")
    approvals = tmp_path / "config" / "approvals"
    approvals.mkdir()
    (approvals / "eurlex.yaml").write_text(_VALID_APPROVAL)
    rc, out, _err = _run([], capsys)
    assert rc == 0
    assert "| eurlex | OK |" in out
