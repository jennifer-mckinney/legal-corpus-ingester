"""G0-5 acceptance tests for the P9 pre-push gate (terms-analysis#175).

The gate is `.githooks/pre-push`, installed by `scripts/install-hooks.sh`.
These tests run the repo's REAL hook and installer inside a throwaway sandbox
built by `p9_gate_harness.sh` (bash, driven via subprocess): a main checkout,
a linked `git worktree`, and a local bare remote. Nothing here reads or writes
the real repository's git config.

Contract under test:
  (a) worktree push WITH a valid signoff in the common git dir succeeds
  (b) push WITHOUT signoff is refused, and the path the gate tells the user to
      write resolves to the common git dir
  (c) main checkout and worktree behave identically
  (d) install-hooks.sh exits 0 and sets core.hooksPath == .githooks
  (e) install-hooks.sh replaces an absolute .git/hooks hooksPath, exits exactly 0
  (f) [ingester] pre-push is tracked executable and CI propagates pytest status
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = Path(__file__).resolve().parent / "p9_gate_harness.sh"
NOT_FOUND_MSG = "P9 pre-push gate: signoff not found"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="git and bash are required for the P9 gate harness",
)


def _sandbox_env(sandbox: Path) -> dict[str, str]:
    """Environment with no inherited git state and an isolated global config."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["HOME"] = str(sandbox / "home")
    env["GIT_CONFIG_GLOBAL"] = str(sandbox / "home" / ".gitconfig")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.pop("CDPATH", None)
    # Marker consumed by _run(): every command must execute under this root.
    env["P9_SANDBOX_ROOT"] = str(sandbox.resolve())
    return env


def _run(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # Safety rail: sandbox commands (git, bash, the hook) may only run with a
    # sandbox env and a cwd inside the sandbox (pytest tmp_path), never in a
    # real checkout.
    root = env.get("P9_SANDBOX_ROOT")
    assert root, "refusing to run a sandbox command without P9_SANDBOX_ROOT"
    real_cwd = Path(cwd).resolve()
    assert real_cwd == Path(root) or Path(root) in real_cwd.parents, (
        f"refusing to run {args[0]} in {real_cwd}: outside sandbox {root}"
    )
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=60)


def _git(cwd: Path, env: dict[str, str], *args: str) -> str:
    proc = _run(["git", *args], cwd, env)
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout.strip()


class Sandbox:
    """Throwaway main checkout + worktree + bare remote under tmp_path."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.env = _sandbox_env(root)
        proc = _run(["bash", str(HARNESS), str(REPO_ROOT), str(root)], root, self.env)
        assert proc.returncode == 0, f"harness setup failed:\n{proc.stdout}\n{proc.stderr}"
        self.main = root / "main"
        self.wt = root / "wt"
        self.remote = root / "remote.git"
        origin = _git(self.main, self.env, "remote", "get-url", "origin")
        assert Path(origin).resolve() == self.remote.resolve(), f"unexpected origin {origin}"

    def checkout(self, where: str) -> Path:
        return {"main": self.main, "worktree": self.wt}[where]

    def install(self, where: str) -> subprocess.CompletedProcess[str]:
        cwd = self.checkout(where)
        return _run(["bash", "scripts/install-hooks.sh"], cwd, self.env)

    def common_dir(self, cwd: Path) -> Path:
        raw = _git(cwd, self.env, "rev-parse", "--git-common-dir")
        path = Path(raw)
        return (path if path.is_absolute() else cwd / path).resolve()

    def commit(self, cwd: Path, name: str) -> str:
        (cwd / name).write_text(name + "\n")
        _git(cwd, self.env, "add", name)
        _git(cwd, self.env, "commit", "-q", "-m", f"add {name}")
        return _git(cwd, self.env, "rev-parse", "HEAD")

    def write_signoff(self, cwd: Path, sha: str) -> Path:
        reviews = self.common_dir(cwd) / "reviews"
        reviews.mkdir(parents=True, exist_ok=True)
        signoff = reviews / f"{sha}.signoff.json"
        signoff.write_text(
            json.dumps(
                {
                    "head_sha": sha,
                    "security_engineer": {"verdict": "PASS", "findings": []},
                    "grumpy_developer": {"verdict": "PASS", "findings": []},
                }
            )
        )
        return signoff

    def push(self, cwd: Path, branch: str) -> subprocess.CompletedProcess[str]:
        return _run(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], cwd, self.env)

    def remote_sha(self, branch: str) -> str | None:
        proc = _run(
            [
                "git",
                "--git-dir",
                str(self.remote),
                "rev-parse",
                "--verify",
                "-q",
                f"refs/heads/{branch}",
            ],
            self.root,
            self.env,
        )
        return proc.stdout.strip() if proc.returncode == 0 else None


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


@pytest.fixture
def installed(sandbox: Sandbox) -> Sandbox:
    """Hooks installed from the main checkout (the path that works today),
    so push failures below isolate the hook's own worktree handling."""
    proc = sandbox.install("main")
    assert proc.returncode == 0, f"install-hooks.sh from main failed: {proc.stderr}"
    return sandbox


