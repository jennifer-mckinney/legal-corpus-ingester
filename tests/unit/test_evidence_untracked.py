"""Acceptance tests: the ingester tracks nothing under ``docs/evidence/``.

Card: legal-corpus-ingester#120 (terms-analysis#384 / #125 import blocker).
Owner decision 2026-10-15
("1a"): this repository must not track any file under ``docs/evidence/``.
Tracked evidence makes the terms-analysis monorepo tree guard refuse the
import. Evidence stays local and untracked, as it already is in
terms-analysis.

Acceptance criteria, each checked against the REAL repository:

1. No tracked path (file, symlink or gitlink) at or under ``docs/evidence/``,
   at any depth, in the index (``git ls-files``) or in ``HEAD``.
2. ``docs/evidence/`` is ignored at every depth and for any file name, by a
   rule in a TRACKED ignore file (a fresh CI checkout has no
   ``.git/info/exclude`` entries and no global excludes), and the matching
   rule is not a negation.
3. Positive control: that rule ignores nothing else. No other tracked file is
   ignored, and neighbouring names (``docs/evidence-notes.md``, an
   ``evidence/`` directory elsewhere) stay visible to ``git add``.

CI runs this module on every pull request (the "Unit tests" step of
``.github/workflows/ci.yml``), so these tests are the regression guard (F12).
Nothing here skips (F9): if git cannot list the repository, the test fails.

The detector and the ignore verdict are proven on sandbox repositories too
(section 2, block and allow vectors per rule), so a green real-repo run means
the state is right, not that the check looked at nothing.
"""
from __future__ import annotations

import os
import subprocess
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The card's acceptance criterion (owner decision "1a"), not a config value.
EVIDENCE_DIR = "docs/evidence"
EVIDENCE_PREFIX = EVIDENCE_DIR.encode() + b"/"

# Test-harness bound only (not product config): a hung git fails the test.
HARNESS_TIMEOUT_S = 60
# How many offending paths an assertion message shows before "and N more".
MESSAGE_SHOW = 20


# --------------------------------------------------------------------------
# Generated probe names (QUALITY-BAR R2): structure forgery, encoding, size
# --------------------------------------------------------------------------

_LINE_BREAKS = ["\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "]
# Cc except NUL (cannot be in a path) and "/" (not Cc, but a separator).
_CC = [chr(c) for c in range(0x01, 0x20)] + ["\x7f"]
_CF = [chr(c) for c in range(0x10000) if unicodedata.category(chr(c)) == "Cf"]
_ZS_LOOKALIKE = [" ", " ", "　", "／", "∕", "е"]  # NBSP, figure, ideographic, fullwidth /, division /, Cyrillic e
# gitignore metacharacters that forge a rule when a name is written into one.
_IGNORE_META = ["!", "#", "*", "?", "[", "]", "\\", " ", "**"]
# Invalid UTF-8: lone byte, encoded lone surrogate, overlong "/".
_BAD_UTF8 = [b"\xff", b"\xed\xa0\x80", b"\xc0\xaf"]
# NAME_MAX on Linux and macOS is 255 bytes; a longest-legal name.
_LONG_NAME = b"x" * 255


def _probe_names() -> list[bytes]:
    names: set[bytes] = {b"probe.md", b"probe.txt", b".hidden", b"2026-10-15-red.txt", _LONG_NAME}
    names.update(b"a" + ch.encode() + b"b" for ch in _LINE_BREAKS + _CC + _CF + _ZS_LOOKALIKE)
    names.update(m.encode() + b"probe" for m in _IGNORE_META)
    names.update(b"probe" + m.encode() for m in _IGNORE_META)
    names.update(b"a" + bad + b"b" for bad in _BAD_UTF8)
    names.update({"café.md".encode(), "café.md".encode()})  # NFC and NFD
    return sorted(names)


PROBE_NAMES = _probe_names()
# The generated set is never silently empty or truncated (R2 "print the count").
_MIN_PROBES = len(set(_LINE_BREAKS + _CC + _CF))


def _evidence_probes() -> list[bytes]:
    """The directory itself, every name at depth 1, every name at depth 4."""
    probes = [EVIDENCE_PREFIX]
    for name in PROBE_NAMES:
        probes.append(EVIDENCE_PREFIX + name)
        probes.append(EVIDENCE_PREFIX + b"a/b/c/" + name)
    return probes


