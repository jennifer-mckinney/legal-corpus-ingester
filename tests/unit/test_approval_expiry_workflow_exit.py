"""The approval-expiry job must go red when check_approvals.py exits non-zero (terms-analysis#173).

Behavioural: the real `Run approval check` step script is extracted from
.github/workflows/approval-expiry.yml and executed with the shell GitHub would
use for it, in a sandbox workspace. A `|| true`, a status-swallowing pipe or a
missing pipefail turns these red.

#13: the expiry issue's text must depend on the checker's exit code. The check
step and the issue step are run back to back the way Actions runs them (the h4
pattern from test_vcr_canary_contract.py), with a fake `gh` recording the issue
it would open.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.unit.test_vcr_canary_contract import _EXPR_RE, _parse_github_output, _resolve_env

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "approval-expiry.yml"
_JOB = "approval-expiry"
_STEP = "Run approval check"
# GitHub's shell for a `run:` step with no `shell:` anywhere on a Linux runner.
_GITHUB_DEFAULT_SHELL = "bash -e {0}"
_FIXTURE_SOURCE = _REPO_ROOT / "tests" / "fixtures" / "sources" / "eurlex.yaml"
_FIXTURE_EXPIRED = _REPO_ROOT / "tests" / "fixtures" / "approvals" / "expired.yaml"

sys.path.insert(0, str(_REPO_ROOT / "scripts"))
# The config exit code comes from the checker itself, never restated here (F13).
from check_approvals import EXIT_CONFIG as _EXIT_CONFIG  # noqa: E402


def _load_step(name: str = _STEP) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    """Return (run script, effective shell, job, step) for the named step of the job."""
    try:
        doc = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        # GitHub rejects an unparseable workflow at load time, so the job never runs at all.
        pytest.fail(f"{_WORKFLOW.name} does not parse, so the scheduled job never runs: {exc}")
    job = doc["jobs"][_JOB]
    matches = [s for s in job["steps"] if s.get("name") == name]
    assert len(matches) == 1, f"expected exactly one step named {name!r}"
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
    # A wrapper that execs sys.executable by its own path, not a symlink to it: Python locates
    # pyvenv.cfg next to the path it was invoked as, so a symlink from elsewhere silently drops
    # the venv and runs the base interpreter (no pyyaml on a GitHub-hosted runner; the laptop's
    # Homebrew python masked this, #19).
    bindir = tmp_path / "bin"
    bindir.mkdir()
    wrapper = bindir / "python"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    # The step may record its result for later steps (#13); give it the per-step files
    # Actions provides, never the CI runner's own GITHUB_OUTPUT/GITHUB_ENV of this test job.
    for var in ("GITHUB_OUTPUT", "GITHUB_ENV"):
        (tmp_path / var.lower()).touch()
        env[var] = str(tmp_path / var.lower())
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


# ---------------------------------------------------------------------------
# #13: the expiry issue's text depends on the checker's exit code.
#
# The real `Run approval check` and `Open issue on expiry` steps run back to
# back as Actions runs them: each under the workflow's effective shell, the
# check step may write GITHUB_OUTPUT / GITHUB_ENV, the issue step's `env:` is
# resolved from what the check step really wrote (a missing step id or output
# is the empty string), and the issue step's `if:` is evaluated from the check
# step's real outcome. A fake `gh` first on PATH records each call and the
# title/body it would post; nothing reaches a network.
# ---------------------------------------------------------------------------

_ISSUE_STEP = "Open issue on expiry"
# Status functions Actions evaluates in `if:`; job_failed -> value.
_STATUS_FUNCS = {
    "success()": lambda failed: not failed,
    "failure()": lambda failed: failed,
    "always()": lambda failed: True,
}
_OUTCOME_TERM = re.compile(r"steps\.([\w-]+)\.(outcome|conclusion)\s*(==|!=)\s*'([^']*)'")
# The catch-all the card forbids on every path, in any of its phrasings
# ("expired or invalid", "have expired or are invalid", "is expired or is invalid").
_CATCH_ALL = re.compile(r"\bexpired\s+or\s+(?:are\s+|is\s+)?invalid\b", re.I)
# GitHub rejects an issue body longer than this many characters.
_GITHUB_ISSUE_BODY_MAX = 65536
# Ambient variables a broken issue step might read instead of the check step's
# real result; each claims the "expired" code (indirection, attack list A).
_AMBIENT_RC_NAMES = ("rc", "RC", "CHECK_RC", "EXIT_CODE", "APPROVAL_RC", "APPROVAL_CHECK_RC", "CHECKER_RC")

_FAKE_GH = '''#!{exe}
import json, os, sys
argv = sys.argv[1:]
rec = {{"argv": argv}}


def opt(names):
    for i, a in enumerate(argv):
        for n in names:
            if a == n and i + 1 < len(argv):
                return argv[i + 1]
            if a.startswith(n + "="):
                return a[len(n) + 1:]
    return None


if argv[:2] == ["issue", "create"]:
    rec["title"] = opt(["--title", "-t"])
    rec["body"] = opt(["--body", "-b"])
    body_file = opt(["--body-file", "-F"])
    if body_file is not None:
        raw = sys.stdin.buffer.read() if body_file == "-" else open(body_file, "rb").read()
        rec["body"] = raw.decode("utf-8", "surrogateescape")
with open(os.environ["GH_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(rec) + "\\n")
if argv[:2] == ["issue", "list"]:
    print(os.environ.get("FAKE_GH_OPEN_COUNT", "0"))
'''


def _effective_shell_argv(doc: dict[str, Any], job: dict[str, Any], step: dict[str, Any], script: Path) -> list[str]:
    shell = (
        step.get("shell")
        or ((job.get("defaults") or {}).get("run") or {}).get("shell")
        or ((doc.get("defaults") or {}).get("run") or {}).get("shell")
        or _GITHUB_DEFAULT_SHELL
    )
    return [part.replace("{0}", str(script)) for part in shlex.split(str(shell))]


def _step_runs(step: dict[str, Any], job_failed: bool, outcomes: dict[str, str]) -> bool:
    """Evaluate an `if:` made of `&&`-joined status functions and
    `steps.<id>.outcome|conclusion ==|!= '<v>'` terms. Anything else is not
    modelled and fails loudly rather than guessing."""
    cond = _EXPR_RE.sub(lambda m: m.group(1), str(step.get("if", "success()"))).strip()
    terms = [t.strip() for t in cond.split("&&")]
    if not any(t in _STATUS_FUNCS for t in terms):
        terms.append("success()")  # Actions adds an implicit success() to any `if:` without one.
    for term in terms:
        if term in _STATUS_FUNCS:
            value = _STATUS_FUNCS[term](job_failed)
        else:
            m = _OUTCOME_TERM.fullmatch(term)
            if not m:
                pytest.fail(f"harness does not model `if: {cond}`")
            actual = outcomes.get(m.group(1), "")
            value = (actual == m.group(4)) if m.group(3) == "==" else (actual != m.group(4))
        if not value:
            return False
    return True


def _simulate_check_then_issue(
    ws: Path, tmp_path: Path, extra_env: dict[str, str] | None = None
) -> tuple[int, subprocess.CompletedProcess[str] | None, list[dict[str, Any]]]:
    """Run the check step, then the issue step if its `if:` allows.

    Returns (check step exit, issue step result or None if skipped, gh calls)."""
    doc = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    job = doc["jobs"][_JOB]
    _, _, _, check = _load_step(_STEP)
    _, _, _, issue = _load_step(_ISSUE_STEP)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(_FAKE_GH.format(exe=sys.executable), encoding="utf-8")
    (bindir / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    for p in bindir.iterdir():
        p.chmod(0o755)
    gh_log = tmp_path / "gh.log"
    gh_output = tmp_path / "github_output"
    gh_env = tmp_path / "github_env"
    gh_output.touch()
    gh_env.touch()
    # A clean env: nothing GitHub-ish leaks in from the developer's shell.
    keep = {k: v for k, v in os.environ.items() if k in ("HOME", "LANG", "LC_ALL", "TMPDIR")}
    base = {
        **keep,
        **(extra_env or {}),
        "PATH": f"{bindir}:/usr/bin:/bin",
        "GH_LOG": str(gh_log),
        "GITHUB_OUTPUT": str(gh_output),
        "GITHUB_ENV": str(gh_env),
    }
    inherited = {**_resolve_env(doc.get("env"), {}), **_resolve_env(job.get("env"), {})}

    check_sh = tmp_path / "check.sh"
    check_sh.write_text(str(check["run"]), encoding="utf-8")
    check_proc = subprocess.run(
        _effective_shell_argv(doc, job, check, check_sh),
        cwd=ws, env={**base, **inherited, **_resolve_env(check.get("env"), {})},
        capture_output=True, text=True, errors="backslashreplace", timeout=60, check=False,
    )
    step_outputs: dict[str, dict[str, str]] = {}
    outcomes: dict[str, str] = {}
    if check.get("id"):
        step_outputs[str(check["id"])] = _parse_github_output(gh_output)
        outcomes[str(check["id"])] = "success" if check_proc.returncode == 0 else "failure"
    job_failed = check_proc.returncode != 0

    issue_proc = None
    if _step_runs(issue, job_failed, outcomes):
        issue_sh = tmp_path / "issue.sh"
        issue_sh.write_text(str(issue["run"]), encoding="utf-8")
        issue_proc = subprocess.run(
            _effective_shell_argv(doc, job, issue, issue_sh),
            cwd=ws,
            env={**base, **_parse_github_output(gh_env), **inherited, **_resolve_env(issue.get("env"), step_outputs)},
            capture_output=True, text=True, errors="backslashreplace", timeout=60, check=False,
        )
    calls = [json.loads(ln) for ln in gh_log.read_text(encoding="utf-8").splitlines()] if gh_log.exists() else []
    return check_proc.returncode, issue_proc, calls


def _created(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in calls if c["argv"][:2] == ["issue", "create"]]


def _only_body(calls: list[dict[str, Any]], issue_proc: subprocess.CompletedProcess[str] | None) -> str:
    """The body of the single issue the run opened; fails if not exactly one."""
    assert issue_proc is not None, "the issue step was skipped on a failed check"
    assert issue_proc.returncode == 0, f"issue step failed: stderr={issue_proc.stderr!r}"
    creates = _created(calls)
    assert len(creates) == 1, f"expected one issue, gh calls: {calls}"
    body = creates[0].get("body")
    assert isinstance(body, str), f"issue created without a body: {creates[0]}"
    return body


def _ws_real_checker(tmp_path: Path, approval: str | None) -> Path:
    """Workspace with the real checker; `approval` is APPROVAL.yaml text or None (empty dir)."""
    ws = _workspace(tmp_path)
    shutil.copytree(_REPO_ROOT / "scripts", ws / "scripts")
    if approval is not None:
        (ws / "config" / "approvals" / "approval.yaml").write_text(approval, encoding="utf-8")
    return ws


def _ws_fake_checker(tmp_path: Path, rc: int, noise: bytes = b"") -> Path:
    """Workspace whose checker writes `noise` to stdout and stderr, then exits `rc`."""
    ws = _workspace(tmp_path)
    (ws / "scripts").mkdir()
    noise_file = tmp_path / "noise.bin"
    noise_file.write_bytes(noise)
    (ws / "scripts" / "check_approvals.py").write_text(
        "import sys\n"
        f"data = open({str(noise_file)!r}, 'rb').read()\n"
        "sys.stdout.buffer.write(data); sys.stdout.flush()\n"
        "sys.stderr.buffer.write(data); sys.stderr.flush()\n"
        f"sys.exit({rc})\n",
        encoding="utf-8",
    )
    return ws


def _expired_approval() -> str:
    return _FIXTURE_EXPIRED.read_text(encoding="utf-8")


def _current_approval() -> str:
    # Far enough out that no --warn-days default turns it into a warning.
    future = (date.today() + timedelta(days=3650)).isoformat()
    text = re.sub(r'(?m)^expiry:.*$', f'expiry: "{future}"', _expired_approval())
    assert future in text
    return text


def _real_rc(tmp_path: Path, approval: str | None) -> int:
    ws = _ws_real_checker(tmp_path, approval)
    return subprocess.run(
        [sys.executable, "scripts/check_approvals.py", "--approvals-dir", "config/approvals"],
        cwd=ws, capture_output=True, text=True, timeout=60, check=False,
    ).returncode


@pytest.fixture(scope="module")
def expired_rc(tmp_path_factory: pytest.TempPathFactory) -> int:
    """The checker's exit code for an expired approval, read from its behaviour (F13)."""
    rc = _real_rc(tmp_path_factory.mktemp("expired_rc"), _expired_approval())
    assert rc not in (0, _EXIT_CONFIG), f"expired fixture no longer yields a distinct failure code: {rc}"
    return rc


