"""Fix-coder unit tests for the P9 pre-push gate, round 2 (terms-analysis#175).

Covers the resolutions in docs/evidence/2026-10-07-g0-5-fix-r2.md that the
acceptance suite (test_p9_prepush_gate.py) does not pin:

- reviewed-range check: every commit new to the remote must lie in
  range.base..tip; without range.base the tip must be the only new commit
  (security F1, grumpy F2)
- zero-open-findings for BOTH reviewer roles, every non-[] findings value
  refused (grumpy F1, security F3, mutation M3)
- error paths: blob/tree/unknown objects, python3 missing, a validator that
  exits 0 without its OK line (grumpy F3, mutation M22)
- SHA-256 repositories: 64-hex shas, 64-zero deletions, 64-hex range.base
  (grumpy F3, mutations M18/M19)
- the nothing-to-push case exits 0 with a notice (grumpy F4, lead decision c)
- the installer replaces an absolute hooksPath and exits 0 (grumpy F7)
- the shared doc section is byte-identical across repos and states the hook
  header's contract verbatim (grumpy F5)
- the CI-skip marker parses CI=false / CI=0 as "not CI" (grumpy F8)

Sandbox commands reuse the acceptance harness: throwaway main checkout,
linked worktree and local bare remote under tmp_path, isolated HOME and git
config. Only read-only reads of tracked files touch the real repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_p9_gate_fix_r1 import _hook, _pass, _signoff, in_ci
from tests.unit.test_p9_prepush_gate import (  # noqa: F401
    REPO_ROOT,
    Sandbox,
    _git,
    _run,
    _sandbox_env,
    pytestmark,
)

# Shared pin: the same constant appears in the sibling repo's copy of this
# test, so a one-sided edit of the shared doc section fails that repo's suite.
CANONICAL_P9_DOC_SHA256 = "6d4f2d58b8a900d8fdb8e756c4331209bc9a32563c7de690705f6f7bfe8705eb"
SHARED_BEGIN = "<!-- p9-shared:begin -->"
SHARED_END = "<!-- p9-shared:end -->"

ZERO_SHA = "0" * 40


@pytest.fixture
def installed(tmp_path: Path) -> Sandbox:
    sb = Sandbox(tmp_path)
    proc = sb.install("main")
    assert proc.returncode == 0, proc.stderr
    return sb


def _hook_as(
    sb: Sandbox, cwd: Path, remote_name: str, stdin: str, path: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the installed hook as git would, with a chosen remote name / PATH."""
    root = sb.env["P9_SANDBOX_ROOT"]
    real_cwd = Path(cwd).resolve()
    assert real_cwd == Path(root) or Path(root) in real_cwd.parents
    env = dict(sb.env)
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        [str(cwd / ".githooks" / "pre-push"), remote_name, str(sb.remote)],
        cwd=cwd,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _remote_has(sb: Sandbox, sha: str) -> bool:
    proc = _run(
        ["git", "--git-dir", str(sb.remote), "cat-file", "-e", f"{sha}^{{commit}}"],
        sb.root,
        sb.env,
    )
    return proc.returncode == 0


# Reviewed range (security F1, grumpy F2) ----------------------------------


def _range_scene(sb: Sandbox) -> dict[str, str]:
    """seed (on the remote) -> a (unsigned) -> b (tip) on main, plus a signed
    side commit already pushed to the remote that is not an ancestor of b."""
    cwd = sb.main
    seed = sb.remote_sha("main")
    assert seed is not None
    _git(cwd, sb.env, "checkout", "-q", "-b", "side")
    side = sb.commit(cwd, "side.txt")
    _signoff(sb, cwd, side, _pass(side))
    proc = _run(["git", "push", "origin", "side"], cwd, sb.env)
    assert proc.returncode == 0, proc.stderr
    _git(cwd, sb.env, "checkout", "-q", "main")
    a = sb.commit(cwd, "intermediate.txt")
    b = sb.commit(cwd, "tip.txt")
    return {"seed": seed, "side": side, "a": a, "b": b}


