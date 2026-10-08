"""Fix-coder unit tests for the P9 pre-push gate, round 1 (terms-analysis#175).

Covers the resolutions in docs/evidence/2026-10-07-g0-5-fix-r1.md that the
acceptance suite (test_p9_prepush_gate.py) does not pin:

- cross-repo parity: the hook and installer are byte-identical in
  terms-analysis and legal-corpus-ingester (grumpy F5)
- the REAL checkout has the gate wired (grumpy F2); skipped in CI, where the
  fresh checkout has no local hook config
- per-ref edge cases: annotated tags, malformed stdin (grumpy F1, security F1);
  the empty-stdin case moved to test_p9_gate_fix_r2.py when its contract
  changed to exit 0 (round-2 lead decision)
- schema edge cases: `override: null`, non-object signoff (grumpy F3)
- the validator runs `python3 -I`, so a json.py planted in the checkout cannot
  replace the JSON parser

Sandbox commands reuse the acceptance harness: throwaway main checkout,
linked worktree and local bare remote under tmp_path, isolated HOME and git
config. Only read-only git queries touch the real repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_p9_prepush_gate import (  # noqa: F401
    REPO_ROOT,
    Sandbox,
    _git,
    _run,
    pytestmark,
)

# Shared pins. The same constants appear in the sibling repo's copy of this
# test, so a one-sided edit of either file fails that repo's suite.
CANONICAL_PRE_PUSH_SHA256 = "86ab230230467d6c572ee41745805d6b83a431ec6f25f77b0691fd085283106f"
CANONICAL_INSTALLER_SHA256 = "3dd85d1670d11a6dea98ba9a8d79c75d89990bcc25979e5dfac59964822a736c"

ZERO_SHA = "0" * 40


def _sha256(rel: str) -> str:
    return hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()


# Parity (grumpy F5) -------------------------------------------------------


def test_pre_push_hook_matches_shared_canonical_hash() -> None:
    assert _sha256(".githooks/pre-push") == CANONICAL_PRE_PUSH_SHA256, (
        "pre-push diverged from the canonical hook shared with the sibling repo"
    )


def test_pre_push_pin_file_matches_shared_canonical_hash() -> None:
    pinned = (REPO_ROOT / ".githooks" / "pre-push.sha256").read_text().split()[0]
    assert pinned == CANONICAL_PRE_PUSH_SHA256


def test_installer_matches_shared_canonical_hash() -> None:
    assert _sha256("scripts/install-hooks.sh") == CANONICAL_INSTALLER_SHA256, (
        "install-hooks.sh diverged from the canonical installer shared with the sibling repo"
    )


def test_canonical_files_carry_no_repo_specific_names() -> None:
    for rel in (".githooks/pre-push", "scripts/install-hooks.sh"):
        text = (REPO_ROOT / rel).read_text()
        # The only repo name allowed is the shared card reference.
        stripped = text.replace("terms-analysis#175", "").replace(
            "terms-analysis and legal-corpus-ingester", ""
        )
        assert "terms-analysis" not in stripped, f"{rel} names terms-analysis"
        assert "legal-corpus-ingester" not in stripped, f"{rel} names legal-corpus-ingester"


# Real checkout is wired (grumpy F2) --------------------------------------


def in_ci(value: str | None) -> bool:
    """True only for the CI markers runners actually set ("true", "1").

    `bool(os.environ.get("CI"))` also skipped on CI=false or CI=0, which
    silently disabled this guard on developer machines that export them.
    """
    return (value or "").strip().lower() in {"1", "true"}


def test_real_checkout_runs_the_tracked_gate() -> None:
    """Fails on a clone where install-hooks.sh was never run, or where
    core.hooksPath points anywhere other than the tracked .githooks/.

    Never skipped: under CI a checkout without the gate installed is a
    failure, so the workflow must run install-hooks.sh before the suite
    (QUALITY-BAR D, DEV-FUNDAMENTALS F9 and F12, terms-analysis#175 r5)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"

    def query(*args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    toplevel = Path(query("rev-parse", "--show-toplevel")).resolve()
    hooks = Path(query("rev-parse", "--git-path", "hooks"))
    hooks = (hooks if hooks.is_absolute() else toplevel / hooks).resolve()
    where = "the CI workflow must run" if in_ci(os.environ.get("CI")) else "run"
    assert hooks == toplevel / ".githooks", (
        f"git runs hooks from {hooks}, not the tracked {toplevel / '.githooks'}; "
        f"{where} `bash scripts/install-hooks.sh` in this checkout"
    )
    assert os.access(hooks / "pre-push", os.X_OK), ".githooks/pre-push is not executable"


# Hook invoked directly with crafted stdin ----------------------------------


def _hook(sb: Sandbox, cwd: Path, stdin: str) -> subprocess.CompletedProcess[str]:
    """Run the installed hook the way git does: `<remote> <url>` args + ref lines."""
    root = sb.env["P9_SANDBOX_ROOT"]
    real_cwd = Path(cwd).resolve()
    assert real_cwd == Path(root) or Path(root) in real_cwd.parents
    return subprocess.run(
        [str(cwd / ".githooks" / "pre-push"), "origin", str(sb.remote)],
        cwd=cwd,
        env=sb.env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture
def installed(tmp_path: Path) -> Sandbox:
    sb = Sandbox(tmp_path)
    proc = sb.install("main")
    assert proc.returncode == 0, proc.stderr
    return sb


def _pass(sha: str) -> dict:
    return {
        "head_sha": sha,
        "security_engineer": {"verdict": "PASS", "findings": []},
        "grumpy_developer": {"verdict": "PASS", "findings": []},
    }


def _signoff(sb: Sandbox, cwd: Path, sha: str, doc: object) -> None:
    reviews = sb.common_dir(cwd) / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    (reviews / f"{sha}.signoff.json").write_text(json.dumps(doc))


def test_malformed_sha_on_stdin_is_refused(installed: Sandbox) -> None:
    line = f"refs/heads/main ../../etc/passwd refs/heads/main {ZERO_SHA}\n"
    proc = _hook(installed, installed.main, line)
    assert proc.returncode != 0
    assert "is not an object id" in proc.stderr


def test_delete_line_is_allowed_and_signed_line_still_checked(installed: Sandbox) -> None:
    cwd = installed.main
    sha = installed.commit(cwd, "mixed.txt")
    stdin = (
        f"(delete) {ZERO_SHA} refs/heads/gone {sha}\n"
        f"refs/heads/main {sha} refs/heads/main {ZERO_SHA}\n"
    )
    # Unsigned non-delete line: refused even though the delete line is fine.
    assert _hook(installed, cwd, stdin).returncode != 0
    _signoff(installed, cwd, sha, _pass(sha))
    proc = _hook(installed, cwd, stdin)
    assert proc.returncode == 0, proc.stderr
    assert "no signoff needed" in proc.stdout


def test_annotated_tag_of_signed_commit_is_allowed(installed: Sandbox) -> None:
    cwd = installed.main
    sha = installed.commit(cwd, "tagged.txt")
    _signoff(installed, cwd, sha, _pass(sha))
    _git(cwd, installed.env, "tag", "-a", "v-signed", "-m", "signed")

    proc = _run(["git", "push", "origin", "v-signed"], cwd, installed.env)

    assert proc.returncode == 0, proc.stderr


def test_annotated_tag_of_unsigned_commit_is_refused(installed: Sandbox) -> None:
    cwd = installed.main
    sha = installed.commit(cwd, "tagged-unsigned.txt")
    _git(cwd, installed.env, "tag", "-a", "v-unsigned", "-m", "unsigned")

    proc = _run(["git", "push", "origin", "v-unsigned"], cwd, installed.env)

    assert proc.returncode != 0
    assert f"{sha}.signoff.json" in proc.stderr, "refusal must name the peeled commit's signoff"


# Signoff schema edges ------------------------------------------------------


@pytest.mark.parametrize(
    ("doc_for", "needle"),
    [
        pytest.param(
            lambda sha: {**_pass(sha), "override": None, "security_engineer": None},
            "security_engineer must be an object",
            id="override-null-and-null-section",
        ),
        pytest.param(lambda sha: [sha], "signoff must be a JSON object", id="list-document"),
        pytest.param(
            lambda sha: {**_pass(sha), "override": "yes"},
            "override must be an object",
            id="override-string",
        ),
        pytest.param(
            lambda sha: {"head_sha": sha, "override": {"used": 1, "reason": "r", "authorized_by": "a"}},
            "override.used must be true or false",
            id="override-used-int",
        ),
    ],
)
def test_malformed_signoff_is_refused_with_reason(installed: Sandbox, doc_for, needle: str) -> None:
    cwd = installed.main
    sha = installed.commit(cwd, "schema.txt")
    _signoff(installed, cwd, sha, doc_for(sha))

    proc = installed.push(cwd, "probe-schema")

    assert proc.returncode != 0
    assert installed.remote_sha("probe-schema") is None
    assert needle in proc.stderr, proc.stderr


def test_override_null_with_passing_verdicts_is_allowed(installed: Sandbox) -> None:
    cwd = installed.main
    sha = installed.commit(cwd, "override-null.txt")
    _signoff(installed, cwd, sha, {**_pass(sha), "override": None})

    proc = installed.push(cwd, "probe-override-null")

    assert proc.returncode == 0, proc.stderr
    assert "P9 OVERRIDE ACTIVE" not in proc.stderr


def test_override_announces_reason_and_authorizer(installed: Sandbox) -> None:
    cwd = installed.main
    sha = installed.commit(cwd, "override-announce.txt")
    _signoff(
        installed,
        cwd,
        sha,
        {"head_sha": sha, "override": {"used": True, "reason": "hotfix", "authorized_by": "owner"}},
    )

    proc = installed.push(cwd, "probe-announce")

    assert proc.returncode == 0, proc.stderr
    assert "P9 OVERRIDE ACTIVE: hotfix (authorized by owner)" in proc.stderr


def test_planted_json_module_cannot_replace_the_parser(installed: Sandbox) -> None:
    """Without `python3 -I`, a json.py at the checkout root would be imported
    by the validator and could wave an invalid signoff through."""
    cwd = installed.main
    (cwd / "json.py").write_text(
        "import sys\nprint('planted json.py ran', file=sys.stderr)\nsys.exit(0)\n"
    )
    sha = installed.commit(cwd, "planted.txt")
    _signoff(installed, cwd, sha, {"head_sha": sha, "security_engineer": {"verdict": "FAIL"}})

    proc = installed.push(cwd, "probe-planted")

    assert proc.returncode != 0
    assert "planted json.py ran" not in proc.stderr
    assert "security_engineer verdict: FAIL" in proc.stderr


def test_installer_leaves_pin_files_non_executable(tmp_path: Path) -> None:
    sb = Sandbox(tmp_path)
    pin = sb.main / ".githooks" / "pre-push.sha256"
    pin.write_text("0" * 64 + "  pre-push\n")
    pin.chmod(0o644)

    proc = sb.install("main")

    assert proc.returncode == 0, proc.stderr
    assert not os.access(pin, os.X_OK)
    assert os.access(sb.main / ".githooks" / "pre-push", os.X_OK)
