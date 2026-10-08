"""Single-source guard for the canary issue outcomes (terms-analysis#92 r7).

ISSUE_OUTCOMES in scripts/vcr_drift_report.py is the only place that maps the
report step's exit code + report presence to an issue title and labels. These
tests pin: the classifier CLI, that the workflow takes its title/labels from
it (no re-implemented branching, no stray title literals), and the issue step's
end-to-end behaviour under `bash -eo pipefail` with a fake gh.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "vcr_drift_report.py"
WORKFLOW = REPO / ".github" / "workflows" / "vcr-drift.yml"
sys.path.insert(0, str(REPO / "scripts"))

import vcr_drift_report as vdr  # noqa: E402

DAY = "2026-10-07"


def _classify(*args: str) -> subprocess.CompletedProcess[str]:
    # -I plus a stdlib-only path: proves the classifier needs no venv/PyYAML.
    return subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPT), "--classify-outcome", f"--date={DAY}", *args],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("rc", "exists", "key"),
    [
        ("1", "1", "drift"),
        ("2", "1", "nothing_compared"),
        ("1", "0", "broken"),
        ("2", "0", "broken"),
        ("0", "1", "broken"),
        ("", "0", "broken"),
        ("", "1", "broken"),
        ("127", "1", "broken"),
        ("garbage", "0", "broken"),
    ],
)
def test_classifier_cli_matches_table(rc: str, exists: str, key: str) -> None:
    proc = _classify(f"--rc={rc}", f"--report-exists={exists}", "--field=key")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == key
    outcome = vdr.classify_outcome(rc, exists == "1")
    assert outcome.key == key
    title = _classify(f"--rc={rc}", f"--report-exists={exists}").stdout.strip()
    assert title == outcome.title.format(date=DAY)
    labels = _classify(f"--rc={rc}", f"--report-exists={exists}", "--field=labels").stdout.strip()
    assert labels == ",".join(outcome.labels)
    why = _classify(f"--rc={rc}", f"--report-exists={exists}", "--field=why").stdout.strip()
    assert why == vdr.classify_why(rc, exists == "1") and why


def test_compared_nothing_never_shares_drift_title() -> None:
    zero = vdr.classify_outcome(str(vdr.EXIT_NOTHING_COMPARED), True)
    assert zero.title != vdr.OUTCOME_DRIFT.title
    assert "drift detected" not in zero.title


def test_print_outcome_table_covers_every_entry() -> None:
    table = vdr.render_outcome_table()
    # header + separator + one row per entry + the "any other" row
    assert len(table.splitlines()) == 2 + len(vdr.ISSUE_OUTCOMES) + 1
    for outcome, _why in vdr.ISSUE_OUTCOMES.values():
        assert outcome.title.format(date="YYYY-MM-DD") in table


def test_issue_wrapper_forwards_to_classifier() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(REPO / "scripts" / "vcr_issue_outcome.py"),
            "--rc=2",
            "--report-exists=1",
            f"--date={DAY}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == vdr.OUTCOME_NOTHING_COMPARED.title.format(date=DAY)


def test_report_mode_still_requires_dirs() -> None:
    proc = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 2 and "required" in proc.stderr


# ---------------------------------------------------------------------------
# Workflow: no re-implemented mapping
# ---------------------------------------------------------------------------


def _issue_run() -> str:
    wf = yaml.safe_load(WORKFLOW.read_text())
    return next(
        s["run"] for s in wf["jobs"]["drift"]["steps"] if s.get("name") == "Open issue on failure"
    )


_TITLE_RE = re.compile(r"VCR drift (?:detected|canary (?:broken|compared))[^\"\n]*")


def test_workflow_has_no_title_literals_outside_classifier() -> None:
    """Only one pinned literal may appear: the classifier-unavailable fallback,
    which must equal the table's broken title (grumpy r7 #1 removed the dead
    h4 self-check literal)."""
    text = WORKFLOW.read_text()
    found = _TITLE_RE.findall(text)
    assert found == [vdr.OUTCOME_BROKEN.title.format(date="$day")], (
        f"stray issue-title literals in workflow: {found}"
    )
    run = _issue_run()
    assert "expected_zero_title" not in run
    assert "self-check" not in run
    assert f'title="{vdr.OUTCOME_BROKEN.title.format(date="$day")}"' in run
    assert f'labels="{",".join(vdr.OUTCOME_BROKEN.labels)}"' in run
    # The title is assigned from the classifier, not chosen by rc branches.
    assert re.search(r'title=\$\(classify "\$\{outcome_args\[@\]\}"\)', run)
    assert "scripts/vcr_issue_outcome.py" in run
    assert not re.search(r'REPORT_RC"?\s*=\s*"?\d', run), "workflow branches on REPORT_RC itself"


def test_every_outcome_label_has_a_spec_and_carries_the_dedupe_label() -> None:
    """Label parity (grumpy r7 #2): the step creates exactly the classifier's
    labels, so every label any outcome can return needs a colour/description,
    and the dedupe search label must be on every outcome or an open alert of
    another class would be missed and duplicated."""
    outcomes = [o for o, _why in vdr.ISSUE_OUTCOMES.values()] + [vdr.DEFAULT_OUTCOME]
    for outcome in outcomes:
        assert set(outcome.labels) <= set(vdr.LABEL_SPECS), outcome
        assert vdr.DEDUPE_LABEL in outcome.labels, outcome
        assert vdr.label_specs(outcome).splitlines() == [
            f"{n}\t{vdr.LABEL_SPECS[n][0]}\t{vdr.LABEL_SPECS[n][1]}" for n in outcome.labels
        ]


def test_workflow_takes_label_names_from_classifier() -> None:
    """No hard-coded label names outside the pinned classifier-failed fallback."""
    run = _issue_run()
    assert not re.search(r"gh label create\s+[\"']?[A-Za-z]", run), "literal gh label create"
    assert re.search(r'gh label create "\$name"', run)
    assert re.search(r"label_specs=\$\(classify .*--field=label_specs\)", run)
    # The one dedupe literal must be the table's DEDUPE_LABEL.
    dedupe = re.findall(r'gh issue list --label "([^"]+)"', run)
    assert dedupe == [vdr.DEDUPE_LABEL], dedupe
    names = set(vdr.LABEL_SPECS) | {vdr.DEDUPE_LABEL}
    allowed = ("labels=", "gh issue list --label", "issue #$open_issue already open")
    stray = [
        ln for ln in run.splitlines()
        if any(n in ln for n in names) and not any(a in ln for a in allowed)
    ]
    assert not stray, f"hard-coded label names in issue step: {stray}"


def test_workflow_body_text_comes_from_classifier_why() -> None:
    run = _issue_run()
    assert re.search(r"why=\$\(classify .*--field=why\)", run)
    assert "failed before producing a drift report" not in run


def test_issue_step_prefers_venv_python() -> None:
    run = _issue_run()
    assert re.search(r"\.venv/bin/python -I", run)
    assert re.search(r'"\$py" -I scripts/vcr_issue_outcome\.py', run)


def test_workflow_comments_do_not_restate_mapping() -> None:
    comments = [ln for ln in WORKFLOW.read_text().splitlines() if ln.lstrip().startswith("#")]
    restated = [
        c
        for c in comments
        if re.search(r"\b[012]\s*(=|\+|\()\s*\w*\s*(drift|compared|broken)", c, re.I)
        or re.search(r"drift \(1\)|\(2\)", c)
    ]
    assert not restated, f"workflow comments restate the outcome mapping: {restated}"


# ---------------------------------------------------------------------------
# Issue step simulated under bash -eo pipefail with a fake gh
# ---------------------------------------------------------------------------

_FAKE_GH = """#!/usr/bin/env bash
echo "$*" >> "$GH_LOG"
if [ "$1 $2" = "issue list" ]; then printf '%s' "${FAKE_OPEN_ISSUE:-}"; fi
"""


_GOOD_PY = '#!/usr/bin/env bash\necho "{tag}" >> "$PY_LOG"\nexec "{exe}" "$@"\n'
# Models a python3 that cannot run at all, e.g. macOS /usr/bin/python3 before
# the Xcode licence is accepted: it prints an error and exits non-zero.
_BROKEN_PY = '#!/usr/bin/env bash\necho "{tag}" >> "$PY_LOG"\necho "license not agreed" >&2\nexit 69\n'


def _shim(path: Path, template: str, tag: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template.format(tag=tag, exe=sys.executable))
    path.chmod(0o755)


def _run_step(
    tmp_path: Path,
    rc: str,
    report: str | None,
    with_script: bool = True,
    venv: str | None = None,
    python3: str = _GOOD_PY,
    script_patch: tuple[str, str] | None = None,
) -> tuple[int, str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(_FAKE_GH)
    (bindir / "gh").chmod(0o755)
    _shim(bindir / "python3", python3, "python3")
    work = tmp_path / "w"
    (work / "scripts").mkdir(parents=True)
    if venv is not None:
        _shim(work / ".venv" / "bin" / "python", venv, "venv")
    if with_script:
        for name in ("vcr_drift_report.py", "vcr_issue_outcome.py"):
            text = (REPO / "scripts" / name).read_text()
            if script_patch and name == "vcr_drift_report.py":
                assert script_patch[0] in text
                text = text.replace(script_patch[0], script_patch[1], 1)
            (work / "scripts" / name).write_text(text)
    if report is not None:
        (work / "drift-report.md").write_text(report)
    log = tmp_path / "gh.log"
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GH_LOG": str(log),
        "PY_LOG": str(tmp_path / "py.log"),
        "RUNNER_TEMP": str(tmp_path),
        "RUN_URL": "https://github.example/run/9",
        "RUN_ID": "9",
        "REPORT_RC": rc,
    }
    proc = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", _issue_run()],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, log.read_text() if log.exists() else "", proc.stderr


@pytest.mark.parametrize(
    ("rc", "report", "key"),
    [
        ("0", "# clean", "broken"),
        ("1", "# drift", "drift"),
        ("1", None, "broken"),
        ("2", "compared: 0", "nothing_compared"),
        ("", None, "broken"),
    ],
)
def test_issue_step_titles_from_classifier(
    tmp_path: Path, rc: str, report: str | None, key: str
) -> None:
    code, log, stderr = _run_step(tmp_path, rc, report)
    assert code == 0, stderr
    assert "::error::" not in stderr, stderr
    create = [ln for ln in log.splitlines() if ln.startswith("issue create")]
    assert len(create) == 1, log
    outcome = vdr.classify_outcome(rc, report is not None)
    assert outcome.key == key
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert f"--title {outcome.title.format(date=day)} " in create[0]
    assert create[0].endswith(f"--label {','.join(outcome.labels)}")
    # Labels created are exactly the outcome's, with the table's colour/description.
    made = [ln for ln in log.splitlines() if ln.startswith("label create")]
    assert made == [
        f"label create {n} --force --color {vdr.LABEL_SPECS[n][0]} "
        f"--description {vdr.LABEL_SPECS[n][1]}"
        for n in outcome.labels
    ]
    assert f"issue list --label {vdr.DEDUPE_LABEL} " in log
    # Body text (grumpy r7 #3): the report when attached, else the row's why.
    body = (tmp_path / "vcr-issue-body.md").read_text()
    if outcome.attach_report:
        assert body.startswith(report or "")
    else:
        assert vdr.classify_why(rc, report is not None) in body
        assert "before producing a drift report" not in body


def test_new_table_label_is_created_before_issue_create(tmp_path: Path) -> None:
    """A label added only to the table is created by the step, so the canary
    cannot go silent on a missing label (grumpy r7 #2)."""
    patch = (
        '    "needs-review": ("FBCA04", "Needs human review"),\n}',
        '    "needs-review": ("FBCA04", "Needs human review"),\n'
        '    "canary-new": ("000000", "New label"),\n}',
    )
    labels_patch = (
        '_ISSUE_LABELS = ("corpus-drift", "needs-review")',
        '_ISSUE_LABELS = ("corpus-drift", "needs-review", "canary-new")',
    )
    src = (REPO / "scripts" / "vcr_drift_report.py").read_text()
    assert patch[0] in src and labels_patch[0] in src
    patched = src.replace(patch[0], patch[1], 1).replace(labels_patch[0], labels_patch[1], 1)
    code, log, stderr = _run_step(tmp_path, "1", "# drift", script_patch=(src, patched))
    assert code == 0, stderr
    lines = log.splitlines()
    made = [i for i, ln in enumerate(lines) if ln.startswith("label create canary-new ")]
    create = [i for i, ln in enumerate(lines) if ln.startswith("issue create")]
    assert made and create and made[0] < create[0], log
    assert lines[create[0]].endswith("--label corpus-drift,needs-review,canary-new")


def test_issue_step_still_alerts_when_classifier_missing(tmp_path: Path) -> None:
    code, log, stderr = _run_step(tmp_path, "1", "# drift", with_script=False)
    assert code == 0, stderr
    assert "outcome classifier failed" in stderr
    create = [ln for ln in log.splitlines() if ln.startswith("issue create")]
    assert len(create) == 1 and "VCR drift canary broken" in create[0]
    assert create[0].endswith("--label corpus-drift,needs-review")
    made = [ln.split()[2] for ln in log.splitlines() if ln.startswith("label create")]
    assert made == list(vdr.OUTCOME_BROKEN.labels)
    body = (tmp_path / "vcr-issue-body.md").read_text()
    assert "classifier could not run" in body


# ---------------------------------------------------------------------------
# Interpreter selection (fix r8 item 4)
# ---------------------------------------------------------------------------


def test_issue_step_uses_venv_python_when_it_runs(tmp_path: Path) -> None:
    code, log, stderr = _run_step(tmp_path, "2", "compared: 0", venv=_GOOD_PY)
    assert code == 0, stderr
    used = set((tmp_path / "py.log").read_text().split())
    assert used == {"venv"}, used
    assert vdr.OUTCOME_NOTHING_COMPARED.title.format(date="") in log


def test_issue_step_falls_back_to_python3_when_venv_broken(tmp_path: Path) -> None:
    code, log, stderr = _run_step(tmp_path, "1", "# drift", venv=_BROKEN_PY)
    assert code == 0, stderr
    assert "::error::" not in stderr, stderr
    assert "python3" in (tmp_path / "py.log").read_text().split()
    assert vdr.OUTCOME_DRIFT.title.format(date="") in log


def test_unrunnable_python3_gives_broken_for_every_rc(tmp_path: Path) -> None:
    """Root cause of the r8 observation: a python3 that cannot start (macOS
    /usr/bin/python3 stub before the Xcode licence is accepted) makes every
    classify call fail, so every rc gets the fallback `broken` title. The
    step still alerts and says why on stderr."""
    code, log, stderr = _run_step(tmp_path, "1", "# drift", python3=_BROKEN_PY)
    assert code == 0, stderr
    assert "outcome classifier failed under python3" in stderr
    assert vdr.OUTCOME_BROKEN.title.format(date="") in log


def _isolated_pythons() -> list[str]:
    exes = [sys.executable]
    base = Path(sys.base_prefix) / "bin" / "python3"
    if base.exists() and str(base) != sys.executable:
        exes.append(str(base))
    return exes


@pytest.mark.parametrize("exe", _isolated_pythons())
@pytest.mark.parametrize("flags", [("-I",), ("-I", "-S")])
@pytest.mark.parametrize(
    ("rc", "exists", "outcome"),
    [
        ("0", "1", vdr.OUTCOME_BROKEN),
        ("1", "1", vdr.OUTCOME_DRIFT),
        ("1", "0", vdr.OUTCOME_BROKEN),
        ("2", "1", vdr.OUTCOME_NOTHING_COMPARED),
        ("", "0", vdr.OUTCOME_BROKEN),
    ],
)
def test_wrapper_titles_under_isolated_python(
    tmp_path: Path, exe: str, flags: tuple[str, ...], rc: str, exists: str, outcome: object
) -> None:
    """The wrapper is stdlib-only: isolated mode (and no site-packages) on both
    the venv and the base interpreter, run from an unrelated cwd, still gives
    the table's title for each rc."""
    assert isinstance(outcome, vdr.IssueOutcome)
    proc = subprocess.run(
        [exe, *flags, str(REPO / "scripts" / "vcr_issue_outcome.py"),
         f"--rc={rc}", f"--report-exists={exists}", f"--date={DAY}"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == outcome.title.format(date=DAY)
