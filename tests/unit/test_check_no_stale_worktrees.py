"""Acceptance tests for worktree hygiene (terms-analysis#123, gate GM-1).

Card: agent worktrees live under ``.claude/worktrees/``. A worktree there must
never be dragged into a commit, and the repo itself (not one developer's
machine) must say so. Lead ruling 2026-10-10: the issue's original test
(``git worktree list | wc -l == 1``) is wrong, because legitimate per-lane
worktrees exist. The check is:

1. ``.claude/worktrees/`` is ignored by a TRACKED ignore file (``.gitignore``),
   not by ``.git/info/exclude`` or a global excludes file, which a fresh CI
   checkout does not have; and
2. nothing under ``.claude/worktrees/`` is tracked (file, symlink or gitlink).

Mechanism under test: ``scripts/governance/check-no-stale-worktrees.sh``,
following the exit-code convention of the sibling governance scripts
(``scripts/governance/README.md``). The roots it checks come from ONE config
file, ``.claude/governance/worktree-roots.txt`` (F13: paths are config, not
code). Format: one repo-relative root per line, ``#`` comment lines and empty
lines ignored, an optional trailing ``/``. Every other line must be a clean
relative path of ASCII letters, digits, ``.``, ``_``, ``-`` and ``/`` (no
empty, ``.`` or ``..`` segment, no segment starting with ``-``) no longer
than the filesystem's PATH_MAX (``getconf PATH_MAX .``; a longer root cannot
name a real path, and this bound comes from the platform, not a literal).
Anything else, anywhere in the file, fails closed at load before any check.

Exit contract (pinned in ``EXIT`` below; document it in the governance README):
  0  clean; stdout carries the sentinel ``CLEAN <n>``, n = roots checked (F12)
  1  violation: a root is tracked-into or not ignored by a tracked ignore file
  2  config missing, unreadable, empty, a symlink, or a root fails validation
  3  git unavailable, not a repository, or a git query failed (never "clean")

Sandbox tests copy the real script into a throwaway repo and override the root
through that repo's own config (``SANDBOX_ROOT``), so they also prove the
script reads config rather than hard-coding ``.claude/worktrees``. Only the
real-repository and CI-wiring tests name the card's root, because that root
IS the card's acceptance criterion.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_REL = Path("scripts/governance/check-no-stale-worktrees.sh")
CONFIG_REL = Path(".claude/governance/worktree-roots.txt")
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# The card's acceptance criterion (lead ruling on #123), not a config restatement.
CARD_ROOT = ".claude/worktrees"

# Exit-code contract for the script (see module docstring).
EXIT = {"clean": 0, "violation": 1, "config": 2, "git": 3}
SENTINEL = re.compile(rb"^CLEAN (\d+)$", re.MULTILINE)

# Test-harness bound only (not product config): a hung or per-root-forking
# script fails here instead of hanging CI.
HARNESS_TIMEOUT_S = 60

# Sandbox-only root, deliberately different from CARD_ROOT.
SANDBOX_ROOT = "sandbox/trees"
GOOD_IGNORE = f"/{SANDBOX_ROOT}/\n"

# Generated hostile code points (QUALITY-BAR R2): Cc (except NUL and LF, which
# are tested separately), Cf, Cs (as encoded bytes), Zl, Zp, every line break.
_CC = [chr(c) for c in range(0x01, 0x20) if c != 0x0A] + ["\x7f", "\x85"]
_CF = [
    "­", "؜", "᠎", "​", "‌", "‍", "‎", "‏",
    "‪", "‫", "‬", "‭", "‮", "⁠", "⁦", "⁧",
    "⁨", "⁩", "﻿",
]
_LINE_BREAKS = ["\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "]
_LOOKALIKES = ["∕", "／", "⁄", "․", "с", "．"]
# Raw byte sequences that are not valid UTF-8: a stray 0xff, an overlong
# encoding, and a lone UTF-16 surrogate encoded as UTF-8 (CESU-style).
_BAD_UTF8 = [b"\xff", b"\xc0\xaf", b"\xed\xa0\x80"]


def _hostile_inserts() -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    for label, chars in (("Cc", _CC), ("Cf", _CF), ("linebreak", _LINE_BREAKS), ("lookalike", _LOOKALIKES)):
        for ch in chars:
            out.append((f"{label}-U+{ord(ch):04X}", ch.encode("utf-8")))
    out.append(("NUL", b"\x00"))
    for raw in _BAD_UTF8:
        out.append((f"badutf8-{raw.hex()}", raw))
    out.append(("ansi-escape", b"\x1b[31m"))
    return out


HOSTILE_INSERTS = _hostile_inserts()


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


def _clean_env(tmp_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """An env with no inherited git redirection (GIT_DIR, GIT_INDEX_FILE from a
    hook, the user's global config or global excludes)."""
    home_cfg = tmp_path / "_gitcfg"
    home_cfg.mkdir(parents=True, exist_ok=True)
    global_cfg = home_cfg / "global"
    if not global_cfg.exists():
        global_cfg.write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = str(global_cfg)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["XDG_CONFIG_HOME"] = str(home_cfg)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra:
        env.update(extra)
    return env


def _git(repo: Path, env: dict[str, str], *args: str, input: bytes | None = None) -> bytes:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=repo, env=env, input=input, capture_output=True, timeout=HARNESS_TIMEOUT_S, check=False,
    )
    assert proc.returncode == 0, f"git {args[0]} failed in test setup: {proc.stderr[-500:]!r}"
    return proc.stdout


