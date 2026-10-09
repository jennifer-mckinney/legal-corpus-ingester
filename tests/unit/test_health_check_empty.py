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

import errno
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import pytest
import yaml

# scripts/ is not a package; inject it into the path.
_SCRIPTS = Path(__file__).parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

# These live in scripts/, so they can only be imported after the path insert.
import check_approvals  # noqa: E402
import health_check  # noqa: E402
from health_check import main  # noqa: E402

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


def _empty_dir_message(config_dir: Path) -> str:
    """The shared empty-dir wording (grumpy r2-4), read from the script, never restated."""
    return health_check._empty_dir_problem(config_dir, "config dir")


# ---------------------------------------------------------------------------
# Card spec
# ---------------------------------------------------------------------------


def test_empty_config_dir_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    rc, _out, err = _run(config_dir, tmp_path, capsys)
    assert rc == _EXIT_CONFIG
    assert f"Error: {_empty_dir_message(config_dir)}" in err.splitlines()


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
    assert f"Error: {_empty_dir_message(config_dir)}" in err.splitlines()


def test_symlink_to_empty_dir_exits_config_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    real = tmp_path / "real-empty"
    real.mkdir()
    link = tmp_path / "config"
    link.symlink_to(real, target_is_directory=True)
    rc, _out, err = _run(link, tmp_path, capsys)
    assert rc == _EXIT_CONFIG
    assert f"Error: {_empty_dir_message(link)}" in err.splitlines()


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
        ("key: [unclosed\n", "not valid YAML (line "),
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
    assert _empty_dir_message(config_dir) not in err


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
    with pytest.raises(UnicodeDecodeError) as decoded:
        b"name: caf\xe9\n".decode("utf-8")
    # Grumpy r2-4: the reason is the shared load-failure text, on the line naming the file.
    reason = health_check._load_failure(decoded.value)
    assert any("latin.yaml" in line and reason in line for line in err.splitlines()), err
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
    reason = health_check._load_failure(PermissionError(errno.EACCES, os.strerror(errno.EACCES)))
    assert any("locked.yaml" in line and reason in line for line in err.splitlines()), err


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
    # Grumpy r2-3: a full, capped prefix of whole "\x0a" escapes, nothing less.
    token = "\\x0a"
    assert huge == token * (cap // len(token)) + "...(truncated)"


# One character per escape width the renderer emits: \\ (2), \xNN (4), \uNNNN (6), \UNNNNNNNN (10).
_ESCAPE_CHARS = {"backslash": "\\", "x-escape": "\n", "u-escape": "\u202e", "big-u-escape": "\U000e0001"}
_CAP_SPLITS = [
    pytest.param(ch, before, id=f"{name}-{before}-before-cap")
    for name, ch in _ESCAPE_CHARS.items()
    # Width from the renderer itself (both copies are byte-identical, tested below).
    for before in range(len(health_check.display_path(ch)) + 1)  # 0 = starts at the cap, w = ends at it
]


@_SCRIPT_MODULES
@pytest.mark.parametrize(("ch", "before"), _CAP_SPLITS)
def test_display_path_truncation_never_splits_an_escape(mod: ModuleType, ch: str, before: int) -> None:
    # Security F2 residual: the cap must not cut "\x0a" to "\x0" or "\\" to a lone "\",
    # which would make the next character read as part of an escape. The escape starts
    # `before` characters ahead of the cap, so 1..w-1 put the cap inside it.
    cap = mod._MAX_DISPLAY_CHARS  # F13: read, not restated
    raw = "a" * (cap - before) + ch + "tail"
    tokens = [mod.display_path(c) for c in raw]  # each character's whole escape
    assert all(len(t) <= cap for t in tokens)
    kept = ""
    for token in tokens:  # longest run of whole escapes that fits under the cap
        if len(kept) + len(token) > cap:
            break
        kept += token
    assert mod.display_path(raw) == kept + "...(truncated)"


# Last line of the shared block in both scripts (grumpy r2-4: the block now also holds
# the load-failure and empty-dir wording, so it ends at an explicit marker).
_BLOCK_END = "# End of the byte-identical block (terms-analysis#173)."
# Shared helpers that must live inside the locked block, once per script.
_SHARED_DEFS = (
    "def scan_yaml_dir(",
    "def _load_failure(",
    "def _empty_dir_problem(",
    "class _NoAliasLoader(",
    "def _load_yaml(",
)


def test_folded_block_is_byte_identical_in_both_scripts() -> None:
    # The banner promises one byte-identical copy per script; this is what enforces it.
    blocks = []
    for script in ("health_check.py", "check_approvals.py"):
        src = (_SCRIPTS / script).read_text(encoding="utf-8")
        start = src.index("# Directory listing and log-path rendering")
        assert src.count(_BLOCK_END) == 1, f"{script}: shared block has no single end marker"
        end = src.index(_BLOCK_END, start)
        block = src[start:end]
        for definition in _SHARED_DEFS:
            assert definition in block, f"{script}: {definition} is outside the shared block"
            assert src.count(definition) == 1, f"{script}: {definition} defined more than once"
        blocks.append(block)
    assert blocks[0] == blocks[1]


_HOSTILE_DIRS = [Path("plain"), Path(_HOSTILE_NAME), Path("a|b\\x1b")]


@pytest.mark.parametrize("d", _HOSTILE_DIRS, ids=["plain", "forging-name", "pipe-and-backslash"])
@pytest.mark.parametrize("label", ["config dir", "approvals dir"])
def test_empty_dir_message_is_one_wording_in_both_scripts(d: Path, label: str) -> None:
    # Grumpy r2-4: one message, one spelling, whichever script or code path emits it.
    message = health_check._empty_dir_problem(d, label)
    assert message == check_approvals._empty_dir_problem(d, label)
    # Honest and safe: names the dir by label and escaped path, no raw control bytes (F8).
    assert f"{label} {health_check.display_path(d)}" in message
    assert len(message.splitlines()) == 1
    assert "\x1b" not in message and "\u202e" not in message


def test_both_scripts_print_the_shared_empty_dir_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Behaviour, not just the helper: each main() prints exactly that line for an empty dir.
    empty = tmp_path / "empty"
    empty.mkdir()
    rc, _out, err = _run(empty, tmp_path, capsys)
    assert rc == _EXIT_CONFIG
    assert err.splitlines() == [f"Error: {health_check._empty_dir_problem(empty, 'config dir')}"]
    no_sources = tmp_path / "no-sources"
    no_sources.mkdir()  # zero configs: no "unverified" clause, so the line is the message alone
    rc = check_approvals.main(["--sources-dir", str(no_sources), "--approvals-dir", str(empty)])
    err = capsys.readouterr().err
    assert rc == _EXIT_CONFIG
    assert err.splitlines() == [f"Error: {check_approvals._empty_dir_problem(empty, 'approvals dir')}"]


def _deep_nesting() -> bytes:
    # Nested past the parser's recursion limit: safe_load raises RecursionError.
    return b"k: " + b"[" * 5000 + b"]" * 5000 + b"\n"


@pytest.mark.parametrize(
    ("body", "exc_type", "has_mark"),
    [
        (b"key: [unclosed\n", yaml.YAMLError, True),  # parse error with a line mark
        (b"key: \x07BELL\n", yaml.YAMLError, False),  # reader error, no mark
        (b"name: caf\xe9\n", UnicodeDecodeError, False),  # not UTF-8: fails at decode
        (_deep_nesting(), RecursionError, False),  # not a YAMLError at all
    ],
    ids=["parse-error", "reader-error-no-mark", "non-utf8", "deep-nesting"],
)
def test_load_failure_text_is_the_same_in_both_scripts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], body: bytes, exc_type: type[Exception], has_mark: bool
) -> None:
    # Grumpy r2-4: one load-failure rule and wording. Same bytes, same reason, both jobs.
    # Grumpy r3-1: pin the exception type and mark per case, so a body that fails for a
    # different reason cannot pass as the case it is named for.
    with pytest.raises(exc_type) as failed:
        yaml.safe_load(body.decode("utf-8"))
    assert isinstance(failed.value, exc_type)
    assert (getattr(failed.value, "problem_mark", None) is not None) is has_mark
    reason = check_approvals._load_failure(failed.value)
    assert health_check._load_failure(failed.value) == reason
    assert reason and "BELL" not in reason and "unclosed" not in reason  # no file bytes (F8)

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "ghost.yaml").write_bytes(body)
    rc, out, err = _run(cfg, tmp_path, capsys)
    assert rc == _EXIT_CONFIG, err
    assert out == ""
    assert any("ghost.yaml" in line and reason in line for line in err.splitlines()), err

    srcs = tmp_path / "srcs"
    srcs.mkdir()
    rc = check_approvals.main(["--sources-dir", str(srcs), "--approvals-dir", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 1
    assert f"| ghost | ERROR | {check_approvals.table_cell(reason)} | - |" in out.splitlines()


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
        assert len(rows) == 2, f"header plus one source row expected: {rows!r}"
        for row in rows:
            assert _REPORT_ROW.fullmatch(row), f"row has a forged cell delimiter: {row!r}"
        # Grumpy r2-2: the stage is escaped, not dropped.
        assert f"| {health_check.table_cell(stage)} |" in rows[1]


# ---------------------------------------------------------------------------
# Grumpy 4: the config dir vanishing after main()'s check must not read as success
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["removed", "emptied"])
def test_config_dir_lost_after_check_exits_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    # Reaches the guard in _source_names the only way it can be reached from main():
    # the dir changes between check and use. Grumpy r2-1: one outcome only. main()
    # must catch YamlDirError and return exit 2; deleting that catch fails this test.
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
    rc, out, err = _run(config_dir, tmp_path, capsys)
    assert rc == _EXIT_CONFIG, f"lost config dir: rc={rc}, out={out!r}"
    assert out == ""
    assert not (tmp_path / "out").exists(), "no report is written for a config error"
    if how == "removed":
        with pytest.raises(health_check.YamlDirError) as missing:
            health_check.scan_yaml_dir(config_dir, "config dir")
        expected = str(missing.value)
    else:
        expected = _empty_dir_message(config_dir)
    assert err.splitlines() == [f"Error: {expected}"]


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


# ---------------------------------------------------------------------------
# Security r3 F4: YAML merge-key bomb and aliases in source configs
# ---------------------------------------------------------------------------

# Bounds for the bomb child. Same shape as the approvals test: RSS is sampled from
# outside, because the exponential work runs in C inside yaml.safe_load.
_BOMB_TIMEOUT_S = 10.0
_BOMB_MAX_RSS_KB = 256 * 1024
_BOMB_POLL_S = 0.02


def _merge_key_bomb(levels: int) -> str:
    """Each level merges the previous mapping twice via `<<`: ~2**levels work inside safe_load."""
    lines = ["m0: &m0 {k0: 1}"]
    for i in range(1, levels + 1):
        lines.append(f"m{i}: &m{i} {{<<: [*m{i - 1}, *m{i - 1}], k{i}: 1}}")
    return "\n".join(lines) + "\n"


def _run_bounded(config_dir: Path, tmp_path: Path) -> tuple[int, str, str]:
    """Run health_check.py in a child; kill it and fail if it passes the time or RSS cap."""
    argv = [
        sys.executable, str(_SCRIPTS / "health_check.py"),
        "--config-dir", str(config_dir),
        "--state-dir", str(tmp_path / "state"),
        "--out-dir", str(tmp_path / "out"),
    ]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    start = time.monotonic()
    peak = 0
    try:
        while proc.poll() is None:
            ps = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(proc.pid)], capture_output=True, text=True, timeout=5, check=False
            )
            peak = max(peak, int(ps.stdout.strip() or 0))
            elapsed = time.monotonic() - start
            if peak > _BOMB_MAX_RSS_KB or elapsed > _BOMB_TIMEOUT_S:
                proc.kill()
                proc.communicate()
                pytest.fail(
                    f"YAML bomb was expanded: rss={peak} KB (cap {_BOMB_MAX_RSS_KB}), "
                    f"{elapsed:.1f}s (cap {_BOMB_TIMEOUT_S}s)"
                )
            time.sleep(_BOMB_POLL_S)
        out, err = proc.communicate(timeout=_BOMB_TIMEOUT_S)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    return proc.returncode, out, err


