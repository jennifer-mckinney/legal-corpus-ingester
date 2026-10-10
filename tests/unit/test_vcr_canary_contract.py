"""Acceptance tests for the VCR Drift Canary contract (terms-analysis#92, G0-3).

These tests pin the "no silent green" contract for the weekly canary:

  a. the drift script refuses to report success when it compared nothing;
  b. a missing baseline directory is an error (regression guard);
  c. the workflow cannot mask failures or point the report at a path the
     stash step never creates, and it alerts even without a report;
  d. integration tests do not pin ``record_mode`` (any value) so the CLI flag
     can work;
  e. the re-record step replaces cassettes (``rewrite``), proven behaviourally;
  f. the drift script rejects empty cassettes, bounds its report size, contains
     upstream text inside a fenced block, and names status drift distinctly;
  g. recorded responses are scrubbed of Set-Cookie;
  h. workflow hardening: no persisted credentials, issue dedupe, no ``|| true``,
     and an exit-2 run is not titled "drift detected".

Round-1 P9 review inputs: g0-3-grumpy.md (#1,#2,#3,#5,#6,#7) and
g0-3-security.md (#1-#5).

Triage: terms-analysis docs/evidence/2026-10-07-vcr-canary-triage.md (B1-B4).
"""
from __future__ import annotations

import http.server
import importlib.util
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "vcr_drift_report.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "vcr-drift.yml"
INTEGRATION_DIR = REPO_ROOT / "tests" / "integration"

# A single "|" that is not part of "||" (shell pipe, not logical OR).
_PIPE_RE = re.compile(r"(?<!\|)\|(?!\|)")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the drift script the way the workflow does: as a subprocess."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )


def _load_workflow() -> dict[Any, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "vcr-drift.yml did not parse to a mapping"
    return data


def _drift_job(wf: dict[Any, Any]) -> dict[str, Any]:
    jobs = wf.get("jobs") or {}
    assert len(jobs) == 1, f"expected exactly one job in vcr-drift.yml, got {list(jobs)}"
    return next(iter(jobs.values()))


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return list(job.get("steps") or [])


def _run_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in _steps(job) if isinstance(s.get("run"), str)]


def _shell_lines(script: str) -> list[str]:
    """Join backslash continuations and split a run: block into logical lines."""
    joined = re.sub(r"\\\n\s*", " ", script)
    lines: list[str] = []
    for raw in joined.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Split simple command chains so "mkdir -p X && cp ..." is two commands.
        lines.extend(part.strip() for part in re.split(r"&&|;", line) if part.strip())
    return lines


def _norm(path: str) -> str:
    return posixpath.normpath(path.strip().strip("'\""))


def _step_using(job: dict[str, Any], needle: str) -> dict[str, Any]:
    matches = [s for s in _run_steps(job) if needle in s["run"]]
    assert len(matches) == 1, f"expected one run step containing {needle!r}, got {len(matches)}"
    return matches[0]


def _shell_has_pipefail(shell: object) -> bool:
    return isinstance(shell, str) and "pipefail" in shell


# ---------------------------------------------------------------------------
# a / b: drift script exit-code contract
# ---------------------------------------------------------------------------


def test_a_empty_dirs_exit_nonzero_and_say_zero_compared(tmp_path: Path) -> None:
    """Both dirs empty must be an error, not 'no drift' (silent green)."""
    baseline = tmp_path / "baseline"
    current = tmp_path / "current"
    baseline.mkdir()
    current.mkdir()
    out = tmp_path / "drift-report.md"

    proc = _run_script(
        "--baseline", str(baseline), "--current", str(current), "--output", str(out)
    )
    combined = (proc.stdout + proc.stderr).lower()

    assert proc.returncode != 0, (
        "drift script exited 0 after comparing zero cassettes; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert re.search(r"compared (0|zero) cassettes|(0|zero) cassettes compared", combined), (
        f"drift script did not say it compared zero cassettes; output={combined!r}"
    )


def test_b_missing_baseline_exits_nonzero(tmp_path: Path) -> None:
    """Regression guard: a missing baseline dir is an error (passes today)."""
    current = tmp_path / "current"
    current.mkdir()
    proc = _run_script(
        "--baseline",
        str(tmp_path / "does-not-exist"),
        "--current",
        str(current),
        "--output",
        str(tmp_path / "drift-report.md"),
    )
    assert proc.returncode != 0
    assert "baseline directory not found" in proc.stderr.lower()