def _require_script() -> Path:
    src = REPO_ROOT / SCRIPT_REL
    if not src.is_file():
        pytest.fail(f"{SCRIPT_REL} does not exist yet (#123)")
    return src


@dataclass
class Sandbox:
    repo: Path
    env: dict[str, str]
    tmp: Path

    def git(self, *args: str, input: bytes | None = None) -> bytes:
        return _git(self.repo, self.env, *args, input=input)

    def blob(self, content: bytes) -> str:
        return self.git("hash-object", "-w", "--stdin", input=content).decode().strip()

    def track_raw(self, paths: list[bytes], mode: str = "100644", content: bytes = b"x\n") -> None:
        """Track arbitrary path bytes through the index (no filesystem names,
        so invalid UTF-8 and control characters work on every OS)."""
        sha = self.blob(content) if mode != "160000" else self.git("rev-parse", "HEAD").decode().strip()
        records = b"".join(f"{mode} {sha}\t".encode() + p + b"\0" for p in paths)
        self.git("update-index", "-z", "--add", "--index-info", input=records)
        self.git("commit", "-q", "--no-verify", "-m", "track")

    def run(self, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["bash", str(self.repo / SCRIPT_REL)], cwd=cwd or self.repo, env=env or self.env,
            capture_output=True, timeout=HARNESS_TIMEOUT_S, check=False,
        )


def _make_sandbox(tmp_path: Path, config: bytes | None, gitignore: str = GOOD_IGNORE) -> Sandbox:
    src = _require_script()
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    env = _clean_env(tmp_path)
    _git(repo, env, "init", "-q", "-b", "main")
    (repo / SCRIPT_REL).parent.mkdir(parents=True)
    shutil.copy2(src, repo / SCRIPT_REL)
    if config is not None:
        (repo / CONFIG_REL).parent.mkdir(parents=True)
        (repo / CONFIG_REL).write_bytes(config)
    (repo / ".gitignore").write_text(gitignore, encoding="utf-8")
    (repo / "README").write_text("sandbox\n", encoding="utf-8")
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "-q", "--no-verify", "-m", "base")
    return Sandbox(repo=repo, env=env, tmp=tmp_path)


def _default_config() -> bytes:
    return f"# sandbox roots\n{SANDBOX_ROOT}/\n".encode()


def _assert_output_safe(proc: subprocess.CompletedProcess[bytes], sb: Sandbox, hostile: bytes | None = None) -> None:
    """F2/F8: output is valid UTF-8, carries no control/format/separator
    characters, no raw hostile bytes and no absolute sandbox path."""
    for name, raw in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            pytest.fail(f"{name} is not valid UTF-8 (raw bytes echoed): {exc}")
        bad = sorted({f"U+{ord(c):04X}" for c in text if c not in "\n\t" and unicodedata.category(c) in {"Cc", "Cf", "Cs", "Zl", "Zp"}})
        assert not bad, f"{name} echoes unsafe characters {bad}"
        if hostile is not None and hostile.strip():
            assert hostile not in raw, f"{name} echoes the hostile bytes {hostile!r} raw"
        for p in {str(sb.repo), os.path.realpath(sb.repo), str(sb.tmp), os.path.realpath(sb.tmp)}:
            assert p not in text, f"{name} leaks an absolute path"


def _assert_clean(proc: subprocess.CompletedProcess[bytes], roots: int) -> None:
    assert proc.returncode == EXIT["clean"], f"want exit 0, got {proc.returncode}: {proc.stderr[-400:]!r}"
    m = SENTINEL.findall(proc.stdout)
    assert m == [str(roots).encode()], f"want sentinel 'CLEAN {roots}', stdout={proc.stdout[-300:]!r}"


def _assert_exit(proc: subprocess.CompletedProcess[bytes], kind: str) -> None:
    assert proc.returncode == EXIT[kind], (
        f"want exit {EXIT[kind]} ({kind}), got {proc.returncode}; stdout={proc.stdout[-300:]!r} stderr={proc.stderr[-300:]!r}"
    )
    assert not SENTINEL.search(proc.stdout), "a failing run must not print the CLEAN sentinel"


