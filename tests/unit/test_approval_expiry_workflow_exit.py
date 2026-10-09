"""The approval-expiry job must go red when check_approvals.py exits non-zero (terms-analysis#173).

Behavioural: the real `Run approval check` step script is extracted from
.github/workflows/approval-expiry.yml and executed with the shell GitHub would
use for it, in a sandbox workspace. A `|| true`, a status-swallowing pipe or a
missing pipefail turns these red.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "approval-expiry.yml"
_JOB = "approval-expiry"
_STEP = "Run approval check"
# GitHub's shell for a `run:` step with no `shell:` anywhere on a Linux runner.
_GITHUB_DEFAULT_SHELL = "bash -e {0}"
_FIXTURE_SOURCE = _REPO_ROOT / "tests" / "fixtures" / "sources" / "eurlex.yaml"
_EXIT_CONFIG = 2


def _load_step() -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    """Return (run script, effective shell, job, step) for the approval-check step."""
    try:
        doc = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        # GitHub rejects an unparseable workflow at load time, so the job never runs at all.
        pytest.fail(f"{_WORKFLOW.name} does not parse, so the scheduled job never runs: {exc}")
    job = doc["jobs"][_JOB]
    matches = [s for s in job["steps"] if s.get("name") == _STEP]
    assert len(matches) == 1, f"expected exactly one step named {_STEP!r}"
    step = matches[0]
    shell = (
        step.get("shell")
        or ((job.get("defaults") or {}).get("run") or {}).get("shell")
        or ((doc.get("defaults") or {}).get("run") or {}).get("shell")
        or _GITHUB_DEFAULT_SHELL
    )
    return str(step["run"]), str(shell), job, step


def _run_step(workspace: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    script, shell, _job, _step = _load_step()
    script_path = tmp_path / "step.sh"
    script_path.write_text(script)
    # `python` on PATH resolves to the interpreter running the suite, as the venv does on the runner.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python").symlink_to(sys.executable)
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    argv = [part.replace("{0}", str(script_path)) for part in shlex.split(shell)]
    return subprocess.run(argv, cwd=workspace, env=env, capture_output=True, text=True, timeout=60, check=False)


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "config" / "sources").mkdir(parents=True)
    (ws / "config" / "sources" / "eurlex.yaml").write_text(_FIXTURE_SOURCE.read_text())
    (ws / "config" / "approvals").mkdir()
    return ws


@pytest.mark.parametrize("checker_rc", [0, 1, 2, 3])
def test_approval_check_step_exit_equals_checker_exit(tmp_path: Path, checker_rc: int) -> None:
    ws = _workspace(tmp_path)
    (ws / "scripts").mkdir()
    log = tmp_path / "checker-argv.log"
    # Fake checker: records that it ran, then exits with the code under test.
    (ws / "scripts" / "check_approvals.py").write_text(
        "import sys\n"
        f"open({str(log)!r}, 'w').write(' '.join(sys.argv[1:]))\n"
        f"sys.exit({checker_rc})\n"
    )
    result = _run_step(ws, tmp_path)
    assert log.read_text() == "--approvals-dir config/approvals", "the step did not run the checker"
    assert result.returncode == checker_rc, (
        f"step swallowed checker exit {checker_rc} (got {result.returncode}); "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_approval_check_step_fails_job_on_empty_approvals_dir(tmp_path: Path) -> None:
    # Real checker, sources configured, approvals dir empty: the job must go red with exit 2.
    ws = _workspace(tmp_path)
    shutil.copytree(_REPO_ROOT / "scripts", ws / "scripts")
    result = _run_step(ws, tmp_path)
    assert result.returncode == _EXIT_CONFIG, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "config/approvals" in result.stderr


def test_approval_check_step_failure_is_not_masked_by_continue_on_error() -> None:
    # Job-level semantics cannot be executed locally; pin the exact keys instead.
    _script, _shell, job, step = _load_step()
    assert step.get("continue-on-error", False) is False
    assert job.get("continue-on-error", False) is False
