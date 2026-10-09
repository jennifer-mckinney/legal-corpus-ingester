"""Contracts every GitHub workflow must meet (terms-analysis#90).

1. Pipefail. GitHub's default step shell is `bash -e {0}` with no pipefail, so
   `pytest ... | tail -30` reports tail's exit status (always 0) and a red test
   run shows green. Each workflow sets `defaults.run.shell: bash -eo pipefail {0}`,
   and every piped `run:` step's EFFECTIVE shell (step `shell:`, else job
   default, else workflow default) must carry pipefail. A single step override
   such as `shell: bash -e {0}` is enough to bring the masking back.
2. Token scope. `GH_TOKEN`/`GITHUB_TOKEN` is set only on the steps that call
   `gh`, never at workflow or job level where Install's third-party build hooks
   could read it. `actions/checkout` sets `persist-credentials: false` so the
   token is not written into `.git/config` for every later step.
3. Labels. `gh issue create --label X` fails when label X does not exist, so an
   alert step whose label was never created can never fire. Every label a
   workflow uses must be created earlier in the same step with
   `gh label create`. In-step creation was chosen over a checked-in labels
   manifest: a manifest needs a separate sync job (or a human) to reach the
   repo, which is exactly the out-of-band wiring that was missing, while
   in-step creation heals a deleted or renamed label on the next run.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from legal_corpus_ingester import cli

_WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
_REQUIRED_SHELL = "bash -eo pipefail {0}"
# Matches a shell pipe but not `||`.
_PIPE = re.compile(r"(?<!\|)\|(?!\|)")
_TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN")
_LABEL_USE = re.compile(r"--label[ =]+[\"']?([^\"'\s]+)")
_LABEL_CREATE = re.compile(r"gh\s+label\s+create\s+[\"']?([^\"'\s]+)")

_WORKFLOW_FILES = sorted(_WORKFLOWS_DIR.glob("*.yml")) + sorted(_WORKFLOWS_DIR.glob("*.yaml"))
# vcr-drift.yml is owned by feat/g0-3-vcr-canary (terms-analysis#92), which adds the
# pipefail default, in-step label creation and persist-credentials: false. strict=True
# turns each xfail into a failure once that branch merges, so whichever branch merges
# second must drop the markers in the merge commit. Dropped for pipefail and
# persist-credentials when G0-3 merged; the label check stays xfail because
# vcr-drift.yml creates its labels in a loop over `$name`, which labels_not_created
# cannot trace.
_VCR_DRIFT_XFAIL = pytest.mark.xfail(strict=True, reason="fixed on feat/g0-3-vcr-canary, terms-analysis#92")


def _params(xfail_vcr: bool) -> list[Any]:
    params: list[Any] = []
    for path in _WORKFLOW_FILES:
        marks = [_VCR_DRIFT_XFAIL] if xfail_vcr and path.name == "vcr-drift.yml" else []
        params.append(pytest.param(path, id=path.name, marks=marks))
    return params


def _load(path: Path) -> dict[str, Any]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{path.name} is not a mapping"
    return doc


def _default_shell(node: dict[str, Any]) -> str | None:
    shell = ((node.get("defaults") or {}).get("run") or {}).get("shell")
    return str(shell) if shell else None


def _steps(doc: dict[str, Any]) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    out = []
    for job_name, job in (doc.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            out.append((job_name, job, step))
    return out


def _step_label(job_name: str, step: dict[str, Any]) -> str:
    name = step.get("name") or step.get("uses") or str(step.get("run", "")).splitlines()[0]
    return f"{job_name}: {name}"


def unguarded_piped_steps(doc: dict[str, Any]) -> list[str]:
    """Piped `run:` steps whose effective shell lacks pipefail."""
    wf_shell = _default_shell(doc)
    unguarded: list[str] = []
    for job_name, job, step in _steps(doc):
        run = str(step.get("run") or "")
        if not run or not _PIPE.search(run):
            continue
        # Effective shell: step override, else job default, else workflow default.
        shell = step.get("shell") or _default_shell(job) or wf_shell
        if shell and "pipefail" in str(shell):
            continue
        if "set -o pipefail" in run or "set -eo pipefail" in run:
            continue
        unguarded.append(_step_label(job_name, step))
    return unguarded


def broad_token_scopes(doc: dict[str, Any]) -> list[str]:
    """Workflow- or job-level env blocks that expose a GitHub token."""
    found = [f"workflow env: {v}" for v in _TOKEN_VARS if v in (doc.get("env") or {})]
    for job_name, job in (doc.get("jobs") or {}).items():
        found += [f"job {job_name} env: {v}" for v in _TOKEN_VARS if v in (job.get("env") or {})]
    return found


# `gh` in command position (bare or by path), not part of a longer word or variable name.
_GH_CALL = re.compile(r"(?<![\w.$-])gh(?=[ \t;|&)]|$)", re.MULTILINE)


def _invokes_gh(run: str) -> bool:
    """True if a `run:` script calls `gh`; whole-line shell comments are ignored."""
    code = "\n".join(line for line in run.splitlines() if not line.lstrip().startswith("#"))
    return bool(_GH_CALL.search(code))


def gh_steps_without_token(doc: dict[str, Any]) -> list[str]:
    """`run:` steps that call `gh` with no non-empty GH_TOKEN in step, job or workflow env.

    Job- and workflow-level tokens count as available here; whether a workflow may put
    the token there is broad_token_scopes' contract, checked separately.
    """
    wf_env = doc.get("env") or {}
    missing = []
    for job_name, job, step in _steps(doc):
        if not _invokes_gh(str(step.get("run") or "")):
            continue
        # Step env overrides job env, which overrides workflow env.
        env = {**wf_env, **(job.get("env") or {}), **(step.get("env") or {})}
        if not str(env.get("GH_TOKEN") or "").strip():
            missing.append(_step_label(job_name, step))
    return missing


def checkouts_persisting_credentials(doc: dict[str, Any]) -> list[str]:
    """`actions/checkout` steps that do not set `persist-credentials: false`."""
    bad = []
    for job_name, _job, step in _steps(doc):
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            if (step.get("with") or {}).get("persist-credentials") is not False:
                bad.append(_step_label(job_name, step))
    return bad


def labels_not_created(doc: dict[str, Any]) -> list[str]:
    """Labels used with `--label` that are not `gh label create`d earlier in the step."""
    missing = []
    for job_name, _job, step in _steps(doc):
        run = str(step.get("run") or "")
        for use in _LABEL_USE.finditer(run):
            created_before = {m.group(1) for m in _LABEL_CREATE.finditer(run[: use.start()])}
            for label in use.group(1).split(","):
                if label and label not in created_before:
                    missing.append(f"{_step_label(job_name, step)}: {label}")
    return missing


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_workflow_dir_is_not_empty() -> None:
    # Guard: a moved directory must not turn the contract into zero vacuous passes.
    assert len(_WORKFLOW_FILES) >= 5


@pytest.mark.parametrize("path", _WORKFLOW_FILES, ids=lambda p: p.name)
def test_workflow_is_valid_yaml(path: Path) -> None:
    # Not xfailed for any file: GitHub rejects an unparseable workflow at load time, so
    # it never runs (refresh.yml and approval-expiry.yml were in this state before #90).
    doc = _load(path)
    assert doc.get("jobs"), f"{path.name} has no jobs"


# ---------------------------------------------------------------------------
# 1. Pipefail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _params(xfail_vcr=False))
def test_workflow_default_shell_is_pipefail(path: Path) -> None:
    assert _default_shell(_load(path)) == _REQUIRED_SHELL, (
        f"{path.name} lacks workflow-level `defaults.run.shell: {_REQUIRED_SHELL}`"
    )


@pytest.mark.parametrize("path", _params(xfail_vcr=False))
def test_every_piped_step_runs_with_pipefail(path: Path) -> None:
    unguarded = unguarded_piped_steps(_load(path))
    assert not unguarded, f"{path.name}: piped steps whose effective shell lacks pipefail: {unguarded}"


def _wf(step: dict[str, Any], job_defaults: str | None = None) -> dict[str, Any]:
    job: dict[str, Any] = {"runs-on": "x", "steps": [step]}
    if job_defaults:
        job["defaults"] = {"run": {"shell": job_defaults}}
    return {"defaults": {"run": {"shell": _REQUIRED_SHELL}}, "jobs": {"j": job}}


def test_step_shell_override_without_pipefail_is_caught() -> None:
    # The round-2 mutation: a step override keeps the workflow default but masks the pipe.
    doc = _wf({"name": "masked", "shell": "bash -e {0}", "run": "false | tail -1"})
    assert unguarded_piped_steps(doc) == ["j: masked"]


def test_job_shell_override_without_pipefail_is_caught() -> None:
    doc = _wf({"name": "masked", "run": "false | tail -1"}, job_defaults="bash -e {0}")
    assert unguarded_piped_steps(doc) == ["j: masked"]


def test_pipefail_overrides_and_non_pipes_pass() -> None:
    assert not unguarded_piped_steps(_wf({"shell": "bash -eo pipefail {0}", "run": "a | b"}))
    assert not unguarded_piped_steps(_wf({"run": "a | b"}))
    # `||` is not a pipe.
    assert not unguarded_piped_steps(_wf({"shell": "bash -e {0}", "run": "a || b"}))


# ---------------------------------------------------------------------------
# 2. Token scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _params(xfail_vcr=False))
def test_github_token_is_only_step_scoped(path: Path) -> None:
    broad = broad_token_scopes(_load(path))
    assert not broad, f"{path.name}: token exposed beyond the step that needs it: {broad}"


@pytest.mark.parametrize("path", _params(xfail_vcr=False))
def test_checkout_does_not_persist_credentials(path: Path) -> None:
    bad = checkouts_persisting_credentials(_load(path))
    assert not bad, f"{path.name}: checkout without `persist-credentials: false`: {bad}"


def test_token_scope_checkers_catch_mutations() -> None:
    doc = {
        "env": {"GITHUB_TOKEN": "x"},
        "jobs": {
            "j": {
                "env": {"GH_TOKEN": "x"},
                "steps": [
                    {"uses": "actions/checkout@abc"},
                    {"uses": "actions/checkout@abc", "with": {"persist-credentials": "false"}},
                    {"uses": "actions/checkout@abc", "with": {"persist-credentials": False}},
                ],
            }
        },
    }
    assert broad_token_scopes(doc) == ["workflow env: GITHUB_TOKEN", "job j env: GH_TOKEN"]
    # The quoted string "false" is not the YAML boolean and is flagged.
    assert len(checkouts_persisting_credentials(doc)) == 2


@pytest.mark.parametrize("path", _params(xfail_vcr=False))
def test_every_gh_step_has_gh_token(path: Path) -> None:
    # Moving the token from job to step level must not strand a `gh` step without it:
    # gh then fails auth and the alert never fires (PR #26/#27 review).
    missing = gh_steps_without_token(_load(path))
    assert not missing, f"{path.name}: steps call `gh` without GH_TOKEN: {missing}"


def test_gh_token_checker_catches_mutations() -> None:
    tok = {"GH_TOKEN": "${{ github.token }}"}
    doc = {
        "jobs": {
            "j": {
                "env": {"ISSUE_LABEL": "x"},
                "steps": [
                    {"name": "bare", "run": "gh issue list"},
                    {"name": "subshell", "run": 'n=$(gh issue list --json number)'},
                    {"name": "after-if", "run": "if ! gh api x; then exit 1; fi"},
                    {"name": "path", "run": "/usr/bin/gh api x"},
                    {"name": "line-end", "run": "command -v gh"},
                    {"name": "empty", "env": {"GH_TOKEN": ""}, "run": "gh api x"},
                    {"name": "other-var", "env": {"GITHUB_TOKEN": "t"}, "run": "gh api x"},
                    {"name": "scoped", "env": tok, "run": "gh issue create"},
                    {"name": "comment", "run": "# gh issue create runs later\necho hi"},
                    {"name": "words", "run": "echo ghost sigh gh_x gh-y $gh x.gh"},
                    {"uses": "actions/checkout@abc"},
                ],
            },
            "k": {"env": tok, "steps": [{"name": "job-env", "run": "gh api x"}]},
        },
    }
    assert gh_steps_without_token(doc) == [
        "j: bare", "j: subshell", "j: after-if", "j: path", "j: line-end", "j: empty", "j: other-var",
    ]
    assert gh_steps_without_token({"env": tok, **doc}) == ["j: empty"]


# ---------------------------------------------------------------------------
# 3. Labels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _params(xfail_vcr=True))
def test_every_label_is_created_in_workflow(path: Path) -> None:
    missing = labels_not_created(_load(path))
    assert not missing, f"{path.name}: `--label` used without an earlier `gh label create`: {missing}"


def test_label_checker_catches_missing_and_late_creation() -> None:
    def doc(run: str) -> dict[str, Any]:
        return {"jobs": {"j": {"steps": [{"name": "s", "run": run}]}}}

    assert labels_not_created(doc('gh issue create --label "a,b"')) == ["j: s: a", "j: s: b"]
    assert labels_not_created(doc("gh issue create --label a\ngh label create a --force")) == ["j: s: a"]
    assert not labels_not_created(
        doc('gh label create "a" --force\ngh label create b\ngh issue create --label "a,b"')
    )
    # The dedupe query uses --label too, so it must also come after creation.
    assert labels_not_created(doc('gh issue list --label "a"\ngh label create a')) == ["j: s: a"]


# ---------------------------------------------------------------------------
# refresh.yml change detection and approval-expiry alert scoping
# ---------------------------------------------------------------------------


def _step_by_name(doc: dict[str, Any], name: str) -> tuple[int, dict[str, Any]]:
    for job in (doc.get("jobs") or {}).values():
        for i, step in enumerate(job.get("steps") or []):
            if step.get("name") == name:
                return i, step
    raise AssertionError(f"step {name!r} not found")


def test_refresh_detects_change_from_durable_record_not_pre_checkout_symlink() -> None:
    doc = _load(_WORKFLOWS_DIR / "refresh.yml")
    text = (_WORKFLOWS_DIR / "refresh.yml").read_text(encoding="utf-8")
    # The pre-refresh readlink was always empty because checkout's clean wipes out/.
    assert "pre_refresh" not in text
    _, detect = _step_by_name(doc, "Detect bundle change")
    assert "scripts/detect_bundle_change.py detect" in detect["run"]
    issue_idx, _ = _step_by_name(doc, "Open issue on corpus change")
    record_idx, record = _step_by_name(doc, "Record announced bundle")
    # The record is written only after the alert step, and only when it ran.
    assert record_idx > issue_idx
    assert "scripts/detect_bundle_change.py record" in record["run"]
    assert record["if"] == "steps.detect_change.outputs.changed == 'true'"


def test_approval_alert_is_scoped_to_the_check_step() -> None:
    doc = _load(_WORKFLOWS_DIR / "approval-expiry.yml")
    _, check = _step_by_name(doc, "Run approval check")
    _, alert = _step_by_name(doc, "Open issue on expiry")
    assert check.get("id") == "approval_check"
    # Bare failure() also fires on checkout/Install failures with a misleading expiry alert.
    assert alert["if"] == "failure() && steps.approval_check.outcome == 'failure'"


def test_refresh_dedupe_branch_comments_instead_of_silently_skipping() -> None:
    # When an issue is already open the new bundle must still be announced (terms-analysis#90),
    # because the record step advances right after; a bare echo would swallow the change.
    doc = _load(_WORKFLOWS_DIR / "refresh.yml")
    _, step = _step_by_name(doc, "Open issue on corpus change")
    run = step["run"]
    assert "gh issue comment" in run
    assert "skipping duplicate" not in run
    # The version reaches the comment via env, never interpolated into the script.
    assert "${{" not in run
    assert step["env"]["BUNDLE_VER"] == "${{ steps.detect_change.outputs.new_target }}"
    assert "${BUNDLE_VER}" in run.split("gh issue comment", 1)[1]


# ---------------------------------------------------------------------------
# health.yml: the known unwired state is a warning, every other failure is red
# ---------------------------------------------------------------------------
# actions/checkout cleans untracked and ignored files (-ffdx) before these steps, which
# wipes the gitignored state/, so the steps must read checkpoints from outside the
# checkout. The steps run for real under the workflow's own shell, in a throwaway
# "checkout" directory.

_REPO_ROOT = _WORKFLOWS_DIR.parents[1]
_HEALTH_STEPS = ("Run ingester status", "Run health check")


def _run_health_step(
    tmp_path: Path, step_name: str, *, python_rc: int | None = None, xdg: Path | None = None
) -> tuple[subprocess.CompletedProcess[str], Path, list[str]]:
    """Run a health.yml step; return (result, checkout dir, argv the fake command got).

    python_rc None runs the real scripts/health_check.py and CLI; an int makes a fake
    `python`/`ingester` that records its argv and exits with that code.
    """
    doc = _load(_WORKFLOWS_DIR / "health.yml")
    _, step = _step_by_name(doc, step_name)
    checkout = tmp_path / "checkout"
    (checkout / ".venv" / "bin").mkdir(parents=True)
    (checkout / ".venv" / "bin" / "activate").write_text("")
    (checkout / "scripts").symlink_to(_REPO_ROOT / "scripts")
    (checkout / "config").symlink_to(_REPO_ROOT / "config")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    argv_log = tmp_path / "argv.json"
    for tool in ("python", "ingester"):
        if python_rc is None:
            body = (
                f'exec "{sys.executable}" "$@"\n' if tool == "python"
                else f'exec "{sys.executable}" -c "from legal_corpus_ingester.cli import app; app()" "$@"\n'
            )
        else:
            body = (
                f'"{sys.executable}" -c "import json,sys; json.dump(sys.argv[2:], open(sys.argv[1], \'w\'))"'
                f' "{argv_log}" "$@"\nexit {python_rc}\n'
            )
        (fakebin / tool).write_text("#!/bin/bash\n" + body)
        (fakebin / tool).chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": f"{fakebin}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(home)}
    if python_rc is None:
        # Import this checkout's code, as pytest does (pyproject pythonpath), not an install.
        env["PYTHONPATH"] = str(_REPO_ROOT / "src")
    if xdg is not None:
        env["XDG_STATE_HOME"] = str(xdg)
    script = tmp_path / "step.sh"
    script.write_text(str(step["run"]))
    shell = _default_shell(doc)
    assert shell == _REQUIRED_SHELL
    result = subprocess.run(
        shell.replace("{0}", str(script)).split(),
        cwd=checkout, env=env, capture_output=True, text=True, timeout=60,
    )
    argv = json.loads(argv_log.read_text()) if argv_log.exists() else []
    return result, checkout, argv


@pytest.mark.parametrize("step_name", _HEALTH_STEPS)
@pytest.mark.parametrize("use_xdg", [True, False], ids=["xdg", "home-fallback"])
def test_health_steps_read_state_outside_the_checkout(tmp_path: Path, step_name: str, use_xdg: bool) -> None:
    xdg = tmp_path / "xdg" if use_xdg else None
    result, checkout, argv = _run_health_step(tmp_path, step_name, python_rc=0, xdg=xdg)
    assert result.returncode == 0, result.stderr
    state_dir = Path(argv[argv.index("--state-dir") + 1])
    base = xdg if xdg is not None else tmp_path / "home" / ".local" / "state"
    assert state_dir == base / "legal-corpus-ingester" / "state"
    assert not state_dir.resolve().is_relative_to(checkout.resolve())


@pytest.mark.parametrize(
    ("rc", "step_rc", "warns"),
    [
        (0, 0, False),
        (cli.EXIT_NOT_WIRED, 0, True),
        (1, 1, False),
        (2, 2, False),
        (cli.EXIT_CONSUMER_SKIPPED, cli.EXIT_CONSUMER_SKIPPED, False),
        (127, 127, False),
    ],
)
def test_health_check_step_excuses_only_the_not_wired_code(
    tmp_path: Path, rc: int, step_rc: int, warns: bool
) -> None:
    result, _, argv = _run_health_step(tmp_path, "Run health check", python_rc=rc, xdg=tmp_path / "xdg")
    assert argv[0] == "scripts/health_check.py"
    assert result.returncode == step_rc, (result.stdout, result.stderr)
    assert ("::warning title=Refresh not wired::" in result.stdout) is warns


def test_health_check_step_end_to_end_unwired_is_warning_and_ignores_checkout_state(tmp_path: Path) -> None:
    # A fresh checkpoint inside the checkout must not be read: the real runner wipes it.
    (tmp_path / "checkout" / "state").mkdir(parents=True)
    (tmp_path / "checkout" / "state" / "eurlex_gdpr.checkpoint.json").write_text('{"stage": "done"}')
    result, _, _ = _run_health_step(tmp_path, "Run health check", xdg=tmp_path / "xdg")
    assert not cli.REFRESH_WIRED
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "::warning title=Refresh not wired::" in result.stdout
    assert "Refresh is not wired yet" in result.stdout
    assert "| fresh |" not in result.stdout


def test_health_check_step_end_to_end_stale_source_fails(tmp_path: Path) -> None:
    state = tmp_path / "xdg" / "legal-corpus-ingester" / "state"
    state.mkdir(parents=True)
    cp = state / "eurlex_gdpr.checkpoint.json"
    cp.write_text(json.dumps({"stage": "done"}))
    old = time.time() - 30 * 86400
    os.utime(cp, (old, old))
    result, _, _ = _run_health_step(tmp_path, "Run health check", xdg=tmp_path / "xdg")
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "| eurlex_gdpr |" in result.stdout
    assert "| stale |" in result.stdout
    assert "::warning" not in result.stdout