# --------------------------------------------------------------------------
# 1. Real repository: the card's acceptance criteria
# --------------------------------------------------------------------------


def _parse_config_roots(raw: bytes) -> list[str]:
    roots = []
    for line in raw.decode("utf-8").split("\n"):
        if line == "" or line.startswith("#"):
            continue
        roots.append(line.rstrip("/"))
    return roots


def test_real_repo_ignores_card_root_through_tracked_gitignore(tmp_path: Path) -> None:
    """`git check-ignore .claude/worktrees/x` succeeds (the brief's literal
    probe), the directory itself is ignored too, and the rule lives in a
    tracked file (so a fresh CI clone has it)."""
    for probe in (f"{CARD_ROOT}/x", f"{CARD_ROOT}/"):
        _assert_ignored_by_tracked_rule(tmp_path, probe)


def _assert_ignored_by_tracked_rule(tmp_path: Path, probe: str) -> None:
    env = _clean_env(tmp_path)
    proc = subprocess.run(
        ["git", "check-ignore", "-v", "-z", "--no-index", "--stdin"],
        cwd=REPO_ROOT, env=env, input=probe.encode() + b"\0", capture_output=True,
        timeout=HARNESS_TIMEOUT_S, check=False,
    )
    assert proc.returncode == 0, f"{probe} is not ignored (#123): {proc.stderr[-300:]!r}"
    source, _line, pattern, _path = proc.stdout.split(b"\0")[:4]
    assert not pattern.startswith(b"!"), f"{probe} is matched only by a negation {pattern!r}"
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", source.decode()],
        cwd=REPO_ROOT, env=env, capture_output=True, timeout=HARNESS_TIMEOUT_S, check=False,
    )
    assert tracked.returncode == 0, f"the ignore rule for {CARD_ROOT} comes from an untracked file {source!r}"


def test_real_repo_tracks_nothing_under_card_root(tmp_path: Path) -> None:
    """Green today: nothing under .claude/worktrees is tracked (control)."""
    out = _git(REPO_ROOT, _clean_env(tmp_path), "ls-files", "-z", "--stage", "--", CARD_ROOT)
    assert out == b"", f"tracked under {CARD_ROOT}: {out[:300]!r}"


def test_real_config_lists_card_root() -> None:
    cfg = REPO_ROOT / CONFIG_REL
    assert cfg.is_file() and not cfg.is_symlink(), f"{CONFIG_REL} must exist as a regular file (#123)"
    assert CARD_ROOT in _parse_config_roots(cfg.read_bytes()), f"{CONFIG_REL} does not list {CARD_ROOT}"


def test_real_repo_script_reports_clean(tmp_path: Path) -> None:
    script = _require_script()
    roots = _parse_config_roots((REPO_ROOT / CONFIG_REL).read_bytes())
    proc = subprocess.run(
        ["bash", str(script)], cwd=REPO_ROOT, env=_clean_env(tmp_path),
        capture_output=True, timeout=HARNESS_TIMEOUT_S, check=False,
    )
    _assert_clean(proc, len(roots))


# --------------------------------------------------------------------------
# 2. Repository-state vectors (block and allow per rule)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StateVector:
    id: str
    rule: str  # "tracked" | "ignored"
    kind: str  # "block" | "allow"
    setup: Callable[[Sandbox], dict[str, str] | None]
    gitignore: str = GOOD_IGNORE
    # Each block row names the true cause: "tracked" or "not ignored".
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _noop(sb: Sandbox) -> None:
    return None


def _live_worktree(sb: Sandbox) -> None:
    sb.git("worktree", "add", "-q", "-b", "lane-a", f"{SANDBOX_ROOT}/lane-a")
    (sb.repo / SANDBOX_ROOT / "lane-a" / "scratch.txt").write_text("untracked\n", encoding="utf-8")


def _untracked_files(sb: Sandbox) -> None:
    d = sb.repo / SANDBOX_ROOT / "agent-1"
    d.mkdir(parents=True)
    (d / "notes.md").write_text("x\n", encoding="utf-8")


def _sibling_prefix_tracked(sb: Sandbox) -> None:
    sb.track_raw([f"{SANDBOX_ROOT}.bak/x".encode(), f"{SANDBOX_ROOT}-old".encode()])


def _lookalike_sibling_tracked(sb: Sandbox) -> None:
    parent, leaf = SANDBOX_ROOT.rsplit("/", 1)
    sb.track_raw([f"{parent}/{leaf}∕x".encode(), f"{parent}/{leaf}／x".encode()])