# ---------------------------------------------------------------------------
# c: workflow contract (.github/workflows/vcr-drift.yml)
# ---------------------------------------------------------------------------


def test_c1_piped_run_steps_use_pipefail() -> None:
    """A pipe without pipefail masks the left-hand failure (B2: '| tail -40')."""
    wf = _load_workflow()
    job = _drift_job(wf)
    workflow_shell = ((wf.get("defaults") or {}).get("run") or {}).get("shell")
    job_shell = ((job.get("defaults") or {}).get("run") or {}).get("shell")
    if _shell_has_pipefail(job_shell) or (
        job_shell is None and _shell_has_pipefail(workflow_shell)
    ):
        return

    offenders = []
    for step in _run_steps(job):
        script = step["run"]
        if not _PIPE_RE.search(script):
            continue
        if _shell_has_pipefail(step.get("shell")):
            continue
        if re.search(r"^\s*set\s+(-[a-z]*o\s+pipefail|-o\s+pipefail)", script, re.M):
            continue
        offenders.append(step.get("name", "<unnamed>"))

    assert not offenders, f"piped run steps without pipefail: {offenders}"


def test_c2_no_step_uses_pytest_vcr_flag() -> None:
    """--vcr-record is pytest-vcr; the installed plugin is pytest-recording (B2)."""
    job = _drift_job(_load_workflow())
    offenders = [s.get("name", "<unnamed>") for s in _run_steps(job) if "--vcr-record" in s["run"]]
    assert not offenders, f"steps using unsupported --vcr-record flag: {offenders}"


def _stash_created_dir(stash_script: str) -> str:
    """Model what the stash step creates, with BSD cp semantics.

    CI runs GNU cp on ubuntu-latest. The two differ only for ``cp -r SRC/ DST``
    with DST an existing dir (GNU creates ``DST/basename(SRC)``); the stash step
    copies into an absent DST, where both agree.

    - ``cp -r SRC/ DST`` (trailing slash or ``SRC/.``) copies SRC's contents into DST.
    - ``cp -r SRC DST`` with DST an existing dir creates ``DST/basename(SRC)``.
    - ``cp -r SRC DST`` with DST absent creates DST itself.
    """
    created_dirs: set[str] = set()
    result: str | None = None
    for line in _shell_lines(stash_script):
        tokens = shlex.split(line)
        if not tokens:
            continue
        if tokens[0] == "mkdir":
            created_dirs.update(_norm(t) for t in tokens[1:] if not t.startswith("-"))
        elif tokens[0] == "cp":
            args = [t for t in tokens[1:] if not t.startswith("-")]
            assert len(args) == 2, f"unsupported cp form in stash step: {line!r}"
            src, dst = args
            dst_n = _norm(dst)
            if src.endswith("/") or src.endswith("/."):
                result = dst_n
            elif dst_n in created_dirs or dst.endswith("/"):
                result = posixpath.join(dst_n, posixpath.basename(_norm(src)))
            else:
                result = dst_n
    assert result is not None, "stash step has no cp command"
    return result


def _report_baseline_arg(report_script: str) -> str:
    for line in _shell_lines(report_script):
        tokens = shlex.split(line)
        for i, tok in enumerate(tokens):
            if tok == "--baseline" and i + 1 < len(tokens):
                return _norm(tokens[i + 1])
            if tok.startswith("--baseline="):
                return _norm(tok.split("=", 1)[1])
    raise AssertionError("report step does not pass --baseline")


def test_c3_report_baseline_is_dir_stash_step_creates() -> None:
    """B1: 'cp -r cassettes/ /tmp/vcr-baseline/' never creates .../cassettes."""
    job = _drift_job(_load_workflow())
    stash = _step_using(job, "cp ")
    report = _step_using(job, "vcr_drift_report.py")

    created = _stash_created_dir(stash["run"])
    baseline = _report_baseline_arg(report["run"])
    assert baseline == created, (
        f"report reads --baseline {baseline!r} but stash step creates {created!r}"
    )


