"""Round-5 acceptance tests for the P9 pre-push gate (terms-analysis#175).

Findings: docs/evidence/2026-10-07-g0-5-security-r5.md (F1-F6) and
docs/evidence/2026-10-07-g0-5-grumpy-r5.md (MEDIUM, LOW), plus the
QUALITY-BAR section A attack list run against the whole hook.

Contract pinned here (the Coder implements it; nothing here constrains how):

- F1 indirection: the advertisement must be read from the SAME repository
  the push goes to. When url.*.insteadOf would redirect the hook's read of
  the push URL somewhere else, the push is refused with a message that
  names insteadOf. A push whose URL is rewritten consistently still passes.
- F2 output safety: bytes the remote advertises are never echoed raw. A
  refusal caused by a hostile advertisement carries no C0/C1 control
  characters (newline excepted), no DEL, no Unicode format (Cf) or line/
  paragraph separators, and stderr stays under MAX_STDERR bytes.
- Attack list, output safety for the signoff file: verdict, override.reason
  and override.authorized_by reach the terminal sanitised, on one line.
- F3 time and size: the advertisement read is bounded.
    P9_ADVERT_TIMEOUT_SECONDS  positive integer, default at most 30
    P9_ADVERT_MAX_BYTES        positive integer, default at most 32 MiB;
                               an advertisement of exactly the cap passes,
                               one byte more is refused
  A timeout refuses with "cannot read the refs <remote> advertises" and
  "timed out"; an oversized advertisement refuses with a message saying
  it is too large. Any other value of either variable (0, negative, not a
  plain decimal integer) refuses and names the variable; empty means unset.
- F4 many refs: 60,000 advertised refs (above Linux's 2 MiB and macOS's
  1 MiB ARG_MAX once expanded onto argv) with a legitimate push PASS.
- Grumpy MEDIUM honest message: when the remote advertises commits this
  clone has not fetched, the refusal says to run `git fetch <remote>` and
  never claims range.base is "not on" the remote.
- Grumpy LOW + F6 parity: the hook, scripts/ci/p9-sibling-parity.sh and the
  shared doc block are compared with the sibling repo at an immutable
  commit sha (P9_SIBLING_SHA), never at a branch name. `resolve-ref` prints
  `ref=<name>` and `sha=<hex>` lines, ready to append to GITHUB_OUTPUT.

Every git command runs in a tmp_path sandbox with local bare remotes or a
fake remote helper (`p9fake::`). The only network access is the parity
fetch from raw.githubusercontent.com at a commit sha.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import subprocess
import time
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.unit.test_p9_gate_fix_r1 import _pass, _signoff, in_ci
from tests.unit.test_p9_prepush_gate import (  # noqa: F401
    REPO_ROOT,
    Sandbox,
    _git,
    _run,
    pytestmark,
)

# Repo-specific wiring (the only lines that differ from the sibling's copy).
THIS_REPO = "legal-corpus-ingester"
SIBLING_REPO = "terms-analysis"
SUITE_STEP = "Unit tests"
PARITY_NODE = "tests/unit/test_p9_gate_r5.py::test_p9_shared_files_match_the_sibling_at_the_resolved_sha"

OWNER = "jennifer-mckinney"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PARITY_SCRIPT = REPO_ROOT / "scripts" / "ci" / "p9-sibling-parity.sh"
RESOLVE_STEP = "Resolve sibling ref for P9 hook parity"
ZERO_SHA = "0" * 40
REFUSED = 1
TIMEOUT_ENV = "P9_ADVERT_TIMEOUT_SECONDS"
MAX_BYTES_ENV = "P9_ADVERT_MAX_BYTES"
DEFAULT_TIMEOUT_CEILING = 30
DEFAULT_MAX_BYTES_CEILING = 32 * 1024 * 1024
MAX_STDERR = 4096
CANNOT_READ = "cannot read the refs origin advertises"


@pytest.fixture
def installed(tmp_path: Path) -> Sandbox:
    sb = Sandbox(tmp_path)
    proc = sb.install("main")
    assert proc.returncode == 0, proc.stderr
    return sb


def _bare(sb: Sandbox, name: str) -> Path:
    path = sb.root / name
    _git(sb.root, sb.env, "init", "-q", "--bare", str(path))
    return path


def _ref_on(sb: Sandbox, remote: Path, ref: str) -> str | None:
    proc = _run(["git", "--git-dir", str(remote), "rev-parse", "--verify", "-q", ref], sb.root, sb.env)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _has_commit(sb: Sandbox, repo_args: list[str], sha: str) -> bool:
    proc = _run(["git", *repo_args, "cat-file", "-e", f"{sha}^{{commit}}"], sb.root, sb.env)
    return proc.returncode == 0


def _push(sb: Sandbox, remote: str, refspec: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    root = Path(sb.env["P9_SANDBOX_ROOT"])
    assert sb.main.resolve() == root or root in sb.main.resolve().parents
    try:
        return subprocess.run(
            ["git", "push", remote, refspec],
            cwd=sb.main,
            env=sb.env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"git push {remote} {refspec} did not finish within {timeout}s")


def _seed(sb: Sandbox) -> str:
    seed = sb.remote_sha("main")
    assert seed is not None
    return seed


# Fake remote helper: `p9fake::<anything>` ----------------------------------

FAKE_HELPER = r"""#!/usr/bin/env bash
# Fake git remote helper for p9fake:: URLs (test only).
printf '%s\n' "$$" >> "${P9_FAKE_PIDS}"
exec 2>/dev/null
mode="${P9_FAKE_MODE:-list}"
if [ "${mode}" = hang-capabilities ]; then exec sleep 3600; fi
while IFS= read -r line; do
    case "${line}" in
        capabilities) printf 'list\n\n' ;;
        list*)
            case "${mode}" in
                hang-list) exec sleep 3600 ;;
                endless) while :; do printf '%s refs/heads/endless\n' "${P9_FAKE_SHA}"; done ;;
                *) cat "${P9_FAKE_ADVERT}"; printf '\n' ;;
            esac
            ;;
        "") exit 0 ;;
    esac