def _force_added_file(sb: Sandbox) -> None:
    sb.track_raw([f"{SANDBOX_ROOT}/agent-1/src/schema.py".encode()])


def _root_as_file(sb: Sandbox) -> None:
    sb.track_raw([SANDBOX_ROOT.encode()])


def _root_as_symlink(sb: Sandbox) -> None:
    sb.track_raw([SANDBOX_ROOT.encode()], mode="120000", content=b"../elsewhere")


def _gitlink_under_root(sb: Sandbox) -> None:
    sb.track_raw([f"{SANDBOX_ROOT}/lane-a".encode()], mode="160000")


def _info_exclude_only(sb: Sandbox) -> None:
    exclude = Path(sb.git("rev-parse", "--git-path", "info/exclude").decode().strip())
    exclude = exclude if exclude.is_absolute() else sb.repo / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(f"/{SANDBOX_ROOT}/\n", encoding="utf-8")


def _global_excludes_only(sb: Sandbox) -> dict[str, str]:
    ignore = sb.tmp / "global-ignore"
    ignore.write_text(f"{SANDBOX_ROOT}/\n", encoding="utf-8")
    Path(sb.env["GIT_CONFIG_GLOBAL"]).write_text(f"[core]\n\texcludesFile = {ignore}\n", encoding="utf-8")
    return sb.env


def _untracked_nested_gitignore(sb: Sandbox) -> None:
    parent, leaf = SANDBOX_ROOT.rsplit("/", 1)
    (sb.repo / parent).mkdir(parents=True, exist_ok=True)
    (sb.repo / parent / ".gitignore").write_text(f"/{leaf}/\n", encoding="utf-8")


def _live_worktree_and_tracked(sb: Sandbox) -> None:
    _force_added_file(sb)


STATE_VECTORS: list[StateVector] = [
    # allow
    StateVector("ignored_root_absent", "ignored", "allow", _noop),
    StateVector("ignored_with_live_lane_worktree", "ignored", "allow", _live_worktree),
    StateVector("ignored_with_untracked_files", "ignored", "allow", _untracked_files),
    StateVector("ignored_via_parent_directory", "ignored", "allow", _noop, gitignore=f"/{SANDBOX_ROOT.split('/')[0]}/\n"),
    StateVector("ignored_pattern_without_slashes", "ignored", "allow", _noop, gitignore=f"{SANDBOX_ROOT}\n"),
    StateVector("tracked_sibling_prefix_not_under_root", "tracked", "allow", _sibling_prefix_tracked),
    StateVector("tracked_lookalike_slash_not_under_root", "tracked", "allow", _lookalike_sibling_tracked),
    # block: tracked under an ignored root
    StateVector("tracked_force_added_file", "tracked", "block", _force_added_file, reasons=("tracked",)),
    StateVector("tracked_root_as_regular_file", "tracked", "block", _root_as_file, reasons=("tracked",)),
    StateVector("tracked_root_as_symlink", "tracked", "block", _root_as_symlink, reasons=("tracked",)),
    StateVector("tracked_gitlink_under_root", "tracked", "block", _gitlink_under_root, reasons=("tracked",)),
    # block: not ignored by a tracked file
    StateVector("not_ignored_no_entry", "ignored", "block", _noop, gitignore="", reasons=("not ignored",)),
    StateVector("not_ignored_live_lane_worktree", "ignored", "block", _live_worktree, gitignore="", reasons=("not ignored",)),
    StateVector(
        "not_ignored_negated", "ignored", "block", _noop,
        gitignore=f"/{SANDBOX_ROOT}/\n!/{SANDBOX_ROOT}/\n", reasons=("not ignored",),
    ),
    StateVector(
        "not_ignored_negated_glob", "ignored", "block", _noop,
        gitignore=f"/{SANDBOX_ROOT}/**\n!/{SANDBOX_ROOT}/**\n", reasons=("not ignored",),
    ),
    # A rule that happens to cover one probe child name does not ignore the root.
    StateVector(
        "not_ignored_only_one_child_name", "ignored", "block", _noop,
        gitignore=f"/{SANDBOX_ROOT}/x\n", reasons=("not ignored",),
    ),
    StateVector("not_ignored_info_exclude_only", "ignored", "block", _info_exclude_only, gitignore="", reasons=("not ignored",)),
    StateVector("not_ignored_global_excludes_only", "ignored", "block", _global_excludes_only, gitignore="", reasons=("not ignored",)),
    StateVector(
        "not_ignored_untracked_nested_gitignore", "ignored", "block", _untracked_nested_gitignore,
        gitignore="", reasons=("not ignored",),
    ),
    # block: both at once; both causes must be reported, not just the first
    StateVector(
        "tracked_and_not_ignored", "tracked", "block", _live_worktree_and_tracked,
        gitignore="", reasons=("tracked", "not ignored"),
    ),
]


