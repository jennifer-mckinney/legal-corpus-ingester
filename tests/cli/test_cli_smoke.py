from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
import typer.main
from typer.testing import CliRunner

from legal_corpus_ingester import cli
from legal_corpus_ingester.cli import app
from legal_corpus_ingester.pipeline.state import STAGES, CheckpointState, CheckpointStore
from legal_corpus_ingester.sources.registry import load_all
from tests.cli.helpers import (
    EXIT_CODE_MEANINGS,
    PUBLIC_EXIT_CODES,
    SOURCES_DIR_COMMANDS,
    sources_dir_argv,
    strip_ansi,
)

runner = CliRunner()


def test_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0


def test_init_creates_dirs(tmp_path: object, monkeypatch: object) -> None:  # type: ignore[type-arg]
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert (tmp_path / "config").is_dir()  # type: ignore[operator]
    assert (tmp_path / "out").is_dir()  # type: ignore[operator]
    assert (tmp_path / "state").is_dir()  # type: ignore[operator]


def test_sources_list_no_sources(tmp_path: object, monkeypatch: object) -> None:  # type: ignore[type-arg]
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    (tmp_path / "config" / "sources").mkdir(parents=True)  # type: ignore[operator]
    result = runner.invoke(app, ["sources", "list"])
    assert result.exit_code == 0
    assert "No sources" in result.output


