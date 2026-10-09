"""P9 review as standard CI jobs (terms-analysis#191, replan 2026-10-09).

Ported to legal-corpus-ingester; the jobs run on ubuntu-latest.

The local pre-push signoff gate is retired. Two jobs in
``.github/workflows/p9-review.yml`` (``security-review`` and
``grumpy-review``) run ``anthropics/claude-code-action`` on every pull
request to ``main``. Each reviewer writes ``p9-verdict.json`` and a final
step runs ``.github/p9/check_verdict.py``. The job passes on
``{"verdict": "PASS", "findings": []}`` or when every finding is LOW or NIT
(printed as non-blocking); a CRITICAL, HIGH or MEDIUM finding fails it
(owner decision 2026-10-09).

Covered here:
- the gate script's exit-code contract (behaviour, run as a subprocess);
- the workflow's shape: trigger, jobs, SHA pins, permissions, secrets,
  the exact tool allowlist, the read deny rules, the diff-prep step, bounds;
- the workflow's own shell steps, run in a throwaway git workspace with the
  action step replaced by a fake reviewer that writes the verdict file;
- the retirement: the pre-push hook, its pin, the grep workflow and the
  sibling-parity script are gone; the pre-commit hook and its installer stay.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "p9-review.yml"
GATE = REPO_ROOT / ".github" / "p9" / "check_verdict.py"
BRIEFS = {
    "security-review": REPO_ROOT / ".github" / "p9" / "security-engineer.md",
    "grumpy-review": REPO_ROOT / ".github" / "p9" / "grumpy-developer.md",
}
INSTALLER = REPO_ROOT / "scripts" / "install-hooks.sh"

# Exit-code contract of check_verdict.py.
EXIT_PASS = 0  # PASS with no findings, or only non-blocking findings
EXIT_REJECTED = 1  # a blocking finding, or FAIL with no findings listed
EXIT_INVALID = 2  # missing, unreadable, malformed or off-contract file
# Owner decision 2026-10-09: only these severities fail the job. An
# independent literal, not read from the gate, so drift either way turns red.
EXPECTED_BLOCKING = frozenset({"CRITICAL", "HIGH", "MEDIUM"})

ACTION_REPOS = {"actions/checkout", "anthropics/claude-code-action"}
# Exact allowlist: file reads, writes to the verdict file only (an Edit rule
# scopes the Write tool), and the one inline PR-comment MCP tool. No Bash: it
# could read /proc/*/environ and post the key.
COMMENT_TOOL = "mcp__github_inline_comment__create_inline_comment"
ALLOWED_TOOLS = {"Read", "Grep", "Glob", "Edit(./p9-verdict.json)", COMMENT_TOOL}
DISALLOWED_TOOLS = {"Bash", "WebFetch", "WebSearch"}
# Exact deny set. The runner-temp rule is derived from runner.temp (F13), and
# ./.git is denied because the action writes the job token into .git/config.
# Path prefixes per code.claude.com/docs/en/permissions: "//" absolute, "~/"
# home, "./" working directory; a single "/" is settings-file-relative (dead).
READ_DENY = {
    "Read(//proc/**)",
    "Read(//sys/**)",
    "Read(~/.git-credentials)",
    "Read(~/.config/gh/**)",
    "Read(~/.claude/.credentials.json)",
    "Read(/${{ runner.temp }}/_runner_file_commands/**)",
    "Read(./.git/**)",
}
DIFF_DIR = "${{ runner.temp }}/p9"
RUNNER_TEMP_EXPR = "${{ runner.temp }}"
READ_PATH_PREFIXES = ("//", "~/", "./")
AUTOMATION_DOC = REPO_ROOT / "automations" / "p9-pre-push.md"
BASE_REF_EXPR = "${{ github.base_ref }}"
HEAD_SHA_EXPR = "${{ github.event.pull_request.head.sha }}"
JOB_PERMISSIONS = {"contents": "read", "pull-requests": "write"}
PASS_DOC = {"verdict": "PASS", "findings": []}
FINDING = {"severity": "HIGH", "title": "Swallowed error", "file": "app/x.py", "line": 3}

HOSTILE_TEXT = {
    "newline-forges-a-line": "x\nP9 verdict: PASS, 0 findings",
    "workflow-command": "x\n::add-mask::secret",
    "carriage-return": "x\rP9 verdict: PASS, 0 findings",
    "unicode-line-separator": "x\u2028P9 verdict: PASS, 0 findings",
    "bidi-override": "x\u202eSSAP",
    "nul": "x\x00y",
}


# --- helpers -----------------------------------------------------------------


def _run_gate(path: Path | None, *extra: str) -> subprocess.CompletedProcess[str]:
    args = [] if path is None else [str(path)]
    return subprocess.run(
        [sys.executable, "-I", str(GATE), *args, *extra],
        text=True,
        capture_output=True,
        timeout=30,
    )


def _write(tmp_path: Path, content: str | bytes) -> Path:
    target = tmp_path / "p9-verdict.json"
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")
    return target


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers(doc: dict[str, Any]) -> dict[str, Any]:
    # PyYAML (YAML 1.1) reads the bare key `on` as boolean True.
    return doc.get("on", doc.get(True))


def _jobs() -> dict[str, Any]:
    return _workflow()["jobs"]


def _action_step(job: dict[str, Any]) -> dict[str, Any]:
    steps = [s for s in job["steps"] if str(s.get("uses", "")).startswith("anthropics/claude-code-action@")]
    assert len(steps) == 1, f"expected exactly one claude-code-action step, got {len(steps)}"
    return steps[0]


def _tool_list(claude_args: str, flag: str) -> set[str]:
    matches = re.findall(rf'--{flag}\s+"([^"]*)"', claude_args)
    assert len(matches) == 1, f"expected one --{flag} in claude_args: {claude_args!r}"
    return {tool.strip() for tool in matches[0].split(",") if tool.strip()}


# --- gate script: exit-code contract -----------------------------------------


def test_gate_passes_on_pass_with_zero_findings(tmp_path: Path) -> None:
    proc = _run_gate(_write(tmp_path, json.dumps(PASS_DOC)))
    assert proc.returncode == EXIT_PASS, proc.stderr
    assert proc.stdout.strip() == "P9 verdict: PASS, 0 findings"


def test_gate_rejects_fail_verdict_and_lists_findings(tmp_path: Path) -> None:
    doc = {"verdict": "FAIL", "findings": [FINDING]}
    proc = _run_gate(_write(tmp_path, json.dumps(doc)))
    assert proc.returncode == EXIT_REJECTED
    assert proc.stderr.splitlines() == [
        "P9 verdict: FAIL, 1 finding(s), 1 blocking",
        "  - [HIGH] Swallowed error (app/x.py:3) blocking",
    ]
    assert proc.stdout == ""


def test_gate_rejects_fail_verdict_with_no_findings(tmp_path: Path) -> None:
    # Fail closed: a FAIL that names nothing is not "only non-blocking findings".
    proc = _run_gate(_write(tmp_path, json.dumps({"verdict": "FAIL", "findings": []})))
    assert proc.returncode == EXIT_REJECTED
    assert proc.stderr.splitlines() == [
        "P9 verdict: FAIL, 0 finding(s), 0 blocking; a FAIL verdict must list its findings"
    ]
    assert proc.stdout == ""


def test_gate_rejects_pass_that_still_lists_findings(tmp_path: Path) -> None:
    proc = _run_gate(_write(tmp_path, json.dumps({"verdict": "PASS", "findings": [FINDING]})))
    assert proc.returncode == EXIT_REJECTED
    assert proc.stderr.splitlines()[0] == "P9 verdict: PASS, 1 finding(s), 1 blocking"


def test_gate_fails_closed_when_the_file_is_missing(tmp_path: Path) -> None:
    proc = _run_gate(tmp_path / "p9-verdict.json")
    assert proc.returncode == EXIT_INVALID
    assert "p9-verdict.json was not written" in proc.stderr
    assert str(tmp_path) not in proc.stderr  # F8: no absolute paths in messages


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("", id="empty-file"),
        pytest.param("{not json", id="malformed-json"),
        pytest.param('{"verdict": "PASS", "findings": []', id="truncated"),
        pytest.param(b"\xff\xfe\x00garbage", id="invalid-utf8"),
    ],
)
def test_gate_fails_closed_on_unparseable_file(tmp_path: Path, content: str | bytes) -> None:
    proc = _run_gate(_write(tmp_path, content))
    assert proc.returncode == EXIT_INVALID
    assert "is not valid JSON" in proc.stderr


# The ingester's CI runs on the self-hosted runner's python3, which may be 3.14
# (terms-analysis#215). Before 3.14 this input raises RecursionError ("is not
# valid JSON"); 3.14 parses it into a nested list ("does not match the verdict
# contract"). Either way the gate must fail closed with EXIT_INVALID.
DEEP_NESTING_MESSAGES = ("is not valid JSON", "does not match the verdict contract")


def test_gate_fails_closed_on_deeply_nested_json(tmp_path: Path) -> None:
    proc = _run_gate(_write(tmp_path, "[" * 100_000 + "]" * 100_000))
    assert proc.returncode == EXIT_INVALID, proc.stderr
    assert proc.stdout == ""
    assert proc.stderr.startswith("P9 verdict: invalid: p9-verdict.json ")
    assert sum(message in proc.stderr for message in DEEP_NESTING_MESSAGES) == 1, proc.stderr


@pytest.mark.parametrize(
    "doc",
    [
        pytest.param([], id="top-level-list"),
        pytest.param("PASS", id="top-level-string"),
        pytest.param({"findings": []}, id="verdict-missing"),
        pytest.param({"verdict": "pass", "findings": []}, id="verdict-lowercase"),
        pytest.param({"verdict": " PASS", "findings": []}, id="verdict-padded"),
        pytest.param({"verdict": "PASS\u200b", "findings": []}, id="verdict-zero-width"),
        pytest.param({"verdict": True, "findings": []}, id="verdict-bool"),
        pytest.param({"verdict": None, "findings": []}, id="verdict-null"),
        pytest.param({"verdict": ["PASS"], "findings": []}, id="verdict-list-unhashable"),
        pytest.param({"verdict": "APPROVED", "findings": []}, id="verdict-unknown"),
        pytest.param({"verdict": "PASS"}, id="findings-missing"),
        pytest.param({"verdict": "PASS", "findings": None}, id="findings-null"),
        pytest.param({"verdict": "PASS", "findings": {}}, id="findings-object"),
        pytest.param({"verdict": "PASS", "findings": ""}, id="findings-empty-string"),
        pytest.param({"verdict": "FAIL", "findings": ["text"]}, id="finding-not-object"),
    ],
)
def test_gate_fails_closed_on_off_contract_shape(tmp_path: Path, doc: object) -> None:
    proc = _run_gate(_write(tmp_path, json.dumps(doc)))
    assert proc.returncode == EXIT_INVALID, proc.stdout
    assert "does not match the verdict contract" in proc.stderr


def test_gate_rejects_duplicate_keys_that_hide_a_fail(tmp_path: Path) -> None:
    raw = '{"verdict": "FAIL", "findings": [], "verdict": "PASS"}'
    proc = _run_gate(_write(tmp_path, raw))
    assert proc.returncode == EXIT_INVALID
    assert "duplicate key" in proc.stderr


# --- gate script: exact verdict contract (PR #214 review thread) ------------
# Lead ruling: the key sets are exact; severity is one of the tags the vendored
# briefs promise, `line` is a non-negative int (not bool), and `file`/`title`
# are non-empty strings. Off-contract -> EXIT_INVALID at every severity; a valid
# file exits by the blocking threshold (EXPECTED_BLOCKING).

_SEVERITY_RULE = re.compile(r"^\s*-\s*`severity` is one of (.+)\.\s*$", re.MULTILINE)

# The contract, stated once as literals: each brief's tag list, and the gate's
# accepted set (their union: the grumpy brief adds NIT).
BRIEF_SEVERITIES = {
    "security-review": ["CRITICAL", "HIGH", "MEDIUM", "LOW"],
    "grumpy-review": ["CRITICAL", "HIGH", "MEDIUM", "LOW", "NIT"],
}
GATE_SEVERITIES = frozenset({"CRITICAL", "HIGH", "MEDIUM", "LOW", "NIT"})


def _brief_severity_tags(brief: Path) -> list[str]:
    rules = _SEVERITY_RULE.findall(brief.read_text(encoding="utf-8"))
    assert len(rules) == 1, f"{brief.name}: expected one severity rule, got {rules}"
    return re.findall(r"`([A-Z]+)`", rules[0])


def _gate_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("p9_check_verdict", GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_severity_sets_match_the_contract_literals() -> None:
    assert _gate_module().SEVERITIES == GATE_SEVERITIES
    for name, brief in BRIEFS.items():
        assert _brief_severity_tags(brief) == BRIEF_SEVERITIES[name], brief.name


def test_blocking_set_is_the_owner_threshold() -> None:
    assert _gate_module().BLOCKING_SEVERITIES == EXPECTED_BLOCKING
    assert EXPECTED_BLOCKING < GATE_SEVERITIES


# Each brief and the P9 doc state the threshold in one sentence, pinned here
# against the same literal (F10: docs are tested against the code's contract).
_BLOCKING_RULE = re.compile(r"^\s*-?\s*Blocking severities: (.+?)\.", re.MULTILINE)


@pytest.mark.parametrize(
    "doc", [*BRIEFS.values(), AUTOMATION_DOC], ids=[*BRIEFS, "automation-doc"]
)
def test_docs_state_the_blocking_threshold(doc: Path) -> None:
    rules = _BLOCKING_RULE.findall(doc.read_text(encoding="utf-8"))
    assert len(rules) == 1, f"{doc.name}: expected one 'Blocking severities:' rule, got {rules}"
    assert frozenset(re.findall(r"`([A-Z]+)`", rules[0])) == EXPECTED_BLOCKING


def _finding_doc(**overrides: object) -> dict[str, object]:
    return {"verdict": "FAIL", "findings": [{**FINDING, **overrides}]}


@pytest.mark.parametrize(
    "doc",
    [
        pytest.param({**PASS_DOC, "approved": True}, id="extra-top-level-key"),
        pytest.param({"verdict": "FAIL", "findings": [{**FINDING, "fixed": True}]}, id="extra-finding-key"),
        pytest.param(
            {"verdict": "FAIL", "findings": [{**FINDING, "severity": "LOW", "fixed": True}]},
            id="extra-finding-key-non-blocking",
        ),
        pytest.param(
            {"verdict": "FAIL", "findings": [{k: v for k, v in FINDING.items() if k != "line"}]},
            id="missing-finding-key",
        ),
        pytest.param({"verdict": "FAIL", "findings": [{}]}, id="empty-finding"),
        pytest.param({"verdict": "PASS", "findings": [{**FINDING, "Severity": "HIGH"}]}, id="case-variant-key"),
    ],
)
def test_gate_rejects_inexact_key_sets(tmp_path: Path, doc: object) -> None:
    proc = _run_gate(_write(tmp_path, json.dumps(doc)))
    assert proc.returncode == EXIT_INVALID, proc.stderr
    assert "does not match the verdict contract" in proc.stderr
    assert proc.stdout == ""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # severity: only the briefs' tags, exact spelling.
        pytest.param("severity", "high", id="severity-lowercase"),
        pytest.param("severity", " HIGH", id="severity-padded"),
        pytest.param("severity", "HIGH\u200b", id="severity-zero-width"),
        pytest.param("severity", "INFO", id="severity-unknown"),
        pytest.param("severity", "", id="severity-empty"),
        pytest.param("severity", None, id="severity-null"),
        pytest.param("severity", 3, id="severity-int"),
        pytest.param("severity", ["HIGH"], id="severity-list"),
        *[pytest.param("severity", v, id=f"severity-hostile-{k}") for k, v in HOSTILE_TEXT.items()],
        # line: non-negative int; bool, float, string and null are off-contract.
        pytest.param("line", -1, id="line-negative"),
        pytest.param("line", "3", id="line-string"),
        pytest.param("line", True, id="line-bool-true"),
        pytest.param("line", False, id="line-bool-false"),
        pytest.param("line", 3.0, id="line-float"),
        pytest.param("line", None, id="line-null"),
        *[pytest.param("line", v, id=f"line-hostile-{k}") for k, v in HOSTILE_TEXT.items()],
        # title and file: non-blank strings; whitespace-only counts as blank.
        *[
            pytest.param(f, v, id=f"{f}-blank-{k}")
            for f in ("title", "file")
            for k, v in {"spaces": "   ", "tab": "\t", "newline": "\n"}.items()
        ],
        pytest.param("title", "", id="title-empty"),
        pytest.param("title", None, id="title-null"),
        pytest.param("title", 7, id="title-int"),
        pytest.param("title", ["x"], id="title-list"),
        pytest.param("file", "", id="file-empty"),
        pytest.param("file", None, id="file-null"),
        pytest.param("file", 7, id="file-int"),
        pytest.param("file", {"p": "x"}, id="file-object"),
    ],
)
@pytest.mark.parametrize("verdict", ["FAIL", "PASS"])
def test_gate_rejects_off_contract_finding_values(tmp_path: Path, field: str, value: object, verdict: str) -> None:
    doc = {"verdict": verdict, "findings": [{**FINDING, field: value}]}
    proc = _run_gate(_write(tmp_path, json.dumps(doc)))
    assert proc.returncode == EXIT_INVALID, proc.stderr
    assert "does not match the verdict contract" in proc.stderr
    assert proc.stdout == ""


@pytest.mark.parametrize("severity", sorted(GATE_SEVERITIES))
def test_gate_rejects_one_bad_finding_among_valid_ones(tmp_path: Path, severity: str) -> None:
    # A contract violation exits 2 at every severity: the non-blocking path
    # never swallows an off-contract file.
    valid = {**FINDING, "severity": severity}
    doc = {"verdict": "FAIL", "findings": [valid, {**valid, "line": -1}, valid]}
    proc = _run_gate(_write(tmp_path, json.dumps(doc)))
    assert proc.returncode == EXIT_INVALID, proc.stderr
    assert proc.stdout == ""


@pytest.mark.parametrize("verdict", ["FAIL", "PASS"])
@pytest.mark.parametrize("severity", sorted(GATE_SEVERITIES))
def test_gate_exit_follows_the_blocking_threshold(tmp_path: Path, severity: str, verdict: str) -> None:
    # Every tag a brief allows is a valid finding, never EXIT_INVALID (F3).
    # CRITICAL, HIGH and MEDIUM fail the job; LOW and NIT pass it and are
    # printed as non-blocking so they can be carded (owner decision 2026-10-09).
    doc = {"verdict": verdict, "findings": [{**FINDING, "severity": severity}]}
    proc = _run_gate(_write(tmp_path, json.dumps(doc)))
    if severity in EXPECTED_BLOCKING:
        assert proc.returncode == EXIT_REJECTED, proc.stdout
        assert proc.stdout == ""
        assert proc.stderr.splitlines() == [
            f"P9 verdict: {verdict}, 1 finding(s), 1 blocking",
            f"  - [{severity}] Swallowed error (app/x.py:3) blocking",
        ]
    else:
        assert proc.returncode == EXIT_PASS, proc.stderr
        assert proc.stderr == ""
        assert proc.stdout.splitlines() == [
            f"P9 verdict: {verdict}, 1 finding(s), 0 blocking",
            f"  - [{severity}] Swallowed error (app/x.py:3) non-blocking",
        ]


def test_gate_blocks_when_one_finding_of_many_is_blocking(tmp_path: Path) -> None:
    findings = [
        {**FINDING, "severity": "LOW", "title": "Low one"},
        {**FINDING, "severity": "MEDIUM", "title": "Medium one"},
        {**FINDING, "severity": "NIT", "title": "Nit one"},
    ]
    proc = _run_gate(_write(tmp_path, json.dumps({"verdict": "FAIL", "findings": findings})))
    assert proc.returncode == EXIT_REJECTED, proc.stdout
    assert proc.stdout == ""
    assert proc.stderr.splitlines() == [
        "P9 verdict: FAIL, 3 finding(s), 1 blocking",
        "  - [LOW] Low one (app/x.py:3) non-blocking",
        "  - [MEDIUM] Medium one (app/x.py:3) blocking",
        "  - [NIT] Nit one (app/x.py:3) non-blocking",
    ]


def test_gate_passes_when_every_finding_is_non_blocking(tmp_path: Path) -> None:
    findings = [{**FINDING, "severity": "LOW"}, {**FINDING, "severity": "NIT", "line": 0}]
    proc = _run_gate(_write(tmp_path, json.dumps({"verdict": "FAIL", "findings": findings})))
    assert proc.returncode == EXIT_PASS, proc.stderr
    assert proc.stderr == ""
    assert proc.stdout.splitlines() == [
        "P9 verdict: FAIL, 2 finding(s), 0 blocking",
        "  - [LOW] Swallowed error (app/x.py:3) non-blocking",
        "  - [NIT] Swallowed error (app/x.py:0) non-blocking",
    ]


@pytest.mark.parametrize("line", [0, 1, 2**31], ids=["zero-no-line", "one", "large"])
def test_gate_accepts_non_negative_int_lines(tmp_path: Path, line: int) -> None:
    proc = _run_gate(_write(tmp_path, json.dumps(_finding_doc(line=line))))
    assert proc.returncode == EXIT_REJECTED, proc.stderr
    assert f"(app/x.py:{line})" in proc.stderr


@pytest.mark.parametrize("args", [(), ("a.json", "b.json")], ids=["no-args", "two-args"])
def test_gate_rejects_wrong_usage(tmp_path: Path, args: tuple[str, ...]) -> None:
    proc = subprocess.run(
        [sys.executable, "-I", str(GATE), *args],
        text=True,
        capture_output=True,
        cwd=tmp_path,
        timeout=30,
    )
    assert proc.returncode == EXIT_INVALID
    assert "usage: check_verdict.py <p9-verdict.json>" in proc.stderr


# Hostile text goes only in the free-text fields: severity and line are now
# validated (see test_gate_rejects_off_contract_finding_values), so a hostile
# value there is a contract mismatch, not a printed finding.
# Both output paths are checked: a blocking finding prints to stderr (exit 1),
# a non-blocking one to stdout (exit 0).
@pytest.mark.parametrize(
    ("severity", "expected"),
    [pytest.param("HIGH", EXIT_REJECTED, id="blocking"), pytest.param("LOW", EXIT_PASS, id="non-blocking")],
)
@pytest.mark.parametrize("field", ["title", "file"])
@pytest.mark.parametrize("payload", list(HOSTILE_TEXT.values()), ids=list(HOSTILE_TEXT))
def test_gate_prints_findings_on_one_sanitised_line(
    tmp_path: Path, field: str, payload: str, severity: str, expected: int
) -> None:
    finding = {**FINDING, "severity": severity, field: payload}
    proc = _run_gate(_write(tmp_path, json.dumps({"verdict": "FAIL", "findings": [finding]})))
    assert proc.returncode == expected
    shown, silent = (proc.stderr, proc.stdout) if expected == EXIT_REJECTED else (proc.stdout, proc.stderr)
    lines = shown.splitlines()
    assert len(lines) == 2, lines  # header + exactly one line per finding
    assert all(line.isprintable() for line in lines), lines
    assert not any(line.startswith(("::", "P9 verdict: PASS")) for line in lines), lines
    assert silent == ""


def test_gate_truncates_long_finding_text(tmp_path: Path) -> None:
    finding = {**FINDING, "title": "A" * 2_000_000}
    proc = _run_gate(_write(tmp_path, json.dumps({"verdict": "FAIL", "findings": [finding]})))
    assert proc.returncode == EXIT_REJECTED
    assert max(len(line) for line in proc.stderr.splitlines()) < 1_000


# --- workflow shape ----------------------------------------------------------


def test_workflow_has_exactly_the_two_review_jobs() -> None:
    assert set(_jobs()) == set(BRIEFS)
    for name, job in _jobs().items():
        assert job.get("name", name) == name, "the job name is the required-check name"
        assert job["runs-on"] == "ubuntu-latest"


def test_trigger_is_pull_request_to_main_only() -> None:
    triggers = _triggers(_workflow())
    assert set(triggers) == {"pull_request"}, triggers
    pr = triggers["pull_request"]
    assert pr["branches"] == ["main"]
    assert set(pr["types"]) == {"opened", "synchronize", "reopened", "ready_for_review"}
    assert "pull_request_target" not in WORKFLOW.read_text(encoding="utf-8")


def test_every_action_is_pinned_to_a_full_sha_with_its_tag() -> None:
    uses_lines = [
        line.strip()
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if re.match(r"\s*(-\s+)?uses:", line)
    ]
    assert len(uses_lines) == 2 * len(BRIEFS), uses_lines  # checkout + action per job
    seen = set()
    for line in uses_lines:
        match = re.fullmatch(r"(?:-\s+)?uses:\s+([\w.-]+/[\w.-]+)@([0-9a-f]{40})\s+#\s+v\d+(\.\d+)*", line)
        assert match, f"not SHA-pinned with a trailing tag comment: {line!r}"
        seen.add(match.group(1))
    assert seen == ACTION_REPOS


def test_permissions_are_least_privilege() -> None:
    doc = _workflow()
    assert doc["permissions"] == {}, "workflow default must grant nothing"
    for name, job in _jobs().items():
        assert job["permissions"] == JOB_PERMISSIONS, name


def test_secret_is_referenced_only_as_the_action_api_key() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    # No workflow- or job-level env: the key must reach only the action step.
    assert "env" not in _workflow()
    for name, job in _jobs().items():
        assert "env" not in job, name
    assert set(re.findall(r"secrets\.[A-Za-z0-9_]+", text)) == {"secrets.ANTHROPIC_API_KEY"}
    assert text.count("secrets.ANTHROPIC_API_KEY") == len(BRIEFS)
    for name, job in _jobs().items():
        assert _action_step(job)["with"]["anthropic_api_key"] == "${{ secrets.ANTHROPIC_API_KEY }}"
        for step in job["steps"]:
            assert "secrets." not in json.dumps(step.get("env", {})), name


def test_runs_are_bounded_and_superseded_runs_cancel() -> None:
    doc = _workflow()
    assert doc["concurrency"]["cancel-in-progress"] is True
    assert "github.event.pull_request.number" in doc["concurrency"]["group"]
    for name, job in _jobs().items():
        minutes = job["timeout-minutes"]
        assert isinstance(minutes, int) and 0 < minutes <= 30, name
        args = _action_step(job)["with"]["claude_args"]
        turns = re.search(r"--max-turns\s+(\d+)", args)
        assert turns and 0 < int(turns.group(1)) <= 50, args


def test_claude_gets_only_the_tools_it_needs() -> None:
    for name, job in _jobs().items():
        args = _action_step(job)["with"]["claude_args"]
        assert _tool_list(args, "allowedTools") == ALLOWED_TOOLS, name
        assert _tool_list(args, "disallowedTools") == DISALLOWED_TOOLS, name


def test_reads_are_fenced_and_pr_settings_are_ignored() -> None:
    for name, job in _jobs().items():
        step = _action_step(job)["with"]
        args = step["claude_args"]
        # Only user settings (the action's `settings` input) load, so a
        # .claude/settings.json added by the PR cannot widen the grants.
        assert re.findall(r"--setting-sources\s+(\S+)", args) == ["user"], name
        assert re.findall(r"--add-dir\s+([^\n]+)", args) == [DIFF_DIR], name
        permissions = json.loads(step["settings"])["permissions"]
        assert permissions["blockReadsOutsideWorkingDirectories"] is True, name
        assert set(permissions["deny"]) == READ_DENY, name
        assert "allow" not in permissions, name


def test_every_read_deny_path_renders_with_a_meaningful_prefix() -> None:
    # runner.temp is an absolute path on the runner, so render it as one.
    for rule in READ_DENY:
        assert rule.startswith("Read(") and rule.endswith(")"), rule
        rendered = rule[len("Read(") : -1].replace(RUNNER_TEMP_EXPR, "/home/runner/work/_temp")
        assert rendered.startswith(READ_PATH_PREFIXES), rule
        assert not rendered.startswith("///"), rule


def test_automation_doc_lists_exactly_the_read_deny_rules() -> None:
    text = AUTOMATION_DOC.read_text(encoding="utf-8")
    section = text.split("- **Read deny rules:**", 1)[1].split("\n- **", 1)[0]
    assert set(re.findall(r"`(Read\([^`]*\))`", section)) == READ_DENY


def test_checkout_does_not_persist_the_job_token() -> None:
    for name, job in _jobs().items():
        checkouts = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")]
        assert len(checkouts) == 1, name
        assert checkouts[0]["with"]["persist-credentials"] is False, name


def test_diff_prep_step_runs_before_the_reviewer() -> None:
    for name, job in _jobs().items():
        steps = job["steps"]
        assert steps[0]["with"]["fetch-depth"] == 0, name
        prep = [s for s in steps if s.get("name") == "Prepare PR diff"]
        assert len(prep) == 1, name
        assert steps.index(prep[0]) < steps.index(_action_step(job)), name
        # The base branch arrives through env, never inline in the script.
        assert prep[0]["env"] == {"BASE_REF": BASE_REF_EXPR, "HEAD_SHA": HEAD_SHA_EXPR}, name
        assert "${{" not in prep[0]["run"], name


def test_each_job_prompts_with_its_vendored_brief() -> None:
    for name, job in _jobs().items():
        brief = BRIEFS[name]
        prompt = _action_step(job)["with"]["prompt"]
        assert brief.relative_to(REPO_ROOT).as_posix() in prompt, name
        assert f"First read {DIFF_DIR}/changed-files.txt and\n{DIFF_DIR}/pr.diff" in prompt, name
        assert f"Review the net diff. {DIFF_DIR}/commits.txt lists\nthe PR's commits for context only." in prompt
        text = brief.read_text(encoding="utf-8")
        assert COMMENT_TOOL in text and "p9-verdict.json" in text, brief.name
        assert "gh pr" not in text, brief.name
        # The brief states the same CI tool set as the workflow allowlist.
        tools = text.split("## Tools in CI", 1)[1].split("\n## ", 1)[0]
        assert "`Read`, `Grep`, `Glob`, `Write` limited to `p9-verdict.json`" in tools, brief.name
        assert COMMENT_TOOL in tools and "No Bash, no web, no other MCP." in tools, brief.name
        assert "## Accepted / tracked items: do not re-report" in text, brief.name
        assert all(card in text for card in ("#216", "#215", "rebase")), brief.name
        assert "/Users/" not in text and "/home/" not in text, brief.name
        # The review unit is the PR's net diff; no per-commit asks (#214).
        scope = text.split("## Scope", 1)[1].split("\n## ", 1)[0]
        assert "Review the net\n  diff (base to head)" in scope, brief.name
        assert "`commits.txt` there lists the PR's commits for context only." in tools, brief.name
        lens = text.split("\n## Accepted / tracked items", 1)[0]
        for ask in ("single-purpose", "git bisect", "commit message"):
            assert ask not in lens, (brief.name, ask)


# --- workflow steps, run for real with a fake reviewer -----------------------


def _shell_steps(job: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return the `run:` steps before and after the action step."""
    before: list[dict[str, Any]] = []
    after: list[dict[str, Any]] = []
    seen_action = False
    for step in job["steps"]:
        if step is _action_step(job):
            seen_action = True
        elif "run" in step:
            (after if seen_action else before).append(step)
    assert seen_action and before and after, "expected run steps around the action"
    return before, after