done
"""


class Hook:
    """Runs the installed hook directly, as git would, in its own process
    group, so a hung hook and everything it started can be killed."""

    def __init__(self, sb: Sandbox, tmp_path: Path) -> None:
        self.sb = sb
        self.bindir = tmp_path / "fakebin-helper"
        self.bindir.mkdir()
        helper = self.bindir / "git-remote-p9fake"
        helper.write_text(FAKE_HELPER)
        helper.chmod(0o755)
        self.pids = tmp_path / "helper.pids"
        self.pids.write_text("")
        self.advert = tmp_path / "advert.txt"
        self.advert.write_bytes(b"")

    def run(
        self,
        url: str,
        stdin: str,
        env: dict[str, str] | None = None,
        timeout: float = 60,
    ) -> tuple[subprocess.CompletedProcess[bytes], float]:
        root = Path(self.sb.env["P9_SANDBOX_ROOT"])
        assert self.sb.main.resolve() == root or root in self.sb.main.resolve().parents
        full = dict(self.sb.env)
        full["PATH"] = f"{self.bindir}{os.pathsep}{full.get('PATH', os.defpath)}"
        full["P9_FAKE_PIDS"] = str(self.pids)
        full["P9_FAKE_ADVERT"] = str(self.advert)
        for name in (TIMEOUT_ENV, MAX_BYTES_ENV):
            full.pop(name, None)
        full.update(env or {})
        started = time.monotonic()
        proc = subprocess.Popen(
            [str(self.sb.main / ".githooks" / "pre-push"), "origin", url],
            cwd=self.sb.main,
            env=full,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(stdin.encode(), timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill(proc)
            proc.communicate()
            pytest.fail(f"the hook did not return within {timeout}s for {url}")
        finally:
            self._kill(proc)
        elapsed = time.monotonic() - started
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err), elapsed

    def _kill(self, proc: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        for pid in self.pids.read_text().split():
            try:
                os.kill(int(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, ValueError):
                pass


@pytest.fixture
def hook(installed: Sandbox, tmp_path: Path) -> Hook:
    return Hook(installed, tmp_path)


def _tip_line(tip: str, ref: str = "refs/heads/main") -> str:
    return f"{ref} {tip} {ref} {ZERO_SHA}\n"


def _signed_tip(sb: Sandbox, name: str) -> str:
    tip = sb.commit(sb.main, name)
    _signoff(sb, sb.main, tip, _pass(tip))
    return tip


def _advert_lines(seed: str, count: int) -> bytes:
    """count lines of exactly 60 bytes each, all pointing at seed."""
    return b"".join(f"{seed} refs/heads/n{i:06d}\n".encode() for i in range(count))


# F1: insteadOf / pushInsteadOf divergence -----------------------------------

ALIAS = "p9alias:repo"


def _divergence(sb: Sandbox) -> dict[str, str]:
    """seed and a are on the real remote A (sb.remote); b, the tip, has a
    tip-only signoff; E is an empty bare repo that the push will go to."""
    seed = _seed(sb)
    a = sb.commit(sb.main, "diverge-a.txt")
    proc = _run(["git", "push", "-q", "--no-verify", "origin", "HEAD:refs/heads/mirrored"], sb.main, sb.env)
    assert proc.returncode == 0, proc.stderr
    b = _signed_tip(sb, "diverge-b.txt")
    empty = _bare(sb, "push-target.git")
    return {"seed": seed, "a": a, "b": b, "empty": str(empty), "full": str(sb.remote)}


def _config(sb: Sandbox, key: str, value: str) -> None:
    _git(sb.main, sb.env, "config", key, value)


@pytest.mark.parametrize(
    ("layout", "target"),
    [
        # Mirror pattern from F1: pushInsteadOf sends the push to E, then the
        # hook's read of E is rewritten by insteadOf to A.
        pytest.param("push-instead-of", "origin", id="pushInsteadOf-then-insteadOf"),
        pytest.param("push-instead-of", ALIAS, id="pushInsteadOf-push-to-url"),
        # No pushInsteadOf: insteadOf sends the push to E (one rewrite), the
        # hook's read of E is rewritten a second time, to A.
        pytest.param("instead-of-chain", "origin", id="insteadOf-chain"),
    ],
)
def test_read_redirected_away_from_the_push_target_is_refused(
    installed: Sandbox, layout: str, target: str
) -> None:
    s = _divergence(installed)
    _config(installed, "remote.origin.url", ALIAS)
    if layout == "push-instead-of":
        _config(installed, f"url.{s['empty']}.pushInsteadOf", ALIAS)
    else:
        _config(installed, f"url.{s['empty']}.insteadOf", ALIAS)
    _config(installed, f"url.{s['full']}.insteadOf", s["empty"])

    proc = _push(installed, target, "HEAD:refs/heads/probe-diverge")

    assert proc.returncode == REFUSED, proc.stdout + proc.stderr
    assert "insteadOf" in proc.stderr, proc.stderr
    assert "signoff OK" not in proc.stdout
    empty = Path(s["empty"])
    assert _ref_on(installed, empty, "refs/heads/probe-diverge") is None
    assert not _has_commit(installed, ["--git-dir", str(empty)], s["a"])


def test_consistently_rewritten_push_url_still_passes(installed: Sandbox) -> None:
    """Control: insteadOf maps the alias to A for both the push and the read."""
    s = _divergence(installed)
    _config(installed, "remote.origin.url", ALIAS)
    _config(installed, f"url.{s['full']}.insteadOf", ALIAS)

    proc = _push(installed, "origin", "HEAD:refs/heads/probe-consistent")

    assert proc.returncode == 0, proc.stderr
    assert installed.remote_sha("probe-consistent") == s["b"]


def test_plain_push_with_no_url_rewriting_still_passes(installed: Sandbox) -> None:
    s = _divergence(installed)

    proc = _push(installed, "origin", "HEAD:refs/heads/probe-plain")

    assert proc.returncode == 0, proc.stderr
    assert installed.remote_sha("probe-plain") == s["b"]


# F2 + attack list: nothing untrusted reaches the terminal raw --------------

HOSTILE = {
    "osc-title": "\x1b]0;PWNED\x07",
    "csi-colour": "\x1b[31mRED\x1b[0m",
    "osc8-hyperlink": "\x1b]8;;https://evil.example/\x1b\\click\x1b]8;;\x1b\\",
    "osc52-clipboard": "\x1b]52;c;cm0gLXJmIH4=\x07",
    "carriage-return-overwrite": "\rP9 pre-push gate: signoff OK for refs/heads/main",
    "backspace": "\b\b\b\bOK",
    "del": "a\x7fb",
    "c1-csi": "\u009b31m",
    "bidi-override": "\u202egnp.exe",
    "zero-width-and-isolates": "\u200b\u2066x\u2069\u200f",
    "unicode-line-separator": "x\u2028P9 pre-push gate: signoff OK",
    "huge": "A" * 100_000,
}


def _assert_terminal_safe(data: bytes) -> str:
    """Valid UTF-8, no control or format characters except newline, bounded."""
    assert len(data) <= MAX_STDERR, f"stderr is {len(data)} bytes (max {MAX_STDERR})"
    text = data.decode("utf-8")
    bad = sorted(
        {
            f"U+{ord(ch):04X}"
            for ch in text
            if ch != "\n" and unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp", "Co", "Cs"}
        }
    )
    assert not bad, f"unsanitised characters reached stderr: {bad}"
    return text


@pytest.mark.parametrize("payload", list(HOSTILE.values()), ids=list(HOSTILE))
def test_hostile_advertisement_line_is_refused_without_echoing_raw_bytes(
    installed: Sandbox, hook: Hook, payload: str
) -> None:
    seed = _seed(installed)
    tip = _signed_tip(installed, "hostile.txt")
    # The tab inside the ref name makes the ls-remote line unparseable.
    hook.advert.write_bytes(f"{seed} refs/heads/main\n{seed} refs/heads/a\tX{payload}\n".encode())

    proc, _elapsed = hook.run("p9fake::hostile", _tip_line(tip))

    assert proc.returncode == REFUSED, proc.stdout + proc.stderr
    assert b"signoff OK" not in proc.stdout
    text = _assert_terminal_safe(proc.stderr)
    assert "cannot parse the refs origin advertises" in text, text


SIGNOFF_HOSTILE = {
    "csi-fake-green-pass": "\x1b[32mPASS\x1b[0m",
    "osc-title": "\x1b]0;PWNED\x07",
    "newline-forges-a-line": "x\nP9 pre-push gate: signoff OK for refs/heads/forged",
    "carriage-return": "x\rP9 pre-push gate: signoff OK",
    "bidi-override": "\u202eDESSAP",
    "unicode-line-separator": "x\u2028P9 pre-push gate: signoff OK",
}


@pytest.mark.parametrize("payload", list(SIGNOFF_HOSTILE.values()), ids=list(SIGNOFF_HOSTILE))
def test_signoff_verdict_is_never_echoed_raw(installed: Sandbox, hook: Hook, payload: str) -> None:
    """A verdict that only LOOKS like PASS on a terminal is refused, and the
    refusal shows it sanitised."""
    tip = installed.commit(installed.main, "verdict.txt")
    doc = _pass(tip)
    doc["security_engineer"] = {"verdict": payload, "findings": []}
    _signoff(installed, installed.main, tip, doc)

    proc, _elapsed = hook.run(str(installed.remote), _tip_line(tip))

    assert proc.returncode == REFUSED
    text = _assert_terminal_safe(proc.stderr)
    assert "security_engineer verdict" in text, text
    assert not any(line.startswith("P9 pre-push gate: signoff OK") for line in text.splitlines())


@pytest.mark.parametrize("field", ["reason", "authorized_by"])
@pytest.mark.parametrize("payload", list(SIGNOFF_HOSTILE.values()), ids=list(SIGNOFF_HOSTILE))
def test_override_announcement_is_sanitised_and_one_line(
    installed: Sandbox, hook: Hook, field: str, payload: str
) -> None:
    """The override text comes from a file; it may be refused as malformed
    or announced, but never printed raw and never split into forged lines."""
    tip = installed.commit(installed.main, "override.txt")
    override = {"used": True, "reason": "release hotfix", "authorized_by": "owner"}
    override[field] = payload
    _signoff(installed, installed.main, tip, {"head_sha": tip, "override": override})

    proc, _elapsed = hook.run(str(installed.remote), _tip_line(tip))

    assert proc.returncode in {0, REFUSED}
    text = _assert_terminal_safe(proc.stderr)
    lines = text.splitlines()
    assert not any(line.startswith("P9 pre-push gate: signoff OK") for line in lines), text
    if proc.returncode == 0:
        assert sum(line.startswith("P9 OVERRIDE ACTIVE: ") for line in lines) == 1, text


# F3: hang and size limits ---------------------------------------------------

HANG_TIMEOUT = 2
SLACK = 10


@pytest.mark.parametrize(
    ("url", "mode"),
    [
        pytest.param("p9fake::slow", "hang-capabilities", id="server-never-answers"),
        pytest.param("p9fake::slow", "hang-list", id="server-stalls-before-refs"),
        pytest.param("p9fake::endless", "endless", id="endless-advertisement"),
        pytest.param("fd::0", "list", id="fd-0"),
        pytest.param("fd::3", "list", id="fd-3"),
    ],
)
def test_hanging_advertisement_is_refused_within_the_timeout(
    installed: Sandbox, hook: Hook, url: str, mode: str
) -> None:
    seed = _seed(installed)
    tip = _signed_tip(installed, "hang.txt")

    proc, elapsed = hook.run(
        url,
        _tip_line(tip),
        env={TIMEOUT_ENV: str(HANG_TIMEOUT), "P9_FAKE_MODE": mode, "P9_FAKE_SHA": seed},
        timeout=HANG_TIMEOUT + SLACK + 20,
    )

    assert elapsed < HANG_TIMEOUT + SLACK, f"refusal took {elapsed:.1f}s"
    assert proc.returncode == REFUSED, proc.stdout + proc.stderr
    text = _assert_terminal_safe(proc.stderr)
    assert CANNOT_READ in text, text
    assert "timed out" in text, text


def test_default_timeout_is_bounded(installed: Sandbox, hook: Hook) -> None:
    """With no override, a stalled server is still cut off."""
    tip = _signed_tip(installed, "default-timeout.txt")

    proc, elapsed = hook.run(
        "p9fake::slow",
        _tip_line(tip),
        env={"P9_FAKE_MODE": "hang-list"},
        timeout=DEFAULT_TIMEOUT_CEILING + SLACK + 20,
    )

    assert elapsed < DEFAULT_TIMEOUT_CEILING + SLACK, f"refusal took {elapsed:.1f}s"
    assert proc.returncode == REFUSED
    assert "timed out" in proc.stderr.decode("utf-8", "replace")


LINES = 2000
ADVERT_BYTES = LINES * 60


@pytest.mark.parametrize(
    ("cap", "passes"),
    [
        pytest.param(ADVERT_BYTES + 1, True, id="one-under-the-cap"),
        pytest.param(ADVERT_BYTES, True, id="exactly-the-cap"),
        pytest.param(ADVERT_BYTES - 1, False, id="one-over-the-cap"),
        pytest.param(4096, False, id="far-over-the-cap"),
    ],
)
def test_advertisement_size_cap_boundary(
    installed: Sandbox, hook: Hook, cap: int, passes: bool
) -> None:
    seed = _seed(installed)
    tip = _signed_tip(installed, "size.txt")
    hook.advert.write_bytes(_advert_lines(seed, LINES))
    assert len(hook.advert.read_bytes()) == ADVERT_BYTES

    proc, _elapsed = hook.run("p9fake::sized", _tip_line(tip), env={MAX_BYTES_ENV: str(cap)})

    text = _assert_terminal_safe(proc.stderr)
    if passes:
        assert proc.returncode == 0, text
        assert f"signoff OK for refs/heads/main at {tip[:12]}" in proc.stdout.decode()
    else:
        assert proc.returncode == REFUSED, text
        assert re.search(r"too large|larger than|exceeds", text), text


def test_default_size_cap_refuses_a_huge_advertisement(installed: Sandbox, hook: Hook) -> None:
    """40 MiB, all pointing at seed: with no override the read is capped."""
    seed = _seed(installed)
    tip = _signed_tip(installed, "huge.txt")
    name = "x" * 4000
    line = f"{seed} refs/heads/{name}".encode()
    count = (40 * 1024 * 1024) // (len(line) + 7) + 1
    with hook.advert.open("wb") as fh:
        for i in range(count):
            fh.write(line + f"{i:06d}\n".encode())
    assert hook.advert.stat().st_size > DEFAULT_MAX_BYTES_CEILING

    proc, _elapsed = hook.run("p9fake::huge", _tip_line(tip), timeout=180)

    assert proc.returncode == REFUSED, proc.stdout + proc.stderr[-2000:]
    text = _assert_terminal_safe(proc.stderr)
    assert re.search(r"too large|larger than|exceeds", text), text


@pytest.mark.parametrize("name", [TIMEOUT_ENV, MAX_BYTES_ENV])
@pytest.mark.parametrize("value", ["0", "-5", "abc", "1.5", "10s", " 7", "0x10", "99999999999999999999999"])
def test_unusable_limit_override_refuses_and_names_the_variable(
    installed: Sandbox, hook: Hook, name: str, value: str
) -> None:
    """An override can never switch a limit off: 0 is not 'unlimited'."""
    seed = _seed(installed)
    tip = _signed_tip(installed, "override-env.txt")
    hook.advert.write_bytes(f"{seed} refs/heads/main\n".encode())

    proc, _elapsed = hook.run("p9fake::env", _tip_line(tip), env={name: value})

    assert proc.returncode == REFUSED, proc.stdout + proc.stderr
    assert name in proc.stderr.decode("utf-8", "replace")


@pytest.mark.parametrize("name", [TIMEOUT_ENV, MAX_BYTES_ENV])
def test_empty_limit_override_means_the_default(installed: Sandbox, hook: Hook, name: str) -> None:
    seed = _seed(installed)
    tip = _signed_tip(installed, "empty-env.txt")
    hook.advert.write_bytes(f"{seed} refs/heads/main\n".encode())

    proc, _elapsed = hook.run("p9fake::env", _tip_line(tip), env={name: ""})

    assert proc.returncode == 0, proc.stderr
    assert f"signoff OK for refs/heads/main at {tip[:12]}" in proc.stdout.decode()


# F4: many advertised refs ---------------------------------------------------

MANY = 60_000


def _many_refs(sb: Sandbox, target: str) -> None:
    lines = [f"{target} refs/pull/{i:06d}/head\n" for i in range(MANY)]
    packed = sb.remote / "packed-refs"
    packed.write_text("# pack-refs with: peeled fully-peeled sorted \n" + "".join(lines))
    count = _git(sb.root, sb.env, "--git-dir", str(sb.remote), "for-each-ref", "--format=x", "refs/pull")
    assert len(count.splitlines()) == MANY


def test_many_advertised_refs_tip_only_push_passes(installed: Sandbox) -> None:
    seed = _seed(installed)
    _many_refs(installed, seed)
    tip = _signed_tip(installed, "many-tip.txt")

    proc = _push(installed, "origin", "HEAD:refs/heads/probe-many", timeout=240)

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert installed.remote_sha("probe-many") == tip


def test_many_advertised_refs_range_base_push_passes(installed: Sandbox) -> None:
    seed = _seed(installed)
    _many_refs(installed, seed)
    installed.commit(installed.main, "many-a.txt")
    b = installed.commit(installed.main, "many-b.txt")
    _signoff(installed, installed.main, b, {**_pass(b), "range": {"base": seed}})

    proc = _push(installed, "origin", "HEAD:refs/heads/probe-many-base", timeout=240)

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert installed.remote_sha("probe-many-base") == b


def test_many_advertised_refs_still_refuse_unreviewed_ancestry(installed: Sandbox) -> None:
    """Control: the fix must not pass everything once the list is long."""
    _many_refs(installed, _seed(installed))
    a = installed.commit(installed.main, "many-unreviewed-a.txt")
    _signed_tip(installed, "many-unreviewed-b.txt")

    proc = _push(installed, "origin", "HEAD:refs/heads/probe-many-unreviewed", timeout=240)

    assert proc.returncode == REFUSED
    assert "2 commits are new to origin" in proc.stderr, proc.stderr[-2000:]
    assert not _has_commit(installed, ["--git-dir", str(installed.remote)], a)


# Grumpy MEDIUM: unfetched advertised commits get the true reason -----------


def _remote_moved_on(sb: Sandbox) -> tuple[str, str]:
    """The remote's main moves to x, a child of seed, which this clone never
    fetched. The remote advertises only main = x."""
    seed = _seed(sb)
    git_dir = ["--git-dir", str(sb.remote)]
    tree = _git(sb.root, sb.env, *git_dir, "rev-parse", f"{seed}^{{tree}}")
    x = _git(sb.root, sb.env, *git_dir, "commit-tree", tree, "-p", seed, "-m", "moved on")
    _git(sb.root, sb.env, *git_dir, "update-ref", "refs/heads/main", x)
    assert not _has_commit(sb, ["-C", str(sb.main)], x)
    return seed, x


def test_unfetched_advertisement_range_base_refusal_states_the_true_reason(
    installed: Sandbox,
) -> None:
    seed, _x = _remote_moved_on(installed)
    installed.commit(installed.main, "unfetched-a.txt")
    b = installed.commit(installed.main, "unfetched-b.txt")
    _signoff(installed, installed.main, b, {**_pass(b), "range": {"base": seed}})

    proc = _push(installed, "origin", "HEAD:refs/heads/probe-unfetched-base")

    assert proc.returncode == REFUSED
    assert "git fetch origin" in proc.stderr, proc.stderr
    assert f"range.base {seed[:12]} is not on origin" not in proc.stderr, proc.stderr
    assert installed.remote_sha("probe-unfetched-base") is None


def test_unfetched_advertisement_tip_only_refusal_gives_the_fetch_hint(
    installed: Sandbox,
) -> None:
    _remote_moved_on(installed)
    _signed_tip(installed, "unfetched-tip.txt")

    proc = _push(installed, "origin", "HEAD:refs/heads/probe-unfetched-tip")

    assert proc.returncode == REFUSED
    assert "git fetch origin" in proc.stderr, proc.stderr
    assert installed.remote_sha("probe-unfetched-tip") is None


def test_after_the_hinted_fetch_the_same_push_passes(installed: Sandbox) -> None:
    """Control: the hint is the real fix."""
    seed, _x = _remote_moved_on(installed)
    installed.commit(installed.main, "fetched-a.txt")
    b = installed.commit(installed.main, "fetched-b.txt")
    _signoff(installed, installed.main, b, {**_pass(b), "range": {"base": seed}})
    _git(installed.main, installed.env, "fetch", "-q", "origin")

    proc = _push(installed, "origin", "HEAD:refs/heads/probe-fetched")

    assert proc.returncode == 0, proc.stderr
    assert installed.remote_sha("probe-fetched") == b


def test_known_advertisement_refusal_does_not_suggest_fetching(installed: Sandbox) -> None:
    """Control: the hint appears only when something advertised is unknown."""
    installed.commit(installed.main, "known-a.txt")
    _signed_tip(installed, "known-b.txt")

    proc = _push(installed, "origin", "HEAD:refs/heads/probe-known")

    assert proc.returncode == REFUSED
    assert "2 commits are new to origin" in proc.stderr
    assert "git fetch" not in proc.stderr, proc.stderr


# Grumpy LOW + F6: parity of every shared artifact, at a commit sha ----------

PARITY_FILES = (".githooks/pre-push", "scripts/ci/p9-sibling-parity.sh")
DOC_PATH = "automations/p9-pre-push.md"
SHARED_BEGIN = "<!-- p9-shared:begin -->"
SHARED_END = "<!-- p9-shared:end -->"
RAW_AT_SHA = "https://raw.githubusercontent.com/{owner}/{repo}/{sha}/{path}"
OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
SHA_ENV = "P9_SIBLING_SHA"
REF_ENV = "P9_SIBLING_REF"
OPT_OUT_ENV = "P9_SKIP_SIBLING_PARITY"

Opener = Callable[[str, float], bytes]


class ParityError(Exception):
    """Parity with the sibling cannot be established."""


def _urlopen(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed https host
        return resp.read()


def sibling_raw_url(path: str, sha: str) -> str:
    """The sibling's file at an immutable commit; a branch name is refused."""
    if not OBJECT_ID.fullmatch(sha):
        raise ParityError(f"{sha!r} is not a commit sha; parity is only checked at a sha")
    if path not in (*PARITY_FILES, DOC_PATH):
        raise ParityError(f"{path!r} is not a shared P9 artifact")
    return RAW_AT_SHA.format(owner=OWNER, repo=SIBLING_REPO, sha=sha, path=path)