def test_state_vectors_contract() -> None:
    """Every rule has at least one allow and one block row (QUALITY-BAR 9)."""
    rules = {v.rule for v in STATE_VECTORS}
    assert rules == {"tracked", "ignored"}
    for rule in rules:
        kinds = {v.kind for v in STATE_VECTORS if v.rule == rule}
        assert kinds == {"allow", "block"}, f"rule {rule} lacks a {({'allow', 'block'} - kinds)} row"
    assert all(v.reasons for v in STATE_VECTORS if v.kind == "block")
    assert len({v.id for v in STATE_VECTORS}) == len(STATE_VECTORS)


@pytest.mark.parametrize("vec", STATE_VECTORS, ids=lambda v: v.id)
def test_state_vector(vec: StateVector, tmp_path: Path) -> None:
    sb = _make_sandbox(tmp_path, _default_config(), gitignore=vec.gitignore)
    env = vec.setup(sb) or sb.env
    proc = sb.run(env=env)
    _assert_output_safe(proc, sb)
    if vec.kind == "allow":
        _assert_clean(proc, 1)
        return
    _assert_exit(proc, "violation")
    err = (proc.stdout + proc.stderr).decode("utf-8").lower()
    for reason in vec.reasons:
        assert reason in err, f"message does not state the cause '{reason}': {err[-400:]!r}"
    if vec.reasons == ("tracked",):
        assert "not ignored" not in err, "a tracked-only violation is misreported as 'not ignored'"
        assert "git rm --cached" in err, "the tracked message must give the fix (git rm --cached)"
    if vec.reasons == ("not ignored",):
        assert "git rm --cached" not in err, "a not-ignored-only violation is misreported as tracked"
        assert ".gitignore" in err, "the not-ignored message must give the fix (.gitignore)"


def test_script_checks_repo_top_level_from_subdirectory(tmp_path: Path) -> None:
    """F7: run from a subdirectory, the script still checks the repo root, not
    paths relative to the cwd."""
    sb = _make_sandbox(tmp_path, _default_config(), gitignore="")
    sub = sb.repo / "docs" / "deep"
    sub.mkdir(parents=True)
    proc = sb.run(cwd=sub)
    _assert_exit(proc, "violation")
    sb_clean = _make_sandbox(tmp_path / "clean", _default_config())
    sub_clean = sb_clean.repo / "docs"
    sub_clean.mkdir()
    _assert_clean(sb_clean.run(cwd=sub_clean), 1)


def test_script_reads_roots_from_config_not_code(tmp_path: Path) -> None:
    """F13: with a config naming only SANDBOX_ROOT, an unignored CARD_ROOT is
    out of scope (clean), and an unignored SANDBOX_ROOT is a violation."""
    sb = _make_sandbox(tmp_path, _default_config(), gitignore=GOOD_IGNORE)
    (sb.repo / CARD_ROOT / "agent-x").mkdir(parents=True)
    (sb.repo / CARD_ROOT / "agent-x" / "f").write_text("x\n", encoding="utf-8")
    _assert_clean(sb.run(), 1)


# --------------------------------------------------------------------------
# 3. Hostile tracked names, size: output safety and honest counts
# --------------------------------------------------------------------------

_HOSTILE_GROUPS: dict[str, list[tuple[str, bytes]]] = {}
for _label, _raw in HOSTILE_INSERTS:
    if _raw == b"\x00":
        continue  # NUL cannot occur in a git path
    _HOSTILE_GROUPS.setdefault(_label.split("-")[0], []).append((_label, _raw))


@pytest.mark.parametrize("quote_path", ["true", "false"])
@pytest.mark.parametrize("group", sorted(_HOSTILE_GROUPS))
def test_hostile_tracked_names_reported_safely(group: str, quote_path: str, tmp_path: Path) -> None:
    """Tracked names under the root carrying generated hostile bytes: exit 1,
    and nothing raw reaches the terminal even when the user's git config sets
    core.quotePath=false (indirection through global config)."""
    sb = _make_sandbox(tmp_path, _default_config())
    Path(sb.env["GIT_CONFIG_GLOBAL"]).write_text(f"[core]\n\tquotePath = {quote_path}\n", encoding="utf-8")
    vectors = _HOSTILE_GROUPS[group]
    names = [f"{SANDBOX_ROOT}/a".encode() + raw + b"b" for _l, raw in vectors]
    sb.track_raw(names)
    print(f"generated {len(names)} hostile tracked names for group {group}")
    proc = sb.run()
    _assert_exit(proc, "violation")
    for _label, raw in vectors:
        _assert_output_safe(proc, sb, hostile=raw if raw.strip() else None)
    _assert_output_safe(proc, sb)