Reviewer = Callable[[Path], object]  # stands in for the review action
_needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is required for this test")
GIT_TIMEOUT = 30


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
    ).stdout.strip()


def _step_env(step: dict[str, Any], head_sha: str) -> dict[str, str]:
    """The step's env, with the base branch resolved to main and the PR head sha."""
    values = {BASE_REF_EXPR: "main", HEAD_SHA_EXPR: head_sha}
    resolved: dict[str, str] = {}
    for key, value in step.get("env", {}).items():
        assert value in values, f"unexpected env expression {key}={value!r}"
        resolved[key] = values[value]
    return resolved


def _run_job(
    tmp_path: Path, job: dict[str, Any], reviewer: Reviewer, committed: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the job's shell steps in a workspace shaped like a pull_request run.

    The PR commit branches from `base`; main then moves on (`main-later`), and
    HEAD is a real merge of the PR into main, like GitHub's synthetic merge
    checkout. So `..HEAD` would list the merge commit and `...` would list
    `main-later`; only `origin/main..<PR head>` lists just the PR's commit.
    """
    workspace = tmp_path / "ws"
    runner_temp = tmp_path / "runner-temp"
    (workspace / ".github" / "p9").mkdir(parents=True)
    runner_temp.mkdir()
    shutil.copy2(GATE, workspace / ".github" / "p9" / "check_verdict.py")
    _git(workspace, "init", "-q")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "base")
    base_sha = _git(workspace, "rev-parse", "HEAD")
    for rel, content in {"app/changed.py": "x = 1\n", **(committed or {})}.items():
        (workspace / rel).parent.mkdir(parents=True, exist_ok=True)
        (workspace / rel).write_text(content, encoding="utf-8")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "pr")
    head_sha = _git(workspace, "rev-parse", "HEAD")
    _git(workspace, "checkout", "-q", "--detach", base_sha)
    (workspace / "app").mkdir(exist_ok=True)
    (workspace / "app" / "main_only.py").write_text("y = 2\n", encoding="utf-8")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "main-later")
    _git(workspace, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(workspace, "merge", "-q", "--no-ff", "-m", "synthetic merge", head_sha)
    assert _git(workspace, "rev-list", "--parents", "-n", "1", "HEAD").count(" ") == 2, "HEAD must be a merge"
    env = {"PATH": os.environ["PATH"], "RUNNER_TEMP": str(runner_temp), "HOME": str(tmp_path)}
    before, after = _shell_steps(job)
    for step in before:
        subprocess.run(
            ["bash", "-e", "-c", step["run"]],
            cwd=workspace,
            env={**env, **_step_env(step, head_sha)},
            check=True,
            timeout=30,
        )
    reviewer(workspace)  # stands in for anthropics/claude-code-action
    result = None
    for step in after:
        result = subprocess.run(
            ["bash", "-e", "-c", step["run"]],
            cwd=workspace,
            env={**env, **_step_env(step, head_sha)},
            text=True,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            break
    assert result is not None
    return result


def _writes(doc: object) -> Reviewer:
    return lambda ws: (ws / "p9-verdict.json").write_text(json.dumps(doc), encoding="utf-8")


@_needs_git
@pytest.mark.parametrize("name", sorted(BRIEFS))
@pytest.mark.parametrize(
    ("reviewer", "expected", "message"),
    [
        pytest.param(_writes(PASS_DOC), EXIT_PASS, "P9 verdict: PASS, 0 findings", id="pass"),
        pytest.param(
            _writes({"verdict": "FAIL", "findings": [FINDING]}), EXIT_REJECTED, "P9 verdict: FAIL", id="fail"
        ),
        pytest.param(
            _writes({"verdict": "FAIL", "findings": [{**FINDING, "severity": "LOW"}]}),
            EXIT_PASS,
            "  - [LOW] Swallowed error (app/x.py:3) non-blocking",
            id="fail-non-blocking-only",
        ),
        pytest.param(lambda ws: None, EXIT_INVALID, "was not written", id="reviewer-wrote-nothing"),
    ],
)
def test_job_steps_gate_on_the_reviewer_verdict(
    tmp_path: Path, name: str, reviewer: Reviewer, expected: int, message: str
) -> None:
    proc = _run_job(tmp_path, _jobs()[name], reviewer)
    assert proc.returncode == expected, proc.stderr
    assert message in proc.stdout + proc.stderr


@_needs_git
@pytest.mark.parametrize("name", sorted(BRIEFS))
def test_prepare_step_writes_the_pr_diff_for_the_reviewer(tmp_path: Path, name: str) -> None:
    seen: list[str] = []

    def read_inputs(ws: Path) -> None:
        for rel in ("pr.diff", "changed-files.txt", "commits.txt"):
            seen.append((tmp_path / "runner-temp" / "p9" / rel).read_text(encoding="utf-8"))
        _writes(PASS_DOC)(ws)

    proc = _run_job(tmp_path, _jobs()[name], read_inputs)
    assert proc.returncode == EXIT_PASS, proc.stderr
    diff, changed, commits = seen
    assert "+++ b/app/changed.py" in diff
    assert ".github/p9/check_verdict.py" not in diff, "the base commit must not be in the diff"
    assert changed == "app/changed.py\n"
    assert "main_only.py" not in diff, "main's later commits must not be in the diff"
    # Only the PR's own commit, one --oneline row: "<sha> pr". Neither the
    # synthetic merge commit nor main's later commit may appear.
    assert re.fullmatch(r"[0-9a-f]{7,40} pr\n", commits), commits


@_needs_git
@pytest.mark.parametrize("name", sorted(BRIEFS))
def test_a_verdict_committed_in_the_pr_cannot_pass_the_job(tmp_path: Path, name: str) -> None:
    proc = _run_job(tmp_path, _jobs()[name], lambda ws: None, committed={"p9-verdict.json": json.dumps(PASS_DOC)})
    assert proc.returncode == EXIT_INVALID, proc.stderr


@_needs_git
@pytest.mark.parametrize("name", sorted(BRIEFS))
def test_reviewer_cannot_swap_the_gate_script(tmp_path: Path, name: str) -> None:
    def tamper(ws: Path) -> None:
        (ws / ".github" / "p9" / "check_verdict.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        _writes({"verdict": "FAIL", "findings": [FINDING]})(ws)

    proc = _run_job(tmp_path, _jobs()[name], tamper)
    assert proc.returncode == EXIT_REJECTED, proc.stderr


# --- retirement of the local pre-push gate ------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        ".githooks/pre-push",
        ".githooks/pre-push.sha256",
        ".github/workflows/enforce-p9-review.yml",
        "scripts/ci/p9-sibling-parity.sh",
        "tests/unit/p9_gate_harness.sh",
        "tests/unit/test_p9_prepush_gate.py",
    ],
)
def test_retired_p9_gate_files_are_gone(rel: str) -> None:
    assert not (REPO_ROOT / rel).exists(), f"{rel} belongs to the retired local P9 gate"


@_needs_git
def test_pre_commit_hook_and_installer_are_tracked_executable() -> None:
    out = subprocess.run(
        ["git", "ls-files", "-s", ".githooks/pre-commit", "scripts/install-hooks.sh"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=GIT_TIMEOUT,
    ).stdout.splitlines()
    assert len(out) == 2 and all(line.startswith("100755 ") for line in out), out


@_needs_git
def test_installer_wires_pre_commit_and_no_longer_creates_reviews(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(HOME=str(home), GIT_CONFIG_GLOBAL=str(home / ".gitconfig"), GIT_CONFIG_NOSYSTEM="1")
    repo = tmp_path / "repo"
    (repo / ".githooks").mkdir(parents=True)
    (repo / "scripts").mkdir()
    shutil.copy2(REPO_ROOT / ".githooks" / "pre-commit", repo / ".githooks" / "pre-commit")
    shutil.copy2(INSTALLER, repo / "scripts" / "install-hooks.sh")
    (repo / ".githooks" / "pre-commit").chmod(0o644)
    subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True, timeout=GIT_TIMEOUT)

    proc = subprocess.run(
        ["bash", "scripts/install-hooks.sh"], cwd=repo, env=env, text=True, capture_output=True, timeout=GIT_TIMEOUT
    )

    assert proc.returncode == 0, proc.stderr
    hooks_path = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=GIT_TIMEOUT,
    ).stdout.strip()
    assert hooks_path == ".githooks"
    assert os.access(repo / ".githooks" / "pre-commit", os.X_OK)
    assert not (repo / ".git" / "reviews").exists()