def test_c4_issue_step_does_not_depend_solely_on_report_file() -> None:
    """B4: a setup error writes no report, so a report-gated alert never fires."""
    job = _drift_job(_load_workflow())
    issue_steps = [s for s in _run_steps(job) if "gh issue create" in s["run"]]
    assert issue_steps, "no step opens an issue"

    for step in issue_steps:
        script = step["run"]
        gated = re.search(r"if\s+\[\s*-f\s+\S*drift-report\.md\s*\]", script)
        if not gated:
            return
        # Gated on the report: an else branch must still open an issue.
        parts = re.split(r"^\s*else\b", script, maxsplit=1, flags=re.M)
        else_part = parts[1] if len(parts) == 2 else ""
        if "gh issue create" in else_part:
            return

    pytest.fail("every issue-opening path is gated on drift-report.md existing")


# ---------------------------------------------------------------------------
# d: integration tests must not pin record_mode
# ---------------------------------------------------------------------------


def _non_comment_lines(path: Path) -> list[tuple[int, str]]:
    return [
        (n, line)
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if not line.lstrip().startswith("#")
    ]


def test_d_integration_does_not_pin_record_mode_of_any_value() -> None:
    """B3 / grumpy #7: ANY record_mode in vcr_config, a vcr marker kwarg, or a
    ``record_mode`` fixture override beats the CLI --record-mode in pytest-recording."""
    pattern = re.compile(r"""record_mode["']?\s*[:=]|def\s+record_mode\b""")
    files = sorted(INTEGRATION_DIR.rglob("*.py"))
    conftest = REPO_ROOT / "tests" / "conftest.py"
    if conftest.exists():
        files.append(conftest)
    offenders = [
        f"{p.relative_to(REPO_ROOT)}:{n}"
        for p in files
        for n, line in _non_comment_lines(p)
        if pattern.search(line)
    ]
    assert not offenders, f"record_mode is pinned (CLI flag would be ignored): {offenders}"


# ---------------------------------------------------------------------------
# helpers for cassette-pair tests
# ---------------------------------------------------------------------------


def _cassette_yaml(body: str = "x", uri: str = "http://h/a", status: int = 200) -> str:
    return yaml.safe_dump(
        {
            "version": 1,
            "interactions": [
                {
                    "request": {"method": "GET", "uri": uri, "body": None, "headers": {}},
                    "response": {
                        "status": {"code": status, "message": "x"},
                        "headers": {},
                        "body": {"string": body},
                    },
                }
            ],
        }
    )


def _pair(tmp_path: Path, baseline_text: str, current_text: str) -> subprocess.CompletedProcess[str]:
    b, c = tmp_path / "baseline", tmp_path / "current"
    (b / "eurlex").mkdir(parents=True)
    (c / "eurlex").mkdir(parents=True)
    (b / "eurlex" / "x.yaml").write_text(baseline_text, encoding="utf-8")
    (c / "eurlex" / "x.yaml").write_text(current_text, encoding="utf-8")
    return _run_script(
        "--baseline", str(b), "--current", str(c), "--output", str(tmp_path / "report.md")
    )


