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
4. One persisted state dir (terms-analysis#93 rulings K and L, ingester #71
   option 1). Refresh writes checkpoints, the licence baseline and the bundle
   record; health reads them. Both workflows declare the same INGESTER_STATE_DIR,
   and it persists between hosted runners through the documented
   actions/cache/restore + actions/cache/save pair. Refresh restores before it
   runs and saves under a key unique to the run and attempt with `if: always()`,
   so failure checkpoints persist too. Health restores and never saves. A
   workflow_dispatch input seeds the first bundle record, and a cache miss
   stays loud (`--require-state`; health's never-run exit).
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
import unicodedata
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from legal_corpus_ingester import cli
from legal_corpus_ingester.config import Settings
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
    # Round 5 L: the guard stays live with the cache wired; a skipped or ignored detect would let
    # a lost record pass green.
    assert "if" not in detect and "continue-on-error" not in detect, detect
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


# ---------------------------------------------------------------------------
# K and L (terms-analysis#93, ingester #71): the workflow env, rendered as the runner does
# ---------------------------------------------------------------------------

_REFRESH_YML = _WORKFLOWS_DIR / "refresh.yml"
# The env var Settings reads for state_dir, derived from the Settings class (F13), never restated.
_STATE_DIR_VAR = f"{Settings.model_config['env_prefix']}state_dir".upper()
assert "state_dir" in Settings.model_fields
_EXPR = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


class _Unmodelled(ValueError):
    """A ${{ }} expression this model does not resolve; the checker reports it, never guesses."""


def _contexts(root: Path, checkout: Path, run_id: str = "1001", run_attempt: str = "1") -> dict[str, str]:
    """The runner contexts a state path or cache key may use, with test values."""
    return {
        "runner.temp": str(root / "runner-temp"),
        "github.workspace": str(checkout),
        "runner.os": "Linux",
        "github.run_id": run_id,
        "github.run_attempt": run_attempt,
    }


# Contexts the runner evaluates in workflow- and job-level `env`, per GitHub's context
# availability table (K2): `runner` is NOT among them, so ${{ runner.temp }} there is an error
# (actionlint rejects it), not a path. Steps' `with:` inputs may use runner.* (cache keys).
_ENV_LEVEL_CONTEXTS = frozenset({"github", "secrets", "inputs", "vars", "needs", "strategy", "matrix"})


def _env_contexts(contexts: dict[str, str]) -> dict[str, str]:
    """Only the contexts a workflow- or job-level env value may use."""
    return {k: v for k, v in contexts.items() if k.split(".", 1)[0] in _ENV_LEVEL_CONTEXTS}


def _render(value: object, contexts: dict[str, str], env: dict[str, str]) -> str:
    """Evaluate ${{ ctx }} and ${{ env.X }} as the runner does. Shell syntax is NOT expanded:
    the runner passes `$HOME` or `~` in an env value or a `with:` input through literally."""

    def sub(match: re.Match[str]) -> str:
        expr = match.group(1)
        if expr in contexts:
            return contexts[expr]
        if expr.startswith("env.") and expr[4:] in env:
            return env[expr[4:]]
        raise _Unmodelled(f"unmodelled expression ${{{{ {expr} }}}}")

    return _EXPR.sub(sub, str(value))


def _job_env(doc: dict[str, Any], job_name: str, contexts: dict[str, str]) -> dict[str, str]:
    """Workflow env, then job env, rendered in order (a later value may use ${{ env.X }})."""
    env: dict[str, str] = {}
    for node in (doc, (doc.get("jobs") or {}).get(job_name) or {}):
        for name, value in (node.get("env") or {}).items():
            env[str(name)] = _render(value, _env_contexts(contexts), env)
    return env


def _declared_state_dir(doc: dict[str, Any], job_name: str) -> object:
    """The raw INGESTER_STATE_DIR the job sees (job env overrides workflow env), or None."""
    job = (doc.get("jobs") or {}).get(job_name) or {}
    for node in (job, doc):
        env = node.get("env") or {}
        if _STATE_DIR_VAR in env:
            return env[_STATE_DIR_VAR]
    return None


def state_dir_problems(raw: object, contexts: dict[str, str], env: dict[str, str] | None = None) -> list[str]:
    """Why a declared INGESTER_STATE_DIR value would not be one durable dir outside the checkout."""
    if raw is None:
        return [f"{_STATE_DIR_VAR} is not declared in the workflow or job env"]
    text = str(raw)
    if not text.strip():
        return [f"{_STATE_DIR_VAR} is empty"]
    if any(unicodedata.category(c) in _UNSAFE_CATEGORIES for c in text):
        return [f"{_STATE_DIR_VAR} holds a control, format or line-separator character"]
    try:
        rendered = _render(text, _env_contexts(contexts), env or {})
    except _Unmodelled as exc:
        return [f"{_STATE_DIR_VAR}: {exc}"]
    if "$" in rendered or rendered.startswith("~") or "/~" in rendered:
        return [f"{_STATE_DIR_VAR} uses shell syntax, which a workflow env value never expands"]
    path = Path(os.path.normpath(rendered))
    if not path.is_absolute():
        return [f"{_STATE_DIR_VAR} is relative: it lands in the checkout, which checkout's clean wipes"]
    workspace = Path(os.path.normpath(contexts["github.workspace"]))
    if path.is_relative_to(workspace):
        return [f"{_STATE_DIR_VAR} is inside the checkout, which checkout's clean wipes"]
    if workspace.is_relative_to(path):
        return [f"{_STATE_DIR_VAR} contains the checkout, so caching it caches the checkout"]
    return []


def _health_state_dir(runner_root: Path, xdg: Path | None, home: Path | None = None) -> Path:
    """Where health.yml's steps must read state: the declared INGESTER_STATE_DIR, rendered.

    Until K lands health.yml declares none; then the steps' own XDG layout is the expectation,
    so these job tests keep checking the steps while K's own test is the red one.
    """
    doc = _load(_HEALTH_YML)
    job_name, _, _ = _health_step(doc, "health-check")
    contexts = _contexts(runner_root, runner_root / "checkout")
    raw = _declared_state_dir(doc, job_name)
    if raw is None:
        base = xdg if xdg is not None else (home or runner_root / "home") / ".local" / "state"
        return base / "legal-corpus-ingester" / "state"
    return Path(_render(raw, contexts, _job_env(doc, job_name, contexts)))


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
    tmp_path: Path,
    key: str,
    *,
    python_rc: int | None = None,
    xdg: Path | None = None,
    runner_root: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, list[str]]:
    """Run a health.yml step; return (result, checkout dir, argv the fake command got).

    key names the step by the command it runs (a _HEALTH_STEPS key). python_rc None runs
    the real scripts/health_check.py and CLI; an int makes a fake `python`/`ingester`
    that records its argv and exits with that code. The step gets the workflow and job env
    rendered as the runner renders it (K). The checkout (github.workspace) and HOME live under
    runner_root (default tmp_path), so steps of one job share one checkout and workspace, as
    on a real runner; only this step's fakes and script live under tmp_path.
    """
    doc = _load(_HEALTH_YML)
    job_name, _, step = _health_step(doc, key)
    root = runner_root or tmp_path
    checkout = root / "checkout"
    # Idempotent: the first step of the job lays out the checkout, later steps reuse it (a test
    # may already have made the dir, e.g. by creating the state dir beside it).
    (checkout / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (checkout / ".venv" / "bin" / "activate").write_text("")
    for name in ("scripts", "config"):
        if not (checkout / name).is_symlink():
            (checkout / name).symlink_to(_REPO_ROOT / name)
    fakebin = tmp_path / "bin"
    fakebin.mkdir(parents=True)
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
    home = root / "home"
    home.mkdir(exist_ok=True)
    env = {"PATH": f"{fakebin}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(home)}
    if python_rc is None:
        # Import this checkout's code, as pytest does (pyproject pythonpath), not an install.
        env["PYTHONPATH"] = str(_REPO_ROOT / "src")
    if xdg is not None:
        env["XDG_STATE_HOME"] = str(xdg)
    env.update(_job_env(doc, job_name, _contexts(root, checkout)))
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
    # K: both steps read the declared INGESTER_STATE_DIR (refresh writes there), whatever
    # XDG_STATE_HOME or HOME say; a step left on its own path reads a dir refresh never fills.
    xdg = tmp_path / "xdg" if use_xdg else None
    result, checkout, argv = _run_health_step(tmp_path, key, python_rc=0, xdg=xdg)
    assert result.returncode == 0, result.stderr
    state_dir = Path(argv[argv.index("--state-dir") + 1])
    assert state_dir == _health_state_dir(tmp_path, xdg, tmp_path / "home")
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
        result, _, _ = _run_health_step(tmp_path / key, key, xdg=xdg, runner_root=tmp_path)
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
    state = _health_state_dir(tmp_path, xdg)
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
    state = _health_state_dir(tmp_path, xdg)
    state.mkdir(parents=True)
    store = CheckpointStore(state)
    for config in sorted((_REPO_ROOT / "config" / "sources").glob("*.yaml")):
        store.save(CheckpointState(source_name=config.stem, stage="done", corpus_version="v1"))
    assert cli.REFRESH_WIRED
    job_rc, failed, ran, output = _run_health_job(tmp_path, xdg)
    assert (job_rc, failed) == (0, None), output[-2000:]
    assert sorted(ran) == sorted(_HEALTH_STEPS)
    # One job, one checkout: the steps' scratch dirs hold no checkout of their own.
    assert sorted(p.name for p in tmp_path.glob("*/checkout")) == []
    assert (tmp_path / "checkout").is_dir()
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
    state = _health_state_dir(tmp_path, tmp_path / "xdg")
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


# ---------------------------------------------------------------------------
# K: refresh and health declare one INGESTER_STATE_DIR (terms-analysis#93)
# ---------------------------------------------------------------------------

_REFRESH_RUN = re.compile(r"(?<![\w.-])ingester\s+refresh\b")
_DETECT_RUN = re.compile(r"(?<![\w.-])scripts/detect_bundle_change\.py\s+detect\b")
_RECORD_RUN = re.compile(r"(?<![\w.-])scripts/detect_bundle_change\.py\s+record\b")


def _cmd_steps(doc: dict[str, Any], pattern: re.Pattern[str]) -> list[tuple[str, int, dict[str, Any]]]:
    """(job, index, step) of every step whose run, comments stripped, runs *pattern*."""
    return [
        (job_name, i, step)
        for job_name, job in (doc.get("jobs") or {}).items()
        for i, step in enumerate(job.get("steps") or [])
        if pattern.search(_SHELL_COMMENT.sub("", str(step.get("run") or "")))
    ]


def _one_cmd_step(doc: dict[str, Any], pattern: re.Pattern[str]) -> tuple[str, int, dict[str, Any]]:
    hits = _cmd_steps(doc, pattern)
    assert len(hits) == 1, f"expected one step running {pattern.pattern!r}, found {len(hits)}"
    return hits[0]


# Declared values, each with the verdict the checker must give. Negatives: every way a value
# looks like a path in YAML but is not one durable dir outside the checkout on the runner.
_STATE_DIR_VALUES: list[tuple[object, bool]] = [
    # K2: the parent of the workspace, beside the checkout, as both workflows declare it.
    ("${{ github.workspace }}/../legal-corpus-ingester-state", True),
    ("${{github.workspace}}/../state", True),
    # K2: the runner context is not available in workflow or job env (GitHub's table).
    ("${{ runner.temp }}/legal-corpus-ingester/state", False),
    ("${{runner.temp}}/state", False),
    # Back into the checkout, or a dir that holds the checkout.
    ("${{ github.workspace }}/../checkout/state", False),
    ("${{ github.workspace }}/x/../state", False),
    ("${{ github.workspace }}/..", False),
    ("${{ github.workspace }}/../..", False),
    (None, False),
    ("", False),
    ("   ", False),
    ("state", False),
    ("./state", False),
    ("${XDG_STATE_HOME:-$HOME/.local/state}/legal-corpus-ingester/state", False),
    ("$HOME/.local/state/legal-corpus-ingester/state", False),
    ("~/.local/state/legal-corpus-ingester/state", False),
    # Absolute once rendered, but the shell variable stays literal: env values never expand.
    ("${{ github.workspace }}/../$RUNNER_NAME/state", False),
    ("${{ github.workspace }}/state", False),
    ("${{ secrets.STATE_DIR }}/state", False),
    ("${{ github.workspace }}/../state\n/etc", False),
    ("${{ github.workspace }}/../state x", False),
    ("${{ github.workspace }}/../st‮ate", False),
    ("${{ github.workspace }}/../st\x00ate", False),
]


def test_state_dir_table_has_positive_and_negative_rows() -> None:
    verdicts = {ok for _, ok in _STATE_DIR_VALUES}
    assert verdicts == {True, False}


@pytest.mark.parametrize(("raw", "ok"), _STATE_DIR_VALUES, ids=[repr(v)[:40] for v, _ in _STATE_DIR_VALUES])
def test_state_dir_checker_verdicts(tmp_path: Path, raw: object, ok: bool) -> None:
    problems = state_dir_problems(raw, _contexts(tmp_path, tmp_path / "checkout"))
    assert (problems == []) is ok, problems


@pytest.mark.parametrize(
    ("workflow", "pattern"),
    [(_REFRESH_YML, _REFRESH_RUN), (_HEALTH_YML, _HEALTH_STEPS["health-check"])],
    ids=["refresh", "health"],
)
def test_workflow_declares_a_durable_state_dir(tmp_path: Path, workflow: Path, pattern: re.Pattern[str]) -> None:
    # K: the job that writes (refresh) or reads (health) state declares INGESTER_STATE_DIR.
    doc = _load(workflow)
    job_name, _, _ = _one_cmd_step(doc, pattern)
    contexts = _contexts(tmp_path, tmp_path / "checkout")
    raw = _declared_state_dir(doc, job_name)
    assert state_dir_problems(raw, contexts, _job_env(doc, job_name, contexts)) == []


def test_refresh_and_health_declare_the_same_state_dir(tmp_path: Path) -> None:
    # K (Copilot, refresh.yml:63): refresh writes where health reads, on every runner.
    contexts = _contexts(tmp_path, tmp_path / "checkout")
    rendered = {}
    for workflow, pattern in ((_REFRESH_YML, _REFRESH_RUN), (_HEALTH_YML, _HEALTH_STEPS["health-check"])):
        doc = _load(workflow)
        job_name, _, _ = _one_cmd_step(doc, pattern)
        raw = _declared_state_dir(doc, job_name)
        assert raw is not None, f"{workflow.name}: {_STATE_DIR_VAR} is not declared"
        rendered[workflow.name] = _render(raw, contexts, _job_env(doc, job_name, contexts))
    assert len(set(rendered.values())) == 1, rendered


# ---------------------------------------------------------------------------
# L: actions/cache persistence, as the actions/cache restore and save READMEs document it
# (inputs path, key, restore-keys, fail-on-cache-miss, lookup-only; save with the same path)
# ---------------------------------------------------------------------------

_CACHE = "actions/cache"
_CACHE_RESTORE = "actions/cache/restore"
_CACHE_SAVE = "actions/cache/save"
# The repo's pin convention, as every existing `uses:` line follows it: a full commit SHA and
# a version comment (checked against the whole repo below, so it is not a guess).
_PINNED_USES = re.compile(r"^\s*(?:-\s+)?uses:\s+([\w.-]+/[\w./-]+)@([0-9a-f]{40})\s+#\s*v\d+(?:\.\d+)*\s*$")
_USES_LINE = re.compile(r"^\s*(?:-\s+)?uses:")


def _action(step: dict[str, Any]) -> str | None:
    uses = step.get("uses")
    return str(uses).split("@", 1)[0] if uses else None


def _action_steps(doc: dict[str, Any], action: str) -> list[tuple[str, int, dict[str, Any]]]:
    return [
        (job_name, i, step)
        for job_name, job in (doc.get("jobs") or {}).items()
        for i, step in enumerate(job.get("steps") or [])
        if _action(step) == action
    ]


def _lines(value: object) -> list[str]:
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


def _cache_inputs(
    doc: dict[str, Any], job_name: str, step: dict[str, Any], contexts: dict[str, str]
) -> dict[str, Any]:
    """The step's `with:` inputs rendered as the runner renders them (multi-line lists split)."""
    env = _job_env(doc, job_name, contexts)
    env.update({str(k): _render(v, contexts, env) for k, v in (step.get("env") or {}).items()})
    with_ = step.get("with") or {}
    return {
        "path": [_render(line, contexts, env) for line in _lines(with_.get("path"))],
        "key": _render(with_.get("key") or "", contexts, env).strip(),
        "restore-keys": [_render(line, contexts, env) for line in _lines(with_.get("restore-keys"))],
        "fail-on-cache-miss": str(with_.get("fail-on-cache-miss", "false")).strip().lower(),
        "lookup-only": str(with_.get("lookup-only", "false")).strip().lower(),
    }


def _bare_if(step: dict[str, Any]) -> str:
    text = str(step.get("if", "")).strip()
    match = re.fullmatch(r"\$\{\{\s*(.*?)\s*\}\}", text)
    return match.group(1) if match else text


def _rendered_state_dir(doc: dict[str, Any], job_name: str, contexts: dict[str, str]) -> str:
    raw = _declared_state_dir(doc, job_name)
    assert raw is not None, f"{_STATE_DIR_VAR} is not declared (K)"
    return _render(raw, contexts, _job_env(doc, job_name, contexts))


def _the_one(doc: dict[str, Any], action: str, where: str) -> tuple[str, int, dict[str, Any]]:
    hits = _action_steps(doc, action)
    assert len(hits) == 1, f"{where}: expected exactly one {action} step, found {len(hits)}"
    return hits[0]


def test_refresh_restores_the_state_cache_before_it_runs(tmp_path: Path) -> None:
    doc = _load(_REFRESH_YML)
    job_name, run_idx, _ = _one_cmd_step(doc, _REFRESH_RUN)
    _, detect_idx, _ = _one_cmd_step(doc, _DETECT_RUN)
    rjob, ridx, restore = _the_one(doc, _CACHE_RESTORE, "refresh.yml")
    assert rjob == job_name and ridx < run_idx and ridx < detect_idx, (
        "refresh.yml: the cache restore must run before refresh and change detection read state"
    )
    # Restore always runs: a skipped or ignored restore makes every run a first run.
    assert "if" not in restore and "continue-on-error" not in restore
    contexts = _contexts(tmp_path, tmp_path / "checkout")
    inputs = _cache_inputs(doc, rjob, restore, contexts)
    assert _rendered_state_dir(doc, job_name, contexts) in inputs["path"], inputs["path"]
    assert inputs["key"], "restore needs a primary key"
    assert inputs["restore-keys"], "restore needs a restore-keys prefix to find the latest run's save"
    # The first ever run has no cache; it must reach the seed step, so a miss cannot fail here.
    assert inputs["fail-on-cache-miss"] != "true"
    # lookup-only checks for a cache without downloading it: nothing would be restored.
    assert inputs["lookup-only"] != "true"


def test_refresh_saves_the_state_cache_under_a_unique_key_on_every_outcome(tmp_path: Path) -> None:
    doc = _load(_REFRESH_YML)
    job_name, run_idx, _ = _one_cmd_step(doc, _REFRESH_RUN)
    # The combined actions/cache saves only when the job succeeds: failure checkpoints and the
    # licence baseline of a failed run would be lost.
    assert _action_steps(doc, _CACHE) == [], "refresh.yml uses the combined actions/cache"
    _, _, restore = _the_one(doc, _CACHE_RESTORE, "refresh.yml")
    sjob, sidx, save = _the_one(doc, _CACHE_SAVE, "refresh.yml")
    assert sjob == job_name
    assert _bare_if(save) == "always()", f"save must run on every outcome, got if: {save.get('if')!r}"
    writers = [run_idx] + [i for _, i, _ in _cmd_steps(doc, _RECORD_RUN)]
    assert sidx > max(writers), "refresh.yml: the cache save must run after every step that writes state"
    contexts = _contexts(tmp_path, tmp_path / "checkout")
    restore_in = _cache_inputs(doc, job_name, restore, contexts)
    save_in = _cache_inputs(doc, job_name, save, contexts)
    # restore/README: use the same path as the save; the cache version hashes the paths.
    assert save_in["path"] == restore_in["path"], (save_in["path"], restore_in["path"])
    keys = {
        _cache_inputs(doc, job_name, save, _contexts(tmp_path, tmp_path / "checkout", run, attempt))["key"]
        for run, attempt in (("1001", "1"), ("1001", "2"), ("1002", "1"))
    }
    assert len(keys) == 3, f"save key must be unique per run and attempt (a cache key is immutable): {keys}"
    for key in keys:
        assert any(key.startswith(prefix) for prefix in restore_in["restore-keys"]), (
            f"the next refresh cannot find {key!r} with restore-keys {restore_in['restore-keys']}"
        )


def test_health_restores_the_refresh_cache_and_never_saves(tmp_path: Path) -> None:
    hdoc, rdoc = _load(_HEALTH_YML), _load(_REFRESH_YML)
    for action in (_CACHE, _CACHE_SAVE):
        assert _action_steps(hdoc, action) == [], f"health.yml must never save state ({action})"
    hjob, hidx, restore = _the_one(hdoc, _CACHE_RESTORE, "health.yml")
    for key in _HEALTH_STEPS:
        job_name, idx, _ = _health_step(hdoc, key)
        assert job_name == hjob and hidx < idx, f"health.yml: restore must precede the {key} step"
    assert "if" not in restore and "continue-on-error" not in restore
    contexts = _contexts(tmp_path, tmp_path / "checkout")
    health_in = _cache_inputs(hdoc, hjob, restore, contexts)
    rjob, _, _ = _one_cmd_step(rdoc, _REFRESH_RUN)
    _, _, save = _the_one(rdoc, _CACHE_SAVE, "refresh.yml")
    save_in = _cache_inputs(rdoc, rjob, save, contexts)
    assert health_in["path"] == save_in["path"], (health_in["path"], save_in["path"])
    assert any(save_in["key"].startswith(p) for p in health_in["restore-keys"]), (
        save_in["key"], health_in["restore-keys"]
    )
    assert health_in["lookup-only"] != "true"


def test_cache_actions_are_pinned_like_every_other_action() -> None:
    cache_shas: set[str] = set()
    for path in _WORKFLOW_FILES:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not _USES_LINE.match(line):
                continue
            match = _PINNED_USES.match(line)
            assert match, f"{path.name}: not pinned to a commit SHA with a version comment: {line.strip()!r}"
            if match.group(1).startswith(_CACHE + "/"):
                cache_shas.add(match.group(2))
    for path in (_REFRESH_YML, _HEALTH_YML):
        assert _action_steps(_load(path), _CACHE_RESTORE), f"{path.name} has no {_CACHE_RESTORE} step"
    # restore and save are sub-actions of one repo: one release, one SHA.
    assert len(cache_shas) == 1, cache_shas


def _dispatch_inputs(doc: dict[str, Any]) -> dict[str, Any]:
    # PyYAML reads the bare key `on` as the boolean True.
    on = doc.get("on", doc.get(True)) or {}
    dispatch = on.get("workflow_dispatch") if isinstance(on, dict) else None
    return dict((dispatch or {}).get("inputs") or {})


def test_refresh_has_a_dispatch_seed_that_writes_the_first_record() -> None:
    # #71: with --require-state the first record must be written on purpose, once.
    doc = _load(_REFRESH_YML)
    inputs = _dispatch_inputs(doc)
    assert inputs, "refresh.yml: workflow_dispatch has no inputs, so nothing can seed the first record"
    seeds = []
    for job_name, idx, step in _cmd_steps(doc, _RECORD_RUN):
        gate = _bare_if(step)
        used = [n for n in inputs if re.search(rf"\b(?:github\.event\.)?inputs\.{re.escape(str(n))}\b", gate)]
        if used:
            seeds.append((idx, step, used[0]))
    assert len(seeds) == 1, f"expected one record step gated on a dispatch input, found {len(seeds)}"
    seed_idx, seed, name = seeds[0]
    spec = inputs[name] or {}
    # Never seeds by accident: a schedule has no inputs and a plain dispatch keeps the default.
    assert spec.get("type") == "boolean", spec
    assert spec.get("default", False) in (False, "false"), spec
    assert "continue-on-error" not in seed
    _, run_idx, _ = _one_cmd_step(doc, _REFRESH_RUN)
    _, detect_idx, _ = _one_cmd_step(doc, _DETECT_RUN)
    _, save_idx, _ = _the_one(doc, _CACHE_SAVE, "refresh.yml")
    # After refresh publishes, before detect's --require-state reads the record, before the save.
    assert run_idx < seed_idx < detect_idx < save_idx, (run_idx, seed_idx, detect_idx, save_idx)


@pytest.mark.parametrize("workflow", [_REFRESH_YML, _HEALTH_YML], ids=["refresh", "health"])
def test_dispatch_inputs_never_reach_a_run_script(workflow: Path) -> None:
    # Script injection: ${{ inputs.x }} is pasted into the script before bash parses it.
    for job_name, _, step in _steps(_load(workflow)):
        run = str(step.get("run") or "")
        assert not re.search(r"\$\{\{[^}]*\binputs\.", run), _step_label(job_name, step)


def _load_detect_bundle_change() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_detect_bundle_change_for_workflow_contract", _REPO_ROOT / "scripts" / "detect_bundle_change.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_bundle_record_lives_in_the_cached_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The seed and the record are only durable if the file they write is inside a cached path.
    # Each detect/record step runs for real with a fake `python` that logs its argv and env;
    # the state file is its --state-file, else the script's own default for that env.
    doc = _load(_REFRESH_YML)
    job_name, _, _ = _one_cmd_step(doc, _REFRESH_RUN)
    checkout = tmp_path / "checkout"
    contexts = _contexts(tmp_path, checkout)
    _, _, restore = _the_one(doc, _CACHE_RESTORE, "refresh.yml")
    cached = [Path(p) for p in _cache_inputs(doc, job_name, restore, contexts)["path"]]
    module = _load_detect_bundle_change()
    steps = _cmd_steps(doc, _DETECT_RUN) + _cmd_steps(doc, _RECORD_RUN)
    assert len(steps) >= 2
    (checkout / ".venv" / "bin").mkdir(parents=True)
    (checkout / ".venv" / "bin" / "activate").write_text("")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    log = tmp_path / "python.json"
    (fakebin / "python").write_text(
        "#!/bin/bash\n"
        f'"{sys.executable}" -c "import json,os,sys; json.dump({{\'argv\': sys.argv[2:], '
        f"'env': dict(os.environ)}}, open(sys.argv[1], 'w'))\" \"{log}\" \"$@\"\n"
    )
    (fakebin / "python").chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    for _, _, step in steps:
        env = {"PATH": f"{fakebin}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(home),
               "GITHUB_OUTPUT": str(tmp_path / "gh-output")}
        env.update(_job_env(doc, job_name, contexts))
        env.update({str(k): _render(v, contexts, env) for k, v in (step.get("env") or {}).items()})
        script = tmp_path / "step.sh"
        script.write_text(str(step["run"]))
        log.unlink(missing_ok=True)
        result = subprocess.run(
            _REQUIRED_SHELL.replace("{0}", str(script)).split(),
            cwd=checkout, env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        seen = json.loads(log.read_text())
        argv = seen["argv"]
        if "--state-file" in argv:
            state_file = Path(argv[argv.index("--state-file") + 1])
        else:
            for name in ("XDG_STATE_HOME", "HOME"):
                if name in seen["env"]:
                    monkeypatch.setenv(name, seen["env"][name])
                else:
                    monkeypatch.delenv(name, raising=False)
            state_file = module.default_state_file()
        label = _step_label(job_name, step)
        assert state_file.is_absolute(), (label, state_file)
        assert any(state_file.is_relative_to(c) for c in cached), (
            f"{label}: the bundle record {state_file} is outside every cached path {cached}"
        )


# --- terms-analysis#93 Round 11 (rthoB): only a seed dispatch seeds the licence baseline -------
# What `inputs.seed_bundle_record` evaluates to on each trigger, and whether that run may seed. A
# schedule has no inputs (the expression renders as ''); a plain dispatch keeps the default.
_SEED_EVENTS: dict[str, tuple[str, bool]] = {
    "schedule": ("", False),
    "dispatch-default": ("false", False),
    "dispatch-seed": ("true", True),
}


def test_seed_event_table_has_both_verdicts() -> None:
    assert {seeds for _, seeds in _SEED_EVENTS.values()} == {True, False}


def _run_refresh_step(tmp_path: Path, seed_input: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run refresh.yml's one `ingester refresh` step with a fake `ingester` that logs its argv.

    The step's own `env:` is rendered with the dispatch input set to *seed_input*. Only plain
    `${{ inputs.x }}` / `${{ github.event.inputs.x }}` references are modelled; any other
    expression raises _Unmodelled rather than being guessed.
    """
    doc = _load(_REFRESH_YML)
    job_name, _, step = _one_cmd_step(doc, _REFRESH_RUN)
    checkout = tmp_path / "checkout"
    (checkout / ".venv" / "bin").mkdir(parents=True)
    (checkout / ".venv" / "bin" / "activate").write_text("")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    argv_log = tmp_path / "argv.json"
    (fakebin / "ingester").write_text(
        "#!/bin/bash\n"
        f'"{sys.executable}" -c "import json,sys; json.dump(sys.argv[2:], open(sys.argv[1], \'w\'))"'
        f' "{argv_log}" "$@"\n'
    )
    (fakebin / "ingester").chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    contexts = _contexts(tmp_path, checkout)
    job_env = _job_env(doc, job_name, contexts)
    inputs = {
        "inputs.seed_bundle_record": seed_input,
        "github.event.inputs.seed_bundle_record": seed_input,
    }
    env = {"PATH": f"{fakebin}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(home), **job_env}
    for name, value in (step.get("env") or {}).items():
        env[str(name)] = _render(value, {**contexts, **inputs}, job_env)
    script = tmp_path / "step.sh"
    script.write_text(str(step["run"]))
    shell = _default_shell(doc)
    assert shell == _REQUIRED_SHELL
    result = subprocess.run(
        shell.replace("{0}", str(script)).split(),
        cwd=checkout, env=env, capture_output=True, text=True, timeout=60,
    )
    argv = json.loads(argv_log.read_text()) if argv_log.exists() else []
    return result, argv


@pytest.mark.parametrize("event", sorted(_SEED_EVENTS))
def test_refresh_step_passes_the_seed_flag_only_on_a_seed_dispatch(tmp_path: Path, event: str) -> None:
    # Round 11 item 2: the flag is cli's constant (never restated here); a scheduled run and a
    # plain dispatch never seed, so a cache miss there stays red instead of re-baselining.
    flag = getattr(cli, "REFRESH_SEED_FLAG", None)
    assert isinstance(flag, str) and flag.startswith("--"), "cli names no REFRESH_SEED_FLAG (Round 11 item 2)"
    doc = _load(_REFRESH_YML)
    _, _, step = _one_cmd_step(doc, _REFRESH_RUN)
    # Refresh itself runs on every trigger; only the flag depends on the input.
    assert "if" not in step, step.get("if")
    seed_input, seeds = _SEED_EVENTS[event]
    result, argv = _run_refresh_step(tmp_path, seed_input)
    assert result.returncode == 0, (event, result.stderr[-400:])
    assert argv[:2] == ["refresh", "--all"], (event, argv)
    assert argv.count(flag) == (1 if seeds else 0), (event, argv)