# Paths the rule must NOT swallow. Case variants are left out on purpose: a
# macOS clone sets core.ignorecase=true, so they would differ between a
# laptop and CI.
NEIGHBOUR_PROBES = [
    b"docs/evidence-notes.md",
    b"docs/evidence.md",
    b"docs/evidencex/probe.md",
    b"docs/adr/evidence/probe.md",
    b"evidence/probe.md",
    b"tests/evidence/probe.py",
    b"src/legal_corpus_ingester/evidence/probe.py",
    b"xdocs/evidence/probe.md",
]


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


def _clean_env(tmp_path: Path) -> dict[str, str]:
    """No inherited git redirection: no GIT_DIR/GIT_INDEX_FILE from a hook, no
    user global config, no global excludes file (same as the #123 tests)."""
    cfg_home = tmp_path / "_gitcfg"
    cfg_home.mkdir(parents=True, exist_ok=True)
    global_cfg = cfg_home / "global"
    if not global_cfg.exists():
        global_cfg.write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = str(global_cfg)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["XDG_CONFIG_HOME"] = str(cfg_home)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run_git(repo: Path, env: dict[str, str], *args: str, input: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=repo, env=env, input=input, capture_output=True, timeout=HARNESS_TIMEOUT_S, check=False,
    )


def _git(repo: Path, env: dict[str, str], *args: str, input: bytes | None = None) -> bytes:
    proc = _run_git(repo, env, *args, input=input)
    assert proc.returncode == 0, f"git {args[0]} failed: {proc.stderr[-500:]!r}"
    return proc.stdout


def _show(paths: list[bytes]) -> str:
    """Render paths for an assertion message: repr (no raw control bytes,
    F2/F8), at most MESSAGE_SHOW of them, with an honest count."""
    shown = "\n  ".join(repr(p)[:200] for p in paths[:MESSAGE_SHOW])
    more = len(paths) - MESSAGE_SHOW
    return f"{len(paths)} path(s):\n  {shown}" + (f"\n  ... and {more} more" if more > 0 else "")


def _under_evidence(path: bytes) -> bool:
    return path == EVIDENCE_DIR.encode() or path.startswith(EVIDENCE_PREFIX)


def _tracked(repo: Path, env: dict[str, str], source: str) -> list[bytes]:
    """Every tracked path, from the index or from HEAD, NUL-separated so names
    with line breaks cannot split or forge entries."""
    if source == "index":
        out = _git(repo, env, "ls-files", "-z", "--full-name")
    else:
        out = _git(repo, env, "ls-tree", "-r", "-z", "--full-tree", "--name-only", "HEAD")
    return [p for p in out.split(b"\0") if p]


def _tracked_under_evidence(repo: Path, env: dict[str, str], source: str) -> list[bytes]:
    paths = _tracked(repo, env, source)
    # Positive attestation (F5/F12): the listing really covered the tree, so an
    # empty result below means "nothing under docs/evidence", not "listed nothing".
    assert b".gitignore" in paths, f"the {source} listing does not include .gitignore; the check saw nothing"
    return sorted(p for p in paths if _under_evidence(p))


@dataclass(frozen=True)
class IgnoreMatch:
    path: bytes
    source: bytes  # empty when no rule matched
    pattern: bytes

    @property
    def matched(self) -> bool:
        return self.source != b""


def _ignore_matches(repo: Path, env: dict[str, str], probes: list[bytes]) -> list[IgnoreMatch]:
    """`git check-ignore -v -n -z --no-index`, one record per probe, in order.
    --no-index evaluates tracked paths against the rules as well."""
    proc = _run_git(repo, env, "check-ignore", "-v", "-n", "-z", "--no-index", "--stdin",
                    input=b"".join(p + b"\0" for p in probes))
    # 0 = something matched, 1 = nothing matched; anything else is a git error,
    # never evidence either way.
    assert proc.returncode in (0, 1), f"git check-ignore failed ({proc.returncode}): {proc.stderr[-300:]!r}"
    fields = proc.stdout.split(b"\0")
    assert fields[-1] == b"" and (len(fields) - 1) % 4 == 0, "unparseable check-ignore output"
    records = [fields[i:i + 4] for i in range(0, len(fields) - 1, 4)]
    assert len(records) == len(probes), f"check-ignore answered {len(records)} of {len(probes)} probes"
    return [IgnoreMatch(path=r[3], source=r[0], pattern=r[2]) for r in records]