def test_many_tracked_paths_bounded_output_with_count(tmp_path: Path) -> None:
    """30k tracked paths: exit 1, the true count is reported, and the output
    is truncated rather than echoing every path."""
    count = 30_000
    sb = _make_sandbox(tmp_path, _default_config())
    names = [f"{SANDBOX_ROOT}/agent-{i:05d}/f.py".encode() for i in range(count)]
    sb.track_raw(names)
    proc = sb.run()
    _assert_exit(proc, "violation")
    out = proc.stdout + proc.stderr
    assert re.search(rb"\b%d\b" % count, out), "the message must state how many tracked paths were found"
    assert len(out) < sum(len(n) for n in names) // 10, f"output not truncated: {len(out)} bytes"


def test_huge_tracked_name_truncated(tmp_path: Path) -> None:
    sb = _make_sandbox(tmp_path, _default_config())
    long_leaf = b"a" * (2 * 1024 * 1024)
    sb.track_raw([f"{SANDBOX_ROOT}/".encode() + long_leaf])
    proc = sb.run()
    _assert_exit(proc, "violation")
    out = proc.stdout + proc.stderr
    assert len(out) < len(long_leaf) // 100, f"a 2 MB name is echoed: {len(out)} bytes of output"


def test_many_roots_complete_within_harness_bound(tmp_path: Path) -> None:
    """30k configured roots (F6: no per-root argv growth or process fan-out):
    the run finishes inside the harness bound and attests every root."""
    count = 30_000
    cfg = "".join(f"r{i:05d}/t\n" for i in range(count)).encode()
    sb = _make_sandbox(tmp_path, cfg, gitignore="/r*/t/\n")
    _assert_clean(sb.run(), count)


# --------------------------------------------------------------------------
# 4. Config load: fail closed (F1, F5, F13)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigVector:
    id: str
    kind: str  # "allow" | "block"
    config: bytes | None
    roots: int = 0
    gitignore: str = GOOD_IGNORE


_MID = len(SANDBOX_ROOT) // 2


def _with_insert(raw: bytes) -> bytes:
    root = SANDBOX_ROOT.encode()
    return root[:_MID] + raw + root[_MID:] + b"\n"


CONFIG_VECTORS: list[ConfigVector] = [
    ConfigVector("plain", "allow", f"{SANDBOX_ROOT}\n".encode(), roots=1),
    ConfigVector("trailing_slash", "allow", f"{SANDBOX_ROOT}/\n".encode(), roots=1),
    ConfigVector("comments_and_blank_lines", "allow", f"# roots\n\n{SANDBOX_ROOT}\n\n".encode(), roots=1),
    ConfigVector("no_final_newline", "allow", SANDBOX_ROOT.encode(), roots=1),
    ConfigVector(
        "two_roots", "allow", f"{SANDBOX_ROOT}\nother/lanes\n".encode(), roots=2,
        gitignore=f"{GOOD_IGNORE}/other/lanes/\n",
    ),
    ConfigVector("missing_file", "block", None),
    ConfigVector("empty_file", "block", b""),
    ConfigVector("comments_only", "block", b"# nothing here\n#\n"),
    ConfigVector("whitespace_only_line", "block", b"   \n"),
    ConfigVector("absolute", "block", f"/{SANDBOX_ROOT}\n".encode()),
    ConfigVector("dotdot", "block", b"..\n"),
    ConfigVector("dotdot_segment", "block", b"sandbox/../trees\n"),
    ConfigVector("dot", "block", b".\n"),
    ConfigVector("dot_segment", "block", f"./{SANDBOX_ROOT}\n".encode()),
    ConfigVector("empty_segment", "block", b"sandbox//trees\n"),
    ConfigVector("leading_dash", "block", b"-sandbox/trees\n"),
    ConfigVector("segment_leading_dash", "block", b"sandbox/--output=x\n"),
    ConfigVector("glob_star", "block", b"sandbox/tr*es\n"),
    ConfigVector("glob_question", "block", b"sandbox/tr?es\n"),
    ConfigVector("glob_bracket", "block", b"sandbox/[t]rees\n"),
    ConfigVector("pathspec_magic", "block", b":(exclude)sandbox\n"),
    ConfigVector("backslash", "block", b"sandbox\\trees\n"),
    ConfigVector("tilde_home", "block", b"~/trees\n"),
    ConfigVector("trailing_space", "block", f"{SANDBOX_ROOT} \n".encode()),
    ConfigVector("leading_space", "block", f" {SANDBOX_ROOT}\n".encode()),
    ConfigVector("crlf_line_ending", "block", f"{SANDBOX_ROOT}\r\n".encode()),
    ConfigVector("huge_line_2mb", "block", b"a" * (2 * 1024 * 1024) + b"\n"),
    ConfigVector("valid_then_invalid", "block", f"{SANDBOX_ROOT}\n../escape\n".encode()),
] + [ConfigVector(f"hostile_{label}", "block", _with_insert(raw)) for label, raw in HOSTILE_INSERTS]