def test_signed_tip_over_unsigned_intermediate_without_base_is_refused(
    installed: Sandbox,
) -> None:
    s = _range_scene(installed)
    _signoff(installed, installed.main, s["b"], _pass(s["b"]))

    proc = installed.push(installed.main, "probe-range")

    assert proc.returncode != 0, "an unreviewed intermediate commit was published"
    assert "2 commits are new to origin" in proc.stderr, proc.stderr
    assert installed.remote_sha("probe-range") is None
    assert not _remote_has(installed, s["a"])


def test_range_base_on_remote_covers_the_intermediate_commits(installed: Sandbox) -> None:
    s = _range_scene(installed)
    doc = {**_pass(s["b"]), "range": {"base": s["seed"], "head": s["b"]}}
    _signoff(installed, installed.main, s["b"], doc)

    proc = installed.push(installed.main, "probe-range")

    assert proc.returncode == 0, proc.stderr
    assert installed.remote_sha("probe-range") == s["b"]


@pytest.mark.parametrize(
    ("range_for", "needle"),
    [
        pytest.param(lambda s: None, "2 commits are new to origin", id="range-null"),
        pytest.param(lambda s: {}, "2 commits are new to origin", id="range-without-base"),
        pytest.param(lambda s: "x", "range must be an object", id="range-string"),
        pytest.param(lambda s: [s["seed"]], "range must be an object", id="range-list"),
        pytest.param(lambda s: {"base": "origin/main"}, "range.base must be", id="base-ref-name"),
        pytest.param(lambda s: {"base": s["seed"][:39]}, "range.base must be", id="base-39-hex"),
        pytest.param(lambda s: {"base": s["seed"] + "0"}, "range.base must be", id="base-41-hex"),
        pytest.param(lambda s: {"base": s["seed"].upper()}, "range.base must be", id="base-upper"),
        pytest.param(lambda s: {"base": s["seed"] + "\n"}, "range.base must be", id="base-newline"),
        pytest.param(lambda s: {"base": None}, "range.base must be", id="base-null"),
        pytest.param(lambda s: {"base": 7}, "range.base must be", id="base-int"),
        pytest.param(lambda s: {"base": "e" * 40}, "is not a commit in this", id="base-unknown"),
        pytest.param(lambda s: {"base": s["side"]}, "is not an ancestor", id="base-not-ancestor"),
        pytest.param(lambda s: {"base": s["a"]}, "is not on origin", id="base-not-on-remote"),
        pytest.param(lambda s: {"base": s["b"]}, "is not on origin", id="base-is-the-tip"),
    ],
)
def test_bad_or_uncovering_range_is_refused(installed: Sandbox, range_for, needle: str) -> None:
    s = _range_scene(installed)
    _signoff(installed, installed.main, s["b"], {**_pass(s["b"]), "range": range_for(s)})

    proc = installed.push(installed.main, "probe-range")

    assert proc.returncode != 0
    assert needle in proc.stderr, proc.stderr
    assert installed.remote_sha("probe-range") is None


def test_tip_already_on_remote_needs_no_range(installed: Sandbox) -> None:
    """Boundary: zero commits new to the remote, so the tip-only review covers all."""
    seed = installed.remote_sha("main")
    assert seed is not None
    _signoff(installed, installed.main, seed, _pass(seed))

    proc = _run(
        ["git", "push", "origin", f"{seed}:refs/heads/probe-zero-new"],
        installed.main,
        installed.env,
    )

    assert proc.returncode == 0, proc.stderr
    assert installed.remote_sha("probe-zero-new") == seed


def test_complete_override_replaces_the_range_check(installed: Sandbox) -> None:
    s = _range_scene(installed)
    override = {"used": True, "reason": "hotfix", "authorized_by": "owner"}
    _signoff(installed, installed.main, s["b"], {"head_sha": s["b"], "override": override})

    proc = installed.push(installed.main, "probe-override-range")

    assert proc.returncode == 0, proc.stderr
    assert "P9 OVERRIDE ACTIVE: hotfix (authorized by owner)" in proc.stderr