def _is_tracked_file(repo: Path, env: dict[str, str], source: bytes) -> bool:
    if os.path.isabs(source):
        return False  # .git/info/exclude of a worktree, a global excludes file
    proc = _run_git(repo, env, "ls-files", "--error-unmatch", "--", source.decode("utf-8", "surrogateescape"))
    return proc.returncode == 0


def _not_ignored_by_tracked_rule(repo: Path, env: dict[str, str], probes: list[bytes]) -> list[bytes]:
    """Probes that are NOT ignored by a non-negated rule in a tracked file."""
    misses = []
    tracked_source: dict[bytes, bool] = {}
    for m in _ignore_matches(repo, env, probes):
        if not m.matched or m.pattern.startswith(b"!"):
            misses.append(m.path)
            continue
        if m.source not in tracked_source:
            tracked_source[m.source] = _is_tracked_file(repo, env, m.source)
        if not tracked_source[m.source]:
            misses.append(m.path)
    return misses


def _ignored(repo: Path, env: dict[str, str], probes: list[bytes]) -> list[bytes]:
    """Probes that some non-negated rule ignores (from any source)."""
    return [m.path for m in _ignore_matches(repo, env, probes) if m.matched and not m.pattern.startswith(b"!")]


def _neighbours_and_other_tracked(repo: Path, env: dict[str, str]) -> list[bytes]:
    others = [p for p in _tracked(repo, env, "index") if not _under_evidence(p)]
    assert others, "no tracked files outside docs/evidence; the control saw nothing"
    return NEIGHBOUR_PROBES + others


# --------------------------------------------------------------------------
# 1. Real repository: the card's acceptance criteria
# --------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["index", "HEAD"])
def test_real_repo_tracks_nothing_under_docs_evidence(source: str, tmp_path: Path) -> None:
    """RED at d25c8dc: 7 evidence files are tracked."""
    offenders = _tracked_under_evidence(REPO_ROOT, _clean_env(tmp_path), source)
    assert offenders == [], (
        f"{EVIDENCE_DIR}/ must track nothing (owner decision 2026-10-15 '1a', TA #384); "
        f"tracked in {source}: {_show(offenders)}\n"
        f"fix: git rm -r --cached -- {EVIDENCE_DIR} and add an ignore rule to the tracked .gitignore"
    )


def test_real_repo_ignores_docs_evidence_at_every_depth_through_tracked_gitignore(tmp_path: Path) -> None:
    """RED at d25c8dc: no ignore rule covers docs/evidence/ at all."""
    probes = _evidence_probes()
    assert len(PROBE_NAMES) >= _MIN_PROBES, f"probe generation shrank to {len(PROBE_NAMES)}"
    misses = _not_ignored_by_tracked_rule(REPO_ROOT, _clean_env(tmp_path), probes)
    assert misses == [], (
        f"{EVIDENCE_DIR}/ must be ignored by a non-negated rule in a tracked ignore file "
        f"(checked {len(probes)} generated probes); not ignored: {_show(misses)}"
    )


def test_real_repo_evidence_rule_ignores_nothing_else(tmp_path: Path) -> None:
    """GREEN at d25c8dc (control): no tracked file outside docs/evidence/ and
    no neighbouring name is ignored. Goes red if the fix over-reaches, for
    example an unanchored ``evidence/`` or ``docs/evidence*``."""
    env = _clean_env(tmp_path)
    over = _ignored(REPO_ROOT, env, _neighbours_and_other_tracked(REPO_ROOT, env))
    assert over == [], f"the ignore rules swallow paths outside {EVIDENCE_DIR}/: {_show(over)}"


# --------------------------------------------------------------------------
# 2. Sandbox vectors: the checks above catch each failure and pass each fix
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Sandbox:
    repo: Path
    env: dict[str, str]