def test_expiry_issue_body_says_expired_when_an_approval_expired(tmp_path: Path, expired_rc: int) -> None:
    """#13: the real checker on an expired APPROVAL.yaml -> the body names the expiry,
    and never the catch-all 'expired or invalid'."""
    ws = _ws_real_checker(tmp_path, _expired_approval())
    rc, issue_proc, calls = _simulate_check_then_issue(ws, tmp_path)
    assert rc == expired_rc
    body = _only_body(calls, issue_proc)
    assert re.search(r"\bexpired\b", body, re.I), f"exit {rc} body does not say expired: {body!r}"
    assert not _CATCH_ALL.search(body), f"exit {rc} body is the catch-all: {body!r}"


_CONFIG_EXIT_CASES = {
    # case: (workspace builder, ambient env)
    "real_checker_empty_approvals_dir": (lambda tp: _ws_real_checker(tp, None), {}),
    "real_checker_missing_approvals_dir": (
        lambda tp: (lambda ws: (ws / "config" / "approvals").rmdir() or ws)(_ws_real_checker(tp, None)),
        {},
    ),
    # Indirection: ambient variables claim the "expired" code; only the check
    # step's own result may decide the text.
    "ambient_env_claims_expired": (
        lambda tp: _ws_fake_checker(tp, _EXIT_CONFIG),
        {name: "1" for name in _AMBIENT_RC_NAMES},
    ),
}


