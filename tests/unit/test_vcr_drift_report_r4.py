"""Round-4 review fixes for the VCR drift canary (terms-analysis#92)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import vcr_drift_report as vdr  # noqa: E402
import vcr_skip_guard as guard  # noqa: E402

_JUNIT = '<testsuites><testsuite name="p">{cases}</testsuite></testsuites>'


def _write(tmp_path: Path, cases: str) -> Path:
    p = tmp_path / "junit.xml"
    p.write_text(_JUNIT.format(cases=cases), encoding="utf-8")
    return p


def test_baseline_only_labels_say_not_re_recorded_not_removed(tmp_path: Path) -> None:
    base, cur = tmp_path / "b", tmp_path / "c"
    (base / "eurlex").mkdir(parents=True)
    cur.mkdir()
    (base / "eurlex" / "t.yaml").write_text("interactions: []\n", encoding="utf-8")
    out = tmp_path / "r.md"
    vdr.main(["--baseline", str(base), "--current", str(cur), "--output", str(out)])
    text = out.read_text(encoding="utf-8")
    assert "endpoint removed" not in text and "may be removed" not in text
    assert text.count("not re-recorded this run") == 2


def test_guard_passes_when_nothing_skipped(tmp_path: Path) -> None:
    code, msg = guard.check(_write(tmp_path, '<testcase classname="a" name="t1"/>'))
    assert code == 0 and "0 skipped" in msg


def test_guard_fails_and_names_skipped_test(tmp_path: Path) -> None:
    cases = '<testcase classname="a" name="t1"/><testcase classname="a" name="t2"><skipped/></testcase>'
    code, msg = guard.check(_write(tmp_path, cases))
    assert code == 1 and "a::t2" in msg


def test_guard_fails_on_zero_tests_and_bad_file(tmp_path: Path) -> None:
    assert guard.check(_write(tmp_path, ""))[0] == 2
    assert guard.check(tmp_path / "missing.xml")[0] == 2
    bad = tmp_path / "bad.xml"
    bad.write_text("<not xml", encoding="utf-8")
    assert guard.check(bad)[0] == 2


def test_guard_cli_exit_code(tmp_path: Path) -> None:
    junit = _write(tmp_path, '<testcase classname="a" name="t"><skipped/></testcase>')
    rc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "vcr_skip_guard.py"), str(junit)],
        capture_output=True,
    ).returncode
    assert rc == 1


def test_workflow_wires_junit_and_guard_after_rerecord() -> None:
    wf = yaml.safe_load((REPO / ".github" / "workflows" / "vcr-drift.yml").read_text())
    steps = wf["jobs"]["drift"]["steps"]
    runs = [s.get("run", "") for s in steps]
    rec = next(i for i, r in enumerate(runs) if "--record-mode=rewrite" in r)
    grd = next(i for i, r in enumerate(runs) if "vcr_skip_guard.py" in r)
    rep = next(i for i, r in enumerate(runs) if "vcr_drift_report.py" in r)
    assert "--junitxml" in runs[rec]
    assert rec < grd < rep
    # same junit path in both steps
    assert "vcr-rerecord-junit.xml" in runs[rec] and "vcr-rerecord-junit.xml" in runs[grd]