def _one_new_commit(sb: Sandbox) -> tuple[str, str]:
    seed = sb.remote_sha("main")
    assert seed is not None
    tip = sb.commit(sb.main, "one-new.txt")
    _signoff(sb, sb.main, tip, _pass(tip))
    return seed, tip


def test_glob_remote_name_does_not_borrow_tracking_refs(installed: Sandbox) -> None:
    """`--remotes=orig*` would match origin/main and wave the seed through."""
    _seed, tip = _one_new_commit(installed)
    line = f"refs/heads/main {tip} refs/heads/main {ZERO_SHA}\n"

    proc = _hook_as(installed, installed.main, "orig*", line)

    assert proc.returncode != 0
    assert "2 commits are new to orig*" in proc.stderr, proc.stderr


def test_remote_sha_from_stdin_counts_as_published(installed: Sandbox) -> None:
    seed, tip = _one_new_commit(installed)
    line = f"refs/heads/main {tip} refs/heads/main {seed}\n"

    proc = _hook_as(installed, installed.main, "orig*", line)

    assert proc.returncode == 0, proc.stderr
    assert "signoff OK" in proc.stdout


def test_unknown_remote_sha_is_ignored_not_fatal(installed: Sandbox) -> None:
    _seed, tip = _one_new_commit(installed)
    line = f"refs/heads/main {tip} refs/heads/main {'e' * 40}\n"

    proc = _hook_as(installed, installed.main, "origin", line)

    assert proc.returncode == 0, proc.stderr


# Zero-open-findings for both roles (grumpy F1, security F3, M3) -----------


@pytest.mark.parametrize("role", ["security_engineer", "grumpy_developer"])
@pytest.mark.parametrize(
    "findings",
    [
        pytest.param(["G1 HIGH open"], id="string-finding"),
        pytest.param([{"severity": "NIT"}], id="nit-finding"),
        pytest.param({}, id="empty-object"),
        pytest.param("", id="empty-string"),
        pytest.param(None, id="null"),
        pytest.param("x", id="string"),
        pytest.param(0, id="zero"),
        pytest.param(False, id="false"),
    ],
)
def test_pass_with_findings_other_than_empty_list_is_refused(
    installed: Sandbox, role: str, findings: object
) -> None:
    sha = installed.commit(installed.main, "findings.txt")
    doc = _pass(sha)
    doc[role]["findings"] = findings
    _signoff(installed, installed.main, sha, doc)

    proc = installed.push(installed.main, "probe-findings")

    assert proc.returncode != 0
    assert f"{role} findings must be [] for PASS" in proc.stderr, proc.stderr
    assert installed.remote_sha("probe-findings") is None


def test_pass_with_empty_findings_for_both_roles_is_allowed(installed: Sandbox) -> None:
    sha = installed.commit(installed.main, "no-findings.txt")
    _signoff(installed, installed.main, sha, _pass(sha))

    proc = installed.push(installed.main, "probe-no-findings")

    assert proc.returncode == 0, proc.stderr
    assert installed.remote_sha("probe-no-findings") == sha


@pytest.mark.parametrize("role", ["security_engineer", "grumpy_developer"])
def test_pass_without_findings_key_is_refused_with_reason(installed: Sandbox, role: str) -> None:
    # P9 fix: an absent key cannot be told from "never recorded", so it is refused.
    sha = installed.commit(installed.main, f"missing-{role}.txt")
    doc = _pass(sha)
    del doc[role]["findings"]
    _signoff(installed, installed.main, sha, doc)

    proc = installed.push(installed.main, "probe-missing-findings")

    assert proc.returncode != 0
    assert f"{role} findings must be [] for PASS, got '<missing>'" in proc.stderr, proc.stderr
    assert installed.remote_sha("probe-missing-findings") is None


