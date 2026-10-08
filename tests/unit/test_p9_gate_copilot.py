"""Acceptance tests for the Copilot findings on the P9 pre-push gate (terms-analysis#175).

Findings: docs/evidence/2026-10-07-g0-5-copilot.md (C1, C3, C4).

- C1: what the remote already has must come from the remote's CURRENT
  advertisement for the push URL, never from local refs/remotes/<name>/*,
  which go stale after a server-side delete or a retargeted remote URL. When
  the advertisement cannot be obtained the gate fails closed.
- C4: a remote whose name contains `/` (and a push straight to a URL, where
  git passes the URL as the name) must not lose what the remote already has:
  a new-branch push (remote sha all zeros) over history the remote holds is
  judged against that history.
- C3: the hook is byte-identical to the SIBLING repo's hook as published on
  GitHub, not just to a constant copied into this repo.

Sandbox commands reuse the acceptance harness: throwaway main checkout,
linked worktree and local bare remotes under tmp_path, isolated HOME and git
config. The only network access is the C3 parity fetch from GitHub.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.unit.test_p9_gate_fix_r1 import _pass, _signoff, in_ci
from tests.unit.test_p9_prepush_gate import (  # noqa: F401
    REPO_ROOT,
    Sandbox,
    _git,
    _run,
    pytestmark,
)

ZERO_SHA = "0" * 40
REFUSED = 1  # git push exit status when the pre-push hook refuses


@pytest.fixture
def installed(tmp_path: Path) -> Sandbox:
    sb = Sandbox(tmp_path)
    proc = sb.install("main")
    assert proc.returncode == 0, proc.stderr
    return sb


def _push(sb: Sandbox, remote: str, refspec: str) -> subprocess.CompletedProcess[str]:
    """A real `git push`, so git runs the installed hook with its real arguments."""
    return _run(["git", "push", remote, refspec], sb.main, sb.env)


def _hook_with_url(
    sb: Sandbox, remote_name: str, url: str, stdin: str
) -> subprocess.CompletedProcess[str]:
    """Run the installed hook directly, as git would, with a chosen push URL."""
    root = sb.env["P9_SANDBOX_ROOT"]
    real_cwd = sb.main.resolve()
    assert real_cwd == Path(root) or Path(root) in real_cwd.parents
    return subprocess.run(
        [str(sb.main / ".githooks" / "pre-push"), remote_name, url],
        cwd=sb.main,
        env=sb.env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _bare(sb: Sandbox, name: str) -> Path:
    path = sb.root / name
    _git(sb.root, sb.env, "init", "-q", "--bare", str(path))
    return path


def _ref_on(sb: Sandbox, remote: Path, ref: str) -> str | None:
    proc = _run(
        ["git", "--git-dir", str(remote), "rev-parse", "--verify", "-q", ref], sb.root, sb.env
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def _has_commit(sb: Sandbox, remote: Path, sha: str) -> bool:
    proc = _run(
        ["git", "--git-dir", str(remote), "cat-file", "-e", f"{sha}^{{commit}}"], sb.root, sb.env
    )
    return proc.returncode == 0


def _tracking(sb: Sandbox, ref: str) -> str | None:
    proc = _run(["git", "rev-parse", "--verify", "-q", ref], sb.main, sb.env)
    return proc.stdout.strip() if proc.returncode == 0 else None


# C1: retargeted remote ------------------------------------------------------


def _retargeted(sb: Sandbox) -> dict[str, str]:
    """seed -> a on remote A (pushed, signed, tracked as origin/feature), then
    b on top; origin's URL is retargeted to an EMPTY bare remote B while the
    stale refs/remotes/origin/{main,feature} stay in place."""
    seed = sb.remote_sha("main")
    assert seed is not None
    _git(sb.main, sb.env, "checkout", "-q", "-b", "feature")
    a = sb.commit(sb.main, "feature-a.txt")
    _signoff(sb, sb.main, a, _pass(a))
    proc = _push(sb, "origin", "feature")
    assert proc.returncode == 0, proc.stderr
    b = sb.commit(sb.main, "feature-b.txt")
    empty = _bare(sb, "retarget.git")
    _git(sb.main, sb.env, "remote", "set-url", "origin", str(empty))
    # Precondition: the tracking refs are stale, the new remote has nothing.
    assert _tracking(sb, "refs/remotes/origin/main") == seed
    assert _tracking(sb, "refs/remotes/origin/feature") == a
    assert _ref_on(sb, empty, "refs/heads/main") is None
    return {"seed": seed, "a": a, "b": b, "empty": str(empty)}


def test_retargeted_remote_tip_only_signoff_over_unpublished_ancestry_is_refused(
    installed: Sandbox,
) -> None:
    s = _retargeted(installed)
    _signoff(installed, installed.main, s["b"], _pass(s["b"]))

    proc = _push(installed, "origin", "HEAD:refs/heads/probe-retarget")

    assert proc.returncode == REFUSED, "stale tracking refs vouched for an empty remote"
    assert "3 commits are new to origin" in proc.stderr, proc.stderr
    assert "signoff OK" not in proc.stdout
    empty = Path(s["empty"])
    assert _ref_on(installed, empty, "refs/heads/probe-retarget") is None
    assert not _has_commit(installed, empty, s["a"])


def test_retargeted_remote_range_base_only_on_old_remote_is_refused(installed: Sandbox) -> None:
    s = _retargeted(installed)
    _signoff(installed, installed.main, s["b"], {**_pass(s["b"]), "range": {"base": s["a"]}})

    proc = _push(installed, "origin", "HEAD:refs/heads/probe-retarget-base")

    assert proc.returncode == REFUSED
    assert f"range.base {s['a'][:12]} is not on origin" in proc.stderr, proc.stderr
    assert _ref_on(installed, Path(s["empty"]), "refs/heads/probe-retarget-base") is None


# C1: stale tracking ref after a server-side delete -------------------------


def _deleted_upstream(sb: Sandbox) -> dict[str, str]:
    """seed -> a pushed as `feature` (tracked locally), then deleted on the
    server only; b is committed on top of a. origin/feature still says a."""
    seed = sb.remote_sha("main")
    assert seed is not None
    _git(sb.main, sb.env, "checkout", "-q", "-b", "feature")
    a = sb.commit(sb.main, "deleted-a.txt")
    _signoff(sb, sb.main, a, _pass(a))
    proc = _push(sb, "origin", "feature")
    assert proc.returncode == 0, proc.stderr
    _git(sb.root, sb.env, "--git-dir", str(sb.remote), "update-ref", "-d", "refs/heads/feature")
    b = sb.commit(sb.main, "deleted-b.txt")
    # Precondition: no ref on the server reaches a; the local tracking ref does.
    assert sb.remote_sha("feature") is None
    assert _tracking(sb, "refs/remotes/origin/feature") == a
    return {"seed": seed, "a": a, "b": b}


def test_stale_tracking_ref_after_server_delete_does_not_cover_ancestry(
    installed: Sandbox,
) -> None:
    s = _deleted_upstream(installed)
    _signoff(installed, installed.main, s["b"], _pass(s["b"]))

    proc = _push(installed, "origin", "HEAD:refs/heads/probe-stale")

    assert proc.returncode == REFUSED, "a deleted branch's tracking ref vouched for a"
    assert "2 commits are new to origin" in proc.stderr, proc.stderr
    assert installed.remote_sha("probe-stale") is None


def test_stale_tracking_ref_after_server_delete_is_not_a_valid_range_base(
    installed: Sandbox,
) -> None:
    s = _deleted_upstream(installed)
    _signoff(installed, installed.main, s["b"], {**_pass(s["b"]), "range": {"base": s["a"]}})

    proc = _push(installed, "origin", "HEAD:refs/heads/probe-stale-base")

    assert proc.returncode == REFUSED
    assert f"range.base {s['a'][:12]} is not on origin" in proc.stderr, proc.stderr
    assert installed.remote_sha("probe-stale-base") is None


# C1: fail closed when the advertisement cannot be obtained -----------------


@pytest.mark.parametrize(
    "url_for",
    [
        pytest.param(lambda sb: str(sb.root / "missing.git"), id="missing-path"),
        pytest.param(lambda sb: (sb.root / "missing.git").as_uri(), id="missing-file-url"),
        pytest.param(lambda sb: str(sb.root / "home"), id="directory-not-a-repo"),
    ],
)
def test_unreachable_remote_refuses_a_validly_signed_push(
    installed: Sandbox, url_for: Callable[[Sandbox], str]
) -> None:
    """The tip is the only commit new to origin (origin/main is the seed), so
    today's tracking-ref check passes it; without the advertisement the gate
    cannot know that, and must refuse."""
    seed = installed.remote_sha("main")
    tip = installed.commit(installed.main, "unreachable.txt")
    _signoff(installed, installed.main, tip, _pass(tip))
    assert _tracking(installed, "refs/remotes/origin/main") == seed
    url = url_for(installed)
    line = f"refs/heads/main {tip} refs/heads/main {ZERO_SHA}\n"

    proc = _hook_with_url(installed, "origin", url, line)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "signoff OK" not in proc.stdout
    assert re.search(r"P9 pre-push gate: .*cannot .*origin", proc.stderr), proc.stderr


# C1 / C4 positive controls --------------------------------------------------


def test_tip_only_signoff_for_the_only_new_commit_passes(installed: Sandbox) -> None:
    """Control (green today): one new commit over the remote's main."""
    tip = installed.commit(installed.main, "only-new.txt")
    _signoff(installed, installed.main, tip, _pass(tip))

    proc = _push(installed, "origin", "HEAD:refs/heads/probe-only-new")

    assert proc.returncode == 0, proc.stderr
    assert f"signoff OK for HEAD at {tip[:12]}" in proc.stdout
    assert installed.remote_sha("probe-only-new") == tip