def shared_block(text: str) -> str:
    if text.count(SHARED_BEGIN) != 1 or text.count(SHARED_END) != 1:
        raise ParityError("the shared P9 doc block markers must each appear exactly once")
    start = text.index(SHARED_BEGIN)
    end = text.index(SHARED_END)
    if end < start:
        raise ParityError("the shared P9 doc block end marker comes before its start")
    return text[start : end + len(SHARED_END)]


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def local_digests() -> dict[str, str]:
    found = {path: _digest((REPO_ROOT / path).read_bytes()) for path in PARITY_FILES}
    doc = (REPO_ROOT / DOC_PATH).read_text(encoding="utf-8")
    found[DOC_PATH] = _digest(shared_block(doc).encode("utf-8"))
    return found


def sibling_digests(sha: str, opener: Opener = _urlopen) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in (*PARITY_FILES, DOC_PATH):
        url = sibling_raw_url(path, sha)
        try:
            body = opener(url, 20.0)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ParityError(f"cannot fetch {url}: {exc}") from exc
        if path == DOC_PATH:
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ParityError(f"{url} is not UTF-8 text") from exc
            found[path] = _digest(shared_block(text).encode("utf-8"))
        else:
            if not body.startswith(b"#!"):
                raise ParityError(f"{url} did not return a script ({len(body)} bytes)")
            found[path] = _digest(body)
    return found


