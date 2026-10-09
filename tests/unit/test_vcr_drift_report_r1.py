"""Round-1 P9 review fixes for the VCR drift canary (terms-analysis#92).

Covers: zero-interaction cassettes as parse errors, status drift as its own
class, bounded report size with a truncation marker, Markdown containment of
upstream bytes, the response-header scrubber, and the workflow's report and
issue steps executed under ``bash -eo pipefail`` with a stubbed ``gh``.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import vcr_drift_report as vdr  # noqa: E402

WORKFLOW = REPO / ".github" / "workflows" / "vcr-drift.yml"
CASSETTES = REPO / "tests" / "fixtures" / "cassettes"
INTEGRATION = REPO / "tests" / "integration" / "test_eurlex_fetcher.py"


def _ix(body: str = "ok", uri: str = "http://x.com", status: int = 200) -> dict[str, Any]:
    return {
        "request": {"uri": uri, "method": "GET"},
        "response": {"status": {"code": status}, "body": {"string": body}},
    }


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data) if not isinstance(data, str) else data)


def _pair(tmp_path: Path, b: Any, c: Any, name: str = "x.yaml") -> tuple[Path, Path]:
    bdir, cdir = tmp_path / "b", tmp_path / "c"
    _write(bdir / name, b)
    _write(cdir / name, c)
    return bdir, cdir


# ---------------------------------------------------------------------------
# grumpy #2: zero-interaction cassettes are parse errors, never "compared"
# ---------------------------------------------------------------------------

_EMPTY_SHAPES = {
    "non-dict": "just a string\n",
    "empty-list": {"interactions": []},
    "null": {"interactions": None},
    "missing-key": {"version": 1},
}


@pytest.mark.parametrize("shape", sorted(_EMPTY_SHAPES))
@pytest.mark.parametrize("side", ["baseline", "current"])
def test_empty_cassette_is_parse_error_on_either_side(
    tmp_path: Path, shape: str, side: str
) -> None:
    good = {"interactions": [_ix()]}
    bad = _EMPTY_SHAPES[shape]
    b, c = (bad, good) if side == "baseline" else (good, bad)
    bdir, cdir = _pair(tmp_path, b, c)
    report, any_drift, compared = vdr.generate_report_with_count(bdir, cdir)
    assert (any_drift, compared) == (True, 0)
    assert "Parse errors" in report
    assert f"{side}/x.yaml" in report


def test_empty_pair_alongside_good_pair_exits_one(tmp_path: Path) -> None:
    bdir, cdir = _pair(tmp_path, {"interactions": []}, {"interactions": []})
    _write(bdir / "ok.yaml", {"interactions": [_ix()]})
    _write(cdir / "ok.yaml", {"interactions": [_ix()]})
    out = tmp_path / "r.md"
    code = vdr.main(["--baseline", str(bdir), "--current", str(cdir), "--output", str(out)])
    assert code == 1
    assert "no interactions in cassette" in out.read_text()


def test_interactions_error_messages() -> None:
    assert vdr._interactions_error({"interactions": [_ix()]}) is None
    assert "not a mapping" in str(vdr._interactions_error(["x"]))
    assert "no 'interactions' key" in str(vdr._interactions_error({}))
    assert "not a list" in str(vdr._interactions_error({"interactions": "x"}))
    assert "no interactions" in str(vdr._interactions_error({"interactions": []}))


# Copilot PR #25: a non-empty interactions list whose entries, request,
# response or status are not mappings made diff_cassette raise AttributeError,
# which crashed the whole report instead of listing a parse error.
_NESTED_SHAPES: dict[str, Any] = {
    "null-interaction": {"interactions": [None]},
    "str-interaction": {"interactions": ["bad"]},
    "str-request": {"interactions": [{"request": "bad", "response": _ix()["response"]}]},
    "list-request": {"interactions": [{"request": ["x"], "response": _ix()["response"]}]},
    "str-response": {"interactions": [{"request": _ix()["request"], "response": "bad"}]},
    "str-status": {
        "interactions": [{"request": _ix()["request"], "response": {"status": "bad"}}]
    },
    "null-status": {
        "interactions": [{"request": _ix()["request"], "response": {"status": None}}]
    },
    "second-entry-bad": {"interactions": [_ix(), None]},
}


@pytest.mark.parametrize("shape", sorted(_NESTED_SHAPES))
@pytest.mark.parametrize("side", ["baseline", "current"])
def test_malformed_nested_interaction_is_parse_error(
    tmp_path: Path, shape: str, side: str
) -> None:
    good = {"interactions": [_ix(), _ix()]}
    bad = _NESTED_SHAPES[shape]
    b, c = (bad, good) if side == "baseline" else (good, bad)
    bdir, cdir = _pair(tmp_path, b, c)
    report, any_drift, compared = vdr.generate_report_with_count(bdir, cdir)
    assert (any_drift, compared) == (True, 0)
    assert "Parse errors" in report
    assert f"{side}/x.yaml" in report
    assert "interaction " in str(vdr._interactions_error(bad))


def test_malformed_cassette_does_not_hide_drift_in_other_pairs(tmp_path: Path) -> None:
    """One bad cassette is a parse error; the other pairs are still diffed."""
    bdir, cdir = _pair(tmp_path, _NESTED_SHAPES["null-interaction"], {"interactions": [_ix()]})
    _write(bdir / "ok.yaml", {"interactions": [_ix(body="old")]})
    _write(cdir / "ok.yaml", {"interactions": [_ix(body="new")]})
    out = tmp_path / "r.md"
    code = vdr.main(["--baseline", str(bdir), "--current", str(cdir), "--output", str(out)])
    assert code == 1
    text = out.read_text()
    assert "baseline/x.yaml" in text
    assert "interaction 0: body changed" in text


def test_absent_or_null_request_response_still_compares() -> None:
    """Absent/null request or response keep the existing tolerant comparison."""
    assert vdr._interactions_error({"interactions": [{}]}) is None
    assert vdr._interactions_error({"interactions": [{"request": None, "response": None}]}) is None
    assert vdr._interactions_error({"interactions": [{"response": {"body": "x"}}]}) is None


# ---------------------------------------------------------------------------
# grumpy #6: status drift is its own class
# ---------------------------------------------------------------------------


def test_status_drift_counted_separately_from_uri() -> None:
    r = vdr.diff_cassette({"interactions": [_ix(status=200)]}, {"interactions": [_ix(status=503)]})
    assert (r["changed"], r["status_drift"], r["uri_drift"]) == (True, 1, 0)


def test_uri_drift_is_not_status_drift(tmp_path: Path) -> None:
    bdir, cdir = _pair(
        tmp_path,
        {"interactions": [_ix(uri="http://a")]},
        {"interactions": [_ix(uri="http://b")]},
    )
    report, _d, _n = vdr.generate_report_with_count(bdir, cdir)
    assert "URI drift in 1 interaction(s)" in report
    assert "status drift" not in report


def test_report_names_status_drift(tmp_path: Path) -> None:
    bdir, cdir = _pair(
        tmp_path, {"interactions": [_ix(status=200)]}, {"interactions": [_ix(status=503)]}
    )
    report, _d, _n = vdr.generate_report_with_count(bdir, cdir)
    assert "status drift in 1 interaction(s)" in report
    assert "URI drift" not in report


# ---------------------------------------------------------------------------
# security F1: bounded report
# ---------------------------------------------------------------------------


def test_single_huge_line_is_clipped_with_marker(tmp_path: Path) -> None:
    bdir, cdir = _pair(
        tmp_path, {"interactions": [_ix("a")]}, {"interactions": [_ix("Z" * 200_000)]}
    )
    report, _d, _n = vdr.generate_report_with_count(bdir, cdir)
    assert len(report.encode()) < 65536
    assert vdr.TRUNCATION_MARKER in report
    longest = max(len(ln) for ln in report.splitlines())
    assert longest < vdr.MAX_SNIPPET_LINE_CHARS + 100


def test_many_lines_capped_by_line_count_and_bytes() -> None:
    old = "\n".join(f"old {i} " + "x" * 190 for i in range(500))
    new = "\n".join(f"new {i} " + "y" * 190 for i in range(500))
    snippet, cut = vdr._diff_snippet(old, new)
    assert cut is True
    assert len(snippet.encode()) <= vdr.MAX_SNIPPET_BYTES + 100
    assert len(snippet.splitlines()) <= vdr.MAX_SNIPPET_LINES + 1


def test_small_diff_is_not_marked_truncated() -> None:
    snippet, cut = vdr._diff_snippet("a\n", "b\n")
    assert cut is False
    assert "-a" in snippet and "+b" in snippet


def test_whole_report_capped_with_trailer_and_balanced_fences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUN_URL", "https://github.example/o/r/actions/runs/42")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    bdir, cdir = tmp_path / "b", tmp_path / "c"
    for i in range(80):  # 80 drifting cassettes x ~2 KB snippet each > 60 KB
        multi_old = "\n".join(f"o{j}" + "a" * 180 for j in range(12))
        multi_new = "\n".join(f"n{j}" + "b" * 180 for j in range(12))
        _write(bdir / f"c{i:03}.yaml", {"interactions": [_ix(multi_old)]})
        _write(cdir / f"c{i:03}.yaml", {"interactions": [_ix(multi_new)]})
    report, any_drift, compared = vdr.generate_report_with_count(bdir, cdir)
    assert (any_drift, compared) == (True, 80)
    assert len(report.encode()) <= vdr.MAX_REPORT_BYTES
    assert vdr.TRUNCATION_MARKER in report
    assert "https://github.example/o/r/actions/runs/42" in report
    assert "vcr-drift-report-42" in report
    # Every fence opened is closed: whole blocks only are dropped.
    fences = [ln for ln in report.splitlines() if re.fullmatch(r"`{3,}(diff)?", ln)]
    assert len(fences) % 2 == 0


def test_bounded_join_keeps_whole_blocks() -> None:
    text, cut = vdr._bounded_join(["aaaa", "bbbb", "cccc"], 10)
    assert (text, cut) == ("aaaa\nbbbb", True)
    assert vdr._bounded_join(["a"], 10) == ("a", False)


# ---------------------------------------------------------------------------
# security F2: upstream bytes stay literal
# ---------------------------------------------------------------------------


def test_fence_is_longer_than_any_backtick_run() -> None:
    content = "x ````` y ``` z"
    fenced = vdr._fence(content, "diff")
    first, *_mid, last = fenced.splitlines()
    assert first == "`" * 6 + "diff" and last == "`" * 6


def test_fence_minimum_three() -> None:
    assert vdr._fence("plain").splitlines()[0] == "```"


def test_inline_code_cannot_be_broken_out_of() -> None:
    assert vdr._code("a`b") == "``a`b``"
    assert vdr._code("`x") == "`` `x ``"
    assert vdr._code("line1\nline2") == "`line1 line2`"


def test_payload_inside_fence_and_uris_in_code(tmp_path: Path) -> None:
    payload = "@octocat #1 ![x](https://e) <img src=x> ``` break"
    bdir, cdir = _pair(
        tmp_path,
        {"interactions": [_ix("old", uri="http://a/@octocat")]},
        {"interactions": [_ix(payload, uri="http://b/[click](https://evil)")]},
    )
    report, _d, _n = vdr.generate_report_with_count(bdir, cdir)
    inside = False
    fence = ""
    for ln in report.splitlines():
        if not inside and re.fullmatch(r"`{3,}\w*", ln):
            inside, fence = True, re.match(r"`+", ln).group(0)  # type: ignore[union-attr]
            continue
        if inside and ln == fence:
            inside = False
            continue
        if not inside and "<img" in ln:
            pytest.fail(f"payload outside fence: {ln!r}")
    assert "`'http://b/[click](https://evil)'`" in report


def test_parse_error_message_is_single_line_code(tmp_path: Path) -> None:
    bdir, cdir = _pair(tmp_path, ": : invalid\n", ": : invalid\n")
    report, _d, _n = vdr.generate_report_with_count(bdir, cdir)
    err_lines = [ln for ln in report.splitlines() if ln.startswith("- `baseline/")]
    assert len(err_lines) == 1 and "YAML parse error" in err_lines[0]


# ---------------------------------------------------------------------------
# security F3: response-header scrubber + committed cassettes are clean
# ---------------------------------------------------------------------------


def _integration_module() -> Any:
    spec = importlib.util.spec_from_file_location("_eurlex_it", INTEGRATION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_scrubber_drops_cookie_and_auth_headers_case_insensitively() -> None:
    mod = _integration_module()
    resp = {
        "headers": {
            "Set-Cookie": ["s=1"],
            "SET-COOKIE2": ["t=2"],
            "WWW-Authenticate": ["Basic"],
            "X-Auth-Token": ["tok"],
            "Content-Type": ["text/html"],
        },
        "body": {"string": "ok"},
    }
    out = mod._scrub_response(resp)
    assert out is resp
    assert list(out["headers"]) == ["Content-Type"]


def test_scrubber_tolerates_missing_headers() -> None:
    mod = _integration_module()
    assert mod._scrub_response({"body": {"string": ""}}) == {"body": {"string": ""}}


_SENSITIVE = re.compile(r"cookie|authorization|authenticate|api-key|auth-token", re.I)


def _keys(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [str(k) for k in node] + [k for v in node.values() for k in _keys(v)]
    if isinstance(node, list):
        return [k for v in node for k in _keys(v)]
    return []


def test_committed_cassettes_carry_no_sensitive_headers() -> None:
    files = sorted(CASSETTES.rglob("*.yaml"))
    assert files, "no committed cassettes found"
    for path in files:
        bad = [k for k in _keys(yaml.safe_load(path.read_text())) if _SENSITIVE.search(k)]
        assert not bad, f"{path.name}: sensitive header keys {bad}"


# ---------------------------------------------------------------------------
# Workflow steps executed locally under bash -eo pipefail
# ---------------------------------------------------------------------------


def _step(name: str) -> dict[str, Any]:
    wf = yaml.safe_load(WORKFLOW.read_text())
    for step in wf["jobs"]["drift"]["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"step {name!r} not found")


def _bash(script: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    full_env = {**os.environ, **env}
    return subprocess.run(
        ["bash", "-eo", "pipefail", "-c", script],
        cwd=cwd, env=full_env, capture_output=True, text=True, check=False,
    )


@pytest.mark.parametrize(
    ("setup", "expected_rc"),
    [("same", 0), ("drift", 1), ("empty", 2)],
)
def test_report_step_records_exit_code(tmp_path: Path, setup: str, expected_rc: int) -> None:
    runner_temp = tmp_path / "rt"
    (runner_temp / "vcr-baseline").mkdir(parents=True)
    work = tmp_path / "w"
    (work / "tests" / "fixtures" / "cassettes").mkdir(parents=True)
    (work / "scripts").mkdir()
    (work / "scripts" / "vcr_drift_report.py").write_text(
        (REPO / "scripts" / "vcr_drift_report.py").read_text()
    )
    venv = work / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "activate").write_text(f'export PATH="{Path(sys.executable).parent}:$PATH"\n')
    if setup != "empty":
        _write(runner_temp / "vcr-baseline" / "a.yaml", {"interactions": [_ix("ok")]})
        body = "ok" if setup == "same" else "changed"
        _write(work / "tests" / "fixtures" / "cassettes" / "a.yaml", {"interactions": [_ix(body)]})
    gh_out = tmp_path / "gh_output"
    script = _step("Generate drift report")["run"].replace("python ", f"{sys.executable} ")
    proc = _bash(script, work, {"RUNNER_TEMP": str(runner_temp), "GITHUB_OUTPUT": str(gh_out)})
    assert proc.returncode == expected_rc, proc.stderr
    assert gh_out.read_text().strip() == f"rc={expected_rc}"


_FAKE_GH = """#!/usr/bin/env bash
echo "$*" >> "$GH_LOG"
if [ "$1 $2" = "issue list" ]; then
  printf '%s' "$FAKE_OPEN_ISSUE"