def test_range_base_on_the_remote_passes(installed: Sandbox) -> None:
    """Control (green today): seed (on remote) -> a (unsigned) -> b (base=seed)."""
    seed = installed.remote_sha("main")
    assert seed is not None
    installed.commit(installed.main, "covered-a.txt")
    b = installed.commit(installed.main, "covered-b.txt")
    _signoff(installed, installed.main, b, {**_pass(b), "range": {"base": seed}})

    proc = _push(installed, "origin", "HEAD:refs/heads/probe-covered")

    assert proc.returncode == 0, proc.stderr
    assert installed.remote_sha("probe-covered") == b


def test_missing_tracking_refs_do_not_hide_what_the_remote_advertises(
    installed: Sandbox,
) -> None:
    """The advertisement, not refs/remotes/*, says what the remote has: with
    no tracking refs at all, a tip-only signoff for the one new commit passes."""
    tip = installed.commit(installed.main, "no-tracking.txt")
    _signoff(installed, installed.main, tip, _pass(tip))
    _git(installed.main, installed.env, "update-ref", "-d", "refs/remotes/origin/main")
    assert _tracking(installed, "refs/remotes/origin/main") is None

    proc = _push(installed, "origin", "HEAD:refs/heads/probe-no-tracking")

    assert proc.returncode == 0, proc.stderr
    assert installed.remote_sha("probe-no-tracking") == tip


