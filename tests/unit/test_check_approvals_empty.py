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
import re
import shutil
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

# scripts/ is not a package; inject it into the path.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

import check_approvals
import yaml
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
# An approval file stem that tries to forge the table: an ANSI escape and a cell
# delimiter. Rendered through display_path, then "|" escaped as "\\|" (security F1).
_HOSTILE_STEM = "e\x1b[31m|red"
_HOSTILE_STEM_SHOWN = "e\\x1b[31m\\|red"
# One table row: exactly four cells, each made of escapes or non-delimiter characters.
_TABLE_ROW = re.compile(r"\|(?: (?:\\.|[^\\|\n])* \|){4}")


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
    (sources / "second.yaml").write_text(_FIXTURE_SOURCE.read_text())
    n_configs = len(list(sources.glob("*.yaml")))
    approvals = tmp_path / "empty-approvals"
    approvals.mkdir()
    rc, out, err = _run(["--sources-dir", str(sources), "--approvals-dir", str(approvals)], capsys)
    assert rc == _EXIT_CONFIG
    assert "empty-approvals" in err
    # Honest message: says how many gated sources are left unverified.
    assert f"{n_configs} source config(s)" in err
    assert "unverified" in err
    assert out == ""


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


def test_yaml_directory_beside_real_approval_is_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sources = _sources(tmp_path / "srcs")
    approvals = tmp_path / "appr"
    approvals.mkdir()
    (approvals / "eurlex.yaml").write_text(_VALID_APPROVAL)
    (approvals / "stray.yaml").mkdir()
    rc, out, err = _run(["--sources-dir", str(sources), "--approvals-dir", str(approvals)], capsys)
    assert rc == _EXIT_CONFIG
    assert "stray.yaml in approvals dir" in err
    assert out == ""


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


def test_yaml_directory_in_sources_dir_is_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sources = _sources(tmp_path / "srcs")
    (sources / "stray.yaml").mkdir()
    approvals = tmp_path / "appr"
    approvals.mkdir()
    (approvals / "eurlex.yaml").write_text(_VALID_APPROVAL)
    rc, out, err = _run(["--sources-dir", str(sources), "--approvals-dir", str(approvals)], capsys)
    assert rc == _EXIT_CONFIG
    assert "stray.yaml in sources dir" in err
    assert out == ""


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
# Security F1: untrusted stems, parser text and YAML values in the table (exit 0/1)
# ---------------------------------------------------------------------------


def test_hostile_stem_and_parse_error_are_escaped_on_exit_one_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sources = _sources(tmp_path / "srcs")
    approvals = tmp_path / "appr"
    approvals.mkdir()
    body = "expiry: [PARSER-LEAK\n"
    (approvals / f"{_HOSTILE_STEM}.yaml").write_text(body)
    with pytest.raises(yaml.YAMLError) as parsed:
        yaml.safe_load(body)
    line = parsed.value.problem_mark.line + 1  # what the operator needs to find it
    rc, out, err = _run(["--sources-dir", str(sources), "--approvals-dir", str(approvals)], capsys)
    assert rc == _EXIT_EXPIRED_OR_INVALID
    assert "\x1b" not in out + err
    rows = [r for r in out.splitlines() if r.startswith("| ") and "---" not in r][1:]  # skip header
    assert len(rows) == 1, f"parser text broke the table into extra lines: {out!r}"
    assert _TABLE_ROW.fullmatch(rows[0]), f"row has a forged cell delimiter: {rows[0]!r}"
    assert rows[0].startswith(f"| {_HOSTILE_STEM_SHOWN} | ERROR |")
    # Line number only: the parser's message quotes file bytes (F8).
    assert f"line {line}" in rows[0]
    assert "PARSER-LEAK" not in out + err
    assert "<unicode string>" not in out + err