@pytest.mark.parametrize("case", list(_CONFIG_EXIT_CASES))
def test_expiry_issue_body_says_invalid_input_on_config_exit(tmp_path: Path, case: str) -> None:
    """#13: exit EXIT_CONFIG (invalid input: approvals dir missing or empty) -> the body
    says invalid and never claims an approval expired."""
    build, ambient = _CONFIG_EXIT_CASES[case]
    ws = build(tmp_path)
    rc, issue_proc, calls = _simulate_check_then_issue(ws, tmp_path, extra_env=ambient)
    assert rc == _EXIT_CONFIG
    body = _only_body(calls, issue_proc)
    assert re.search(r"\binvalid\b", body, re.I), f"{case}: exit {rc} body does not say invalid: {body!r}"
    assert not re.search(r"\bexpired\b", body, re.I), f"{case}: exit {rc} body claims expiry: {body!r}"
    assert not _CATCH_ALL.search(body), f"{case}: exit {rc} body is the catch-all: {body!r}"


def test_expiry_issue_body_depends_on_exit_code(tmp_path: Path, expired_rc: int) -> None:
    """#13 acceptance 'Body text depends on the exit code': same step, two codes, two texts."""
    bodies = {}
    for rc in (expired_rc, _EXIT_CONFIG):
        case_dir = tmp_path / f"rc{rc}"
        case_dir.mkdir()
        got, issue_proc, calls = _simulate_check_then_issue(_ws_fake_checker(case_dir, rc), case_dir)
        assert got == rc
        bodies[rc] = _only_body(calls, issue_proc)
    assert bodies[expired_rc] != bodies[_EXIT_CONFIG], f"one body for both codes: {bodies[expired_rc]!r}"