def test_retargeted_remote_that_holds_the_history_passes(installed: Sandbox) -> None:
    """Control for C1: retarget origin to a remote that HAS seed and a (but
    under different branch names); the tip-only signoff for b passes."""
    seed = installed.remote_sha("main")
    assert seed is not None
    a = installed.commit(installed.main, "mirror-a.txt")
    _signoff(installed, installed.main, a, _pass(a))
    mirror = _bare(installed, "mirror.git")
    _git(installed.main, installed.env, "remote", "add", "seeder", str(mirror))
    # Seed the mirror through a second remote (hook bypassed: this is setup,
    # not the push under test) so origin's tracking refs never see it.
    proc = _run(
        ["git", "push", "--no-verify", "seeder", "HEAD:refs/heads/elsewhere"],
        installed.main,
        installed.env,
    )
    assert proc.returncode == 0, proc.stderr
    b = installed.commit(installed.main, "mirror-b.txt")
    _signoff(installed, installed.main, b, _pass(b))
    _git(installed.main, installed.env, "remote", "set-url", "origin", str(mirror))

    proc = _push(installed, "origin", "HEAD:refs/heads/probe-mirror")

    assert proc.returncode == 0, proc.stderr
    assert _ref_on(installed, mirror, "refs/heads/probe-mirror") == b


# C4: remote names containing `/`, and pushes straight to a URL -------------

SLASH_REMOTE = "team/origin"


