"""Implementation tests for the G0-5 P9 gate fix (terms-analysis#175).

Complements the acceptance suite in test_p9_prepush_gate.py (reusing its
throwaway Sandbox: main checkout + linked worktree + bare remote, isolated
HOME/git config). These cover the validations that must survive the
common-dir change, and installer behaviour beyond the acceptance contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.unit.test_p9_prepush_gate import (  # noqa: F401
    REPO_ROOT,
    Sandbox,
    _git,
    _run,
    _shell_enables_pipefail,
    pytestmark,
)


@pytest.fixture
def sb(tmp_path: Path) -> Sandbox:
    box = Sandbox(tmp_path)
    proc = box.install("worktree")
    assert proc.returncode == 0, proc.stderr
    return box


def _signoff(sb: Sandbox, cwd: Path, payload: dict[str, object]) -> None:
    reviews = sb.common_dir(cwd) / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    (reviews / f"{payload['head_sha']}.signoff.json").write_text(json.dumps(payload))


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda d: d["security_engineer"].update(verdict="FAIL"), "security_engineer verdict: FAIL"),
        (lambda d: d["grumpy_developer"].update(verdict="FAIL"), "grumpy_developer verdict: FAIL"),
        (lambda d: d.update(head_sha="0" * 40), "head_sha mismatch"),
    ],
)
def test_worktree_invalid_signoff_is_refused(sb: Sandbox, mutate, needle: str) -> None:
    cwd = sb.wt
    sha = sb.commit(cwd, "invalid.txt")
    payload: dict = {
        "head_sha": sha,
        "security_engineer": {"verdict": "PASS", "findings": []},
        "grumpy_developer": {"verdict": "PASS", "findings": []},
    }
    mutate(payload)
    # Always store under the real HEAD sha so only content validation is tested.
    reviews = sb.common_dir(cwd) / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    (reviews / f"{sha}.signoff.json").write_text(json.dumps(payload))

    proc = sb.push(cwd, "invalid")

    assert proc.returncode != 0
    assert sb.remote_sha("invalid") is None
    assert "signoff invalid" in proc.stderr
    assert needle in proc.stderr


def test_worktree_malformed_json_is_refused(sb: Sandbox) -> None:
    sha = sb.commit(sb.wt, "badjson.txt")
    reviews = sb.common_dir(sb.wt) / "reviews"
    (reviews / f"{sha}.signoff.json").write_text("{not json")

    proc = sb.push(sb.wt, "badjson")

    assert proc.returncode != 0
    assert sb.remote_sha("badjson") is None
    assert "not valid JSON" in proc.stderr


def test_worktree_override_is_honoured_and_announced(sb: Sandbox) -> None:
    sha = sb.commit(sb.wt, "override.txt")
    _signoff(
        sb,
        sb.wt,
        {
            "head_sha": sha,
            "security_engineer": {"verdict": "FAIL"},
            "grumpy_developer": {"verdict": "FAIL"},
            "override": {"used": True, "reason": "hotfix", "authorized_by": "owner"},
        },
    )

    proc = sb.push(sb.wt, "override")

    assert proc.returncode == 0, proc.stderr
    assert sb.remote_sha("override") == sha
    assert "P9 OVERRIDE ACTIVE: hotfix (authorized by owner)" in proc.stderr


def test_signoff_dir_is_shared_not_per_worktree(sb: Sandbox) -> None:
    """A signoff written via the main checkout's common dir gates a worktree push."""
    sha = sb.commit(sb.wt, "shared.txt")
    assert sb.common_dir(sb.main) == sb.common_dir(sb.wt)
    _signoff(
        sb,
        sb.main,
        {"head_sha": sha, "security_engineer": {"verdict": "PASS", "findings": []}, "grumpy_developer": {"verdict": "PASS", "findings": []}},
    )

    assert sb.push(sb.wt, "shared").returncode == 0
    assert sb.remote_sha("shared") == sha


def test_install_is_idempotent(sb: Sandbox) -> None:
    second = sb.install("worktree")
    third = sb.install("main")

    assert second.returncode == 0 and third.returncode == 0
    assert "replacing core.hooksPath" not in second.stderr + third.stderr
    assert _git(sb.main, sb.env, "config", "core.hooksPath") == ".githooks"


def test_install_announces_replacement_of_stale_path(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)
    stale = str(box.common_dir(box.main) / "hooks")
    _git(box.main, box.env, "config", "core.hooksPath", stale)

    proc = box.install("worktree")

    assert proc.returncode == 0, proc.stderr
    assert f"install-hooks: replacing core.hooksPath={stale} with .githooks" in proc.stderr
    assert _git(box.wt, box.env, "config", "core.hooksPath") == ".githooks"


def test_install_refuses_when_effective_value_is_shadowed(tmp_path: Path) -> None:
    """A per-worktree hooksPath outranks the local one; installer must not claim success."""
    box = Sandbox(tmp_path)
    _git(box.main, box.env, "config", "extensions.worktreeConfig", "true")
    _git(box.wt, box.env, "config", "--worktree", "core.hooksPath", "/nonexistent/hooks")

    proc = box.install("worktree")

    assert proc.returncode != 0
    assert "core.hooksPath is still '/nonexistent/hooks'" in proc.stderr


def test_ci_workflow_default_shell_is_pipefail() -> None:
    # Behavioural check, not a string pin: `shell: bash` is equally correct
    # (grumpy F7, docs/evidence/2026-10-07-g0-5-grumpy.md).
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    assert _shell_enables_pipefail(workflow["defaults"]["run"]["shell"])