def _report(tmp_path: Path) -> str:
    return (tmp_path / "report.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# e: re-record contract (grumpy #1)
# ---------------------------------------------------------------------------


def _rerecord_step() -> dict[str, Any]:
    job = _drift_job(_load_workflow())
    return _step_using(job, "--record-mode")


def _rerecord_mode() -> str:
    m = re.search(r"--record-mode[= ]([A-Za-z_]+)", _rerecord_step()["run"])
    assert m, "re-record step has no parsable --record-mode value"
    return m.group(1)


def test_e1_rerecord_step_uses_rewrite_mode() -> None:
    """vcrpy 'all' APPENDS to the committed cassette (old + new), so the canary
    cannot tell drift from no drift. 'rewrite' deletes first."""
    assert _rerecord_mode() == "rewrite", (
        f"re-record uses --record-mode={_rerecord_mode()}; must be 'rewrite' "
        "(replace, not append)"
    )


def test_e2_rerecord_replaces_cassette_with_exactly_one_new_interaction(
    tmp_path: Path,
) -> None:
    """Behavioural: serve body A, record, serve body B, re-record with the
    workflow's own mode; the cassette must hold exactly one interaction (B).

    Lives in tests/unit as a self-contained subprocess run against a loopback
    http.server and a throwaway pytest rootdir; it needs no network and no
    repo fixtures, so it does not belong in tests/integration (which is the
    suite the canary re-records)."""
    mode = _rerecord_mode()
    served = {"body": b"BODY-A"}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            payload = served["body"]
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_a: object) -> None:  # silence
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        work = tmp_path / "rec"
        work.mkdir()
        (work / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        (work / "test_rec.py").write_text(
            textwrap.dedent(
                """
                import os, urllib.request, pytest

                @pytest.mark.vcr
                def test_fetch():
                    with urllib.request.urlopen(os.environ["CANARY_URL"]) as r:
                        assert r.read()
                """
            ),
            encoding="utf-8",
        )
        env = {
            "PATH": "/usr/bin:/bin",
            "CANARY_URL": f"http://127.0.0.1:{server.server_address[1]}/doc",
            "NO_PROXY": "127.0.0.1",
            "no_proxy": "127.0.0.1",
        }

        def record() -> None:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                 f"--record-mode={mode}", "test_rec.py"],
                cwd=work, env=env, capture_output=True, text=True, timeout=120,
            )
            assert proc.returncode == 0, proc.stdout + proc.stderr

        record()
        served["body"] = b"BODY-B"
        record()
    finally:
        server.shutdown()
        server.server_close()

    cassettes = list(work.rglob("*.yaml"))
    assert len(cassettes) == 1, f"expected one cassette, got {cassettes}"
    interactions = yaml.safe_load(cassettes[0].read_text(encoding="utf-8"))["interactions"]
    bodies = [str(i["response"]["body"]["string"]) for i in interactions]
    assert bodies == ["BODY-B"], (
        f"--record-mode={mode} left {len(bodies)} interaction(s) {bodies}; "
        "expected exactly one with the new body"
    )


# ---------------------------------------------------------------------------
# f: drift script hardening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["just a string\n", "interactions: []\n", "interactions: null\n", "version: 1\n"],
    ids=["non-dict", "empty-list", "null", "missing-key"],
)
def test_f1_zero_interaction_cassette_pair_exits_nonzero(tmp_path: Path, text: str) -> None:
    """grumpy #2: a pair with no HTTP interactions is not a comparison."""
    proc = _pair(tmp_path, text, text)
    assert proc.returncode != 0, (
        "cassette pair with zero interactions counted as compared and exited 0; "
        f"stdout={proc.stdout!r}"
    )


def test_f2_report_is_bounded_and_marked_truncated(tmp_path: Path) -> None:
    """security #1: GitHub rejects issue bodies > 65536 chars."""
    proc = _pair(tmp_path, _cassette_yaml("a"), _cassette_yaml("Z" * 200_000))
    assert proc.returncode == 1
    raw = (tmp_path / "report.md").read_bytes()
    assert len(raw) < 65536, f"report is {len(raw)} bytes; exceeds issue body limit"
    assert re.search(r"truncat", raw.decode("utf-8"), re.I), "no truncation marker in report"


_PAYLOAD_TOKENS = ("@octocat", "#1", "![x](http://e)", "```")


def _fenced_regions(report: str) -> list[tuple[str, int, list[int]]]:
    """CommonMark fences: (fence_char, fence_length, inner_line_indexes).

    Opening fence: up to 3 spaces, >=3 backticks or tildes, optional info string
    (no backticks in a backtick fence's info string). Closing fence: same char,
    length >= opening, nothing else on the line. Delimiter lines are NOT inner.
    """
    regions: list[tuple[str, int, list[int]]] = []
    ch, open_len, inner = "", 0, []
    for idx, line in enumerate(report.splitlines()):
        if not open_len:
            m = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                ch, open_len, inner = m.group(1)[0], len(m.group(1)), []
            continue
        m = re.match(r"^ {0,3}(" + re.escape(ch) + r"{3,})\s*$", line)
        if m and len(m.group(1)) >= open_len:
            regions.append((ch, open_len, inner))
            open_len = 0
        else:
            inner.append(idx)
    if open_len:  # unclosed fence runs to end of document (CommonMark)
        regions.append((ch, open_len, inner))
    return regions


