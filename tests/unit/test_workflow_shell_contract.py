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

import dataclasses
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from legal_corpus_ingester import cli
from legal_corpus_ingester.pipeline.state import CheckpointState, CheckpointStore

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
    # ADR-016/#71: on a hosted runner a missing record must be a red run, never an announce.
    assert "--require-state" in detect["run"]
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
_HEALTH_YML = _WORKFLOWS_DIR / "health.yml"
# The health.yml steps that read checkpoints, found by the command each one runs and never
# by step name, so renaming a step cannot slip it past these tests (#17 r1). Shell comments
# are stripped first, so a step that only MENTIONS the other command is not taken for it.
_HEALTH_STEPS: dict[str, re.Pattern[str]] = {
    "health-check": re.compile(r"(?<![\w.-])scripts/health_check\.py\b"),
    "status": re.compile(r"(?<![\w.-])ingester\s+status\b"),
}
_SHELL_COMMENT = re.compile(r"(?m)(?:^|\s)#.*$")


def _load_health_check() -> ModuleType:
    """scripts/health_check.py as a module, for its documented exit codes (F10: never restated)."""
    spec = importlib.util.spec_from_file_location(
        "_health_check_for_workflow_contract", _REPO_ROOT / "scripts" / "health_check.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves the module through sys.modules while the script executes.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_HC = _load_health_check()


def _health_step(doc: dict[str, Any], key: str) -> tuple[str, int, dict[str, Any]]:
    """(job name, index in that job, step) of the ONE step that runs _HEALTH_STEPS[key]."""
    pattern = _HEALTH_STEPS[key]
    hits = [
        (job_name, i, step)
        for job_name, job in (doc.get("jobs") or {}).items()
        for i, step in enumerate(job.get("steps") or [])
        if pattern.search(_SHELL_COMMENT.sub("", str(step.get("run") or "")))
    ]
    assert len(hits) == 1, (
        f"health.yml: expected exactly one step running {pattern.pattern!r}, "
        f"found {[(job, step.get('name')) for job, _, step in hits]}"
    )
    return hits[0]


def _run_health_step(
    tmp_path: Path, key: str, *, python_rc: int | None = None, xdg: Path | None = None
) -> tuple[subprocess.CompletedProcess[str], Path, list[str]]:
    """Run a health.yml step; return (result, checkout dir, argv the fake command got).

    key names the step by the command it runs (a _HEALTH_STEPS key). python_rc None runs
    the real scripts/health_check.py and CLI; an int makes a fake `python`/`ingester`
    that records its argv and exits with that code.
    """
    doc = _load(_HEALTH_YML)
    _, _, step = _health_step(doc, key)
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


@pytest.mark.parametrize("key", sorted(_HEALTH_STEPS))
@pytest.mark.parametrize("use_xdg", [True, False], ids=["xdg", "home-fallback"])
def test_health_steps_read_state_outside_the_checkout(tmp_path: Path, key: str, use_xdg: bool) -> None:
    xdg = tmp_path / "xdg" if use_xdg else None
    result, checkout, argv = _run_health_step(tmp_path, key, python_rc=0, xdg=xdg)
    assert result.returncode == 0, result.stderr
    state_dir = Path(argv[argv.index("--state-dir") + 1])
    base = xdg if xdg is not None else tmp_path / "home" / ".local" / "state"
    assert state_dir == base / "legal-corpus-ingester" / "state"
    assert not state_dir.resolve().is_relative_to(checkout.resolve())


# Every exit code health_check.py documents, read from the script (F10). Each one except
# EXIT_NOT_WIRED must fail the step with that same code; only EXIT_NOT_WIRED is a warning.
_HC_EXIT_CODES = {name: value for name, value in vars(_HC).items() if name.startswith("EXIT_")}
# Pass-through codes: every documented one, the neighbours of the warning code, and
# shell-level codes (126 not executable, 127 not found, 143 SIGTERM, 255 maximum).
_PASS_THROUGH_CODES = sorted(
    {code for code in _HC_EXIT_CODES.values() if code not in (0, cli.EXIT_NOT_WIRED)}
    | {cli.EXIT_NOT_WIRED - 1, cli.EXIT_NOT_WIRED + 1, cli.EXIT_CONSUMER_SKIPPED, 126, 127, 143, 255}
)


def test_health_check_exit_table_covers_every_documented_code() -> None:
    # #17 added EXIT_CHECKPOINT_INVALID; a reorder must not lose its pass-through row.
    assert "EXIT_CHECKPOINT_INVALID" in _HC_EXIT_CODES
    assert _HC_EXIT_CODES["EXIT_CHECKPOINT_INVALID"] in _PASS_THROUGH_CODES
    # health_check re-exports EXIT_NOT_WIRED; it has the one warning row, never a pass-through row.
    assert _HC_EXIT_CODES.get("EXIT_NOT_WIRED") == cli.EXIT_NOT_WIRED
    assert cli.EXIT_NOT_WIRED not in _PASS_THROUGH_CODES
    for name, code in _HC_EXIT_CODES.items():
        assert code in (*_PASS_THROUGH_CODES, 0, cli.EXIT_NOT_WIRED), f"{name}={code} has no row in the step exit table"


@pytest.mark.parametrize(
    ("rc", "step_rc", "warns"),
    [(0, 0, False), (cli.EXIT_NOT_WIRED, 0, True)] + [(code, code, False) for code in _PASS_THROUGH_CODES],
)
def test_health_check_step_excuses_only_the_not_wired_code(
    tmp_path: Path, rc: int, step_rc: int, warns: bool
) -> None:
    result, _, argv = _run_health_step(tmp_path, "health-check", python_rc=rc, xdg=tmp_path / "xdg")
    assert argv[0] == "scripts/health_check.py"
    assert result.returncode == step_rc, (result.stdout, result.stderr)
    assert ("::warning title=Refresh not wired::" in result.stdout) is warns


# #17 r1: health_check.py is the guard and `ingester status` is diagnostics. Status crashes
# on a malformed checkpoint (CheckpointStore.load; its own card), so if status runs first
# the job dies with a traceback and EXIT_CHECKPOINT_INVALID can never surface.


def test_health_check_step_runs_before_ingester_status() -> None:
    doc = _load(_HEALTH_YML)
    hc_job, hc_idx, hc_step = _health_step(doc, "health-check")
    st_job, st_idx, _ = _health_step(doc, "status")
    assert hc_job == st_job, f"health check ({hc_job}) and status ({st_job}) must be steps of one job"
    assert hc_idx < st_idx, (
        f"health.yml: the health_check.py step (#{hc_idx}) must run before `ingester status` "
        f"(#{st_idx}), or a malformed checkpoint crashes status first and exit "
        f"{_HC.EXIT_CHECKPOINT_INVALID} never surfaces"
    )
    # The guard must not be made conditional or non-fatal while it is being moved.
    assert "if" not in hc_step
    assert "continue-on-error" not in hc_step


def _run_health_job(tmp_path: Path, xdg: Path) -> tuple[int, str | None, list[str], str]:
    """Run health.yml's checkpoint-reading steps for real, in workflow order, as the runner does.

    Returns (job exit code, key of the step that failed the job or None, keys run, output).
    A failing step ends the job unless it sets `continue-on-error: true`. Step `if:`
    expressions are not modelled, so one fails the test instead of being guessed at.
    """
    doc = _load(_HEALTH_YML)
    located = sorted((_health_step(doc, key), key) for key in _HEALTH_STEPS)
    assert len({job for (job, _, _), _ in located}) == 1, "health.yml: checkpoint steps split across jobs"
    ran: list[str] = []
    output = ""
    for (_, _, step), key in located:
        assert "if" not in step, f"health.yml {key} step has an `if:` this test does not model"
        continue_on_error = step.get("continue-on-error", False)
        assert isinstance(continue_on_error, bool), f"health.yml {key}: unmodelled continue-on-error"
        result, _, _ = _run_health_step(tmp_path / key, key, xdg=xdg)
        ran.append(key)
        output += result.stdout + result.stderr
        if result.returncode != 0 and not continue_on_error:
            return result.returncode, key, ran, output
    return 0, None, ran, output


def _first_source() -> str:
    """A source the real config defines, so health_check.py treats its checkpoint as known."""
    configs = sorted((_REPO_ROOT / "config" / "sources").glob("*.yaml"))
    assert configs, "config/sources holds no source configs"
    return configs[0].stem


_JOB_CANARY = "CANARY-17-JOB"
# Malformed checkpoints of each kind: the card's list, other non-objects, unparseable
# JSON, and hostile text (ANSI clear-screen, bidi override) inside a list.
_MALFORMED_CHECKPOINTS = {
    "empty-list": b"[]",
    "string": b'"done"',
    "null": b"null",
    "unparseable": b"{",
    "hostile-list": json.dumps([f"{_JOB_CANARY}\x1b[2J‮"]).encode(),
}


def _writer_checkpoint() -> dict[str, object]:
    """What CheckpointStore.save writes for the first configured source."""
    return dataclasses.asdict(CheckpointState(source_name=_first_source(), stage="done", corpus_version="v1"))


# #17 r2: objects the health check used to pass that crash `ingester status`
# (CheckpointState(**data) raises TypeError), one per kind. The hostile key would reach the
# CI log raw in status's traceback.
_MALFORMED_CHECKPOINTS.update({
    "legacy-stage-only": json.dumps({"stage": "done"}).encode(),
    "missing-corpus-version": json.dumps(
        {k: v for k, v in _writer_checkpoint().items() if k != "corpus_version"}).encode(),
    "missing-source-name": json.dumps(
        {k: v for k, v in _writer_checkpoint().items() if k != "source_name"}).encode(),
    "unknown-key-hostile": json.dumps({**_writer_checkpoint(), f"{_JOB_CANARY}\x1b[2J‮": "x"}).encode(),
})


@pytest.mark.parametrize("raw", list(_MALFORMED_CHECKPOINTS.values()), ids=list(_MALFORMED_CHECKPOINTS))
def test_health_job_surfaces_invalid_checkpoint_exit_code(tmp_path: Path, raw: bytes) -> None:
    xdg = tmp_path / "xdg"
    state = xdg / "legal-corpus-ingester" / "state"
    state.mkdir(parents=True)
    checkpoint = state / f"{_first_source()}.checkpoint.json"
    checkpoint.write_bytes(raw)
    job_rc, failed, ran, output = _run_health_job(tmp_path, xdg)
    assert (job_rc, failed) == (_HC.EXIT_CHECKPOINT_INVALID, "health-check"), (ran, output[-2000:])
    # Honest, safe output: the guard names the bad file; no traceback, no raw hostile bytes.
    assert checkpoint.name in output
    assert "Traceback" not in output
    for forbidden in (_JOB_CANARY, "\x1b", "‮"):
        assert forbidden not in output


def test_health_job_positive_control_runs_both_steps_and_passes(tmp_path: Path) -> None:
    # Must stay green: a legitimate state runs BOTH steps and the job passes. Refresh is wired
    # (terms-analysis#93), so a legitimate state is a fresh "done" checkpoint for every source;
    # an empty state is now a never-run failure (next test), not a not-wired warning.
    xdg = tmp_path / "xdg"
    state = xdg / "legal-corpus-ingester" / "state"
    state.mkdir(parents=True)
    store = CheckpointStore(state)
    for config in sorted((_REPO_ROOT / "config" / "sources").glob("*.yaml")):
        store.save(CheckpointState(source_name=config.stem, stage="done", corpus_version="v1"))
    assert cli.REFRESH_WIRED
    job_rc, failed, ran, output = _run_health_job(tmp_path, xdg)
    assert (job_rc, failed) == (0, None), output[-2000:]
    assert sorted(ran) == sorted(_HEALTH_STEPS)
    assert "::warning title=Refresh not wired::" not in output


def test_health_check_step_end_to_end_never_run_fails_and_ignores_checkout_state(tmp_path: Path) -> None:
    # A fresh checkpoint inside the checkout must not be read: the real runner wipes it.
    (tmp_path / "checkout" / "state").mkdir(parents=True)
    (tmp_path / "checkout" / "state" / "eurlex_gdpr.checkpoint.json").write_text('{"stage": "done"}')
    result, _, _ = _run_health_step(tmp_path, "health-check", xdg=tmp_path / "xdg")
    # Refresh is wired (terms-analysis#93): a source that never ran is a real problem, no
    # longer the excused not-wired state.
    assert cli.REFRESH_WIRED
    assert result.returncode == _HC.EXIT_SOURCE_PROBLEM, (result.stdout, result.stderr)
    assert "::warning title=Refresh not wired::" not in result.stdout
    assert "never-run" in result.stdout
    assert "| fresh |" not in result.stdout


def test_health_check_step_end_to_end_stale_source_fails(tmp_path: Path) -> None:
    state = tmp_path / "xdg" / "legal-corpus-ingester" / "state"
    state.mkdir(parents=True)
    cp = state / "eurlex_gdpr.checkpoint.json"
    cp.write_text(json.dumps(dataclasses.asdict(
        CheckpointState(source_name="eurlex_gdpr", stage="done", corpus_version="v1"))))
    old = time.time() - 30 * 86400
    os.utime(cp, (old, old))
    result, _, _ = _run_health_step(tmp_path, "health-check", xdg=tmp_path / "xdg")
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "| eurlex_gdpr |" in result.stdout
    assert "| stale |" in result.stdout
    assert "::warning" not in result.stdout
