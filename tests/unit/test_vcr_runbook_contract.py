"""Runbook drift guard for the VCR Drift Canary (terms-analysis#92, G0-3).

Kept separate from test_vcr_canary_contract.py, which is the independent
acceptance file owned by the test agent.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


# Runbook drift guard (terms-analysis#92 round 4 LOW): the operator doc must
# keep matching the workflow it describes.
def test_runbook_matches_current_canary() -> None:
    doc = (Path(__file__).resolve().parents[2] / "automations" / "vcr-drift.md").read_text()
    for banned in ("--vcr-record", "record-mode=all", "Endpoint may be gone"):
        assert banned not in doc, f"runbook still contains stale text: {banned!r}"
    assert "--record-mode=rewrite" in doc
    assert "vcr_skip_guard.py" in doc


# Grumpy r5 LOW (terms-analysis#92), made structural in r7: the runbook's
# issue-outcome table is the generated output of ISSUE_OUTCOMES, so it cannot
# drift from the classifier the workflow calls.
_BEGIN = "<!-- BEGIN outcome-table"
_END = "<!-- END outcome-table -->"


def test_runbook_outcome_table_is_generated_from_issue_outcomes() -> None:
    root = Path(__file__).resolve().parents[2]
    doc = (root / "automations" / "vcr-drift.md").read_text()
    assert doc.count(_BEGIN) == 1 and doc.count(_END) == 1, "outcome-table markers missing or duplicated"
    after_begin = doc.split(_BEGIN, 1)[1]
    block = after_begin.split("\n", 1)[1].split(_END, 1)[0]
    generated = subprocess.run(
        [sys.executable, str(root / "scripts" / "vcr_drift_report.py"), "--print-outcome-table"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert block == generated, (
        "automations/vcr-drift.md outcome table differs from --print-outcome-table; "
        "regenerate it with `python3 scripts/vcr_drift_report.py --print-outcome-table`"
    )
    # Outside the generated block the runbook must not hand-write issue titles.
    outside = doc.replace(_BEGIN + after_begin.split(_END, 1)[0] + _END, "")
    assert not re.search(r"VCR drift (detected|canary (broken|compared))", outside), (
        "runbook restates an issue title outside the generated block"
    )
