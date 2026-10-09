"""Every workflow file is valid, and CI proves it (legal-corpus-ingester#21).

refresh.yml and approval-expiry.yml were invalid YAML: the multi-line
``gh issue create --body`` text dedented below the ``run: |`` block, which
ended the block scalar. GitHub could not read their ``on:`` triggers, so it
recorded a failed zero-job run on every push and never ran their schedules.

Covered here:
- every workflow parses as a YAML mapping with a trigger and at least one job
  (runs with no external tool, so it also guards local runs);
- every workflow passes actionlint. Under CI a missing actionlint FAILS; off
  CI the test may skip, with a reason (QUALITY-BAR D, DEV-FUNDAMENTALS F9);
- the actionlint binary on PATH really lints (a broken file makes it fail),
  so a stand-in that always exits 0 can't turn the gate green;
- CI installs actionlint before the unit-test step that collects this file,
  and the install step itself fails closed on a bad checksum, a version
  mismatch, an unpinned platform and a failed download (run as a script with
  a fake curl and uname);
- refresh.yml and approval-expiry.yml check their issue label and the gh CLI
  up front, and fail loudly with the fix when either is missing.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONLINT_CONFIG = REPO_ROOT / ".github" / "actionlint.yaml"
CI_WORKFLOW = WORKFLOWS_DIR / "ci.yml"
CI_JOB = "test"
INSTALL_STEP = "Install actionlint"
UNIT_STEP = "Unit tests"
PREFLIGHT_STEP = "Preflight"
# The workflows that open issues, with their job and the label they file under.
ISSUE_WORKFLOWS = {
    "refresh.yml": ("refresh", "corpus-refresh"),
    "approval-expiry.yml": ("approval-expiry", "approval-expiry"),
}
# GitHub's default for steps with no shell set.
DEFAULT_SHELL = "bash -e {0}"
# shellcheck and pyflakes are off so the gate gives the same answer on every
# machine, whether or not either tool happens to be installed.
ACTIONLINT_ARGS = ("-no-color", "-shellcheck=", "-pyflakes=")
EXPR = re.compile(r"\$\{\{.*?\}\}")


def _workflow_files() -> list[Path]:
    files = sorted([*WORKFLOWS_DIR.glob("*.yml"), *WORKFLOWS_DIR.glob("*.yaml")])
    assert files, f"no workflow files found under {WORKFLOWS_DIR.relative_to(REPO_ROOT)}"
    return files


def _load(path: Path) -> dict[Any, Any]:
    doc = yaml.safe_load(path.read_text())
    assert isinstance(doc, dict), f"{path.name}: top level is not a mapping"
    return doc


def _on_ci() -> bool:
    return os.environ.get("CI", "").lower() in {"true", "1"}


# 1. Parse floor: no external tool needed ---------------------------------


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_workflow_parses_with_a_trigger_and_jobs(path: Path) -> None:
    try:
        doc = _load(path)
    except yaml.YAMLError as exc:
        pytest.fail(f"{path.name} is not valid YAML, so GitHub never runs it: {exc}")
    # PyYAML reads the bare key `on` as boolean True (YAML 1.1).
    trigger = doc.get("on", doc.get(True))
    assert trigger, f"{path.name}: no `on:` trigger"
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict) and jobs, f"{path.name}: no jobs"
    for name, job in jobs.items():
        assert isinstance(job, dict), f"{path.name}: job {name} is not a mapping"
        assert "runs-on" in job or "uses" in job, f"{path.name}: job {name} has no runs-on"


# 2. actionlint over every workflow ------------------------------------------


def _actionlint() -> str:
    exe = shutil.which("actionlint")
    if exe is None:
        if _on_ci():
            pytest.fail(
                "actionlint is not on PATH under CI; the 'Install actionlint' step in "
                "ci.yml must run before the unit tests"
            )
        pytest.skip("actionlint not on PATH; CI installs the pinned release (ci.yml)")
    return exe


def _run_actionlint(exe: str, files: list[Path]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [exe, *ACTIONLINT_ARGS, "-config-file", str(ACTIONLINT_CONFIG), *map(str, files)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_every_workflow_passes_actionlint() -> None:
    exe = _actionlint()
    proc = _run_actionlint(exe, _workflow_files())
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == "", proc.stdout


def test_actionlint_on_path_rejects_the_issue_21_breakage(tmp_path: Path) -> None:
    """The gate must be able to fail: the exact #21 pattern is rejected."""
    exe = _actionlint()
    bad = tmp_path / ".github" / "workflows" / "bad.yml"
    bad.parent.mkdir(parents=True)
    bad.write_text(
        "on: workflow_dispatch\n"
        "jobs:\n"
        "  j:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: |\n"
        '          gh issue create --body "line one\n'
        "\n"
        '  dedented line"\n'
    )
    proc = _run_actionlint(exe, [bad])
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "could not parse as YAML" in proc.stdout