def test_override_still_replaces_the_findings_key_requirement(installed: Sandbox) -> None:
    sha = installed.commit(installed.main, "override-nokey.txt")
    doc = _pass(sha)
    del doc["security_engineer"]["findings"]
    doc["override"] = {"used": True, "reason": "owner ruling", "authorized_by": "owner"}
    _signoff(installed, installed.main, sha, doc)

    proc = installed.push(installed.main, "probe-override-nokey")

    assert proc.returncode == 0, proc.stderr
    assert "P9 OVERRIDE ACTIVE" in proc.stderr


# Error paths (grumpy F3, M22) ---------------------------------------------


@pytest.mark.parametrize("kind", ["blob", "tree", "unknown"])
def test_non_commit_object_is_refused(installed: Sandbox, kind: str) -> None:
    cwd = installed.main
    if kind == "blob":
        blob = cwd / "probe-blob.txt"
        blob.write_text("blob\n")
        obj = _git(cwd, installed.env, "hash-object", "-w", str(blob))
    elif kind == "tree":
        obj = _git(cwd, installed.env, "rev-parse", "HEAD^{tree}")
    else:
        obj = "e" * 40
    line = f"refs/tags/probe-{kind} {obj} refs/tags/probe-{kind} {ZERO_SHA}\n"

    proc = _hook(installed, cwd, line)

    assert proc.returncode != 0
    assert "does not resolve to a commit" in proc.stderr, proc.stderr


def _tool_dir(tmp_path: Path, python3_script: str | None) -> str:
    """A PATH holding only what the hook needs, plus an optional fake python3."""
    bindir = tmp_path / "toolbin"
    bindir.mkdir()
    for tool in ("bash", "git", "cat", "wc", "tr"):
        found = shutil.which(tool)
        assert found, f"{tool} not on PATH"
        (bindir / tool).symlink_to(found)
    if python3_script is not None:
        fake = bindir / "python3"
        fake.write_text("#!/bin/sh\n" + python3_script + "\n")
        fake.chmod(0o755)
    return str(bindir)


def test_missing_python3_refuses_a_validly_signed_push(installed: Sandbox, tmp_path: Path) -> None:
    _seed, tip = _one_new_commit(installed)
    line = f"refs/heads/main {tip} refs/heads/main {ZERO_SHA}\n"

    proc = _hook_as(installed, installed.main, "origin", line, path=_tool_dir(tmp_path, None))

    assert proc.returncode != 0
    assert "python3" in proc.stderr, proc.stderr
    assert "signoff OK" not in proc.stdout


@pytest.mark.parametrize(
    "script",
    [
        pytest.param("exit 0", id="silent-exit-0"),
        pytest.param("echo OK", id="bare-ok"),
        pytest.param("echo 'OK 2 -'", id="bad-override-flag"),
        pytest.param("echo 'OK 0 origin/main'", id="bad-base"),
        pytest.param("printf 'OK 0 -\\nOK 1 -\\n'", id="two-lines"),
    ],
)
def test_validator_without_an_ok_line_is_refused(
    installed: Sandbox, tmp_path: Path, script: str
) -> None:
    _seed, tip = _one_new_commit(installed)
    line = f"refs/heads/main {tip} refs/heads/main {ZERO_SHA}\n"

    proc = _hook_as(installed, installed.main, "origin", line, path=_tool_dir(tmp_path, script))

    assert proc.returncode != 0
    assert "the signoff validator gave no verdict" in proc.stderr, proc.stderr


@pytest.mark.parametrize(
    ("with_base", "needle"),
    [
        pytest.param(False, "cannot list the commits new to origin", id="no-base"),
        pytest.param(True, "cannot check whether range.base is on origin", id="with-base"),
    ],
)
def test_git_failure_inside_the_range_check_refuses(
    installed: Sandbox, tmp_path: Path, with_base: bool, needle: str
) -> None:
    """A failing `git rev-list` must refuse, never count as "no new commits"."""
    seed, tip = _one_new_commit(installed)
    if with_base:
        _signoff(installed, installed.main, tip, {**_pass(tip), "range": {"base": seed}})
    bindir = Path(_tool_dir(tmp_path, None))
    real_git = os.path.realpath(bindir / "git")
    (bindir / "git").unlink()
    (bindir / "git").write_text(
        f'#!/bin/sh\n[ "$1" = rev-list ] && exit 128\nexec "{real_git}" "$@"\n'
    )
    (bindir / "git").chmod(0o755)
    (bindir / "python3").symlink_to(shutil.which("python3"))
    line = f"refs/heads/main {tip} refs/heads/main {ZERO_SHA}\n"

    proc = _hook_as(installed, installed.main, "origin", line, path=str(bindir))

    assert proc.returncode != 0
    assert needle in proc.stderr, proc.stderr