def _push_outcome(sb: Sandbox, where: str, with_signoff: bool) -> tuple[bool, bool]:
    cwd = sb.checkout(where)
    branch = f"{where}-{'signed' if with_signoff else 'unsigned'}"
    sha = sb.commit(cwd, f"{branch}.txt")
    if with_signoff:
        sb.write_signoff(cwd, sha)
    proc = sb.push(cwd, branch)
    return proc.returncode == 0, sb.remote_sha(branch) == sha


# (a) ---------------------------------------------------------------------


@pytest.mark.parametrize("where", ["main", "worktree"])
def test_push_with_valid_signoff_in_common_dir_succeeds(installed: Sandbox, where: str) -> None:
    cwd = installed.checkout(where)
    sha = installed.commit(cwd, f"a-{where}.txt")
    installed.write_signoff(cwd, sha)

    proc = installed.push(cwd, f"a-{where}")

    assert proc.returncode == 0, (
        f"push from {where} with signoff at "
        f"{installed.common_dir(cwd) / 'reviews'} was refused:\n{proc.stderr}"
    )
    assert installed.remote_sha(f"a-{where}") == sha


# (b) ---------------------------------------------------------------------


@pytest.mark.parametrize("where", ["main", "worktree"])
def test_push_without_signoff_is_refused_and_names_common_dir_path(
    installed: Sandbox, where: str
) -> None:
    cwd = installed.checkout(where)
    sha = installed.commit(cwd, f"b-{where}.txt")

    proc = installed.push(cwd, f"b-{where}")

    assert proc.returncode != 0, f"push from {where} without signoff was allowed"
    assert installed.remote_sha(f"b-{where}") is None
    assert NOT_FOUND_MSG in proc.stderr

    # The gate must point the user at a location where a signoff would
    # actually be honoured: <git-common-dir>/reviews/<sha>.signoff.json.
    match = re.search(r"Expected signoff:\s*(\S+)", proc.stderr)
    assert match, f"gate did not print an 'Expected signoff:' path:\n{proc.stderr}"
    printed = Path(match.group(1))
    if not printed.is_absolute():
        printed = cwd / printed
    expected = installed.common_dir(cwd) / "reviews" / f"{sha}.signoff.json"
    assert os.path.realpath(printed) == os.path.realpath(expected), (
        f"gate in {where} names {printed}, but signoffs live at {expected}"
    )


# (c) ---------------------------------------------------------------------


def test_main_checkout_and_worktree_behave_identically(installed: Sandbox) -> None:
    outcomes = {
        (where, signed): _push_outcome(installed, where, signed)
        for where in ("main", "worktree")
        for signed in (True, False)
    }
    for signed in (True, False):
        assert outcomes[("main", signed)] == outcomes[("worktree", signed)], (
            f"signoff={signed}: main={outcomes[('main', signed)]} "
            f"worktree={outcomes[('worktree', signed)]} (pushed_ok, remote_updated)"
        )
    # And the shared behaviour is the correct one.
    assert outcomes[("main", True)] == (True, True)
    assert outcomes[("main", False)] == (False, False)


# (d) ---------------------------------------------------------------------


@pytest.mark.parametrize("where", ["main", "worktree"])
def test_install_hooks_sets_relative_hookspath(sandbox: Sandbox, where: str) -> None:
    cwd = sandbox.checkout(where)

    proc = sandbox.install(where)

    assert proc.returncode == 0, f"install-hooks.sh from {where} failed:\n{proc.stderr}"
    assert _git(cwd, sandbox.env, "config", "core.hooksPath") == ".githooks"
    assert (sandbox.common_dir(cwd) / "reviews").is_dir()