def parse_resolved(stdout: str) -> tuple[str, str]:
    """`resolve-ref` output: exactly the lines ref=<name> and sha=<hex>."""
    pairs = dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)
    lines = [line for line in stdout.splitlines() if line]
    if sorted(pairs) != ["ref", "sha"] or len(lines) != 2:
        raise ParityError(f"resolve-ref must print ref= and sha= lines, got {stdout!r}")
    if not OBJECT_ID.fullmatch(pairs["sha"]):
        raise ParityError(f"resolve-ref printed a sha that is not one: {pairs['sha']!r}")
    return pairs["ref"], pairs["sha"]


def resolve_sibling_sha(env: dict[str, str]) -> str:
    """CI passes the sha resolved by the workflow; locally, ask the script."""
    given = env.get(SHA_ENV, "").strip()
    if given:
        if not OBJECT_ID.fullmatch(given):
            raise ParityError(f"{SHA_ENV}={given!r} is not a commit sha")
        return given
    if in_ci(env.get("CI")):
        raise ParityError(f"{SHA_ENV} is not set; the workflow must pass the resolved sibling sha")
    proc = subprocess.run(
        ["bash", str(PARITY_SCRIPT), "resolve-ref", f"{OWNER}/{SIBLING_REPO}", env.get(REF_ENV, "").strip()],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise ParityError(f"resolve-ref failed ({proc.returncode}): {proc.stderr.strip()}")
    return parse_resolved(proc.stdout)[1]


def test_p9_shared_files_match_the_sibling_at_the_resolved_sha() -> None:
    if not in_ci(os.environ.get("CI")) and os.environ.get(OPT_OUT_ENV, "").strip() == "1":
        pytest.skip(f"{OPT_OUT_ENV}=1: cross-repo P9 parity explicitly skipped on this machine")
    try:
        sha = resolve_sibling_sha(dict(os.environ))
        remote = sibling_digests(sha)
    except ParityError as exc:
        pytest.fail(f"cross-repo P9 parity cannot be established (fail closed): {exc}")
    local = local_digests()

    differ = [path for path in local if local[path] != remote.get(path)]

    assert not differ, (
        f"{', '.join(differ)} differ from {SIBLING_REPO}@{sha[:12]}; land the same "
        f"change in both repos (local {local}, sibling {remote})"
    )


# The sha-pinned helpers: unit cases, with a fake opener.


@pytest.mark.parametrize(
    "ref",
    ["main", "feat/g0-5-p9-gate", "abc123", "A" * 40, "a" * 39, "a" * 41, "a" * 40 + "\n", ""],
)
def test_parity_refuses_anything_but_a_full_commit_sha(ref: str) -> None:
    with pytest.raises(ParityError):
        sibling_digests(ref, lambda url, timeout: pytest.fail("must not fetch"))


def _fake_sibling(tmp_files: dict[str, bytes]) -> tuple[Opener, list[str]]:
    seen: list[str] = []

    def opener(url: str, timeout: float) -> bytes:
        seen.append(url)
        for path, body in tmp_files.items():
            if url.endswith("/" + path):
                return body
        raise urllib.error.URLError("404")

    return opener, seen


def _local_bodies() -> dict[str, bytes]:
    bodies = {path: (REPO_ROOT / path).read_bytes() for path in PARITY_FILES}
    bodies[DOC_PATH] = (REPO_ROOT / DOC_PATH).read_bytes()
    return bodies


def test_parity_fetches_every_shared_artifact_at_the_sha() -> None:
    sha = "c" * 40
    opener, seen = _fake_sibling(_local_bodies())

    assert sibling_digests(sha, opener) == local_digests()
    assert seen == [
        RAW_AT_SHA.format(owner=OWNER, repo=SIBLING_REPO, sha=sha, path=path)
        for path in (*PARITY_FILES, DOC_PATH)
    ]


@pytest.mark.parametrize("path", [*PARITY_FILES, DOC_PATH])
def test_parity_detects_a_one_sided_edit_of_each_artifact(path: str) -> None:
    bodies = _local_bodies()
    if path == DOC_PATH:
        text = bodies[path].decode("utf-8")
        bodies[path] = text.replace(SHARED_END, "one-sided edit\n" + SHARED_END).encode("utf-8")
    else:
        bodies[path] = bodies[path] + b"# one-sided edit\n"
    opener, _seen = _fake_sibling(bodies)

    remote = sibling_digests("d" * 40, opener)

    assert [p for p, d in local_digests().items() if remote[p] != d] == [path]


def test_doc_text_outside_the_shared_block_may_differ() -> None:
    bodies = _local_bodies()
    bodies[DOC_PATH] = b"repo-specific preface\n" + bodies[DOC_PATH] + b"\nrepo-specific tail\n"
    opener, _seen = _fake_sibling(bodies)

    assert sibling_digests("e" * 40, opener) == local_digests()


@pytest.mark.parametrize(
    "doc",
    [
        pytest.param("no markers", id="missing"),
        pytest.param(f"{SHARED_BEGIN}\nx\n{SHARED_END}\n{SHARED_BEGIN}\n{SHARED_END}", id="duplicated"),
        pytest.param(f"{SHARED_END}\nx\n{SHARED_BEGIN}", id="reversed"),
    ],
)
def test_parity_fails_closed_on_a_broken_sibling_doc(doc: str) -> None:
    bodies = _local_bodies()
    bodies[DOC_PATH] = doc.encode("utf-8")
    opener, _seen = _fake_sibling(bodies)

    with pytest.raises(ParityError):
        sibling_digests("f" * 40, opener)


def test_parity_in_ci_requires_the_workflow_to_pass_the_sha() -> None:
    with pytest.raises(ParityError, match=SHA_ENV):
        resolve_sibling_sha({"CI": "true", REF_ENV: "feat/g0-5-p9-gate"})


# resolve-ref prints the sha (behaviour, with a fake git) -------------------

SIB = f"{OWNER}/{SIBLING_REPO}"
BRANCH = "feat/g0-5-p9-gate"
HEAD_SHA = "a" * 40
MAIN_SHA = "b" * 40


def _fake_git(tmp_path: Path, heads: dict[str, str], fail: bool = False) -> tuple[str, Path]:
    """A PATH whose `git ls-remote` answers from `heads` (refname -> sha)
    with git's tail-matching of patterns, and logs its arguments; every
    other git command runs the real git."""
    real = shutil.which("git")
    assert real
    bindir = tmp_path / "fakebin-git"
    bindir.mkdir(exist_ok=True)
    table = tmp_path / "heads.tsv"
    table.write_text("".join(f"{sha}\t{ref}\n" for ref, sha in heads.items()))
    log = tmp_path / "ls-remote.log"
    fake = bindir / "git"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = ls-remote ]; then\n'
        f'    printf "%s\\n" "$*" >> "{log}"\n'
        f"    {'exit 128' if fail else ':'}\n"
        '    for last; do :; done\n'
        "    tab=$(printf '\\t')\n"
        f'    while IFS="$tab" read -r sha ref; do\n'
        '        case "$last" in\n'
        '            https://*) printf "%s\\t%s\\n" "$sha" "$ref" ;;\n'
        '            *) case "$ref" in "$last"|*/"$last") printf "%s\\t%s\\n" "$sha" "$ref" ;; esac ;;\n'
        "        esac\n"
        f'    done < "{table}"\n'
        "    exit 0\n"
        "fi\n"
        f'exec "{real}" "$@"\n'
    )
    fake.chmod(0o755)
    return f"{bindir}{os.pathsep}{os.environ['PATH']}", log