fi
for a in "$@"; do
  if [ "$prev" = "--body-file" ]; then cp "$a" "$GH_BODY"; fi
  prev="$a"
done
"""


def _run_issue_step(
    tmp_path: Path, report_rc: str, report_text: str | None, open_issue: str = ""
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH)
    gh.chmod(0o755)
    # The issue step classifies its outcome with scripts/vcr_drift_report.py
    # on bare python3 (terms-analysis#92 r7); pin python3 to this interpreter.
    py = bindir / "python3"
    py.write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')
    py.chmod(0o755)
    work = tmp_path / "w"
    (work / "scripts").mkdir(parents=True)
    for name in ("vcr_drift_report.py", "vcr_issue_outcome.py"):
        (work / "scripts" / name).write_text((REPO / "scripts" / name).read_text())
    if report_text is not None:
        (work / "drift-report.md").write_text(report_text)
    runner_temp = tmp_path / "rt"
    runner_temp.mkdir()
    log, body = tmp_path / "gh.log", tmp_path / "body.md"
    proc = _bash(
        _step("Open issue on failure")["run"],
        work,
        {
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "GH_LOG": str(log),
            "GH_BODY": str(body),
            "FAKE_OPEN_ISSUE": open_issue,
            "RUNNER_TEMP": str(runner_temp),
            "RUN_URL": "https://github.example/run/7",
            "RUN_ID": "7",
            "REPORT_RC": report_rc,
        },
    )
    return proc, log.read_text() if log.exists() else "", body.read_text() if body.exists() else ""


def test_issue_step_drift_creates_issue_with_capped_body(tmp_path: Path) -> None:
    proc, log, body = _run_issue_step(tmp_path, "1", "# r\n" + "x" * 100_000)
    assert proc.returncode == 0, proc.stderr
    assert "label create corpus-drift" in log and "--force" in log
    create = [ln for ln in log.splitlines() if ln.startswith("issue create")]
    assert len(create) == 1 and "VCR drift detected" in create[0]
    assert len(body.encode()) < 65536
    assert "https://github.example/run/7" in body and "vcr-drift-report-7" in body


def test_issue_step_exit_2_is_titled_compared_zero(tmp_path: Path) -> None:
    proc, log, _body = _run_issue_step(tmp_path, "2", "Cassette pairs compared: 0")
    assert proc.returncode == 0, proc.stderr
    create = [ln for ln in log.splitlines() if ln.startswith("issue create")][0]
    assert "compared 0 cassettes" in create and "drift detected" not in create


def test_issue_step_without_report_is_canary_broken(tmp_path: Path) -> None:
    proc, log, body = _run_issue_step(tmp_path, "", None)
    assert proc.returncode == 0, proc.stderr
    assert "VCR drift canary broken" in log
    # Body text is the table row's why, not a fixed sentence (grumpy r7 #3).
    assert "The VCR drift canary failed: Report step never ran" in body


def test_issue_step_stale_report_without_rc_is_canary_broken(tmp_path: Path) -> None:
    # A report file with no recorded rc means the report step did not finish.
    proc, log, _body = _run_issue_step(tmp_path, "", "# stale")
    assert proc.returncode == 0, proc.stderr
    assert "VCR drift canary broken" in log


def test_issue_step_comments_on_existing_open_issue(tmp_path: Path) -> None:
    proc, log, body = _run_issue_step(tmp_path, "1", "# r\ndrift", open_issue="17")
    assert proc.returncode == 0, proc.stderr
    assert "issue create" not in log
    assert any(ln.startswith("issue comment 17 ") for ln in log.splitlines())
    assert body.startswith("**VCR drift detected")


def test_issue_step_label_failure_fails_step(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text("#!/usr/bin/env bash\necho 'HTTP 401' >&2\nexit 1\n")
    gh.chmod(0o755)
    proc = _bash(
        _step("Open issue on failure")["run"],
        tmp_path,
        {"PATH": f"{bindir}:{os.environ['PATH']}", "RUNNER_TEMP": str(tmp_path), "REPORT_RC": "1"},
    )
    assert proc.returncode != 0
    assert "HTTP 401" in proc.stderr