# (e) ---------------------------------------------------------------------


@pytest.mark.parametrize("where", ["main", "worktree"])
def test_install_hooks_overrides_absolute_git_hooks_path(
    sandbox: Sandbox, where: str
) -> None:
    cwd = sandbox.checkout(where)
    stale = str(sandbox.common_dir(cwd) / "hooks")
    _git(cwd, sandbox.env, "config", "core.hooksPath", stale)

    proc = sandbox.install(where)
    after = _run(["git", "config", "core.hooksPath"], cwd, sandbox.env).stdout.strip()

    assert after != stale, "install-hooks.sh left the absolute .git/hooks path active"
    # The installer must replace the stale path and succeed: exactly 0.
    assert proc.returncode == 0, (
        f"install-hooks.sh from {where} exited {proc.returncode}, expected 0:\n{proc.stderr}"
    )
    assert after == ".githooks"


# (f) [ingester only] -----------------------------------------------------


def test_pre_push_hook_is_tracked_and_executable() -> None:
    hook = REPO_ROOT / ".githooks" / "pre-push"
    assert hook.is_file(), ".githooks/pre-push is missing"
    assert os.access(hook, os.X_OK), ".githooks/pre-push is not executable"
    proc = subprocess.run(
        ["git", "ls-files", "-s", "--", ".githooks/pre-push"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        pytest.fail(".githooks/pre-push is not tracked by git")
    assert proc.stdout.split()[0] == "100755", (
        f".githooks/pre-push tracked with mode {proc.stdout.split()[0]}, expected 100755"
    )


def _shell_enables_pipefail(shell: str) -> bool:
    """True if a GitHub Actions `shell:` value runs with pipefail.

    `bash` (explicit) expands to `bash --noprofile --norc -eo pipefail {0}`;
    a custom template such as `bash -eo pipefail {0}` must say so itself.
    The implicit default (`bash -e {0}`, shell unset) has no pipefail.
    """
    return shell.strip() == "bash" or "pipefail" in shell


def test_ci_pytest_steps_propagate_exit_status() -> None:
    """A CI-side hook check is only real if a failing test fails the job.

    GitHub Actions' default shell is `bash -e {0}` (no pipefail), so
    `pytest ... | tail` reports tail's exit status and is always green.
    The check uses each step's EFFECTIVE shell: step `shell:` > job
    `defaults.run.shell` > workflow `defaults.run.shell` > default `bash -e {0}`.
    """
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    default_shell = (workflow.get("defaults") or {}).get("run", {}).get("shell")
    offenders = []
    for job_name, job in workflow["jobs"].items():
        job_shell = (job.get("defaults") or {}).get("run", {}).get("shell")
        for step in job.get("steps", []):
            script = step.get("run") or ""
            if "pytest" not in script or "|" not in script:
                continue
            effective = step.get("shell") or job_shell or default_shell or "bash -e {0}"
            script_sets_pipefail = "set -o pipefail" in script or "set -eo pipefail" in script
            if not (_shell_enables_pipefail(effective) or script_sets_pipefail):
                offenders.append(f"{job_name}: {step.get('name', '<unnamed>')} (shell={effective})")
    assert not offenders, (
        "CI steps pipe pytest output without pipefail, masking failures: " + ", ".join(offenders)
    )


# =========================================================================
# G0-5 round 2: per-ref signoff + signoff-schema acceptance tests
# (grumpy F1/F3/F4/F5/F6, security F1/F3 in docs/evidence/2026-10-07-g0-5-*.md)
# Identical in terms-analysis and legal-corpus-ingester.
# =========================================================================


def _pass_signoff(sha: str) -> dict:
    return {
        "head_sha": sha,
        "security_engineer": {"verdict": "PASS", "findings": []},
        "grumpy_developer": {"verdict": "PASS", "findings": []},
    }


def _write_signoff_doc(sb: Sandbox, cwd: Path, sha: str, doc: dict) -> Path:
    """Write an arbitrary signoff document under <common>/reviews/<sha>.signoff.json."""
    reviews = sb.common_dir(cwd) / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    path = reviews / f"{sha}.signoff.json"
    path.write_text(json.dumps(doc))
    return path


def _push_refspecs(
    sb: Sandbox, cwd: Path, *refspecs: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**sb.env, **(extra_env or {})}
    return _run(["git", "push", "origin", *refspecs], cwd, env)


def _branch_commit(sb: Sandbox, cwd: Path, branch: str, name: str) -> str:
    """Commit `name` on a new branch `branch`, then return to `main`."""
    _git(cwd, sb.env, "checkout", "-q", "-b", branch)
    sha = sb.commit(cwd, name)
    _git(cwd, sb.env, "checkout", "-q", "main")
    return sha


def _signed_head(sb: Sandbox, cwd: Path, name: str) -> str:
    """Advance main's HEAD with a commit that has a valid PASS signoff."""
    sha = sb.commit(cwd, name)
    _write_signoff_doc(sb, cwd, sha, _pass_signoff(sha))
    return sha


# Per-ref signoff ---------------------------------------------------------


def test_head_signed_push_of_unsigned_sha_to_main_is_refused(installed: Sandbox) -> None:
    cwd = installed.main
    before = installed.remote_sha("main")
    unsigned = installed.commit(cwd, "unsigned.txt")
    _signed_head(installed, cwd, "signed-head.txt")  # HEAD is signed, descends from unsigned

    proc = _push_refspecs(installed, cwd, f"{unsigned}:refs/heads/main")

    assert proc.returncode != 0, (
        f"unsigned {unsigned[:12]} reached main on HEAD's signoff:\n{proc.stderr}"
    )
    assert installed.remote_sha("main") == before


def test_head_signed_push_of_other_unsigned_branch_is_refused(installed: Sandbox) -> None:
    cwd = installed.main
    unsigned = _branch_commit(installed, cwd, "probe-unsigned", "probe-unsigned.txt")
    _signed_head(installed, cwd, "signed-head.txt")

    proc = _push_refspecs(installed, cwd, "probe-unsigned")

    assert proc.returncode != 0, (
        f"unsigned branch probe-unsigned ({unsigned[:12]}) pushed on HEAD's signoff"
    )
    assert installed.remote_sha("probe-unsigned") is None


def test_multi_ref_push_with_one_unsigned_ref_is_refused(installed: Sandbox) -> None:
    cwd = installed.main
    unsigned = _branch_commit(installed, cwd, "probe-unsigned", "probe-unsigned.txt")
    signed = _signed_head(installed, cwd, "signed-head.txt")

    proc = _push_refspecs(
        installed,
        cwd,
        f"{signed}:refs/heads/probe-signed",
        f"{unsigned}:refs/heads/probe-unsigned",
    )

    assert proc.returncode != 0, "multi-ref push with an unsigned ref was allowed"
    assert installed.remote_sha("probe-unsigned") is None


def test_delete_ref_push_is_allowed_without_signoff(installed: Sandbox) -> None:
    cwd = installed.main
    seed = installed.remote_sha("main")
    assert seed is not None
    # Create the remote ref directly in the bare repo (no push, no hook).
    _git(
        installed.root,
        installed.env,
        "--git-dir",
        str(installed.remote),
        "update-ref",
        "refs/heads/probe-delete",
        seed,
    )
    # HEAD (seed) has no signoff; a deletion sends nothing to review.
    assert not (installed.common_dir(cwd) / "reviews" / f"{seed}.signoff.json").exists()

    proc = _push_refspecs(installed, cwd, ":refs/heads/probe-delete")

    assert proc.returncode == 0, f"branch deletion was refused:\n{proc.stderr}"
    assert installed.remote_sha("probe-delete") is None


def test_multi_ref_push_with_all_refs_signed_is_allowed(installed: Sandbox) -> None:
    cwd = installed.main
    a = _branch_commit(installed, cwd, "probe-a", "probe-a.txt")
    b = _branch_commit(installed, cwd, "probe-b", "probe-b.txt")
    _write_signoff_doc(installed, cwd, a, _pass_signoff(a))
    _write_signoff_doc(installed, cwd, b, _pass_signoff(b))
    installed.commit(cwd, "unsigned-head.txt")  # HEAD is not pushed and not signed

    proc = _push_refspecs(installed, cwd, "probe-a", "probe-b")

    assert proc.returncode == 0, f"all-signed multi-ref push was refused:\n{proc.stderr}"
    assert installed.remote_sha("probe-a") == a
    assert installed.remote_sha("probe-b") == b


# Signoff schema ----------------------------------------------------------


def test_signoff_whose_head_sha_differs_from_filename_sha_is_refused(installed: Sandbox) -> None:
    cwd = installed.main
    pushed = _branch_commit(installed, cwd, "probe-mismatch", "probe-mismatch.txt")
    head = _signed_head(installed, cwd, "signed-head.txt")
    # A copy of HEAD's genuine signoff, renamed to the pushed sha.
    _write_signoff_doc(installed, cwd, pushed, _pass_signoff(head))

    proc = _push_refspecs(installed, cwd, f"{pushed}:refs/heads/probe-mismatch")

    assert proc.returncode != 0, "signoff with head_sha != filename sha was accepted"
    assert installed.remote_sha("probe-mismatch") is None
    assert "head_sha" in proc.stderr, f"refusal does not name head_sha:\n{proc.stderr}"


def test_override_used_string_false_is_refused(installed: Sandbox) -> None:
    cwd = installed.main
    sha = installed.commit(cwd, "override-string.txt")
    _write_signoff_doc(
        installed,
        cwd,
        sha,
        {
            "head_sha": sha,
            "override": {"used": "false", "reason": "probe", "authorized_by": "probe"},
        },
    )

    proc = installed.push(cwd, "probe-override-string")

    assert proc.returncode != 0, 'override.used == "false" (a string) enabled the override'
    assert installed.remote_sha("probe-override-string") is None


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"used": True}, id="no-reason-no-authorizer"),
        pytest.param({"used": True, "authorized_by": "owner"}, id="missing-reason"),
        pytest.param({"used": True, "reason": "hotfix"}, id="missing-authorized_by"),
        pytest.param({"used": True, "reason": "  ", "authorized_by": "owner"}, id="blank-reason"),
        pytest.param(
            {"used": True, "reason": "hotfix", "authorized_by": ""}, id="empty-authorizer"
        ),
        pytest.param({"used": True, "reason": 1, "authorized_by": "owner"}, id="non-string-reason"),
    ],
)
def test_override_true_without_reason_and_authorizer_is_refused(
    installed: Sandbox, override: dict
) -> None:
    cwd = installed.main
    sha = installed.commit(cwd, "override-incomplete.txt")
    _write_signoff_doc(installed, cwd, sha, {"head_sha": sha, "override": override})

    proc = installed.push(cwd, "probe-override")

    assert proc.returncode != 0, f"override {override} accepted without reason+authorized_by"
    assert installed.remote_sha("probe-override") is None