@pytest.mark.parametrize("source", ["real_checker_current_approval", "fake_checker_exit_0"])
def test_clean_check_opens_no_issue(tmp_path: Path, source: str) -> None:
    """Boundary: exit 0 -> the issue step is skipped and gh is never called."""
    if source == "real_checker_current_approval":
        ws = _ws_real_checker(tmp_path, _current_approval())
    else:
        ws = _ws_fake_checker(tmp_path, 0)
    rc, issue_proc, calls = _simulate_check_then_issue(ws, tmp_path)
    assert rc == 0
    assert issue_proc is None, "the issue step ran on a clean check"
    assert calls == [], f"a clean run touched gh: {calls}"


# Exit codes the checker does not define: argparse/usage-style, command not
# found, killed by SIGKILL (OOM), and the top of the range.
_UNDEFINED_EXIT_CODES = (3, 127, 137, 255)


@pytest.mark.parametrize("code", _UNDEFINED_EXIT_CODES)
def test_undefined_exit_code_is_not_reported_as_expired(tmp_path: Path, code: int, expired_rc: int) -> None:
    """Boundary: any other non-zero exit is never filed as an expiry, and is never
    silent: either the issue step fails loudly, or the issue it opens names the code."""
    assert code not in (0, expired_rc, _EXIT_CONFIG), f"vector {code} collides with a defined exit code"
    rc, issue_proc, calls = _simulate_check_then_issue(_ws_fake_checker(tmp_path, code), tmp_path)
    assert rc == code
    assert issue_proc is not None, f"exit {code}: the issue step was skipped"
    bodies = [str(c.get("body") or "") for c in _created(calls)]
    for body in bodies:
        assert not re.search(r"\bexpired\b", body, re.I), f"exit {code} reported as expired: {body!r}"
    loud = issue_proc.returncode != 0 or any(re.search(rf"\b{code}\b", b) for b in bodies)
    assert loud, f"exit {code} handled silently: rc={issue_proc.returncode} gh calls={calls}"