@pytest.mark.parametrize(
    ("body", "expected_cell"),
    [
        # M8: a reader error (a control character) is a YAMLError with no line mark.
        (b"expiry: \x07BELL-LEAK\n", "not valid YAML"),
        # M9: bytes that are not UTF-8; current fail behaviour is the type name only.
        (b'source_id: "caf\xe9 LATIN-LEAK"\nexpiry: "2099-01-01"\n', "cannot read: UnicodeDecodeError"),
    ],
    ids=["yaml-error-without-line-mark", "non-utf8-bytes"],
)
def test_unloadable_approval_file_is_an_error_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], body: bytes, expected_cell: str
) -> None:
    sources = _sources(tmp_path / "srcs")
    approvals = tmp_path / "appr"
    approvals.mkdir()
    (approvals / "ghost.yaml").write_bytes(body)
    if expected_cell == "not valid YAML":  # control: the case really has no mark to report
        with pytest.raises(yaml.YAMLError) as parsed:
            yaml.safe_load(body.decode())
        assert getattr(parsed.value, "problem_mark", None) is None
    rc, out, err = _run(["--sources-dir", str(sources), "--approvals-dir", str(approvals)], capsys)
    assert rc == _EXIT_EXPIRED_OR_INVALID
    assert f"| ghost | ERROR | {expected_cell} | - |" in out.splitlines()
    # Neither the parser's message nor the file's bytes reach the output (F8).
    for leak in ("BELL-LEAK", "LATIN-LEAK", "<unicode string>", "\x07"):
        assert leak not in out + err


_HOSTILE_VALUE = "a\\e[2J|FORGED\\nx\\L\\u202e"  # YAML escapes: ESC, |, LF, U+2028, RLO


@pytest.mark.parametrize(
    ("approval", "expected_rc"),
    [
        (  # source_id on the exit-0 path: a valid, current approval
            f'source_id: "{_HOSTILE_VALUE}"\nsigned_artifact_sha256: "{"a" * 64}"\nexpiry: "2099-01-01"\n',
            0,
        ),
        (  # source_id on an ERROR row
            f'source_id: "{_HOSTILE_VALUE}"\nexpiry: "2099-01-01"\n',
            _EXIT_EXPIRED_OR_INVALID,
        ),
        (  # the raw expiry value on the invalid-date row
            f'source_id: "plain"\nexpiry: "{_HOSTILE_VALUE}"\n',
            _EXIT_EXPIRED_OR_INVALID,
        ),
    ],
    ids=["source-id-ok-row", "source-id-error-row", "invalid-expiry-row"],
)
def test_hostile_yaml_values_are_escaped_in_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], approval: str, expected_rc: int
) -> None:
    sources = _sources(tmp_path / "srcs")
    approvals = tmp_path / "appr"
    approvals.mkdir()
    (approvals / "eurlex.yaml").write_text(approval)
    rc, out, err = _run(["--sources-dir", str(sources), "--approvals-dir", str(approvals)], capsys)
    assert rc == expected_rc
    for raw in ("\x1b", "\u2028", "\u202e"):
        assert raw not in out + err
    assert not any(line.startswith("FORGED") for line in out.splitlines())
    rows = [r for r in out.splitlines() if r.startswith("| ") and "---" not in r][1:]
    assert len(rows) == 1, f"a value broke the table into extra lines: {out!r}"
    assert _TABLE_ROW.fullmatch(rows[0]), f"row has a forged cell delimiter: {rows[0]!r}"


# ---------------------------------------------------------------------------
# Grumpy 4: the approvals dir vanishing after main()'s check must not read as success
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["removed", "emptied"])
def test_approvals_dir_lost_after_check_does_not_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    # Reaches the guard in check_approvals_dir the only way it can be reached from
    # main(): the dir changes between the config check and the walk.
    sources = _sources(tmp_path / "srcs")
    approvals = tmp_path / "appr"
    approvals.mkdir()
    (approvals / "eurlex.yaml").write_text(_VALID_APPROVAL)
    real_check = check_approvals._config_problems

    def check_then_lose_dir(sources_dir: Path, approvals_dir: Path) -> list[str]:
        problems = real_check(sources_dir, approvals_dir)
        if how == "removed":
            shutil.rmtree(approvals_dir)
        else:
            for f in approvals_dir.iterdir():
                f.unlink()
        return problems

    monkeypatch.setattr(check_approvals, "_config_problems", check_then_lose_dir)
    argv = ["--sources-dir", str(sources), "--approvals-dir", str(approvals)]
    try:
        rc: int | None = main(argv)
    except check_approvals.YamlDirError:
        rc = None  # uncaught: the script dies with a traceback (exit 1), never 0
    out = capsys.readouterr().out
    # None: YamlDirError escaped main (exit 1); 1: reported invalid; 2: config error.
    assert rc in (None, _EXIT_EXPIRED_OR_INVALID, _EXIT_CONFIG), f"lost approvals dir reported success: {out!r}"


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


def test_zero_sources_but_real_approvals_still_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Approvals present means there is work to do; an empty sources dir alone is not exit 2.
    sources = _sources(tmp_path / "srcs", configs=0)
    approvals = tmp_path / "appr"
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