def test_complete_override_is_allowed(installed: Sandbox) -> None:
    """Control: a well-formed override must still work after tightening."""
    cwd = installed.main
    sha = installed.commit(cwd, "override-ok.txt")
    _write_signoff_doc(
        installed,
        cwd,
        sha,
        {"head_sha": sha, "override": {"used": True, "reason": "hotfix", "authorized_by": "owner"}},
    )

    proc = installed.push(cwd, "probe-override-ok")

    assert proc.returncode == 0, f"complete override was refused:\n{proc.stderr}"
    assert installed.remote_sha("probe-override-ok") == sha


def test_security_pass_with_non_empty_findings_is_refused(installed: Sandbox) -> None:
    cwd = installed.main
    sha = installed.commit(cwd, "pass-with-findings.txt")
    doc = _pass_signoff(sha)
    doc["security_engineer"]["findings"] = [{"id": "F1", "severity": "HIGH"}]
    _write_signoff_doc(installed, cwd, sha, doc)

    proc = installed.push(cwd, "probe-findings")

    assert proc.returncode != 0, "security PASS with open findings was accepted"
    assert installed.remote_sha("probe-findings") is None


@pytest.mark.parametrize("role", ["security_engineer", "grumpy_developer"])
def test_reviewer_pass_without_findings_key_is_refused(installed: Sandbox, role: str) -> None:
    cwd = installed.main
    sha = installed.commit(cwd, f"pass-no-findings-{role}.txt")
    doc = _pass_signoff(sha)
    del doc[role]["findings"]  # a PASS must state "findings": [] explicitly
    _write_signoff_doc(installed, cwd, sha, doc)

    proc = installed.push(cwd, f"probe-nokey-{role}")

    assert proc.returncode != 0, f"{role} PASS with no findings key was accepted"
    assert installed.remote_sha(f"probe-nokey-{role}") is None