def _hostile_checker_output() -> tuple[bytes, int]:
    """Generated, not listed: every Cc, Cf, Zl and Zp code point, an encoded lone
    surrogate, invalid UTF-8, markdown/mention forgery and a 2 MB line."""
    chars = [chr(c) for c in range(0x110000) if unicodedata.category(chr(c)) in {"Cc", "Cf", "Zl", "Zp"}]
    text = "".join(chars) + "\n## forged heading\n@hostile-mention <!-- x -->\n" + "A" * (2 * 1024 * 1024)
    return text.encode("utf-8") + b"\xed\xa0\x80\xff\xfe\x00", len(chars)


@pytest.mark.parametrize("which", ["expired", "config"])
def test_expiry_issue_body_never_carries_raw_checker_output(tmp_path: Path, which: str, expired_rc: int) -> None:
    """Output safety (attack list A): hostile checker stdout/stderr never reaches the
    issue body raw, and the body stays within GitHub's size limit."""
    noise, generated = _hostile_checker_output()
    print(f"generated {generated} hostile code points")
    code = expired_rc if which == "expired" else _EXIT_CONFIG
    rc, issue_proc, calls = _simulate_check_then_issue(_ws_fake_checker(tmp_path, code, noise), tmp_path)
    assert rc == code
    assert issue_proc is not None and issue_proc.returncode == 0, issue_proc and issue_proc.stderr
    creates = _created(calls)
    assert creates, f"no issue opened on exit {code}"
    for rec in creates:
        for field in ("title", "body"):
            text = str(rec.get(field) or "")
            assert len(text) <= _GITHUB_ISSUE_BODY_MAX, f"{field} is {len(text)} chars"
            bad = sorted({ch for ch in text if ch not in "\n\t" and unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Zl", "Zp"}})
            assert not bad, f"{field} carries raw control/format characters: {[hex(ord(c)) for c in bad][:10]}"
            assert "@hostile-mention" not in text, f"{field} carries a raw @-mention from checker output"