def _resolve(tmp_path: Path, path: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["PATH"] = path
    return subprocess.run(
        ["bash", str(PARITY_SCRIPT), "resolve-ref", *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize(
    ("heads", "head_ref", "expected"),
    [
        pytest.param(
            {f"refs/heads/{BRANCH}": HEAD_SHA, "refs/heads/main": MAIN_SHA},
            BRANCH,
            f"ref={BRANCH}\nsha={HEAD_SHA}\n",
            id="sibling-has-the-branch",
        ),
        pytest.param(
            {"refs/heads/main": MAIN_SHA}, "other-card", f"ref=main\nsha={MAIN_SHA}\n", id="falls-back-to-main"
        ),
        pytest.param({"refs/heads/main": MAIN_SHA}, "", f"ref=main\nsha={MAIN_SHA}\n", id="no-head-ref"),
    ],
)
def test_resolve_ref_prints_the_ref_and_its_commit_sha(
    tmp_path: Path, heads: dict[str, str], head_ref: str, expected: str
) -> None:
    path, log = _fake_git(tmp_path, heads)

    proc = _resolve(tmp_path, path, SIB, head_ref)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == expected
    assert all(f"https://github.com/{SIB}" in line for line in log.read_text().splitlines())


@pytest.mark.parametrize(
    ("heads", "head_ref", "fail"),
    [
        pytest.param({}, "", False, id="sibling-has-no-main"),
        pytest.param({}, "other-card", False, id="neither-branch-nor-main"),
        pytest.param({"refs/heads/main": MAIN_SHA}, "", True, id="github-cannot-be-asked"),
        pytest.param({"refs/heads/main": "not-a-sha"}, "", False, id="malformed-sha"),
    ],
)
def test_resolve_ref_fails_closed_without_a_sha(
    tmp_path: Path, heads: dict[str, str], head_ref: str, fail: bool
) -> None:
    path, _log = _fake_git(tmp_path, heads, fail=fail)

    proc = _resolve(tmp_path, path, SIB, head_ref)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "sha=" not in proc.stdout


# The workflow passes the sha to both parity runs ---------------------------


def _steps() -> list[dict[str, Any]]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        if any(step.get("name") == RESOLVE_STEP for step in job["steps"]):
            return list(job["steps"])
    raise AssertionError(f"no job in {WORKFLOW.name} has a '{RESOLVE_STEP}' step")


def _step(steps: list[dict[str, Any]], name: str) -> tuple[int, dict[str, Any]]:
    found = [(n, s) for n, s in enumerate(steps) if s.get("name") == name]
    assert len(found) == 1, f"expected one '{name}' step, found {len(found)}"
    return found[0]


def test_resolve_step_writes_ref_and_sha_to_the_step_outputs(tmp_path: Path) -> None:
    _n, step = _step(_steps(), RESOLVE_STEP)
    path, _log = _fake_git(tmp_path, {f"refs/heads/{BRANCH}": HEAD_SHA, "refs/heads/main": MAIN_SHA})
    root = tmp_path / "repo"
    (root / "scripts" / "ci").mkdir(parents=True)
    shutil.copy2(PARITY_SCRIPT, root / "scripts" / "ci" / PARITY_SCRIPT.name)
    output = tmp_path / "github_output"
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"PATH": path, "HEAD_REF": BRANCH, "GITHUB_OUTPUT": str(output)})

    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        cwd=root / step.get("working-directory", "."),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert output.read_text() == f"ref={BRANCH}\nsha={HEAD_SHA}\n"


def test_parity_runs_get_the_resolved_sha_and_the_new_parity_test() -> None:
    steps = _steps()
    resolve_at, resolve = _step(steps, RESOLVE_STEP)
    sha_expr = "${{ steps.%s.outputs.sha }}" % resolve["id"]
    parity = [(n, s) for n, s in enumerate(steps) if "p9-sibling-parity.sh check" in s.get("run", "")]
    assert len(parity) == 1, f"expected one parity check step, found {len(parity)}"
    parity_at, check = parity[0]
    suite_at, suite = _step(steps, SUITE_STEP)

    assert resolve_at < parity_at and resolve_at < suite_at
    assert check.get("env", {}).get(SHA_ENV) == sha_expr
    assert suite.get("env", {}).get(SHA_ENV) == sha_expr
    assert check["run"].split("p9-sibling-parity.sh check", 1)[1].split() == [PARITY_NODE]
    assert "if" not in check and "continue-on-error" not in check