def test_actionlint_off_ci_skips_and_under_ci_fails_without_the_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("CI", raising=False)
    with pytest.raises(pytest.skip.Exception):
        _actionlint()
    for value in ("true", "1"):
        monkeypatch.setenv("CI", value)
        with pytest.raises(pytest.fail.Exception, match="not on PATH under CI"):
            _actionlint()


# Helpers: run one workflow step's shell with fakes --------------------------


def _job(workflow: Path, job: str) -> dict[str, Any]:
    jobs = _load(workflow)["jobs"]
    assert job in jobs, f"{workflow.name}: no job {job}"
    found: dict[str, Any] = jobs[job]
    return found


def _step_names(workflow: Path, job: str) -> list[str]:
    return [str(s.get("name", "")) for s in _job(workflow, job)["steps"]]


def _step(workflow: Path, job: str, name: str) -> dict[str, Any]:
    matches = [s for s in _job(workflow, job)["steps"] if s.get("name") == name]
    assert len(matches) == 1, f"{workflow.name}: expected one step named {name!r}"
    found: dict[str, Any] = matches[0]
    return found


def _shell(workflow: Path) -> list[str]:
    shell = _load(workflow).get("defaults", {}).get("run", {}).get("shell", DEFAULT_SHELL)
    return str(shell).split()


def _resolve(value: Any, exprs: dict[str, str]) -> str:
    text = str(value)
    for expr, sub in exprs.items():
        text = text.replace(expr, sub)
    left = EXPR.findall(text)
    assert not left, f"unresolved expressions in step: {left}"
    return text


def _run_step(
    workflow: Path,
    job: str,
    name: str,
    tmp_path: Path,
    *,
    bin_dir: Path,
    env: dict[str, str] | None = None,
    exprs: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    exprs = exprs or {}
    step = _step(workflow, job, name)
    merged = {**_job(workflow, job).get("env", {}), **step.get("env", {})}
    script = tmp_path / f"step-{name.replace(' ', '-')}.sh"
    script.write_text(_resolve(step["run"], exprs))
    base = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "RUNNER_TEMP": str(tmp_path / "runner-temp"),
        "GITHUB_PATH": str(tmp_path / "github-path"),
        "GITHUB_REPOSITORY": "owner/repo",
    }
    (tmp_path / "runner-temp").mkdir(exist_ok=True)
    (tmp_path / "github-path").touch()
    full = {**base, **{k: _resolve(v, exprs) for k, v in merged.items()}, **(env or {})}
    argv = [part.replace("{0}", str(script)) for part in _shell(workflow)]
    bash = shutil.which(argv[0])
    assert bash is not None, f"{argv[0]} not found"
    argv[0] = bash  # resolved here, so a test PATH without bash still runs the step
    return subprocess.run(
        argv, cwd=tmp_path, env=full, capture_output=True, text=True, timeout=60, check=False
    )


def _fake(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


# 3. CI wiring and the install step -----------------------------------------


def test_ci_installs_actionlint_before_the_unit_tests_that_collect_this_file() -> None:
    names = _step_names(CI_WORKFLOW, CI_JOB)
    assert INSTALL_STEP in names and UNIT_STEP in names, names
    assert names.index(INSTALL_STEP) < names.index(UNIT_STEP)
    unit_run = str(_step(CI_WORKFLOW, CI_JOB, UNIT_STEP)["run"])
    assert re.search(r"pytest tests/unit\b", unit_run), unit_run
    assert Path(__file__).parent == REPO_ROOT / "tests" / "unit"


def _install_env(step: dict[str, Any]) -> dict[str, str]:
    env = {str(k): str(v) for k, v in step.get("env", {}).items()}
    assert "ACTIONLINT_VERSION" in env, env
    return env


def _tarball(tmp_path: Path, version_line: str) -> tuple[Path, str]:
    """A release-shaped tarball whose `actionlint` prints `version_line`."""
    payload = f"#!/bin/bash\necho '{version_line}'\n".encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("actionlint")
        info.size = len(payload)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(payload))
    path = tmp_path / "release.tar.gz"
    path.write_bytes(buf.getvalue())
    return path, hashlib.sha256(buf.getvalue()).hexdigest()


