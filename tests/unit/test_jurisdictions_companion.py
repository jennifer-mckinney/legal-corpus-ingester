"""Acceptance tests: the jurisdiction sync test never skips in CI (terms-analysis#175 r5).

Findings: grumpy r5 HIGH and security r5 F5 (QUALITY-BAR D, "a skip is a
failure in CI"). Copilot's 5285610 made test_sync_script_is_idempotent skip
whenever the adjacent terms-analysis checkout is absent, which is every CI
run, so CI never exercised scripts/sync_jurisdictions.py.

Contract:
- TERMS_ANALYSIS_ROOT names the companion terms-analysis checkout. The sync
  script reads <root>/src/backend/app/schemas.py; unset or empty means the
  adjacent ../terms-analysis directory, as today.
- Under CI ("true" or "1"), a missing companion FAILS the sync test. Off CI
  it may skip, with a reason.
- The CI workflow provisions the companion checkout and points
  TERMS_ANALYSIS_ROOT at it for the unit tests.

Behavioural cases run a copy of the repo under tmp_path whose parent has no
terms-analysis, so the real worktree's sources are never rewritten.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
ROOT_ENV = "TERMS_ANALYSIS_ROOT"
NODE = "tests/unit/test_jurisdictions.py::test_sync_script_is_idempotent"
FIXTURE_CODE = "ZZ-R5-FIXTURE"
CHECKOUT = "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683"
COMPANION_REPO = "jennifer-mckinney/terms-analysis"
UNIT_STEP = "Unit tests"


def _copy_repo(tmp_path: Path) -> Path:
    """<tmp>/work/legal-corpus-ingester, with no terms-analysis beside it."""
    repo = tmp_path / "work" / "legal-corpus-ingester"
    ignore = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc", ".coverage*")
    for name in ("scripts", "src", "tests"):
        shutil.copytree(REPO_ROOT / name, repo / name, ignore=ignore)
    shutil.copy2(REPO_ROOT / "pyproject.toml", repo / "pyproject.toml")
    assert not (repo.parent / "terms-analysis").exists()
    return repo


def _companion(tmp_path: Path) -> Path:
    root = tmp_path / "companion" / "terms-analysis"
    schemas = root / "src" / "backend" / "app" / "schemas.py"
    schemas.parent.mkdir(parents=True)
    schemas.write_text(
        "from typing import Literal\n\n"
        f'Jurisdiction = Literal["US-CA", "GDPR", "PIPEDA", "LGPD", "{FIXTURE_CODE}"]\n'
    )
    return root


def _env(**extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"CI", "GITHUB_ACTIONS", ROOT_ENV, "PYTHONPATH"} and not k.startswith("PYTEST_")
    }
    env["PYTHONPATH"] = "src"
    env.update(extra)
    return env


def _sync(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "scripts/sync_jurisdictions.py"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _sync_test(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", NODE, "-q", "-rs", "-p", "no:cacheprovider", "-o", "addopts="],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _summary(proc: subprocess.CompletedProcess[str]) -> str:
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    return lines[-1] if lines else ""


# The sync script honours TERMS_ANALYSIS_ROOT ------------------------------


def test_sync_script_reads_the_companion_named_by_the_env(tmp_path: Path) -> None:
    repo = _copy_repo(tmp_path)
    root = _companion(tmp_path)

    proc = _sync(repo, _env(**{ROOT_ENV: str(root)}))

    assert proc.returncode == 0, proc.stderr
    out = (repo / "src" / "legal_corpus_ingester" / "jurisdictions.py").read_text()
    assert f'    "{FIXTURE_CODE}",' in out


def test_sync_script_fails_closed_when_the_named_companion_is_missing(tmp_path: Path) -> None:
    repo = _copy_repo(tmp_path)
    missing = tmp_path / "nowhere" / "terms-analysis"
    before = (repo / "src" / "legal_corpus_ingester" / "jurisdictions.py").read_text()

    proc = _sync(repo, _env(**{ROOT_ENV: str(missing)}))

    assert proc.returncode == 1
    assert str(missing) in proc.stderr, proc.stderr
    assert (repo / "src" / "legal_corpus_ingester" / "jurisdictions.py").read_text() == before


# The sync test: fails in CI, may skip locally -----------------------------


@pytest.mark.parametrize("ci", ["true", "1"])
def test_sync_test_fails_not_skips_in_ci_without_the_companion(tmp_path: Path, ci: str) -> None:
    repo = _copy_repo(tmp_path)

    proc = _sync_test(repo, _env(CI=ci, **{ROOT_ENV: str(tmp_path / "nowhere")}))

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert _summary(proc).startswith("1 failed"), proc.stdout
    assert "skipped" not in _summary(proc)


@pytest.mark.parametrize("ci", ["true", "1"])
def test_sync_test_fails_in_ci_when_the_companion_is_only_implied(tmp_path: Path, ci: str) -> None:
    """No TERMS_ANALYSIS_ROOT and no adjacent checkout: still a failure in CI."""
    repo = _copy_repo(tmp_path)

    proc = _sync_test(repo, _env(CI=ci))

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert _summary(proc).startswith("1 failed"), proc.stdout


@pytest.mark.parametrize("ci", [None, "false", "0", ""])
def test_sync_test_may_skip_off_ci_without_the_companion(tmp_path: Path, ci: str | None) -> None:
    repo = _copy_repo(tmp_path)
    extra = {} if ci is None else {"CI": ci}

    proc = _sync_test(repo, _env(**extra))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _summary(proc).startswith("1 skipped"), proc.stdout
    assert "terms-analysis" in proc.stdout


def test_sync_test_runs_and_passes_in_ci_with_the_companion(tmp_path: Path) -> None:
    repo = _copy_repo(tmp_path)
    root = _companion(tmp_path)

    proc = _sync_test(repo, _env(CI="true", **{ROOT_ENV: str(root)}))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _summary(proc).startswith("1 passed"), proc.stdout
    out = (repo / "src" / "legal_corpus_ingester" / "jurisdictions.py").read_text()
    assert f'    "{FIXTURE_CODE}",' in out, "the test did not read the named companion"


# The workflow provisions the companion ------------------------------------
# Static by necessity: actions/checkout cannot run outside GitHub, so the
# exact identifiers are pinned.


def _steps() -> list[dict[str, Any]]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        if any(step.get("name") == UNIT_STEP for step in job["steps"]):
            return list(job["steps"])
    raise AssertionError(f"no job in {WORKFLOW.name} has a '{UNIT_STEP}' step")


def test_ci_checks_out_the_companion_and_points_the_unit_tests_at_it() -> None:
    steps = _steps()
    companions = [
        (n, s)
        for n, s in enumerate(steps)
        if s.get("uses") == CHECKOUT and (s.get("with") or {}).get("repository") == COMPANION_REPO
    ]
    assert len(companions) == 1, f"expected one {CHECKOUT} step for {COMPANION_REPO}"
    checkout_at, checkout = companions[0]
    path = (checkout.get("with") or {}).get("path", "")
    assert path and not path.startswith(("/", "..")), f"companion path {path!r} must be inside the workspace"
    unit = [(n, s) for n, s in enumerate(steps) if s.get("name") == UNIT_STEP]
    assert len(unit) == 1
    unit_at, unit_step = unit[0]

    assert checkout_at < unit_at
    assert (unit_step.get("env") or {}).get(ROOT_ENV) in {path, "${{ github.workspace }}/" + path}
    assert "continue-on-error" not in checkout and "if" not in checkout