def test_config_vectors_contract() -> None:
    kinds = {v.kind for v in CONFIG_VECTORS}
    assert kinds == {"allow", "block"}
    assert len({v.id for v in CONFIG_VECTORS}) == len(CONFIG_VECTORS)
    generated = [v for v in CONFIG_VECTORS if v.id.startswith("hostile_")]
    print(f"generated {len(generated)} hostile config vectors")
    assert len(generated) == len(HOSTILE_INSERTS)


@pytest.mark.parametrize("vec", CONFIG_VECTORS, ids=lambda v: v.id)
def test_config_vector(vec: ConfigVector, tmp_path: Path) -> None:
    sb = _make_sandbox(tmp_path, vec.config, gitignore=vec.gitignore)
    proc = sb.run()
    if vec.kind == "allow":
        _assert_clean(proc, vec.roots)
        _assert_output_safe(proc, sb)
        return
    _assert_exit(proc, "config")
    hostile = None
    if vec.config is not None:
        hostile = vec.config if len(vec.config) > 64 else None
        for label, raw in HOSTILE_INSERTS:
            if vec.id == f"hostile_{label}":
                hostile = raw if raw.strip() else None
    _assert_output_safe(proc, sb, hostile=hostile)
    out = proc.stdout + proc.stderr
    if vec.config is not None and len(vec.config) > 64 * 1024:
        assert len(out) < len(vec.config) // 100, f"config error output is unbounded: {len(out)} bytes"
    assert str(CONFIG_REL).encode() in out, "the config error must name the config file it rejected"


def test_config_symlink_fails_closed(tmp_path: Path) -> None:
    """F7: a config that is a symlink (possibly out of the repo) is refused."""
    sb = _make_sandbox(tmp_path, None)
    target = tmp_path / "outside.txt"
    target.write_bytes(_default_config())
    (sb.repo / CONFIG_REL).parent.mkdir(parents=True, exist_ok=True)
    (sb.repo / CONFIG_REL).symlink_to(target)
    proc = sb.run()
    _assert_exit(proc, "config")
    _assert_output_safe(proc, sb)


def test_config_directory_fails_closed(tmp_path: Path) -> None:
    sb = _make_sandbox(tmp_path, None)
    (sb.repo / CONFIG_REL).mkdir(parents=True)
    _assert_exit(sb.run(), "config")


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0 and not os.environ.get("CI"),
                    reason="root ignores file modes; GitHub-hosted runners are not root")
def test_config_unreadable_fails_closed(tmp_path: Path) -> None:
    sb = _make_sandbox(tmp_path, _default_config())
    cfg = sb.repo / CONFIG_REL
    cfg.chmod(0)
    try:
        _assert_exit(sb.run(), "config")
    finally:
        cfg.chmod(stat.S_IRUSR | stat.S_IWUSR)


# --------------------------------------------------------------------------
# 5. Git failures never read as clean (F4, F5)
# --------------------------------------------------------------------------

_FAKE_GIT = r"""#!/usr/bin/env bash
# Delegates to the real git except for the subcommands named in FAIL_SUBCOMMANDS.
sub=""
args=("$@")
i=0
while [ "$i" -lt "${#args[@]}" ]; do
  a="${args[$i]}"
  case "$a" in
    -c|-C|--git-dir|--work-tree|--namespace) i=$((i + 2)) ;;
    -*) i=$((i + 1)) ;;
    *) sub="$a"; break ;;
  esac
done
if [ "${FAIL_SUBCOMMANDS}" = "ALL" ]; then echo "${FAIL_MESSAGE}" >&2; exit "${FAIL_STATUS}"; fi
case " ${FAIL_SUBCOMMANDS} " in
  *" ${sub} "*) echo "${FAIL_MESSAGE}" >&2; exit "${FAIL_STATUS}" ;;
esac
exec "${REAL_GIT}" "$@"
"""


def _fake_git_env(sb: Sandbox, fail: str, status: str = "128", message: str = "fatal: injected failure") -> dict[str, str]:
    real = shutil.which("git")
    assert real, "git must be installed for this suite"
    bindir = sb.tmp / "fakebin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "git"
    fake.write_text(_FAKE_GIT, encoding="utf-8")
    fake.chmod(0o755)
    env = dict(sb.env)
    env.update({"PATH": f"{bindir}{os.pathsep}{env.get('PATH', '')}", "REAL_GIT": real,
                "FAIL_SUBCOMMANDS": fail, "FAIL_STATUS": status, "FAIL_MESSAGE": message})
    return env