def _install_fakes(tmp_path: Path, tarball: Path, *, uname_s: str, uname_m: str) -> Path:
    bin_dir = tmp_path / "bin"
    log = tmp_path / "curl-args"
    _fake(
        bin_dir,
        "curl",
        f'printf "%s\\n" "$@" > "{log}"\n'
        'out=""; while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\n'
        f'cp "{tarball}" "$out"\n',
    )
    _fake(bin_dir, "uname", f'[ "$1" = "-m" ] && echo {uname_m} || echo {uname_s}\n')
    return bin_dir


def _platform_key(env: dict[str, str], platform: str) -> str:
    key = f"ACTIONLINT_SHA256_{platform.upper()}"
    assert re.fullmatch(r"[0-9a-f]{64}", env.get(key, "")), f"{key} not pinned"
    return key


@pytest.mark.parametrize(
    ("uname_s", "uname_m", "platform"),
    [("Darwin", "arm64", "darwin_arm64"), ("Linux", "x86_64", "linux_amd64")],
)
def test_install_step_puts_the_verified_binary_on_path(
    tmp_path: Path, uname_s: str, uname_m: str, platform: str
) -> None:
    step = _step(CI_WORKFLOW, CI_JOB, INSTALL_STEP)
    env = _install_env(step)
    version = env["ACTIONLINT_VERSION"]
    tarball, digest = _tarball(tmp_path, version)
    bin_dir = _install_fakes(tmp_path, tarball, uname_s=uname_s, uname_m=uname_m)

    proc = _run_step(
        CI_WORKFLOW, CI_JOB, INSTALL_STEP, tmp_path,
        bin_dir=bin_dir, env={_platform_key(env, platform): digest},
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    added = (tmp_path / "github-path").read_text().splitlines()
    assert len(added) == 1
    assert (Path(added[0]) / "actionlint").is_file()
    assert Path(added[0]).parent == tmp_path / "runner-temp"
    url = (tmp_path / "curl-args").read_text().splitlines()
    want = f"/rhysd/actionlint/releases/download/v{version}/actionlint_{version}_{platform}.tar.gz"
    assert any(arg.startswith("https://github.com/") and arg.endswith(want) for arg in url), url


def test_install_step_fails_closed_on_a_checksum_mismatch(tmp_path: Path) -> None:
    env = _install_env(_step(CI_WORKFLOW, CI_JOB, INSTALL_STEP))
    tarball, _ = _tarball(tmp_path, env["ACTIONLINT_VERSION"])
    bin_dir = _install_fakes(tmp_path, tarball, uname_s="Darwin", uname_m="arm64")

    proc = _run_step(CI_WORKFLOW, CI_JOB, INSTALL_STEP, tmp_path, bin_dir=bin_dir)

    assert proc.returncode == 1
    assert "checksum mismatch" in proc.stderr
    assert (tmp_path / "github-path").read_text() == ""


def test_install_step_fails_closed_on_a_version_mismatch(tmp_path: Path) -> None:
    env = _install_env(_step(CI_WORKFLOW, CI_JOB, INSTALL_STEP))
    tarball, digest = _tarball(tmp_path, "0.0.1")
    bin_dir = _install_fakes(tmp_path, tarball, uname_s="Darwin", uname_m="arm64")

    proc = _run_step(
        CI_WORKFLOW, CI_JOB, INSTALL_STEP, tmp_path,
        bin_dir=bin_dir, env={_platform_key(env, "darwin_arm64"): digest},
    )

    assert proc.returncode == 1
    assert f"expected {env['ACTIONLINT_VERSION']}" in proc.stderr
    assert (tmp_path / "github-path").read_text() == ""


def test_install_step_fails_closed_on_an_unpinned_platform(tmp_path: Path) -> None:
    env = _install_env(_step(CI_WORKFLOW, CI_JOB, INSTALL_STEP))
    tarball, _ = _tarball(tmp_path, env["ACTIONLINT_VERSION"])
    bin_dir = _install_fakes(tmp_path, tarball, uname_s="FreeBSD", uname_m="riscv64")

    proc = _run_step(CI_WORKFLOW, CI_JOB, INSTALL_STEP, tmp_path, bin_dir=bin_dir)

    assert proc.returncode == 1
    assert "no pinned actionlint checksum for freebsd_riscv64" in proc.stderr
    assert not (tmp_path / "curl-args").exists()
    assert (tmp_path / "github-path").read_text() == ""


def test_install_step_fails_closed_when_the_download_fails(tmp_path: Path) -> None:
    env = _install_env(_step(CI_WORKFLOW, CI_JOB, INSTALL_STEP))
    tarball, digest = _tarball(tmp_path, env["ACTIONLINT_VERSION"])
    bin_dir = _install_fakes(tmp_path, tarball, uname_s="Darwin", uname_m="arm64")
    _fake(bin_dir, "curl", "echo 'curl: (22) 404' >&2; exit 22\n")

    proc = _run_step(
        CI_WORKFLOW, CI_JOB, INSTALL_STEP, tmp_path,
        bin_dir=bin_dir, env={_platform_key(env, "darwin_arm64"): digest},
    )

    assert proc.returncode == 22
    assert (tmp_path / "github-path").read_text() == ""


# 4. Issue-opening workflows fail loudly on a missing label or gh CLI ---------

TOKEN_EXPRS = {"${{ github.token }}": "fake-token"}


def _gh_fake(bin_dir: Path, tmp_path: Path, *, label_exists: bool) -> None:
    log = tmp_path / "gh-args"
    rc = 0 if label_exists else 1
    msg = "" if label_exists else "echo 'gh: Not Found (HTTP 404)' >&2\n"
    _fake(bin_dir, "gh", f'printf "%s\\n" "$@" >> "{log}"\n{msg}exit {rc}\n')


@pytest.mark.parametrize("name", sorted(ISSUE_WORKFLOWS))
def test_issue_workflow_preflight_is_the_first_step_after_checkout(name: str) -> None:
    job, _ = ISSUE_WORKFLOWS[name]
    names = _step_names(WORKFLOWS_DIR / name, job)
    assert names.index(PREFLIGHT_STEP) == 1, names


@pytest.mark.parametrize("name", sorted(ISSUE_WORKFLOWS))
def test_issue_workflow_preflight_passes_when_the_label_exists(
    name: str, tmp_path: Path
) -> None:
    job, label = ISSUE_WORKFLOWS[name]
    bin_dir = tmp_path / "bin"
    _gh_fake(bin_dir, tmp_path, label_exists=True)

    proc = _run_step(
        WORKFLOWS_DIR / name, job, PREFLIGHT_STEP, tmp_path,
        bin_dir=bin_dir, exprs=TOKEN_EXPRS,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    args = (tmp_path / "gh-args").read_text().splitlines()
    assert f"repos/owner/repo/labels/{label}" in args, args


@pytest.mark.parametrize("name", sorted(ISSUE_WORKFLOWS))
def test_issue_workflow_preflight_fails_loudly_when_the_label_is_missing(
    name: str, tmp_path: Path
) -> None:
    job, label = ISSUE_WORKFLOWS[name]
    bin_dir = tmp_path / "bin"
    _gh_fake(bin_dir, tmp_path, label_exists=False)

    proc = _run_step(
        WORKFLOWS_DIR / name, job, PREFLIGHT_STEP, tmp_path,
        bin_dir=bin_dir, exprs=TOKEN_EXPRS,
    )

    assert proc.returncode == 1
    assert f"::error::Issue label '{label}' could not be read" in proc.stderr
    assert f"gh label create {label}" in proc.stderr


@pytest.mark.parametrize("name", sorted(ISSUE_WORKFLOWS))
def test_issue_workflow_preflight_fails_loudly_without_the_gh_cli(
    name: str, tmp_path: Path
) -> None:
    job, _ = ISSUE_WORKFLOWS[name]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    proc = _run_step(
        WORKFLOWS_DIR / name, job, PREFLIGHT_STEP, tmp_path,
        bin_dir=bin_dir, exprs=TOKEN_EXPRS, env={"PATH": str(bin_dir)},
    )

    assert proc.returncode == 1
    assert "::error::gh CLI not found on the runner" in proc.stderr


@pytest.mark.parametrize("name", sorted(ISSUE_WORKFLOWS))
def test_issue_workflow_files_issues_under_the_label_preflight_checked(name: str) -> None:
    """One label per workflow: the issue step uses the same env the preflight checks."""
    job, label = ISSUE_WORKFLOWS[name]
    assert _job(WORKFLOWS_DIR / name, job).get("env", {}).get("ISSUE_LABEL") == label
    text = (WORKFLOWS_DIR / name).read_text()
    assert f'"{label}"' not in text, "label restated as a literal; use $ISSUE_LABEL"
    assert '--label "$ISSUE_LABEL"' in text