def test_sources_list_help() -> None:
    result = runner.invoke(app, ["sources", "list", "--help"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# status: "nothing configured" is not success (terms-analysis#190, DEV-FUNDAMENTALS F5)
# ---------------------------------------------------------------------------
#
# `ingester status` has five outcomes, and each must be told apart from the others:
#   no_sources  the sources dir loads cleanly but holds no source -> EXIT_NO_SOURCES, stderr says so
#   dir_missing the sources path does not exist or is not a dir   -> EXIT_SOURCES_DIR_MISSING (#108)
#   load_error  the sources dir exists but cannot be loaded        -> EXIT_LOAD_ERROR, stderr says it
#   never_run   sources configured, no checkpoint yet              -> 0, "No runs recorded."
#   healthy     sources configured, checkpoints present            -> 0, one "<name>: <stage>" line
# #108 (owner ruling 2026-10-12): EXIT_NO_SOURCES moved off 2, which is EXIT_USAGE's alone, and a
# missing or non-directory sources path left EXIT_LOAD_ERROR for its own code. An unreadable dir
# exists, so it is not dir_missing; it cannot be listed, so it is not no_sources either: load_error.
# never_run stays exit 0 on purpose: the nightly health.yml runs `ingester status` with the repo's
# config/sources on a fresh hosted runner, whose state dir is empty until refresh is wired and state
# persists (ADR-016). Each failure row pins the exact public code (EXIT_NO_SOURCES /
# EXIT_LOAD_ERROR, whose numeric values test_status_help_documents_exit_codes pins) AND checks
# that refresh exits with the same code on the same sources dir (G0-1, terms-analysis#90), so
# neither the shared constant nor either command can drift unnoticed.

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_SOURCE = _REPO_ROOT / "tests" / "fixtures" / "sources" / "eurlex.yaml"
_REPO_SOURCES = _REPO_ROOT / "config" / "sources"
_NO_RUNS = "No runs recorded."
_NO_SOURCES_MSG = "No sources configured"
_LOAD_ERROR_MSG = "Error loading sources"
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
# One character per hostile class, generated into the `name` field of an otherwise valid source.
# The loader must reject each; status must then report a load error without echoing the raw byte.
_HOSTILE_NAME_CHARS = ("\x1b", "\r", "\x0b", "\x0c", "\x85", " ", " ", "​", "‮", "﻿")


# The same classes in a MISSING --sources-dir argument (#108), plus newline (line forgery), NUL,
# a lone surrogate and a surrogate-escaped invalid UTF-8 byte (how POSIX argv carries b"\xff").
_HOSTILE_PATH_CHARS = (*_HOSTILE_NAME_CHARS, "\n", "\x00", "\ud800", "\udcff")
# The same classes in the name of an EXISTING empty --sources-dir (#108 exit 5). NUL and lone
# surrogates are left out: no filesystem can hold them in a name (APFS refuses invalid UTF-8), so
# they can only ever be missing paths, and the dir_missing rows above cover them.
_HOSTILE_EXISTING_NAME_CHARS = (*_HOSTILE_NAME_CHARS, "\n")


def _unsafe_chars(text: str) -> list[str]:
    """Raw control, format, surrogate and line/paragraph-separator characters (newline allowed)."""
    return [hex(ord(c)) for c in text if c != "\n" and unicodedata.category(c) in _UNSAFE_CATEGORIES]


def _fixture_name() -> str:
    (name,) = load_all(_FIXTURE_SOURCE.parent)  # from the config, not a literal
    return name


def _copy_fixture(sources: Path, filename: str = _FIXTURE_SOURCE.name) -> None:
    sources.mkdir(exist_ok=True)
    (sources / filename).write_text(_FIXTURE_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")


def _save_checkpoint(state: Path, name: str, stage: str) -> None:
    CheckpointStore(state).save(CheckpointState(source_name=name, stage=stage, corpus_version="v-test"))


@dataclass(frozen=True)
class _StatusCase:
    id: str
    kind: str  # no_sources | load_error | never_run | healthy
    # build(root) lays out the case under root (the cwd) and returns
    # (--sources-dir argument, --state-dir argument, expected stdout lines for the exit-0 kinds).
    build: Callable[[Path], tuple[str, str, list[str]]]


def _empty_dir(root: Path) -> tuple[str, str, list[str]]:
    (root / "src").mkdir()
    return "src", "state", []


def _lookalike_files_only(root: Path) -> tuple[str, str, list[str]]:
    # Valid source content under names the loader does not read: nothing is configured.
    src = root / "src"
    for filename in ("eurlex.yml", "eurlex.yaml.disabled", "eurlex.yaml~", "README.md"):
        _copy_fixture(src, filename)
    return "src", "state", []


def _symlink_to_empty_dir(root: Path) -> tuple[str, str, list[str]]:
    (root / "real").mkdir()
    (root / "src").symlink_to(root / "real", target_is_directory=True)
    return "src", "state", []


def _empty_dir_hostile_name(ch: str) -> Callable[[Path], tuple[str, str, list[str]]]:
    # #108 exit 5: an EXISTING empty dir whose name forges output (control, format, line and
    # paragraph separators, newline). Nothing is configured; the raw name is never echoed (F2, F8).
    def build(root: Path) -> tuple[str, str, list[str]]:
        (root / f"src{ch}x").mkdir()
        return f"src{ch}x", "state", []

    return build


def _empty_dir_name_max(root: Path) -> tuple[str, str, list[str]]:
    # Size boundary: an existing empty dir whose name is exactly the filesystem's NAME_MAX (read
    # from the filesystem, no literal). It exists, so it is no_sources, never dir_missing.
    name = "e" * os.pathconf(root, "PC_NAME_MAX")
    (root / name).mkdir()
    return name, "state", []


def _source_only_in_subdir(root: Path) -> tuple[str, str, list[str]]:
    # Indirection: a valid source one level down is not configured (the loader reads *.yaml in
    # the dir itself only), so the dir holds no source, exactly as status and refresh see it.
    (root / "src").mkdir()
    _copy_fixture(root / "src" / "nested")
    return "src", "state", []


def _orphan_checkpoint(root: Path) -> tuple[str, str, list[str]]:
    # A checkpoint left in the state dir must not make "nothing configured" look configured.
    (root / "src").mkdir()
    _save_checkpoint(root / "state", _fixture_name(), STAGES[-1])
    return "src", "state", []


def _hostile_orphan_checkpoint(root: Path) -> tuple[str, str, list[str]]:
    # Orphan checkpoint whose FILE NAME forges a status line with ESC, newline and U+2028.
    (root / "src").mkdir()
    forged = f"orphan\n\x1b[31m{_fixture_name()}: {STAGES[-1]} x"
    _save_checkpoint(root / "state", forged, STAGES[-1])
    return "src", "state", []


def _missing_dir(root: Path) -> tuple[str, str, list[str]]:
    return "src", "state", []


def _dangling_symlink(root: Path) -> tuple[str, str, list[str]]:
    (root / "src").symlink_to(root / "gone", target_is_directory=True)
    return "src", "state", []


def _sources_path_is_file(root: Path) -> tuple[str, str, list[str]]:
    (root / "src").write_text(_FIXTURE_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    return "src", "state", []


def _symlink_to_file(root: Path) -> tuple[str, str, list[str]]:
    (root / "real.yaml").write_text(_FIXTURE_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    (root / "src").symlink_to(root / "real.yaml")
    return "src", "state", []


def _parent_is_a_file(root: Path) -> tuple[str, str, list[str]]:
    # ENOTDIR on a middle component: the path does not exist as a directory.
    (root / "f").write_text("x", encoding="utf-8")
    return "f/src", "state", []


def _fifo(root: Path) -> tuple[str, str, list[str]]:
    # Not a directory, and opening it for reading would block: the check must not read it.
    os.mkfifo(root / "src")
    return "src", "state", []


def _missing_hostile_name(ch: str) -> Callable[[Path], tuple[str, str, list[str]]]:
    # A missing path whose name forges output (control, format, line/paragraph separators,
    # NUL): the exit names the outcome without echoing the raw character (F2, F8).
    def build(root: Path) -> tuple[str, str, list[str]]:
        return f"src{ch}x", "state", []

    return build


def _empty_string_path(root: Path) -> tuple[str, str, list[str]]:
    # Boundary: --sources-dir "" names nothing (stat("") is ENOENT); it must not fall back to the cwd.
    return "", "state", []


def _missing_name_too_long(root: Path) -> tuple[str, str, list[str]]:
    # ENAMETOOLONG, sized from the filesystem's own limit (no literal): still "does not exist",
    # and the huge name is never echoed back (checked in the dir_missing branch).
    return "a" * (os.pathconf(root, "PC_NAME_MAX") + 1), "state", []


def _unreadable_dir(root: Path) -> tuple[str, str, list[str]]:
    # The dir exists (so not dir_missing) but cannot be listed: glob() would see nothing, which
    # must not pass for "holds no source". write+search only, so tmp_path cleanup still works.
    _copy_fixture(root / "src")
    (root / "src").chmod(0o300)
    # Control, fail not skip (F9): as root the dir stays readable and the row would prove nothing.
    assert not os.access(root / "src", os.R_OK), "control: test runs as root; dir is still readable"
    return "src", "state", []


def _one_yaml(content: bytes, filename: str = "a.yaml") -> Callable[[Path], tuple[str, str, list[str]]]:
    def build(root: Path) -> tuple[str, str, list[str]]:
        (root / "src").mkdir()
        (root / "src" / filename).write_bytes(content)
        return "src", "state", []

    return build


def _absolute(
    build: Callable[[Path], tuple[str, str, list[str]]],
) -> Callable[[Path], tuple[str, str, list[str]]]:
    # Same layout, but --sources-dir is passed as an absolute path. The loader's message names
    # that path, so echoing it (or the raw exception) would leak it to stderr (F8, mutant M2).
    def build_absolute(root: Path) -> tuple[str, str, list[str]]:
        sources_arg, state_arg, lines = build(root)
        return str(root / sources_arg), state_arg, lines

    return build_absolute


def _dir_named_yaml(root: Path) -> tuple[str, str, list[str]]:
    (root / "src" / "x.yaml").mkdir(parents=True)
    return "src", "state", []


def _valid_plus_malformed(root: Path) -> tuple[str, str, list[str]]:
    # One good source must not hide a broken one: a partial config is a load error, not a status.
    _copy_fixture(root / "src")
    (root / "src" / "zz.yaml").write_text("", encoding="utf-8")
    return "src", "state", []


def _hostile_name(ch: str) -> Callable[[Path], tuple[str, str, list[str]]]:
    def build(root: Path) -> tuple[str, str, list[str]]:
        (root / "src").mkdir()
        text = _FIXTURE_SOURCE.read_text(encoding="utf-8")
        name = _fixture_name()
        # JSON string syntax is valid YAML, so the escape decodes to the raw character on load.
        hostile = text.replace(f"name: {name}", f"name: {json.dumps(name + ch + 'x')}", 1)
        assert hostile != text, "control: the fixture's name line was not found"
        (root / "src" / "a.yaml").write_text(hostile, encoding="utf-8")
        return "src", "state", []

    return build


def _fixture_never_run(state_exists: bool) -> Callable[[Path], tuple[str, str, list[str]]]:
    def build(root: Path) -> tuple[str, str, list[str]]:
        _copy_fixture(root / "src")
        if state_exists:
            (root / "state").mkdir()
        return "src", "state", [_NO_RUNS]

    return build


def _repo_sources_never_run(root: Path) -> tuple[str, str, list[str]]:
    # Exactly what health.yml runs on a fresh runner: the repo's config, a state dir not yet created.
    return str(_REPO_SOURCES), "state", [_NO_RUNS]


def _fixture_healthy(root: Path) -> tuple[str, str, list[str]]:
    _copy_fixture(root / "src")
    _save_checkpoint(root / "state", _fixture_name(), STAGES[-1])
    return "src", "state", [f"{_fixture_name()}: {STAGES[-1]}"]


def _repo_sources_healthy(root: Path) -> tuple[str, str, list[str]]:
    names = list(load_all(_REPO_SOURCES))
    lines = []
    for i, name in enumerate(names):
        stage = STAGES[i % len(STAGES)]
        _save_checkpoint(root / "state", name, stage)
        lines.append(f"{name}: {stage}")
    return str(_REPO_SOURCES), "state", lines


_STATUS_CASES: tuple[_StatusCase, ...] = (
    _StatusCase("empty-dir", "no_sources", _empty_dir),
    _StatusCase("lookalike-files-only", "no_sources", _lookalike_files_only),
    _StatusCase("symlink-to-empty-dir", "no_sources", _symlink_to_empty_dir),
    _StatusCase("orphan-checkpoint", "no_sources", _orphan_checkpoint),
    _StatusCase("hostile-orphan-checkpoint-filename", "no_sources", _hostile_orphan_checkpoint),
    _StatusCase("empty-dir-name-max", "no_sources", _empty_dir_name_max),
    _StatusCase("source-only-in-subdir", "no_sources", _source_only_in_subdir),
    *(
        _StatusCase(f"empty-dir-hostile-name-U+{ord(ch):04X}", "no_sources", _empty_dir_hostile_name(ch))
        for ch in _HOSTILE_EXISTING_NAME_CHARS
    ),
    # #108: these five rows were load_error (exit 1) before the owner gave them their own code.
    _StatusCase("missing-dir", "dir_missing", _missing_dir),
    _StatusCase("dangling-symlink", "dir_missing", _dangling_symlink),
    _StatusCase("sources-path-is-a-file", "dir_missing", _sources_path_is_file),
    _StatusCase("absolute-missing-dir", "dir_missing", _absolute(_missing_dir)),
    _StatusCase("absolute-path-is-a-file", "dir_missing", _absolute(_sources_path_is_file)),
    _StatusCase("symlink-to-file", "dir_missing", _symlink_to_file),
    _StatusCase("parent-is-a-file", "dir_missing", _parent_is_a_file),
    _StatusCase("sources-path-is-a-fifo", "dir_missing", _fifo),
    _StatusCase("missing-name-too-long", "dir_missing", _missing_name_too_long),
    _StatusCase("empty-string-path", "dir_missing", _empty_string_path),
    *(
        _StatusCase(f"missing-hostile-path-U+{ord(ch):04X}", "dir_missing", _missing_hostile_name(ch))
        for ch in _HOSTILE_PATH_CHARS
    ),
    _StatusCase("unreadable-dir", "load_error", _unreadable_dir),
    _StatusCase("empty-yaml", "load_error", _one_yaml(b"")),
    _StatusCase("yaml-is-a-list", "load_error", _one_yaml(b"- a\n- b\n")),
    _StatusCase("invalid-utf8", "load_error", _one_yaml(b"\xff\xfe")),
    _StatusCase("encoded-lone-surrogate", "load_error", _one_yaml(b"name: \xed\xa0\x80\n")),
    _StatusCase("nul-byte", "load_error", _one_yaml(b"name: a\x00b\n")),
    _StatusCase("hostile-yaml-filename", "load_error", _one_yaml(b"", "\x1b[2J x.yaml")),
    _StatusCase("dir-named-yaml", "load_error", _dir_named_yaml),
    _StatusCase("valid-plus-malformed", "load_error", _valid_plus_malformed),
    _StatusCase("absolute-valid-plus-malformed", "load_error", _absolute(_valid_plus_malformed)),
    _StatusCase("absolute-empty-dir", "no_sources", _absolute(_empty_dir)),
    *(
        _StatusCase(f"hostile-name-U+{ord(ch):04X}", "load_error", _hostile_name(ch))
        for ch in _HOSTILE_NAME_CHARS
    ),
    _StatusCase("never-run-empty-state", "never_run", _fixture_never_run(state_exists=True)),
    _StatusCase("never-run-missing-state", "never_run", _fixture_never_run(state_exists=False)),
    _StatusCase("never-run-repo-config-health-yml", "never_run", _repo_sources_never_run),
    _StatusCase("healthy-fixture", "healthy", _fixture_healthy),
    _StatusCase("healthy-repo-config", "healthy", _repo_sources_healthy),
)


def test_status_cases_cover_every_outcome() -> None:
    # Contract for the table below: every outcome has at least one row, both failure
    # (no_sources, dir_missing, load_error) and success (never_run, healthy), and ids are unique.
    kinds = {c.kind for c in _STATUS_CASES}
    assert kinds == {"no_sources", "dir_missing", "load_error", "never_run", "healthy"}, kinds
    ids = [c.id for c in _STATUS_CASES]
    assert len(ids) == len(set(ids)), ids
    assert len(_HOSTILE_NAME_CHARS) == sum(c.id.startswith("hostile-name-") for c in _STATUS_CASES)
    assert {unicodedata.category(ch) for ch in _HOSTILE_NAME_CHARS} >= {"Cc", "Cf", "Zl", "Zp"}
    # #108: every hostile path character has its own dir_missing row, across Cc/Cf/Cs/Zl/Zp.
    assert len(_HOSTILE_PATH_CHARS) == sum(c.id.startswith("missing-hostile-path-") for c in _STATUS_CASES)
    assert {unicodedata.category(ch) for ch in _HOSTILE_PATH_CHARS} >= set(_UNSAFE_CATEGORIES)
    # #108 exit 5: every hostile character also names an existing empty dir (no_sources).
    assert len(_HOSTILE_EXISTING_NAME_CHARS) == sum(
        c.id.startswith("empty-dir-hostile-name-") and c.kind == "no_sources" for c in _STATUS_CASES
    )
    assert {unicodedata.category(ch) for ch in _HOSTILE_EXISTING_NAME_CHARS} >= {"Cc", "Cf", "Zl", "Zp"}
    # Every failure outcome is also fed an absolute --sources-dir, the input whose path the
    # loader's message carries (F8).
    absolute_kinds = {c.kind for c in _STATUS_CASES if c.id.startswith("absolute-")}
    assert absolute_kinds == {"no_sources", "dir_missing", "load_error"}, absolute_kinds


def _assert_no_leak(root: Path, *streams: str) -> None:
    # F8 / attack list A: no absolute path, no raw control or separator characters.
    for stream in streams:
        assert str(root) not in stream and str(root.resolve()) not in stream, stream
        assert _unsafe_chars(stream) == [], (_unsafe_chars(stream), stream)


def _assert_arg_not_echoed(root: Path, sources_arg: str, *results: object) -> None:
    # A hostile (non-printable) or NAME_MAX-sized --sources-dir argument never appears in any
    # output stream, whole or raw (F2, F8, size). Short printable names are covered by the
    # absolute-path rows through _assert_no_leak.
    if sources_arg and (
        not sources_arg.isprintable() or len(sources_arg) >= os.pathconf(root, "PC_NAME_MAX")
    ):
        for result in results:
            for stream in (result.stdout, result.stderr):  # type: ignore[attr-defined]
                assert sources_arg not in stream, ("raw --sources-dir argument echoed", stream[:200])


@pytest.mark.parametrize("case", _STATUS_CASES, ids=[c.id for c in _STATUS_CASES])
def test_status_outcome_by_configuration(
    case: _StatusCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    sources_arg, state_arg, expected_lines = case.build(tmp_path)
    result = runner.invoke(app, ["status", "--sources-dir", sources_arg, "--state-dir", state_arg])

    if case.kind in ("never_run", "healthy"):
        assert result.exit_code == 0, (result.exit_code, result.stdout, result.stderr)
        assert result.stdout.splitlines() == expected_lines
        assert result.stderr == ""
        return

    # Control: refresh, on the same sources dir, defines which failure this row is (G0-1).
    ref = runner.invoke(app, ["refresh", "--all", "--sources-dir", sources_arg])
    assert isinstance(ref.exception, SystemExit), f"control: refresh crashed: {ref.exception!r}"

    # The fix: status never reports nothing-configured or unloadable config as success.
    assert result.exit_code == ref.exit_code, (
        f"status exited {result.exit_code}, refresh exited {ref.exit_code} on the same sources dir; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # An exit through typer.Exit, not a traceback that happens to exit 1.
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert _NO_RUNS not in result.stdout, "status still prints the never-run message"
    _assert_no_leak(tmp_path, result.stdout, result.stderr)

    # Expected codes come from the pinned public table (#108), so a row fails on the exit code
    # it gets, and test_public_exit_code_values_are_pinned ties the table to cli's constants.
    if case.kind == "no_sources":
        expected = PUBLIC_EXIT_CODES["EXIT_NO_SOURCES"]
        assert ref.exit_code == expected, f"refresh exited {ref.exit_code} on an empty sources dir"
        # The exact public code, not just parity with refresh: if the shared path drifted for
        # both commands, parity alone would still pass.
        assert result.exit_code == expected, (result.exit_code, result.stderr)
        assert _NO_SOURCES_MSG in result.stderr, result.stderr
        assert _LOAD_ERROR_MSG not in result.stderr, "no-sources reported as a load error"
        # An existing dir with a hostile or NAME_MAX-long name is never echoed back, by either
        # command (F2, F8); _assert_no_leak alone lets a raw newline through.
        _assert_arg_not_echoed(tmp_path, sources_arg, result, ref)
    elif case.kind == "dir_missing":
        expected = PUBLIC_EXIT_CODES["EXIT_SOURCES_DIR_MISSING"]
        assert ref.exit_code == expected, (
            f"refresh exited {ref.exit_code} on a missing / non-directory sources path; "
            f"stderr={ref.stderr!r}"
        )
        assert result.exit_code == expected, (result.exit_code, result.stderr)
        # Honest message (F8): its own stem, never another outcome's, and it names the fix.
        for stream in (result.stderr, ref.stderr):
            assert cli.MSG_SOURCES_DIR_MISSING in stream, stream
            assert "--sources-dir" in stream, stream
            assert _NO_SOURCES_MSG not in stream, "missing dir reported as no sources"
            assert _LOAD_ERROR_MSG not in stream, "missing dir reported as a load error"
        # refresh's message is new on this path, so it meets the same output bar as status.
        _assert_no_leak(tmp_path, ref.stdout, ref.stderr)
        # A hostile or oversized argument is never echoed back, whole or raw (F2, F8, size).
        if not sources_arg.isprintable() or len(sources_arg) > os.pathconf(tmp_path, "PC_NAME_MAX"):
            for stream in (result.stdout, result.stderr, ref.stdout, ref.stderr):
                assert sources_arg not in stream, "raw --sources-dir argument echoed"
    else:
        expected = PUBLIC_EXIT_CODES["EXIT_LOAD_ERROR"]
        assert ref.exit_code == expected, f"refresh exited {ref.exit_code} on an unloadable sources dir"
        assert result.exit_code == expected, (result.exit_code, result.stderr)
        assert _LOAD_ERROR_MSG in result.stderr, result.stderr
        assert _NO_SOURCES_MSG not in result.stderr, "load error reported as no sources"


def test_status_default_paths_in_empty_dir_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Was test_status_no_checkpoints, which pinned the bug (exit 0, "No runs"). The default
    # --sources-dir is relative to the cwd; in an empty directory nothing is configured, and
    # status must fail the way refresh does on the same defaults.
    monkeypatch.chdir(tmp_path)
    ref = runner.invoke(app, ["refresh", "--all"])
    assert isinstance(ref.exception, SystemExit) and ref.exit_code != 0, "control: refresh succeeded"
    result = runner.invoke(app, ["status"])
    assert result.exit_code == ref.exit_code, (result.exit_code, result.stdout, result.stderr)
    # The default config/sources does not exist in an empty cwd: that is the missing-dir code
    # (#108; it was EXIT_LOAD_ERROR before the owner split it out).
    assert result.exit_code == PUBLIC_EXIT_CODES["EXIT_SOURCES_DIR_MISSING"], (result.exit_code, result.stderr)
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert _NO_RUNS not in result.stdout
    _assert_no_leak(tmp_path, result.stdout, result.stderr)


_HELP_CLAUSE = re.compile(r"(?<![\w.])(\d+) when ([^;]+)")


def _help_clauses(args: list[str]) -> tuple[list[tuple[str, str]], str]:
    result = runner.invoke(app, args)
    assert result.exit_code == 0, (result.exit_code, result.output)
    text = " ".join(strip_ansi(result.output).split())
    return _HELP_CLAUSE.findall(text), text


def test_status_help_documents_exit_codes() -> None:
    # Card #190: the CLI help states which outcome exits which code: never-run (sources
    # configured, no checkpoint) is a legitimate 0, nothing configured is EXIT_NO_SOURCES, an
    # unloadable sources dir is EXIT_LOAD_ERROR. #108 adds EXIT_SOURCES_DIR_MISSING for a sources
    # path that does not exist or is not a directory, and moves EXIT_NO_SOURCES off 2.
    #
    # The numeric values are a public contract (PUBLIC_EXIT_CODES, pinned against cli in
    # test_public_exit_code_values_are_pinned); the expectations here come from that table.
    names = ("EXIT_NO_SOURCES", "EXIT_SOURCES_DIR_MISSING", "EXIT_LOAD_ERROR")
    outcomes: dict[int, tuple[str, ...]] = {0: ("sources are configured",)}
    outcomes.update({PUBLIC_EXIT_CODES[n]: EXIT_CODE_MEANINGS[n] for n in names})
    assert len(outcomes) == 1 + len(names), "two status outcomes share an exit code"

    # Every "<code> when <outcome>" clause in the help, in order. Exactly the four outcome
    # codes, each once and each tied to its own outcome: a dropped, extra or swapped code fails.
    clauses, text = _help_clauses(["status", "--help"])
    assert sorted(int(code) for code, _ in clauses) == sorted(outcomes), (clauses, text)
    for code, clause in clauses:
        for phrase in outcomes[int(code)]:
            assert phrase in clause, (code, phrase, clause)


def test_top_level_help_lists_every_exit_code() -> None:
    # #108: `ingester --help` lists every public exit code once, as "<code> when <meaning>",
    # each clause carrying its own meaning, so 5 and 6 (and 2 = usage only) are discoverable
    # without reading per-command help. 0 may be listed too; no other number may.
    clauses, text = _help_clauses(["--help"])
    by_code = {code: name for name, code in PUBLIC_EXIT_CODES.items()}
    listed = [int(code) for code, _ in clauses if int(code) != 0]
    assert sorted(listed) == sorted(by_code), (clauses, text)
    for code, clause in clauses:
        if int(code) == 0:
            continue
        for phrase in EXIT_CODE_MEANINGS[by_code[int(code)]]:
            assert phrase in clause, (code, phrase, clause)


# ---------------------------------------------------------------------------
# #108 fold-in (owner ruling 2026-10-12): every --sources-dir command classifies the path alike
# ---------------------------------------------------------------------------
#
# `ingester --help` says 6 means a missing --sources-dir for the whole tool, so fetch, sources list
# and audit-license must exit EXIT_SOURCES_DIR_MISSING on exactly the rows status and refresh do.
# Before the fold-in they exited 1 through load_all ("Sources directory not found: <path>"), which
# also echoed the path. A second ruling the same day gives an existing dir that holds no source
# "its own error code ... 'no source'": fetch and audit-license exit EXIT_NO_SOURCES there, like
# status and refresh. `sources list` on that dir still prints "No sources configured." and exits
# 0, fetch of an unsigned source on a valid dir exits EXIT_NO_APPROVED_SOURCES (terms-analysis#93),
# and an unknown source in a configured dir is 1.

_DIR_MISSING_CASES = tuple(c for c in _STATUS_CASES if c.kind == "dir_missing")
_NO_SOURCES_CASES = tuple(c for c in _STATUS_CASES if c.kind == "no_sources")


def _repo_source_name() -> str:
    return next(iter(load_all(_REPO_SOURCES)))  # from the config, not a literal


def _registered_sources_dir_options() -> dict[str, object]:
    """{command name as `--help` shows it: its --sources-dir parameter}, from the real app."""
    found: dict[str, object] = {}

    def walk(command: object, path: tuple[str, ...]) -> None:
        subcommands = getattr(command, "commands", None)
        if subcommands:
            for name, sub in subcommands.items():
                walk(sub, (*path, name))
            return
        for param in getattr(command, "params", []):
            if "--sources-dir" in getattr(param, "opts", []):
                found[" ".join(path)] = param

    walk(typer.main.get_command(app), ())
    return found


def test_sources_dir_commands_table_matches_the_app() -> None:
    # Contract (F10, F12): the table the tests below iterate is every command the app registers
    # with --sources-dir, so a new one cannot skip the classification unnoticed.
    assert set(_registered_sources_dir_options()) == set(SOURCES_DIR_COMMANDS)


@pytest.mark.parametrize("command", SOURCES_DIR_COMMANDS)
def test_sources_dir_option_is_a_plain_str(command: str) -> None:
    # typer's Path type stats the argument itself and raises on NUL or a lone surrogate before
    # the command can classify it (#108). Static pin of the exact click type name; the NUL and
    # surrogate rows of test_every_sources_dir_command_exits_dir_missing are the behaviour.
    param = _registered_sources_dir_options()[command]
    assert type(param.type).__name__ == "StringParamType", (command, type(param.type))  # type: ignore[attr-defined]


@pytest.mark.parametrize("command", SOURCES_DIR_COMMANDS)
@pytest.mark.parametrize("case", _DIR_MISSING_CASES, ids=[c.id for c in _DIR_MISSING_CASES])
def test_every_sources_dir_command_exits_dir_missing(
    case: _StatusCase, command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Missing, dangling symlink, regular file, symlink to a file, file as parent, FIFO, empty
    # string, a name over NAME_MAX, and one row per hostile character class (Cc, Cf, Cs, Zl, Zp,
    # NUL, invalid UTF-8): each is EXIT_SOURCES_DIR_MISSING with the shared message, on every
    # command, and the argument is never echoed.
    monkeypatch.chdir(tmp_path)
    sources_arg, _, _ = case.build(tmp_path)
    result = runner.invoke(app, sources_dir_argv(command, sources_arg, tmp_path, _fixture_name()))
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert result.exit_code == PUBLIC_EXIT_CODES["EXIT_SOURCES_DIR_MISSING"], (
        result.exit_code, result.stdout, result.stderr,
    )
    assert cli.MSG_SOURCES_DIR_MISSING in result.stderr, result.stderr
    assert "--sources-dir" in result.stderr, "the message must name the fix"
    for stream in (result.stdout, result.stderr):
        assert _NO_SOURCES_MSG not in stream, "missing dir reported as no sources"
        assert _LOAD_ERROR_MSG not in stream, "missing dir reported as a load error"
        assert "Unknown source" not in stream, "missing dir reported as an unknown source"
    _assert_no_leak(tmp_path, result.stdout, result.stderr)
    if sources_arg and (
        not sources_arg.isprintable() or len(sources_arg) > os.pathconf(tmp_path, "PC_NAME_MAX")
    ):
        assert sources_arg not in result.stdout + result.stderr, "raw --sources-dir argument echoed"


def _without_sources_dir(argv: list[str]) -> list[str]:
    i = argv.index("--sources-dir")
    return argv[:i] + argv[i + 2:]


@pytest.mark.parametrize("command", SOURCES_DIR_COMMANDS)
def test_default_sources_dir_in_empty_cwd_is_dir_missing(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The default --sources-dir is relative to the cwd; in an empty cwd it names no directory.
    monkeypatch.chdir(tmp_path)
    argv = _without_sources_dir(sources_dir_argv(command, "unused", tmp_path, _fixture_name()))
    result = runner.invoke(app, argv)
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert result.exit_code == PUBLIC_EXIT_CODES["EXIT_SOURCES_DIR_MISSING"], (result.exit_code, result.stderr)
    assert cli.MSG_SOURCES_DIR_MISSING in result.stderr, result.stderr
    _assert_no_leak(tmp_path, result.stdout, result.stderr)


@pytest.mark.parametrize("case", _NO_SOURCES_CASES, ids=[c.id for c in _NO_SOURCES_CASES])
def test_sources_list_existing_empty_dir_still_exits_zero(
    case: _StatusCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Positive control (green today): the owner did not change `sources list` on an existing dir
    # that holds no source. It is not EXIT_NO_SOURCES and not EXIT_SOURCES_DIR_MISSING.
    monkeypatch.chdir(tmp_path)
    sources_arg, _, _ = case.build(tmp_path)
    result = runner.invoke(app, sources_dir_argv("sources list", sources_arg, tmp_path, ""))
    assert result.exit_code == 0, (result.exit_code, result.stdout, result.stderr)
    assert result.stdout.splitlines() == [f"{_NO_SOURCES_MSG}."], result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("command", ["fetch", "audit-license"])
@pytest.mark.parametrize("case", _NO_SOURCES_CASES, ids=[c.id for c in _NO_SOURCES_CASES])
def test_fetch_and_audit_license_on_empty_dir_exit_no_sources(
    case: _StatusCase, command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Owner ruling 2026-10-12: an empty sources folder "has its own error code and yes, that
    # should be 'no source'". Was test_unknown_source_on_existing_dir_still_exits_one, which
    # pinned exit 1 ("Unknown source") here. Every no_sources row status and refresh use (empty,
    # lookalike files only such as README.md, symlink to an empty dir, source only in a subdir,
    # hostile or NAME_MAX-long dir name, absolute path) is EXIT_NO_SOURCES with the shared
    # message, whatever source is named, and the path is never echoed.
    monkeypatch.chdir(tmp_path)
    sources_arg, _, _ = case.build(tmp_path)
    # Control: refresh defines the row as no_sources (the same check status's table makes).
    ref = runner.invoke(app, sources_dir_argv("refresh", sources_arg, tmp_path, ""))
    assert ref.exit_code == PUBLIC_EXIT_CODES["EXIT_NO_SOURCES"], ("control", ref.exit_code, ref.stderr)

    result = runner.invoke(app, sources_dir_argv(command, sources_arg, tmp_path, _fixture_name()))
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert result.exit_code == PUBLIC_EXIT_CODES["EXIT_NO_SOURCES"], (
        command, result.exit_code, result.stdout, result.stderr,
    )
    # Honest message (F8): the no-sources stem status and refresh print, on stderr like
    # status's, and never another outcome's.
    assert _NO_SOURCES_MSG in result.stderr, result.stderr
    for stream in (result.stdout, result.stderr):
        assert "Unknown source" not in stream, "empty dir reported as an unknown source"
        assert _LOAD_ERROR_MSG not in stream, "empty dir reported as a load error"
        assert cli.MSG_SOURCES_DIR_MISSING not in stream, "existing empty dir reported as missing"
    _assert_no_leak(tmp_path, result.stdout, result.stderr)
    _assert_arg_not_echoed(tmp_path, sources_arg, result)


@pytest.mark.parametrize("command", ["fetch", "audit-license"])
def test_unknown_source_on_configured_dir_still_exits_one(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Positive control (green today): a dir that holds a source, but not the one named, is still
    # "unknown source", exit 1. Exit 5 is only for a dir that holds no source at all.
    monkeypatch.chdir(tmp_path)
    _copy_fixture(tmp_path / "src")
    absent = f"{_fixture_name()}-absent"  # derived from the config, so never a configured name
    result = runner.invoke(app, sources_dir_argv(command, "src", tmp_path, absent))
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert result.exit_code == PUBLIC_EXIT_CODES["EXIT_LOAD_ERROR"], (result.exit_code, result.stderr)
    assert "Unknown source" in result.stderr, result.stderr
    assert _NO_SOURCES_MSG not in result.stdout + result.stderr, "configured dir reported as no sources"
    assert cli.MSG_SOURCES_DIR_MISSING not in result.stderr, result.stderr


def test_sources_list_configured_dir_lists_every_source() -> None:
    # Positive control (green today): the str option still loads and lists a real config.
    result = runner.invoke(app, ["sources", "list", "--sources-dir", str(_REPO_SOURCES)])
    assert result.exit_code == 0, (result.exit_code, result.stderr)
    listed = [line.split()[0] for line in result.stdout.splitlines()[2:]]
    assert listed == list(load_all(_REPO_SOURCES)), result.stdout


# The public codes each command's help must list, as "<code> when <meaning>" clauses like
# `status --help`. 0 is listed too (every one of these can succeed); 2 (usage) is tool-wide and
# lives in the top-level help only.
_COMMAND_HELP_CODES: dict[str, tuple[str, ...]] = {
    # Owner ruling 2026-10-12: fetch and audit-license exit EXIT_NO_SOURCES on an empty dir.
    # terms-analysis#93 wired fetch and refresh: EXIT_NOT_WIRED gave way to 7 and 8.
    "fetch": (
        "EXIT_NO_APPROVED_SOURCES",
        "EXIT_SOURCE_FAILED",
        "EXIT_NO_SOURCES",
        "EXIT_SOURCES_DIR_MISSING",
        "EXIT_LOAD_ERROR",
    ),
    "refresh": (
        "EXIT_NO_APPROVED_SOURCES",
        "EXIT_SOURCE_FAILED",
        # terms-analysis#93 Round 11: only refresh gates on the licence baseline.
        "EXIT_LICENSE_BASELINE_MISSING",
        "EXIT_NO_SOURCES",
        "EXIT_SOURCES_DIR_MISSING",
        "EXIT_LOAD_ERROR",
    ),
    "sources list": ("EXIT_SOURCES_DIR_MISSING", "EXIT_LOAD_ERROR"),
    "audit-license": ("EXIT_NO_SOURCES", "EXIT_SOURCES_DIR_MISSING", "EXIT_LOAD_ERROR"),
}


@pytest.mark.parametrize("command", list(_COMMAND_HELP_CODES))
def test_sources_dir_command_help_documents_exit_codes(command: str) -> None:
    names = _COMMAND_HELP_CODES[command]
    clauses, text = _help_clauses([*command.split(), "--help"])
    assert sorted(int(code) for code, _ in clauses) == sorted([0, *(PUBLIC_EXIT_CODES[n] for n in names)]), (
        command, clauses, text,
    )
    by_code = {PUBLIC_EXIT_CODES[n]: n for n in names}
    for code, clause in clauses:
        if int(code) == 0:
            if command == "sources list":
                # Honest about the one outcome that differs from status / refresh: an existing
                # empty dir is a 0 with "No sources configured.", not EXIT_NO_SOURCES.
                assert _NO_SOURCES_MSG in clause, (code, clause)
            if command == "refresh":
                # terms-analysis#93 decision B: an unsigned source fails the run, so 0 needs
                # every configured source approved, not just "every approved source published".
                assert "every configured source is approved" in clause, (code, clause)
            continue
        for phrase in EXIT_CODE_MEANINGS[by_code[int(code)]]:
            assert phrase in clause, (command, code, phrase, clause)


# Command list at the end of a top-level clause, as in "... not wired yet (fetch, refresh)".
_CLAUSE_SCOPE = re.compile(r"\(([^()]*)\)$")


def test_top_level_exit_code_scope_matches_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `ingester --help` must be true of every --sources-dir command. A clause ending in a
    # "(cmd, cmd)" list claims exactly those commands; a clause without one claims all of them.
    # Each code is checked against what the commands actually exit on its trigger.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "empty").mkdir()
    # An empty approvals dir: every repo source is unsigned, so fetch and refresh run nothing and
    # never reach the network (terms-analysis#93). Env, not the repo's approvals, so signing a
    # real approval cannot turn this into a live fetch.
    (tmp_path / "config" / "approvals").mkdir(parents=True)
    monkeypatch.setenv("INGESTER_CONFIG_DIR", str(tmp_path / "config"))
    triggers = {
        "EXIT_SOURCES_DIR_MISSING": str(tmp_path / "missing"),
        "EXIT_NO_SOURCES": str(tmp_path / "empty"),
        "EXIT_NO_APPROVED_SOURCES": str(_REPO_SOURCES),
    }
    # The behaviour the owner ruled (2026-10-12), so the scope cannot be satisfied by moving the
    # help and the code together in the wrong direction: 6 on every command, 5 ("no source") on
    # every command but `sources list` (an empty listing is exit 0), 7 where the pipeline runs
    # and no source is approved (terms-analysis#93; it replaced 3, which no command exits now).
    expected_scope = {
        "EXIT_SOURCES_DIR_MISSING": set(SOURCES_DIR_COMMANDS),
        "EXIT_NO_SOURCES": set(SOURCES_DIR_COMMANDS) - {"sources list"},
        "EXIT_NO_APPROVED_SOURCES": {"fetch", "refresh"},
    }
    assert set(expected_scope) == set(triggers)
    clauses, text = _help_clauses(["--help"])
    by_code = {int(code): clause for code, clause in clauses}
    for name, sources_arg in triggers.items():
        code = PUBLIC_EXIT_CODES[name]
        clause = by_code[code].split(". ")[0].rstrip(". ")
        scope = _CLAUSE_SCOPE.search(clause)
        claimed = set(scope.group(1).split(", ")) if scope else set(SOURCES_DIR_COMMANDS)
        observed = {
            command for command in SOURCES_DIR_COMMANDS
            if runner.invoke(
                app, sources_dir_argv(command, sources_arg, tmp_path, _repo_source_name())
            ).exit_code == code
        }
        assert observed == expected_scope[name], (name, code, sorted(observed))
        assert claimed == observed, (name, code, clause, sorted(observed))


def test_fetch_dry_run_help() -> None:
    result = runner.invoke(app, ["fetch", "--help"])
    assert result.exit_code == 0
    # Assert on the registered parameters, not the help text: the fetch
    # docstring mentions --dry-run, so a text check passes without the option.
    commands = getattr(typer.main.get_command(app), "commands", {})
    assert "fetch" in commands, f"no fetch command: {sorted(commands)}"
    fetch = commands["fetch"]
    options = {opt for param in fetch.params for opt in getattr(param, "opts", [])}
    assert "--dry-run" in options, f"fetch has no --dry-run option: {sorted(options)}"


# ---------------------------------------------------------------------------
# validate-round-trip tests
# ---------------------------------------------------------------------------

_VALID_MANIFEST_YAML = """\
chunk_count: 10
chunker_version: v1.0.0-vendored-from-terms-analysis@abc123
corpus_version: '2026.07.0'
embedder_model: apertus-8b-instruct
embedder_revision: sha256:abc123def456
sources:
- eurlex
"""


def test_validate_round_trip_help() -> None:
    """validate-round-trip --help exits 0."""
    result = runner.invoke(app, ["validate-round-trip", "--help"])
    assert result.exit_code == 0


def test_validate_round_trip_missing_dir(tmp_path: Path) -> None:
    """Non-existent path exits 1."""
    missing = str(tmp_path) + "/no-such-bundle"
    result = runner.invoke(app, ["validate-round-trip", missing])
    assert result.exit_code == 1


def test_validate_round_trip_no_manifest(tmp_path: Path) -> None:
    """Existing dir without MANIFEST.yaml exits 1 and output contains X-Corpus-Mismatch."""
    bundle = tmp_path / "bundle-v1"
    bundle.mkdir()
    result = runner.invoke(app, ["validate-round-trip", str(bundle)])
    assert result.exit_code == 1
    assert "X-Corpus-Mismatch" in result.output


def test_validate_round_trip_valid_bundle(tmp_path: Path) -> None:
    """Valid bundle with MANIFEST.yaml + required subdirs exits 0."""
    bundle = tmp_path / "2026.07.0"
    bundle.mkdir()

    # Write a valid MANIFEST.yaml
    (bundle / "MANIFEST.yaml").write_text(_VALID_MANIFEST_YAML, encoding="utf-8")

    # Create required subdirectories
    for subdir in ("corpus", "index", "provenance"):
        (bundle / subdir).mkdir()

    # terms-analysis is not installed in the test env; the structural-only opt-out is
    # required for exit 0, and the verdict must say the consumer was skipped (terms-analysis#90).
    result = runner.invoke(
        app, ["validate-round-trip", str(bundle), "--allow-missing-consumer"]
    )
    assert result.exit_code == 0, result.output
    assert "VALID (consumer check skipped)" in result.output


# ---------------------------------------------------------------------------
# prune tests
# ---------------------------------------------------------------------------


def _make_bundle(base: Path, name: str) -> Path:
    """Create a minimal bundle directory under base."""
    d = base / name
    d.mkdir(parents=True)
    return d


def test_prune_help() -> None:
    """prune --help exits 0 and mentions expected flags."""
    result = runner.invoke(app, ["prune", "--help"])
    assert result.exit_code == 0
    out = strip_ansi(result.output)
    assert "--dry-run" in out
    assert "--force" in out


def test_prune_no_flags_prints_guidance(tmp_path: Path, monkeypatch: object) -> None:
    """prune with no action flag prints guidance without deleting anything."""
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    out = tmp_path / "out"
    out.mkdir()
    result = runner.invoke(app, ["prune", "--out-dir", str(out)])
    assert result.exit_code == 0
    # With nothing to prune, output should reference "prune" or "bundle"
    assert "prune" in result.output.lower() or "bundle" in result.output.lower()


def test_prune_dry_run(tmp_path: Path, monkeypatch: object) -> None:
    """prune --dry-run lists bundles that would be pruned without deleting them."""
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    out = tmp_path / "out"
    out.mkdir()
    # Create 15 monthly bundles (2024.01 through 2025.03) so bundles outside the
    # 12-month window and not a quarterly anchor qualify for pruning.
    for year, month in [
        (2024, 1), (2024, 2), (2024, 3), (2024, 4),
        (2024, 5), (2024, 6), (2024, 7), (2024, 8),
        (2024, 9), (2024, 10), (2024, 11), (2024, 12),
        (2025, 1), (2025, 2), (2025, 3),
    ]:
        _make_bundle(out, f"{year}.{month:02d}.0")
    result = runner.invoke(app, ["prune", "--out-dir", str(out), "--dry-run"])
    assert result.exit_code == 0
    assert "would prune" in result.output


def test_prune_force_deletes(tmp_path: Path, monkeypatch: object) -> None:
    """prune --force actually removes prunable bundles and keeps the required ones."""
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    out = tmp_path / "out"
    out.mkdir()
    # Same 15-month dataset — bundles outside the retention window will be deleted.
    for year, month in [
        (2024, 1), (2024, 2), (2024, 3), (2024, 4),
        (2024, 5), (2024, 6), (2024, 7), (2024, 8),
        (2024, 9), (2024, 10), (2024, 11), (2024, 12),
        (2025, 1), (2025, 2), (2025, 3),
    ]:
        _make_bundle(out, f"{year}.{month:02d}.0")
    result = runner.invoke(app, ["prune", "--out-dir", str(out), "--force"])
    assert result.exit_code == 0
    assert "pruned" in result.output
    # At least 4 bundles must remain (the 4-most-recent recency rule)
    remaining = [d for d in out.iterdir() if d.is_dir()]
    assert len(remaining) >= 4


# ---------------------------------------------------------------------------
# validate-round-trip — C1: retrieve returns [None, None] exits 1 (Issue #6)
# ---------------------------------------------------------------------------


def test_cli_validate_round_trip_retrieve_returns_none_list_exits_1(tmp_path: Path, monkeypatch: object) -> None:
    """retrieve() returning [None, None] should exit 1 (garbage result)."""
    import types

    # Build a minimal valid bundle directory
    bundle = tmp_path / "2026.07.001"
    bundle.mkdir()
    (bundle / "corpus").mkdir()
    (bundle / "index").mkdir()
    (bundle / "provenance").mkdir()

    # MANIFEST.yaml uses corpus_version (not bundle_version) and requires
    # corpus_version, chunker_version, embedder_model, embedder_revision,
    # sources, chunk_count — matching the Manifest dataclass exactly.
    manifest_yaml = (
        "corpus_version: '2026.07.001'\n"
        "embedder_model: apertus-8b\n"
        "embedder_revision: abc123\n"
        "chunker_version: 1.0.0\n"
        "chunk_count: 0\n"
        "sources: []\n"
    )
    (bundle / "MANIFEST.yaml").write_text(manifest_yaml, encoding="utf-8")

    # Stub a legal_kb module whose retrieve() returns [None, None]
    stub_mod = types.ModuleType("backend")
    stub_app = types.ModuleType("backend.app")
    stub_services = types.ModuleType("backend.app.services")
    stub_legal_kb = types.ModuleType("backend.app.services.legal_kb")

    class _KB:
        def load_from_bundle(self, bundle_dir: Path) -> None:
            pass

        def retrieve(self, query: str) -> list[object]:
            return [None, None]

    stub_legal_kb.LegalKnowledgeBase = _KB  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "backend", stub_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "backend.app", stub_app)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "backend.app.services", stub_services)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "backend.app.services.legal_kb", stub_legal_kb)  # type: ignore[attr-defined]

    result = runner.invoke(app, ["validate-round-trip", str(bundle)])
    assert result.exit_code == 1
    assert "empty result" in result.output


# ---------------------------------------------------------------------------
# audit-license --offline (Issue #7 / Fix C2)
# ---------------------------------------------------------------------------

# Minimal valid source config matching the current SourceConfig schema:
# - name must be lowercase/digits/hyphens/underscores only (no "EUR-Lex")
# - refresh and pipeline are required fields
_EURLEX_YAML = (
    "name: eurlex\n"
    "jurisdiction: EU\n"
    "base_url: https://eur-lex.europa.eu/\n"
    "license:\n"
    "  spdx: CC-BY-4.0\n"
    "  url: https://eur-lex.europa.eu/content/legal-notice/legal-notice.html\n"
    "refresh:\n"
    "  cadence: weekly\n"
    "pipeline:\n"
    "  fetcher: fetchers.eurlex.EurLexFetcher\n"
)


def test_cli_audit_license_offline_reads_baseline(tmp_path: Path) -> None:
    """--offline flag should read stored baseline and skip live fetch."""
    import json

    # Set up minimal sources dir with a source that has a license URL
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir()
    (sources_dir / "eurlex.yaml").write_text(_EURLEX_YAML, encoding="utf-8")

    # Pre-populate a state file
    state_file = tmp_path / "license-hashes.json"
    state_file.write_text(
        json.dumps({
            "eurlex": {
                "hash": "deadbeef12345678",
                "spdx": "CC-BY-4.0",
                "recorded_at": "2026-07-04T00:00:00Z",
            }
        }),
        encoding="utf-8",
    )

    result = runner.invoke(app, [
        "audit-license", "eurlex",
        "--sources-dir", str(sources_dir),
        "--state-file", str(state_file),
        "--offline",
    ])
    assert result.exit_code == 0
    assert "BASELINE_ONLY" in result.output
    assert "deadbeef1234" in result.output


def test_cli_audit_license_offline_no_baseline(tmp_path: Path) -> None:
    """--offline with no stored baseline should exit 0 with NO_BASELINE message."""
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir()
    (sources_dir / "eurlex.yaml").write_text(_EURLEX_YAML, encoding="utf-8")

    state_file = tmp_path / "empty-hashes.json"
    state_file.write_text("{}", encoding="utf-8")

    result = runner.invoke(app, [
        "audit-license", "eurlex",
        "--sources-dir", str(sources_dir),
        "--state-file", str(state_file),
        "--offline",
    ])
    assert result.exit_code == 0
    assert "NO_BASELINE" in result.output