def test_f3_upstream_text_is_contained_in_a_long_enough_fence(tmp_path: Path) -> None:
    """security #2: mentions, refs, images and backtick runs from upstream must
    not render as live Markdown in the auto-opened issue."""
    body = "line @octocat see #1 ![x](http://e) and ``` fence break\n"
    proc = _pair(tmp_path, _cassette_yaml("old\n"), _cassette_yaml(body))
    assert proc.returncode == 1
    lines = _report(tmp_path).splitlines()
    regions = _fenced_regions("\n".join(lines))
    assert regions, "upstream snippet is not in any fenced block"
    inner_idx = {i for _c, _n, inn in regions for i in inn}
    delimiters = set()  # fence delimiter lines are structure, not payload
    for i, ln in enumerate(lines):
        if i not in inner_idx and re.match(r"^ {0,3}(`{3,}|~{3,})", ln):
            delimiters.add(i)

    for token in _PAYLOAD_TOKENS:
        outside = [
            lines[i] for i, ln in enumerate(lines)
            if token in ln and i not in delimiters and i not in inner_idx
        ]
        assert not outside, f"{token!r} appears outside a fenced block: {outside}"
        assert any(token in lines[i] for i in inner_idx), f"{token!r} not found in any fence"

    for ch, fence_len, inner in regions:
        if ch == "~":
            continue  # backtick runs cannot terminate a tilde fence
        longest_run = max(
            (len(r) for i in inner for r in re.findall(r"`+", lines[i])), default=0
        )
        assert fence_len > longest_run, (
            f"fence of {fence_len} backticks does not exceed content run {longest_run}"
        )


def test_f4_status_code_drift_is_reported_distinctly(tmp_path: Path) -> None:
    """grumpy #6: 200 -> 503 must not be labelled 'URI drift'."""
    proc = _pair(tmp_path, _cassette_yaml(status=200), _cassette_yaml(status=503))
    assert proc.returncode == 1
    report = _report(tmp_path)
    assert re.search(r"status drift", report, re.I), "report does not name status drift"
    assert not re.search(r"URI drift", report), "status change mislabelled as URI drift"


def test_f5_uri_only_drift_is_not_called_status_drift(tmp_path: Path) -> None:
    """Regression guard for the split in f4."""
    proc = _pair(tmp_path, _cassette_yaml(uri="http://h/a"), _cassette_yaml(uri="http://h/b"))
    assert proc.returncode == 1
    report = _report(tmp_path)
    assert re.search(r"URI drift", report)
    assert not re.search(r"status drift", report, re.I)


# ---------------------------------------------------------------------------
# g: cookie scrubbing (security #3)
# ---------------------------------------------------------------------------


def test_g1_no_committed_cassette_has_set_cookie() -> None:
    offenders = [
        str(p.relative_to(REPO_ROOT))
        for p in sorted((REPO_ROOT / "tests" / "fixtures" / "cassettes").rglob("*.yaml"))
        if re.search(r"set-cookie", p.read_text(encoding="utf-8"), re.I)
    ]
    assert not offenders, f"committed cassettes carry Set-Cookie: {offenders}"


def _vcr_config_fixture_files() -> list[Path]:
    return [
        p for p in sorted(INTEGRATION_DIR.rglob("*.py"))
        if re.search(r"def\s+vcr_config\b", p.read_text(encoding="utf-8"))
    ]


def _unwrap_fixture(obj: Any) -> Any:
    for attr in ("__wrapped__", "_get_wrapped_function"):
        got = getattr(obj, attr, None)
        if got is not None:
            return got() if attr == "_get_wrapped_function" else got
    return getattr(obj, "__pytest_wrapped__").obj


