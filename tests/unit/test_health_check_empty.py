"""health_check.py must not report success when the registry is empty (terms-analysis#173).

Kept in its own file, apart from tests/unit/test_health_check.py, because
feat/g0-1-unwired-exit-nonzero (terms-analysis#90) edits that file for the
MISSING config dir case. This file covers an EXISTING dir that yields zero
usable source configs, plus the QUALITY-BAR attack list A for that input.

Contract pinned here: exit 2 (config problem), message on stderr.

Owner ruling (terms-analysis#173): the shared scripts/_yaml_dir.py is folded into
health_check.py and check_approvals.py, so the directory-listing and log-path
rules it held are tested here against BOTH scripts' copies, and each script must
run on its own with no helper module beside it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import pytest

# scripts/ is not a package; inject it into the path.
_SCRIPTS = Path(__file__).parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import check_approvals
import health_check
from health_check import main

_EXIT_CONFIG = 2
_FIXTURE_SOURCE = Path(__file__).resolve().parents[1] / "fixtures" / "sources" / "eurlex.yaml"
# A directory name that tries to forge terminal output: clear-screen escape,
# a newline followed by a fake status line, and a bidi override.
_HOSTILE_NAME = "hostile-cfg\x1b[2J\nFORGED: all sources fresh\u202e"
# A source config stem that tries to forge the report table: an ANSI escape and a cell
# delimiter. Rendered through display_path, then "|" escaped as "\\|" (security F1).
_HOSTILE_STEM = "e\x1b[31m|red"
_HOSTILE_STEM_SHOWN = "e\\x1b[31m\\|red"
# One report row: exactly five cells, each made of escapes or non-delimiter characters.
_REPORT_ROW = re.compile(r"\|(?: (?:\\.|[^\\|\n])* \|){5}")


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


_NOT_A_MAPPING = "is empty or not a YAML mapping"


@pytest.mark.parametrize("beside_real_config", [False, True], ids=["alone", "beside-real-config"])
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("", _NOT_A_MAPPING),
        ("# only a comment\n# and another\n", _NOT_A_MAPPING),
        ("---\n", _NOT_A_MAPPING),
        ("~\n", _NOT_A_MAPPING),
        ("{}\n", _NOT_A_MAPPING),
        ("- a\n- b\n", _NOT_A_MAPPING),
        ("just a string\n", _NOT_A_MAPPING),
        ("key: [unclosed\n", "is not valid YAML (line "),
    ],
    ids=[
        "empty-file", "comments-only", "bare-document-marker", "yaml-null",
        "empty-mapping", "list", "scalar", "parse-error",
    ],
)
def test_yaml_that_parses_to_nothing_exits_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str, expected: str,
    beside_real_config: bool,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    if beside_real_config:
        # One unusable file fails the run even when a good config sits next to it.
        (config_dir / "eurlex.yaml").write_text(_FIXTURE_SOURCE.read_text())
    (config_dir / "ghost.yaml").write_text(content)
    rc, out, err = _run(config_dir, tmp_path, capsys)
    assert rc == _EXIT_CONFIG
    # The message names the offending file so the operator can fix it.
    assert "ghost.yaml" in err
    assert expected in err
    assert "[unclosed" not in err  # the parser's quote of file bytes is not echoed (F8)
    assert out == ""
    assert not (tmp_path / "out").exists(), "no report is written for a config error"


def test_unreadable_config_dir_exits_config_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_dir = tmp_path / "locked-config"
    config_dir.mkdir()
    (config_dir / "eurlex.yaml").write_text(_FIXTURE_SOURCE.read_text())
    with _unreadable(config_dir):
        rc, _out, err = _run(config_dir, tmp_path, capsys)
    assert rc == _EXIT_CONFIG
    assert "locked-config" in err
    assert "cannot read config dir " in err
    assert "Errno" not in err  # strerror only, not the exception's repr with the raw path
    # Honest reason: the dir holds a config, so "no sources" would be a false diagnosis.
    assert "No sources configured" not in err


def test_yaml_directory_beside_real_config_is_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "eurlex.yaml").write_text(_FIXTURE_SOURCE.read_text())
    (config_dir / "stray.yaml").mkdir()
    rc, out, err = _run(config_dir, tmp_path, capsys)
    assert rc == _EXIT_CONFIG
    assert "stray.yaml in config dir" in err
    assert "is not a regular file" in err
    assert out == ""
    assert not (tmp_path / "out").exists(), "no report is written for a config error"


def test_non_utf8_source_yaml_is_config_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "latin.yaml").write_bytes(b"name: caf\xe9\n")
    rc, _out, err = _run(config_dir, tmp_path, capsys)
    assert rc == _EXIT_CONFIG
    assert "cannot read source config latin.yaml" in err
    assert "UnicodeDecodeError" in err
    assert "\xe9" not in err and "\ufffd" not in err  # no raw or replaced file bytes


def test_unreadable_source_file_is_config_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    locked = config_dir / "locked.yaml"
    locked.write_text(_FIXTURE_SOURCE.read_text())
    with _unreadable(locked):
        rc, _out, err = _run(config_dir, tmp_path, capsys)
    assert rc == _EXIT_CONFIG
    assert "cannot read source config locked.yaml" in err


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
# Folded helper (terms-analysis#173 owner ruling): each script carries its own
# copy of the listing and log-path rules; both copies must behave identically.
# ---------------------------------------------------------------------------

_SCRIPT_MODULES = pytest.mark.parametrize(
    "mod", [health_check, check_approvals], ids=["health_check", "check_approvals"]
)


@pytest.mark.parametrize("script", ["health_check.py", "check_approvals.py"])
def test_script_runs_standalone_without_helper_module(tmp_path: Path, script: str) -> None:
    # The scheduled jobs run `python scripts/<script>`; after the fold no sibling
    # helper module may be needed, so a lone copy must reach its own config check.
    alone = tmp_path / "alone"
    alone.mkdir()
    shutil.copy2(_SCRIPTS / script, alone / script)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    missing = tmp_path / "no-such-dir"
    args = (
        ["--config-dir", str(missing), "--state-dir", str(tmp_path / "s"), "--out-dir", str(tmp_path / "o")]
        if script == "health_check.py"
        else ["--sources-dir", str(missing), "--approvals-dir", str(missing)]
    )
    result = subprocess.run(
        [sys.executable, str(alone / script), *args],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False,
    )
    assert "ModuleNotFoundError" not in result.stderr, result.stderr
    assert result.returncode == _EXIT_CONFIG, result.stderr
    assert "no-such-dir" in result.stderr


@_SCRIPT_MODULES
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain/dir", "plain/dir"),
        ("a\nb", "a\\x0ab"),
        ("a\rb", "a\\x0db"),
        ("v\x0bt\x0cf", "v\\x0bt\\x0cf"),
        ("nel\x85x", "nel\\x85x"),
        ("nul\x00x", "nul\\x00x"),
        ("esc\x1b[2J", "esc\\x1b[2J"),
        ("del\x7f", "del\\x7f"),
        ("bidi\u202eRLO", "bidi\\u202eRLO"),
        ("zw\u200bsp", "zw\\u200bsp"),
        ("bom\ufeffx", "bom\\ufeffx"),
        ("line\u2028sep", "line\\u2028sep"),
        ("para\u2029sep", "para\\u2029sep"),
        ("bad\udcffbyte", "bad\\udcffbyte"),
        ("pua\ue000x", "pua\\ue000x"),
        ("unassigned\u0378x", "unassigned\\u0378x"),
        # A literal backslash is escaped, so the text "\x1b" never looks like a real ESC (F2).
        ("back\\slash", "back\\\\slash"),
        ("a\\x1bb", "a\\\\x1bb"),
        # Above U+FFFF: a fixed-width \U escape, so trailing text is not read as hex (F2).
        ("plane16\U0010fffdfd", "plane16\\U0010fffdfd"),
        ("tag\U000e0001x", "tag\\U000e0001x"),
        ("caf\u00e9 r\u00e9sum\u00e9", "caf\u00e9 r\u00e9sum\u00e9"),
    ],
    ids=[
        "plain", "lf", "cr", "vt-ff", "nel", "nul", "ansi-escape", "del", "bidi-override",
        "zero-width", "bom", "line-separator", "paragraph-separator", "lone-surrogate",
        "private-use", "unassigned-cn", "literal-backslash", "literal-escape-text",
        "astral-private-use", "astral-format-tag", "printable-unicode-kept",
    ],
)
def test_display_path_escapes_unsafe_characters(mod: ModuleType, raw: str, expected: str) -> None:
    assert mod.display_path(raw) == expected


@_SCRIPT_MODULES
def test_display_path_output_is_one_encodable_line(mod: ModuleType) -> None:
    shown = mod.display_path("x\udcff\n\u2028\x85\x00y")
    assert len(shown.splitlines()) == 1
    shown.encode("utf-8")  # a lone surrogate would raise here if left raw


@_SCRIPT_MODULES
def test_display_path_caps_length_at_boundary(mod: ModuleType) -> None:
    # Limit read from the script's own constant (F13), not restated here.
    cap = mod._MAX_DISPLAY_CHARS
    assert cap > 0
    assert mod.display_path("a" * cap) == "a" * cap
    assert mod.display_path("a" * (cap + 1)) == "a" * cap + "...(truncated)"
    huge = mod.display_path("\n" * (2 * 1024 * 1024))  # 2 MB of line breaks
    assert len(huge) == cap + len("...(truncated)")


def test_folded_block_is_byte_identical_in_both_scripts() -> None:
    # The banner promises one byte-identical copy per script; this is what enforces it.
    blocks = []
    for script in ("health_check.py", "check_approvals.py"):
        src = (_SCRIPTS / script).read_text(encoding="utf-8")
        start = src.index("# Directory listing and log-path rendering")
        end = src.index("return YamlDirListing(files=files, non_files=non_files)", start)
        blocks.append(src[start:end])
    assert blocks[0] == blocks[1]


@_SCRIPT_MODULES
def test_scan_lists_regular_and_hidden_yaml_files_sorted(mod: ModuleType, tmp_path: Path) -> None:
    for name in ("b.yaml", "a.yaml", ".hidden.yaml", "notes.yml", "c.json"):
        (tmp_path / name).write_text("k: v\n")
    listing = mod.scan_yaml_dir(tmp_path, "config dir")
    # Hidden *.yaml counts, matching the registry's glob("*.yaml"); *.yml does not.
    assert [p.name for p in listing.files] == [".hidden.yaml", "a.yaml", "b.yaml"]
    assert listing.non_files == []


@_SCRIPT_MODULES
def test_scan_follows_symlink_to_file_and_flags_broken_symlink(mod: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "real.txt"
    target.write_text("k: v\n")
    d = tmp_path / "d"
    d.mkdir()
    (d / "linked.yaml").symlink_to(target)
    (d / "dangling.yaml").symlink_to(tmp_path / "gone")
    listing = mod.scan_yaml_dir(d, "config dir")
    assert [p.name for p in listing.files] == ["linked.yaml"]
    assert [p.name for p in listing.non_files] == ["dangling.yaml"]


@_SCRIPT_MODULES
def test_scan_rejects_a_file_given_as_the_dir(mod: ModuleType, tmp_path: Path) -> None:
    f = tmp_path / "not-a-dir"
    f.write_text("")
    with pytest.raises(mod.YamlDirError, match="is not a directory"):
        mod.scan_yaml_dir(f, "config dir")


# ---------------------------------------------------------------------------
# Security F1: untrusted names and checkpoint text in the report (exit 0 and 1 paths)
# ---------------------------------------------------------------------------


def test_hostile_source_stem_is_escaped_in_report_and_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / f"{_HOSTILE_STEM}.yaml").write_text(_FIXTURE_SOURCE.read_text())
    rc, out, _err = _run(config_dir, tmp_path, capsys)
    assert rc == 1  # never run: the stale/never-run path, not a config error
    (report,) = (tmp_path / "out" / "health").glob("*.md")
    for text in (out, report.read_text()):
        assert "\x1b" not in text
        rows = [line for line in text.splitlines() if line.startswith("| ") and "---" not in line]
        assert rows, "control: the report has a table"
        for row in rows:
            assert _REPORT_ROW.fullmatch(row), f"row has a forged cell delimiter: {row!r}"
        assert f"| {_HOSTILE_STEM_SHOWN} |" in text  # still identifies the source


def test_hostile_checkpoint_stage_is_escaped_on_exit_zero_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "eurlex.yaml").write_text(_FIXTURE_SOURCE.read_text())
    state = tmp_path / "state"
    state.mkdir()
    stage = "pub\x1b[2J|lished\nFORGED | x | y | z | fresh |\u2028\u202e"
    (state / "eurlex.checkpoint.json").write_text(json.dumps({"stage": stage}))
    rc, out, _err = _run(config_dir, tmp_path, capsys)
    assert rc == 0  # fresh source: the success path must be safe too
    (report,) = (tmp_path / "out" / "health").glob("*.md")
    for text in (out, report.read_text()):
        for raw in ("\x1b", "\u2028", "\u202e"):
            assert raw not in text
        assert not any(line.startswith("FORGED") for line in text.splitlines())
        rows = [line for line in text.splitlines() if line.startswith("| ") and "---" not in line]
        for row in rows:
            assert _REPORT_ROW.fullmatch(row), f"row has a forged cell delimiter: {row!r}"


# ---------------------------------------------------------------------------
# Grumpy 4: the config dir vanishing after main()'s check must not read as success
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["removed", "emptied"])
def test_config_dir_lost_after_check_does_not_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    # Reaches the guard in _source_names (and build_report's "no sources" branch) the
    # only way it can be reached from main(): the dir changes between check and use.
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "eurlex.yaml").write_text(_FIXTURE_SOURCE.read_text())
    real_check = health_check._config_problems

    def check_then_lose_dir(d: Path) -> list[str]:
        problems = real_check(d)
        if how == "removed":
            shutil.rmtree(d)
        else:
            for f in d.iterdir():
                f.unlink()
        return problems

    monkeypatch.setattr(health_check, "_config_problems", check_then_lose_dir)
    try:
        rc: int | None = main([
            "--config-dir", str(config_dir),
            "--state-dir", str(tmp_path / "state"),
            "--out-dir", str(tmp_path / "out"),
        ])
    except health_check.YamlDirError:
        rc = None  # uncaught: the script dies with a traceback (exit 1), never 0
    out = capsys.readouterr().out
    # None: YamlDirError escaped main (exit 1); 1: reported as a problem; 2: config error.
    assert rc in (None, 1, _EXIT_CONFIG), f"lost config dir reported success: {out!r}"
    assert "No sources configured" not in out, "stdout claims a clean, empty run"


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