# Nothing to push (grumpy F4, lead decision c) -----------------------------


def test_empty_stdin_exits_zero_with_a_notice(installed: Sandbox) -> None:
    proc = _hook(installed, installed.main, "")

    assert proc.returncode == 0, proc.stderr
    assert "nothing to push to origin (no refs received); nothing to review" in proc.stdout


def test_up_to_date_push_succeeds_and_chains(installed: Sandbox) -> None:
    proc = _run(
        ["bash", "-c", "git push origin main && echo CHAINED-RAN"],
        installed.main,
        installed.env,
    )

    assert proc.returncode == 0, proc.stderr
    assert "CHAINED-RAN" in proc.stdout


# SHA-256 repositories (grumpy F3, M18/M19) --------------------------------


@pytest.fixture
def sha256_repo(tmp_path: Path) -> dict[str, object]:
    env = _sandbox_env(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    Path(env["GIT_CONFIG_GLOBAL"]).write_text(
        "[user]\n\tname = P9 Test\n\temail = p9@example.invalid\n"
        "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n"
    )
    remote = tmp_path / "remote.git"
    main = tmp_path / "main"
    probe = _run(["git", "init", "-q", "--bare", "--object-format=sha256", str(remote)], tmp_path, env)
    if probe.returncode != 0:
        pytest.skip(f"git without SHA-256 object format: {probe.stderr.strip()}")
    _git(tmp_path, env, "init", "-q", "--object-format=sha256", str(main))
    (main / ".githooks").mkdir()
    shutil.copy2(REPO_ROOT / ".githooks" / "pre-push", main / ".githooks" / "pre-push")
    (main / ".githooks" / "pre-push").chmod(0o755)
    (main / "README").write_text("seed\n")
    _git(main, env, "add", "README")
    _git(main, env, "commit", "-q", "-m", "seed")
    _git(main, env, "remote", "add", "origin", str(remote))
    _git(main, env, "push", "-q", "origin", "main")  # before the hook is wired
    _git(main, env, "config", "core.hooksPath", ".githooks")
    seed = _git(main, env, "rev-parse", "HEAD")
    assert len(seed) == 64
    return {"env": env, "main": main, "remote": remote, "seed": seed}


def _sha256_commit(repo: dict[str, object], name: str, sign: bool, base: str | None = None) -> str:
    main, env = repo["main"], repo["env"]
    (main / name).write_text(name + "\n")
    _git(main, env, "add", name)
    _git(main, env, "commit", "-q", "-m", name)
    sha = _git(main, env, "rev-parse", "HEAD")
    if sign:
        doc = _pass(sha)
        if base is not None:
            doc["range"] = {"base": base}
        reviews = main / ".git" / "reviews"
        reviews.mkdir(exist_ok=True)
        (reviews / f"{sha}.signoff.json").write_text(json.dumps(doc))
    return sha


def _sha256_push(repo: dict[str, object], *refspecs: str) -> subprocess.CompletedProcess[str]:
    return _run(["git", "push", "origin", *refspecs], repo["main"], repo["env"])


def test_sha256_signed_push_is_allowed(sha256_repo: dict[str, object]) -> None:
    sha = _sha256_commit(sha256_repo, "signed.txt", sign=True)

    proc = _sha256_push(sha256_repo, "HEAD:refs/heads/probe-signed")

    assert proc.returncode == 0, proc.stderr
    assert f"signoff OK for HEAD at {sha[:12]}" in proc.stdout


def test_sha256_unsigned_push_is_refused(sha256_repo: dict[str, object]) -> None:
    _sha256_commit(sha256_repo, "unsigned.txt", sign=False)

    proc = _sha256_push(sha256_repo, "HEAD:refs/heads/probe-unsigned")

    assert proc.returncode != 0
    assert "signoff not found" in proc.stderr


def test_sha256_range_base_covers_intermediate(sha256_repo: dict[str, object]) -> None:
    _sha256_commit(sha256_repo, "mid.txt", sign=False)
    _sha256_commit(sha256_repo, "tip.txt", sign=True, base=sha256_repo["seed"])

    proc = _sha256_push(sha256_repo, "HEAD:refs/heads/probe-range")

    assert proc.returncode == 0, proc.stderr


def test_sha256_deletion_is_allowed(sha256_repo: dict[str, object]) -> None:
    remote, env = sha256_repo["remote"], sha256_repo["env"]
    _git(
        remote.parent,
        env,
        "--git-dir",
        str(remote),
        "update-ref",
        "refs/heads/probe-delete",
        sha256_repo["seed"],
    )

    proc = _sha256_push(sha256_repo, ":refs/heads/probe-delete")

    assert proc.returncode == 0, proc.stderr
    assert "no signoff needed" in proc.stdout


# Installer contract (grumpy F7) -------------------------------------------


@pytest.mark.parametrize("where", ["main", "worktree"])
def test_installer_replaces_absolute_hooks_path_and_exits_zero(tmp_path: Path, where: str) -> None:
    sb = Sandbox(tmp_path)
    cwd = sb.checkout(where)
    stale = str(sb.common_dir(cwd) / "hooks")
    _git(cwd, sb.env, "config", "core.hooksPath", stale)

    proc = sb.install(where)

    assert proc.returncode == 0, proc.stderr
    assert _git(cwd, sb.env, "config", "core.hooksPath") == ".githooks"
    assert f"replacing core.hooksPath={stale} with .githooks" in proc.stderr


# Docs are tested against the hook (grumpy F5) -----------------------------


def _shared_doc_block() -> str:
    text = (REPO_ROOT / "automations" / "p9-pre-push.md").read_text(encoding="utf-8")
    assert text.count(SHARED_BEGIN) == 1 and text.count(SHARED_END) == 1
    start = text.index(SHARED_BEGIN)
    end = text.index(SHARED_END) + len(SHARED_END)
    return text[start:end]


def _hook_contract() -> str:
    lines = (REPO_ROOT / ".githooks" / "pre-push").read_text(encoding="utf-8").splitlines()
    first = next(n for n, line in enumerate(lines) if line.startswith("# Enforced contract"))
    opener = next(n for n in range(first, len(lines)) if lines[n].endswith("):"))
    body = []
    for line in lines[opener + 1 :]:
        if line == "#":
            break
        assert line.startswith("#   "), f"unexpected contract line: {line!r}"
        body.append(line[2:])
    assert len(body) >= 4, "hook contract block not found"
    return "\n".join(body)


def test_shared_doc_section_matches_the_cross_repo_pin() -> None:
    digest = hashlib.sha256(_shared_doc_block().encode("utf-8")).hexdigest()
    assert digest == CANONICAL_P9_DOC_SHA256, (
        "the shared section of automations/p9-pre-push.md diverged from the sibling "
        f"repo's (sha256 {digest}); edit both repos together and re-pin"
    )


def test_doc_states_the_hook_contract_verbatim() -> None:
    assert f"```text\n{_hook_contract()}\n```" in _shared_doc_block(), (
        "automations/p9-pre-push.md 'Enforced contract' differs from the hook header"
    )


# CI-skip marker (grumpy F8) -----------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        (" true ", True),
        ("false", False),
        ("0", False),
        ("", False),
        (None, False),
        ("yes", False),
    ],
)
def test_in_ci_parses_only_real_ci_markers(value: str | None, expected: bool) -> None:
    assert in_ci(value) is expected