# CDPATH ------------------------------------------------------------------


@pytest.fixture
def cdpath_decoy(tmp_path: Path) -> dict[str, str]:
    """An exported CDPATH whose entry contains its own `.git/reviews`."""
    decoy = tmp_path / "cdpath-decoy"
    (decoy / ".git" / "reviews").mkdir(parents=True)
    return {"CDPATH": str(decoy.resolve())}


def test_exported_cdpath_does_not_break_signed_push(
    installed: Sandbox, cdpath_decoy: dict[str, str]
) -> None:
    cwd = installed.main
    sha = _signed_head(installed, cwd, "cdpath-signed.txt")

    proc = _push_refspecs(installed, cwd, "HEAD:refs/heads/probe-cdpath", extra_env=cdpath_decoy)

    assert proc.returncode == 0, f"signed push refused with CDPATH exported:\n{proc.stderr}"
    assert installed.remote_sha("probe-cdpath") == sha


def test_exported_cdpath_does_not_change_expected_signoff_path(
    installed: Sandbox, cdpath_decoy: dict[str, str]
) -> None:
    cwd = installed.main
    sha = installed.commit(cwd, "cdpath-unsigned.txt")

    proc = _push_refspecs(installed, cwd, "HEAD:refs/heads/probe-cdpath", extra_env=cdpath_decoy)

    assert proc.returncode != 0
    assert installed.remote_sha("probe-cdpath") is None
    match = re.search(r"Expected signoff:[ \t]*(\S+)[ \t]*$", proc.stderr, re.MULTILINE)
    assert match, f"no single-line 'Expected signoff:' path:\n{proc.stderr}"
    expected = installed.common_dir(cwd) / "reviews" / f"{sha}.signoff.json"
    assert os.path.realpath(match.group(1)) == os.path.realpath(expected), proc.stderr