def test_g2_vcr_config_scrubs_set_cookie_from_recorded_responses() -> None:
    files = _vcr_config_fixture_files()
    assert files, "no integration vcr_config fixture found"
    for path in files:
        spec = importlib.util.spec_from_file_location(f"_vcrcfg_{path.stem}", path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        config = _unwrap_fixture(mod.vcr_config)()
        scrub = config.get("before_record_response")
        assert callable(scrub), f"{path.name}: vcr_config has no before_record_response scrubber"
        response = {
            "status": {"code": 200, "message": "OK"},
            "headers": {
                "Set-Cookie": ["sid=abc"],
                "set-cookie": ["lb=1"],
                "Content-Type": ["text/xml"],
            },
            "body": {"string": "x"},
        }
        out = scrub(response)
        assert out is not None
        keys = {k.lower() for k in out["headers"]}
        assert "set-cookie" not in keys, f"{path.name}: Set-Cookie survived scrub: {keys}"
        assert "content-type" in keys, "scrubber removed unrelated headers"


# ---------------------------------------------------------------------------
# h: workflow hardening
# ---------------------------------------------------------------------------


def test_h1_checkout_does_not_persist_credentials() -> None:
    job = _drift_job(_load_workflow())
    checkouts = [s for s in _steps(job) if "actions/checkout" in str(s.get("uses", ""))]
    assert checkouts, "no checkout step"
    for step in checkouts:
        value = (step.get("with") or {}).get("persist-credentials")
        assert value in (False, "false"), f"checkout persist-credentials is {value!r}"


def _issue_step() -> dict[str, Any]:
    return _step_using(_drift_job(_load_workflow()), "gh issue create")


def test_h2_issue_step_checks_for_existing_open_issue_first() -> None:
    script = _issue_step()["run"]
    listing = re.search(r"gh issue list\b[^\n]*", script)
    assert listing, "issue step never runs `gh issue list` (no dedupe guard)"
    assert "--state open" in listing.group(0) or "--state=open" in listing.group(0)
    assert "corpus-drift" in listing.group(0), "dedupe list is not scoped to the corpus-drift label"
    assert listing.start() < script.index("gh issue create"), "dedupe check must precede create"


def test_h3_no_or_true_in_any_run_step() -> None:
    offenders = [
        s.get("name", "<unnamed>")
        for s in _run_steps(_drift_job(_load_workflow()))
        if re.search(r"\|\|\s*true\b", s["run"])
    ]
    assert not offenders, f"`|| true` swallows errors in: {offenders}"


# --- h4: behavioural wiring test (grumpy #4 / r7 #1 / r8 #1 / r9 #1) --------
#
# Whack-a-mole rule, third round on h4: static string/regex checks on the YAML
# kept letting wiring mutations through (renamed env key, wrong step id, rc
# line dropped). h4 now RUNS the two steps the way Actions would: the report
# step writes GITHUB_OUTPUT, the issue step's `env:` is resolved from that
# output exactly as Actions resolves expressions (a missing step id or output
# is the empty string), and the issue step runs under `bash -eo pipefail` with
# a fake `gh` that records its argv. Everything runs inside tmp_path; the fake
# `gh` is first on PATH and makes no network call.

_REPORT_STEP_NAME = "Generate drift report"
_ISSUE_STEP_NAME = "Open issue on failure"
_EXPR_RE = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
# Fake values for the trusted github.* context; nothing here reaches a network.
_FAKE_GITHUB = {
    "server_url": "https://github.example",
    "repository": "owner/repo",
    "run_id": "9",
    "token": "fake-token-not-a-secret",
}
_FAKE_GH_PY = (
    "#!{exe}\n"
    "import json, os, sys\n"
    "with open(os.environ['GH_LOG'], 'a', encoding='utf-8') as fh:\n"
    "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
)
_PY_SHIM = '#!/usr/bin/env bash\nexec "{exe}" "$@"\n'


def _step_named(job: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [s for s in _steps(job) if s.get("name") == name]
    assert len(matches) == 1, f"expected one step named {name!r}, got {len(matches)}"
    return matches[0]


def _parse_github_output(path: Path) -> dict[str, str]:
    """Parse a GITHUB_OUTPUT file: `k=v` lines and `k<<DELIM` heredoc blocks."""
    outputs: dict[str, str] = {}
    if not path.exists():
        return outputs
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            key, delim = line.split("<<", 1)
            body: list[str] = []
            i += 1
            while i < len(lines) and lines[i] != delim:
                body.append(lines[i])
                i += 1
            outputs[key] = "\n".join(body)
        elif "=" in line:
            key, value = line.split("=", 1)
            outputs[key] = value
        i += 1
    return outputs


def _resolve_expr(expr: str, step_outputs: dict[str, dict[str, str]]) -> str:
    """Resolve one `${{ ... }}` expression the way Actions does for env values.

    Only `steps.<id>.outputs.<k>` and `github.<k>` are modelled. A reference to
    a step id that did not run (or has no id), or to an output it never wrote,
    is the empty string, exactly as in Actions."""
    m = re.fullmatch(r"steps\.([\w-]+)\.outputs\.([\w-]+)", expr)
    if m:
        return step_outputs.get(m.group(1), {}).get(m.group(2), "")
    m = re.fullmatch(r"github\.([\w-]+)", expr)
    if m:
        return _FAKE_GITHUB.get(m.group(1), "")
    return ""


def _resolve_env(env: dict[str, Any] | None, step_outputs: dict[str, dict[str, str]]) -> dict[str, str]:
    return {
        str(k): _EXPR_RE.sub(lambda m: _resolve_expr(m.group(1), step_outputs), str(v))
        for k, v in (env or {}).items()
    }


def _step_runs(step: dict[str, Any], job_failed: bool) -> bool:
    """Evaluate the status-check `if:` forms the canary uses; anything else is
    unsupported by this harness and fails loudly rather than guessing."""
    cond = str(step.get("if", "success()")).strip()
    cond = _EXPR_RE.sub(lambda m: m.group(1), cond).strip()
    table = {"success()": not job_failed, "failure()": job_failed, "always()": True}
    assert cond in table, f"h4 harness does not model `if: {cond}`"
    return table[cond]


def _base_env(tmp_path: Path, bindir: Path) -> dict[str, str]:
    """A clean env: inherit nothing GitHub-ish (REPORT_RC, GITHUB_*, RUNNER_*)
    from the developer's shell, so a broken binding cannot be masked."""
    keep = {k: v for k, v in os.environ.items() if k in ("HOME", "LANG", "LC_ALL", "TMPDIR")}
    return {
        **keep,
        "PATH": f"{bindir}:/usr/bin:/bin",
        "RUNNER_TEMP": str(tmp_path / "runner_temp"),
        "GH_LOG": str(tmp_path / "gh.log"),
    }


def _h4_fixture(work: Path, runner_temp: Path, case: str) -> None:
    """Lay out baseline/current cassettes that make the report exit 0/1/2."""
    baseline = runner_temp / "vcr-baseline"
    current = work / "tests" / "fixtures" / "cassettes"
    baseline.mkdir(parents=True)
    current.mkdir(parents=True)
    if case == "nothing_compared":
        return  # both trees empty: compared 0, exit 2
    (baseline / "eurlex").mkdir()
    (current / "eurlex").mkdir()
    (baseline / "eurlex" / "x.yaml").write_text(_cassette_yaml("same"), encoding="utf-8")
    body = "changed" if case == "drift" else "same"
    (current / "eurlex" / "x.yaml").write_text(_cassette_yaml(body), encoding="utf-8")


def _simulate_report_then_issue(wf_text: str, tmp_path: Path, case: str) -> tuple[int, list[list[str]]]:
    """Run the report step, then (if its `if:` allows) the issue step.

    Returns (report step exit code, recorded gh argv lists)."""
    wf = yaml.safe_load(wf_text)
    job = _drift_job(wf)
    report = _step_named(job, _REPORT_STEP_NAME)
    issue = _step_named(job, _ISSUE_STEP_NAME)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(_FAKE_GH_PY.format(exe=sys.executable), encoding="utf-8")
    for name in ("python", "python3"):
        (bindir / name).write_text(_PY_SHIM.format(exe=sys.executable), encoding="utf-8")
    for p in bindir.iterdir():
        p.chmod(0o755)

    work = tmp_path / "w"
    (work / "scripts").mkdir(parents=True)
    for name in ("vcr_drift_report.py", "vcr_issue_outcome.py"):
        (work / "scripts" / name).write_text(
            (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    # The report step sources the venv; an empty activate keeps PATH's shims.
    (work / ".venv" / "bin").mkdir(parents=True)
    (work / ".venv" / "bin" / "activate").write_text("", encoding="utf-8")
    runner_temp = tmp_path / "runner_temp"
    _h4_fixture(work, runner_temp, case)

    base = _base_env(tmp_path, bindir)
    gh_output = tmp_path / "github_output"
    gh_output.touch()

    # 1. Report step. Nothing has written outputs yet.
    # Actions would skip a report step whose `if:` is false on a healthy job.
    assert _step_runs(report, job_failed=False), "report step would be skipped on a healthy run"
    report_proc = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", report["run"]],
        cwd=work,
        env={**base, **_resolve_env(report.get("env"), {}), "GITHUB_OUTPUT": str(gh_output)},
        capture_output=True, text=True, timeout=60, check=False,
    )
    step_outputs: dict[str, dict[str, str]] = {}
    if report.get("id"):
        step_outputs[str(report["id"])] = _parse_github_output(gh_output)
    job_failed = report_proc.returncode != 0

    # 2. Issue step, with env resolved from what the report step really wrote.
    if _step_runs(issue, job_failed):
        issue_proc = subprocess.run(
            ["bash", "-eo", "pipefail", "-c", issue["run"]],
            cwd=work,
            env={**base, **_resolve_env(issue.get("env"), step_outputs)},
            capture_output=True, text=True, timeout=60, check=False,
        )
        assert issue_proc.returncode == 0, issue_proc.stderr
    log = tmp_path / "gh.log"
    calls = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return report_proc.returncode, calls


def _created_title(calls: list[list[str]]) -> str | None:
    creates = [c for c in calls if c[:2] == ["issue", "create"]]
    assert len(creates) <= 1, creates
    if not creates:
        return None
    argv = creates[0]
    assert "--title" in argv, argv
    return argv[argv.index("--title") + 1]


_H4_CASES = {
    # case: (expected report rc, title must match, title must not match)
    "clean": (0, None, None),
    "drift": (1, r"drift detected", None),
    "nothing_compared": (2, r"compared 0|0 cassettes", r"drift detected"),
}


def _assert_h4_case(wf_text: str, tmp_path: Path, case: str) -> None:
    want_rc, must, must_not = _H4_CASES[case]
    rc, calls = _simulate_report_then_issue(wf_text, tmp_path, case)
    assert rc == want_rc, f"{case}: report step exited {rc}, fixture expects {want_rc}"
    title = _created_title(calls)
    if must is None:
        assert title is None and not calls, f"{case}: a green run must not touch gh: {calls}"
        return
    assert title is not None, f"{case}: no issue created; gh calls: {calls}"
    assert re.search(must, title, re.I), f"{case}: issue title {title!r} lacks /{must}/"
    if must_not:
        assert not re.search(must_not, title, re.I), f"{case}: issue title {title!r} matches /{must_not}/"


@pytest.mark.parametrize("case", list(_H4_CASES))
def test_h4_exit_2_is_not_titled_drift_detected(tmp_path: Path, case: str) -> None:
    """grumpy #4 / r7-r9: run the real report and issue steps end to end.

    drift -> "drift detected"; rc 2 -> "compared 0 cassettes" (never "drift
    detected"); rc 0 -> the issue step is skipped and gh is never called."""
    _assert_h4_case(WORKFLOW.read_text(encoding="utf-8"), tmp_path, case)


# Wiring mutations that earlier static h4 versions let through. Each must make
# the behavioural h4 fail; applied to a tmp_path copy, never the real file.
_H4_MUTATIONS = {
    # grumpy r9: env key renamed, run block unchanged.
    "env_key_renamed": ("REPORT_RC: ${{ steps.report", "RC_IN: ${{ steps.report"),
    # grumpy r9: env value reads a step id that does not exist.
    "missing_step_id": ("steps.report.outputs.rc", "steps.nope.outputs.rc"),
    # grumpy r8: report step no longer records its exit code.
    "rc_line_removed": ('          echo "rc=$rc" >> "$GITHUB_OUTPUT"\n', ""),
    # grumpy r10: report step skipped on healthy runs, so drift is never reported.
    "report_step_conditional": ("        id: report\n", "        id: report\n        if: failure()\n"),
}


@pytest.mark.parametrize("mutation", list(_H4_MUTATIONS))
def test_h4_bites_on_wiring_mutation(tmp_path: Path, mutation: str) -> None:
    old, new = _H4_MUTATIONS[mutation]
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"mutation {mutation!r} no longer applies to vcr-drift.yml"
    mutated = tmp_path / "vcr-drift.yml"
    mutated.write_text(text.replace(old, new, 1), encoding="utf-8")
    failed = []
    for case in _H4_CASES:
        case_dir = tmp_path / case
        case_dir.mkdir()
        try:
            _assert_h4_case(mutated.read_text(encoding="utf-8"), case_dir, case)
        except AssertionError:
            failed.append(case)
    # Each mutation breaks report->issue wiring (REPORT_RC empties, or the
    # report step would be skipped), so both alerting cases must fail.
    assert {"drift", "nothing_compared"} <= set(failed), (
        f"h4 did not catch mutation {mutation!r}; failing cases: {failed}"
    )