def _sandbox(tmp_path: Path, gitignore: bytes) -> Sandbox:
    env = _clean_env(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, env, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_bytes(gitignore)
    (repo / "README").write_text("sandbox\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "-q", "--no-verify", "-m", "base")
    return Sandbox(repo=repo, env=env)


def _force_track(sb: Sandbox, rel: bytes, content: bytes = b"x\n", mode: str = "100644") -> None:
    """Track ``rel`` through the index only (NUL-terminated --index-info), so
    names a filesystem refuses (invalid UTF-8 on APFS) are still testable."""
    blob = _git(sb.repo, sb.env, "hash-object", "-w", "--stdin", input=content).strip()
    _git(sb.repo, sb.env, "update-index", "-z", "--index-info",
         input=mode.encode() + b" " + blob + b"\t" + rel + b"\0")
    _git(sb.repo, sb.env, "commit", "-q", "--no-verify", "-m", "track")


def _track_symlink(sb: Sandbox) -> None:
    _force_track(sb, b"docs/evidence/link", content=b"../guide.md", mode="120000")


def _track_gitlink(sb: Sandbox) -> None:
    head = _git(sb.repo, sb.env, "rev-parse", "HEAD").strip().decode()
    _git(sb.repo, sb.env, "update-index", "--add", "--cacheinfo", f"160000,{head},docs/evidence/sub")
    _git(sb.repo, sb.env, "commit", "-q", "--no-verify", "-m", "gitlink")


def _untracked_ignore_file(sb: Sandbox) -> None:
    # docs/.gitignore exists on disk but is never committed.
    (sb.repo / "docs" / ".gitignore").write_text("/evidence/\n", encoding="utf-8")


def _info_exclude_only(sb: Sandbox) -> None:
    git_dir = Path(_git(sb.repo, sb.env, "rev-parse", "--absolute-git-dir").strip().decode())
    (git_dir / "info").mkdir(exist_ok=True)
    (git_dir / "info" / "exclude").write_text("/docs/evidence/\n", encoding="utf-8")


def _tracked_docs_gitignore(sb: Sandbox) -> None:
    (sb.repo / "docs" / ".gitignore").write_text("/evidence/\n", encoding="utf-8")
    _git(sb.repo, sb.env, "add", "--", "docs/.gitignore")
    _git(sb.repo, sb.env, "commit", "-q", "--no-verify", "-m", "docs ignore")


@dataclass(frozen=True)
class Vector:
    id: str
    rule: str  # "tracked" | "ignored" | "over_ignore"
    kind: str  # "block" | "allow"
    gitignore: bytes = b"/docs/evidence/\n"
    setup: Callable[[Sandbox], None] | None = None


def _track(rel: bytes) -> Callable[[Sandbox], None]:
    return lambda sb: _force_track(sb, rel)


VECTORS = [
    # Criterion 1: tracked paths, at any depth and of any type.
    Vector("tracked_file_depth1", "tracked", "block", setup=_track(b"docs/evidence/a.txt")),
    Vector("tracked_file_depth4", "tracked", "block", setup=_track(b"docs/evidence/a/b/c/d.md")),
    Vector("tracked_newline_name", "tracked", "block", setup=_track(b"docs/evidence/a\nb.md")),
    Vector("tracked_cf_name", "tracked", "block", setup=_track("docs/evidence/a‮b.md".encode())),
    Vector("tracked_bad_utf8_name", "tracked", "block", setup=_track(b"docs/evidence/a\xffb.md")),
    Vector("tracked_symlink", "tracked", "block", setup=_track_symlink),
    Vector("tracked_gitlink", "tracked", "block", setup=_track_gitlink),
    Vector("tracked_file_named_dir", "tracked", "block", gitignore=b"/docs/evidence/\n",
           setup=_track(b"docs/evidence")),
    Vector("tracked_neighbour_name", "tracked", "allow", setup=_track(b"docs/evidence-notes.md")),
    Vector("tracked_nested_evidence_elsewhere", "tracked", "allow", setup=_track(b"docs/adr/evidence/x.md")),
    Vector("tracked_prefix_dir", "tracked", "allow", setup=_track(b"xdocs/evidence/y.md")),
    # Criterion 2: ignored, by a tracked, non-negated rule, for every probe.
    Vector("ignore_missing", "ignored", "block", gitignore=b"# nothing\n"),
    Vector("ignore_only_md", "ignored", "block", gitignore=b"/docs/evidence/*.md\n"),
    Vector("ignore_children_glob", "ignored", "allow", gitignore=b"/docs/evidence/*\n"),
    Vector("ignore_negated_probe", "ignored", "block", gitignore=b"/docs/evidence/*\n!/docs/evidence/probe.md\n"),
    Vector("ignore_untracked_file", "ignored", "block", gitignore=b"# nothing\n", setup=_untracked_ignore_file),
    Vector("ignore_info_exclude_only", "ignored", "block", gitignore=b"# nothing\n", setup=_info_exclude_only),
    Vector("ignore_anchored_root", "ignored", "allow", gitignore=b"/docs/evidence/\n"),
    Vector("ignore_middle_slash", "ignored", "allow", gitignore=b"docs/evidence/\n"),
    Vector("ignore_tracked_docs_gitignore", "ignored", "allow", gitignore=b"# nothing\n",
           setup=_tracked_docs_gitignore),
    # Criterion 3: the rule over-reaches.
    Vector("over_unanchored", "over_ignore", "block", gitignore=b"evidence/\n"),
    Vector("over_prefix_glob", "over_ignore", "block", gitignore=b"/docs/evidence*\n"),
    Vector("over_every_md", "over_ignore", "block", gitignore=b"/docs/evidence/\n*.md\n"),
    Vector("over_anchored_ok", "over_ignore", "allow", gitignore=b"/docs/evidence/\n"),
]


def test_vectors_contract() -> None:
    """Every rule has at least one block and one allow row (QUALITY-BAR 9)."""
    for rule in ("tracked", "ignored", "over_ignore"):
        kinds = {v.kind for v in VECTORS if v.rule == rule}
        assert kinds == {"block", "allow"}, f"rule {rule} has only {sorted(kinds)}"
    ids = [v.id for v in VECTORS]
    assert len(ids) == len(set(ids)), "duplicate vector ids"


def _verdict(sb: Sandbox, rule: str) -> list[bytes]:
    if rule == "tracked":
        return _tracked_under_evidence(sb.repo, sb.env, "index") + _tracked_under_evidence(sb.repo, sb.env, "HEAD")
    if rule == "ignored":
        return _not_ignored_by_tracked_rule(sb.repo, sb.env, _evidence_probes())
    return _ignored(sb.repo, sb.env, _neighbours_and_other_tracked(sb.repo, sb.env))


@pytest.mark.parametrize("vec", VECTORS, ids=[v.id for v in VECTORS])
def test_vector(vec: Vector, tmp_path: Path) -> None:
    """GREEN at d25c8dc: the checks themselves, on sandbox repositories."""
    sb = _sandbox(tmp_path, vec.gitignore)
    if vec.setup is not None:
        vec.setup(sb)
    problems = _verdict(sb, vec.rule)
    if vec.kind == "block":
        assert problems, f"{vec.id}: the {vec.rule} check passed a broken state"
    else:
        assert problems == [], f"{vec.id}: the {vec.rule} check rejected a correct state: {_show(problems)}"


def test_listing_without_attestation_fails_not_passes(tmp_path: Path) -> None:
    """F5: a tree the listing cannot see is a failure, never 'nothing tracked'."""
    sb = _sandbox(tmp_path, b"/docs/evidence/\n")
    _git(sb.repo, sb.env, "rm", "-q", "--cached", "--", ".gitignore")
    with pytest.raises(AssertionError, match="the check saw nothing"):
        _tracked_under_evidence(sb.repo, sb.env, "index")


def test_git_failure_fails_not_passes(tmp_path: Path) -> None:
    """F4: outside a repository the checks fail; they never report clean."""
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    env = dict(_clean_env(tmp_path), GIT_CEILING_DIRECTORIES=str(tmp_path))
    with pytest.raises(AssertionError, match="failed"):
        _tracked_under_evidence(outside, env, "index")
    with pytest.raises(AssertionError, match="failed"):
        _ignore_matches(outside, env, [EVIDENCE_PREFIX])


def test_message_shows_hostile_names_safely(tmp_path: Path) -> None:
    """F2/F8: a hostile tracked name reaches the message escaped and counted."""
    hostile = "docs/evidence/a \x1b[31mb‮.md".encode()
    sb = _sandbox(tmp_path, b"/docs/evidence/\n")
    _force_track(sb, hostile)
    for i in range(MESSAGE_SHOW + 3):
        _force_track(sb, f"docs/evidence/n{i:02d}.txt".encode())
    offenders = _tracked_under_evidence(sb.repo, sb.env, "index")
    msg = _show(offenders)
    assert hostile in offenders
    bad = {c for c in msg if c not in "\n" and unicodedata.category(c) in {"Cc", "Cf", "Zl", "Zp"}}
    assert not bad, f"message carries raw control/format characters {sorted(map(hex, map(ord, bad)))}"
    assert msg.startswith(f"{MESSAGE_SHOW + 4} path(s):"), msg[:80]
    assert msg.endswith("... and 4 more"), msg[-80:]
    assert str(sb.repo) not in msg and os.path.realpath(sb.repo) not in msg
