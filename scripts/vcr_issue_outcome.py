"""vcr_issue_outcome.py - issue-step entry point for the canary outcome classifier.

Thin stdlib-only wrapper so the workflow's "Open issue on failure" step can
classify its outcome without invoking the report script by path (the report
step is the only step that runs the report itself). The table lives in ONE
place: ISSUE_OUTCOMES in scripts/vcr_drift_report.py (terms-analysis#92 r7).

Usage: python3 scripts/vcr_issue_outcome.py --rc=N --report-exists=0|1
           --date=YYYY-MM-DD
           [--field=title|key|labels|label_specs|attach_report|why]

Stdlib only (vcr_drift_report imports PyYAML lazily), and it puts its own
directory on sys.path, so it also runs under `python3 -I -S`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import vcr_drift_report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    """Forward to vcr_drift_report --classify-outcome."""
    args = sys.argv[1:] if argv is None else argv
    return vcr_drift_report.main(["--classify-outcome", *args])


if __name__ == "__main__":
    sys.exit(main())