def _slash_remote(sb: Sandbox) -> str:
    """Add `team/origin` for the seeded bare remote and fetch it, so its
    tracking refs (refs/remotes/team/origin/main) exist."""
    _git(sb.main, sb.env, "remote", "add", SLASH_REMOTE, str(sb.remote))
    _git(sb.main, sb.env, "fetch", "-q", SLASH_REMOTE)
    seed = sb.remote_sha("main")
    assert seed is not None
    assert _tracking(sb, f"refs/remotes/{SLASH_REMOTE}/main") == seed
    return seed


def test_slash_remote_new_branch_tip_only_signoff_passes(installed: Sandbox) -> None:
    _slash_remote(installed)
    tip = installed.commit(installed.main, "slash-only-new.txt")
    _signoff(installed, installed.main, tip, _pass(tip))

    proc = _push(installed, SLASH_REMOTE, "HEAD:refs/heads/probe-slash-new")

    assert proc.returncode == 0, proc.stderr
    assert installed.remote_sha("probe-slash-new") == tip


def test_slash_remote_new_branch_range_base_on_remote_is_accepted(installed: Sandbox) -> None:
    seed = _slash_remote(installed)
    installed.commit(installed.main, "slash-a.txt")
    b = installed.commit(installed.main, "slash-b.txt")
    _signoff(installed, installed.main, b, {**_pass(b), "range": {"base": seed}})

    proc = _push(installed, SLASH_REMOTE, "HEAD:refs/heads/probe-slash-base")

    assert proc.returncode == 0, proc.stderr
    assert installed.remote_sha("probe-slash-base") == b


def test_slash_remote_new_branch_unreviewed_ancestry_is_refused(installed: Sandbox) -> None:
    """Only a and b are new to team/origin (the seed is there), so the
    refusal must count exactly 2, not count the remote's history as new."""
    _slash_remote(installed)
    a = installed.commit(installed.main, "slash-unreviewed-a.txt")
    b = installed.commit(installed.main, "slash-unreviewed-b.txt")
    _signoff(installed, installed.main, b, _pass(b))

    proc = _push(installed, SLASH_REMOTE, "HEAD:refs/heads/probe-slash-unreviewed")

    assert proc.returncode == REFUSED
    assert f"2 commits are new to {SLASH_REMOTE}" in proc.stderr, proc.stderr
    assert installed.remote_sha("probe-slash-unreviewed") is None
    assert not _has_commit(installed, installed.remote, a)


def test_push_to_a_bare_url_judges_against_that_remote(installed: Sandbox) -> None:
    """`git push <url>` passes the URL as the remote name: still one new commit."""
    tip = installed.commit(installed.main, "url-only-new.txt")
    _signoff(installed, installed.main, tip, _pass(tip))

    proc = _push(installed, str(installed.remote), "HEAD:refs/heads/probe-url-new")

    assert proc.returncode == 0, proc.stderr
    assert installed.remote_sha("probe-url-new") == tip


# C3: cross-repo parity against the sibling's published hook -----------------

SIBLINGS = {
    "terms-analysis": "legal-corpus-ingester",
    "legal-corpus-ingester": "terms-analysis",
}
THIS_REPO = "legal-corpus-ingester"
SIBLING_REPO = SIBLINGS[THIS_REPO]
SIBLING_REF_ENV = "P9_SIBLING_REF"
OPT_OUT_ENV = "P9_SKIP_SIBLING_PARITY"
RAW_URL = "https://raw.githubusercontent.com/jennifer-mckinney/{repo}/{ref}/.githooks/pre-push"
# A git branch or tag name as GitHub serves it raw: no "..", no leading "/" or "-".
SAFE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")

Opener = Callable[[str, float], bytes]


class ParityError(Exception):
    """The sibling's hook could not be fetched; parity cannot be established."""