@pytest.mark.parametrize(
    "fail",
    [
        pytest.param("ls-files ls-tree diff-index diff status", id="tracked_listing_fails"),
        pytest.param("check-ignore", id="check_ignore_fatal"),
        pytest.param("ALL", id="every_git_call_fails"),
    ],
)
def test_git_failure_is_exit_git_not_clean(fail: str, tmp_path: Path) -> None:
    """A git query that fails must exit 3, never 0 ("nothing tracked") and
    never 1 (a failed check-ignore is not proof the root is unignored)."""
    sb = _make_sandbox(tmp_path, _default_config())
    proc = sb.run(env=_fake_git_env(sb, fail))
    _assert_exit(proc, "git")
    _assert_output_safe(proc, sb)


def test_git_missing_is_exit_git(tmp_path: Path) -> None:
    """git not installed: the shell's 127 must become exit 3, not a pass."""
    sb = _make_sandbox(tmp_path, _default_config())
    proc = sb.run(env=_fake_git_env(sb, fail="ALL", status="127", message="git: command not found"))
    _assert_exit(proc, "git")
    _assert_output_safe(proc, sb)


def test_not_a_git_repository_is_exit_git(tmp_path: Path) -> None:
    src = _require_script()
    plain = tmp_path / "plain"
    (plain / SCRIPT_REL).parent.mkdir(parents=True)
    shutil.copy2(src, plain / SCRIPT_REL)
    (plain / CONFIG_REL).parent.mkdir(parents=True)
    (plain / CONFIG_REL).write_bytes(_default_config())
    env = _clean_env(tmp_path, {"GIT_CEILING_DIRECTORIES": str(tmp_path)})
    proc = subprocess.run(["bash", str(plain / SCRIPT_REL)], cwd=plain, env=env,
                          capture_output=True, timeout=HARNESS_TIMEOUT_S, check=False)
    assert proc.returncode == EXIT["git"], f"want exit 3 outside a repo, got {proc.returncode}"
    assert not SENTINEL.search(proc.stdout)


# --------------------------------------------------------------------------
# 6. Wiring: CI runs the check on every pull request (F12)
# --------------------------------------------------------------------------


def _workflow_on(doc: dict[Any, Any]) -> Any:
    return doc.get("on", doc.get(True))  # PyYAML reads a bare `on:` key as True


def _wired_steps() -> list[tuple[str, dict[str, Any], dict[str, Any], str]]:
    found = []
    for path in sorted(WORKFLOWS_DIR.glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        on = _workflow_on(doc)
        triggers = on if isinstance(on, dict) else {t: None for t in ([on] if isinstance(on, str) else on or [])}
        if "pull_request" not in triggers:
            continue
        pr = triggers["pull_request"] or {}
        if pr.get("paths") or pr.get("paths-ignore"):
            continue  # a paths filter would let a PR skip the check
        default_shell = ((doc.get("defaults") or {}).get("run") or {}).get("shell")
        for job in (doc.get("jobs") or {}).values():
            if job.get("if"):
                continue
            job_shell = ((job.get("defaults") or {}).get("run") or {}).get("shell")
            for step in job.get("steps") or []:
                if str(SCRIPT_REL) in str(step.get("run") or ""):
                    shell = step.get("shell") or job_shell or default_shell or "bash -e {0}"
                    found.append((path.name, job, step, shell))
    return found


def test_ci_runs_worktree_check_on_every_pull_request(tmp_path: Path) -> None:
    """The step that CI really runs, executed with its effective shell: red on
    a violating sandbox, CLEAN on a clean one."""
    steps = _wired_steps()
    assert steps, f"no pull_request workflow step runs {SCRIPT_REL} (F12: a guard must be wired)"
    for wf, _job, step, shell in steps:
        assert not step.get("if"), f"{wf}: the check step is conditional"
        assert not step.get("continue-on-error"), f"{wf}: the check step may not continue on error"
        assert "{0}" in shell, f"{wf}: unexpected shell {shell!r}"
        for state, gitignore in (("violation", ""), ("clean", GOOD_IGNORE)):
            sb = _make_sandbox(tmp_path / f"{wf}-{state}", _default_config(), gitignore=gitignore)
            body = sb.tmp / "step.sh"
            body.write_text(str(step["run"]), encoding="utf-8")
            argv = shell.replace("{0}", str(body)).split()
            proc = subprocess.run(argv, cwd=sb.repo, env=sb.env, capture_output=True,
                                  timeout=HARNESS_TIMEOUT_S, check=False)
            if state == "violation":
                assert proc.returncode == EXIT["violation"], f"{wf}: step passed on a violating repo ({proc.returncode})"
            else:
                _assert_clean(proc, 1)
