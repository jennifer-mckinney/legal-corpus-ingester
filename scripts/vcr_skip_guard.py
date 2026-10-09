#!/usr/bin/env python3
"""Fail the VCR drift canary when any re-record test was skipped (terms-analysis#92).

A skipped test records no cassette, so its endpoint was NOT checked this run.
pytest exits 0 for skips, so the exit code alone cannot catch that. The JUnit
XML is the robust signal: it counts <skipped> per testcase (pytest.skip,
skipif, xfail-as-skip and collection-time skips alike) and does not depend on
console formatting or `-rs` output parsing.

Exit codes: 0 = tests ran and none skipped, 1 = skipped tests found,
2 = JUnit file missing/unparseable or zero tests ran (cannot prove coverage).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from xml.etree import ElementTree as ET


def check(junit_path: Path) -> tuple[int, str]:
    """Return (exit_code, message) for a pytest JUnit XML file."""
    try:
        root = ET.parse(junit_path).getroot()  # noqa: S314 - our own CI artifact
    except (OSError, ET.ParseError) as exc:
        return 2, f"cannot read JUnit XML {junit_path}: {exc}"

    cases = list(root.iter("testcase"))
    if not cases:
        return 2, "no tests ran in the re-record step; nothing was checked"

    skipped = [
        f"{c.get('classname', '')}::{c.get('name', '')}"
        for c in cases
        if c.find("skipped") is not None
    ]
    if skipped:
        listing = "\n".join(f"  - {name}" for name in skipped)
        return 1, (
            f"{len(skipped)} test(s) skipped in the re-record run; their endpoints "
            f"were not checked:\n{listing}"
        )
    return 0, f"{len(cases)} test(s) ran, 0 skipped"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit_xml", type=Path)
    args = parser.parse_args(argv)
    code, message = check(args.junit_xml)
    print(message, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