def test_exported_cdpath_does_not_change_install(
    sandbox: Sandbox, cdpath_decoy: dict[str, str]
) -> None:
    cwd = sandbox.main
    proc = _run(["bash", "scripts/install-hooks.sh"], cwd, {**sandbox.env, **cdpath_decoy})

    assert proc.returncode == 0, f"install-hooks.sh failed with CDPATH exported:\n{proc.stderr}"
    assert _git(cwd, sandbox.env, "config", "core.hooksPath") == ".githooks"
    assert (sandbox.common_dir(cwd) / "reviews").is_dir(), (
        f"reviews/ not created in the common git dir with CDPATH exported:\n{proc.stdout}"
    )
    assert cdpath_decoy["CDPATH"] not in proc.stdout + proc.stderr, (
        f"installer resolved the common dir through CDPATH:\n{proc.stdout}{proc.stderr}"
    )


# Tracked hook files ------------------------------------------------------


def _tracked_mode(rel: str) -> str:
    """Index mode of a tracked file in the REAL repo (read-only git query)."""
    proc = subprocess.run(
        ["git", "ls-files", "-s", "--", rel],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    assert proc.returncode == 0 and proc.stdout.strip(), f"{rel} is not tracked by git"
    return proc.stdout.split()[0]


@pytest.mark.parametrize(
    "rel", [".githooks/pre-push", ".githooks/pre-commit", "scripts/install-hooks.sh"]
)
def test_hook_files_are_tracked_executable(rel: str) -> None:
    # Index mode only: no chmod, no working-tree permission check.
    assert _tracked_mode(rel) == "100755", f"{rel} is not tracked as 100755"


def test_pre_push_hook_matches_checked_in_canonical_sha256() -> None:
    """Parity: both repos ship a byte-identical canonical hook, pinned by a
    checked-in `.githooks/pre-push.sha256` (sha256sum format) in each repo."""
    pin = REPO_ROOT / ".githooks" / "pre-push.sha256"
    assert pin.is_file(), ".githooks/pre-push.sha256 is missing"
    fields = pin.read_text().split()
    assert fields and re.fullmatch(r"[0-9a-f]{64}", fields[0]), (
        f".githooks/pre-push.sha256 is not a sha256sum line: {pin.read_text()!r}"
    )
    actual = hashlib.sha256((REPO_ROOT / ".githooks" / "pre-push").read_bytes()).hexdigest()
    assert actual == fields[0], (
        f".githooks/pre-push sha256 {actual} != pinned {fields[0]}; "
        "edit the canonical hook in BOTH repos and re-pin"
    )