def test_yaml_merge_key_bomb_is_a_config_problem_within_bounds(tmp_path: Path) -> None:
    # ~900 bytes, exponential inside yaml.safe_load. Unfixed: >1.6 GB and a hang.
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    body = _merge_key_bomb(25) + "source_id: s\n"
    assert len(body) < 2048  # control: the hostile input itself is small
    (cfg / "mergebomb.yaml").write_text(body)
    rc, out, err = _run_bounded(cfg, tmp_path)
    assert rc == _EXIT_CONFIG, err[-2000:]
    assert out == ""
    assert any("mergebomb.yaml" in line and "not valid YAML" in line for line in err.splitlines()), err[-2000:]
    assert "Traceback" not in err


def test_plain_yaml_alias_in_a_source_config_is_rejected(tmp_path: Path) -> None:
    # Source configs never need anchors, so even a harmless alias is refused at load.
    # Control: the same mapping without the alias loads (no config problem for it).
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "plainalias.yaml").write_text('base: &a "x"\nsource_id: s\ncopy: *a\n')
    (cfg / "noalias.yaml").write_text('base: "x"\nsource_id: s\ncopy: "x"\n')
    rc, _out, err = _run_bounded(cfg, tmp_path)
    lines = err.splitlines()
    # The docstring promises the "(line N)" suffix; the alias sits on line 3 of the file.
    assert any("plainalias.yaml" in line and "not valid YAML (line 3)" in line for line in lines), err[-2000:]
    assert not any("noalias.yaml" in line for line in lines), err[-2000:]
    assert rc == _EXIT_CONFIG, err[-2000:]