def _urlopen(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed https host
        return resp.read()


def sibling_hook_url(ref: str) -> str:
    if not SAFE_REF.fullmatch(ref) or ".." in ref or ref.endswith("/"):
        raise ParityError(f"{SIBLING_REF_ENV}={ref!r} is not a usable branch or tag name")
    return RAW_URL.format(repo=SIBLING_REPO, ref=ref)


def sibling_hook_digest(ref: str, opener: Opener = _urlopen) -> str:
    """sha256 of the sibling's .githooks/pre-push at `ref`; ParityError on any failure."""
    url = sibling_hook_url(ref)
    try:
        body = opener(url, 20.0)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ParityError(f"cannot fetch {url}: {exc}") from exc
    if not body.startswith(b"#!"):
        raise ParityError(f"{url} did not return a hook script ({len(body)} bytes)")
    return hashlib.sha256(body).hexdigest()


def parity_skip_reason(env: dict[str, str]) -> str | None:
    """None means run. Locally the check runs unless explicitly opted out;
    in CI the opt-out is ignored."""
    if in_ci(env.get("CI")):
        return None
    if env.get(OPT_OUT_ENV, "").strip() == "1":
        return f"{OPT_OUT_ENV}=1: cross-repo hook parity explicitly skipped on this machine"
    return None


def test_pre_push_hook_matches_the_sibling_repos_published_hook() -> None:
    reason = parity_skip_reason(dict(os.environ))
    if reason is not None:
        pytest.skip(reason)
    ref = os.environ.get(SIBLING_REF_ENV, "").strip() or "main"
    local = hashlib.sha256((REPO_ROOT / ".githooks" / "pre-push").read_bytes()).hexdigest()

    try:
        remote = sibling_hook_digest(ref)
    except ParityError as exc:
        pytest.fail(f"cross-repo hook parity cannot be established (fail closed): {exc}")

    assert local == remote, (
        f".githooks/pre-push (sha256 {local}) differs from {SIBLING_REPO}@{ref} "
        f"(sha256 {remote}); land the same hook in both repos, or point "
        f"{SIBLING_REF_ENV} at the sibling's PR branch"
    )


def _raises(exc: BaseException) -> Opener:
    def opener(url: str, timeout: float) -> bytes:
        raise exc

    return opener


@pytest.mark.parametrize(
    "opener",
    [
        pytest.param(
            _raises(urllib.error.HTTPError("u", 404, "Not Found", None, None)),  # type: ignore[arg-type]
            id="http-404",
        ),
        pytest.param(_raises(urllib.error.URLError("no route")), id="url-error"),
        pytest.param(_raises(TimeoutError("timed out")), id="timeout"),
        pytest.param(_raises(ConnectionResetError("reset")), id="connection-reset"),
        pytest.param(lambda url, timeout: b"", id="empty-body"),
        pytest.param(lambda url, timeout: b"<html>404</html>", id="html-body"),
    ],
)
def test_sibling_fetch_failure_fails_closed(opener: Opener) -> None:
    with pytest.raises(ParityError):
        sibling_hook_digest("main", opener)


def test_sibling_fetch_success_returns_the_body_digest() -> None:
    body = b"#!/usr/bin/env bash\nexit 0\n"
    seen: list[str] = []

    def opener(url: str, timeout: float) -> bytes:
        seen.append(url)
        return body

    assert sibling_hook_digest("feat/g0-5-p9-gate", opener) == hashlib.sha256(body).hexdigest()
    assert seen == [RAW_URL.format(repo=SIBLING_REPO, ref="feat/g0-5-p9-gate")]


@pytest.mark.parametrize(
    "ref", ["", "../main", "main/../x", "-main", "/main", "main/", "ma in", "main\n", "a" * 201]
)
def test_unusable_sibling_ref_fails_closed(ref: str) -> None:
    with pytest.raises(ParityError):
        sibling_hook_digest(ref, _raises(AssertionError("must not fetch")))


@pytest.mark.parametrize(
    ("env", "skipped"),
    [
        pytest.param({}, False, id="local-default-runs"),
        pytest.param({OPT_OUT_ENV: "1"}, True, id="local-opt-out-skips"),
        pytest.param({OPT_OUT_ENV: "0"}, False, id="local-opt-out-0-runs"),
        pytest.param({OPT_OUT_ENV: "true"}, False, id="local-opt-out-not-1-runs"),
        pytest.param({"CI": "true", OPT_OUT_ENV: "1"}, False, id="ci-ignores-opt-out"),
        pytest.param({"CI": "true"}, False, id="ci-runs"),
    ],
)
def test_parity_check_skips_only_on_explicit_local_opt_out(
    env: dict[str, str], skipped: bool
) -> None:
    reason = parity_skip_reason(env)
    assert (reason is not None) is skipped
    if skipped:
        assert reason is not None and OPT_OUT_ENV in reason
