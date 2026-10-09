#!/usr/bin/env python3
"""P9 review gate (terms-analysis#191).

Run as the last step of each job in .github/workflows/p9-review.yml, after
the reviewer has written p9-verdict.json. The file must be exactly the
verdict contract. The job then fails on any finding whose severity is in
BLOCKING_SEVERITIES (CRITICAL, HIGH, MEDIUM; owner decision 2026-10-09) and
passes when every finding is LOW or NIT. Non-blocking findings are still
printed, marked "non-blocking", so they can be filed as cards.

Contract (written by the reviewer, see .github/p9/*.md):
    {"verdict": "PASS" | "FAIL",
     "findings": [{"severity": ..., "title": ..., "file": ..., "line": ...}]}
Key sets are exact. severity is one of SEVERITIES (exact spelling), title and
file are non-blank strings, line is a non-negative int (bool is refused).

Exit codes:
    0  PASS with zero findings (prints "P9 verdict: PASS, 0 findings"), or
       only non-blocking findings (prints "P9 verdict: <verdict>, <n>
       finding(s), 0 blocking" and one line per finding, to stdout)
    1  any blocking finding, or FAIL with no findings listed (to stderr)
    2  the file is missing, unreadable, not JSON, or off the contract

Standard library only, so the step needs nothing installed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

EXIT_PASS = 0
EXIT_REJECTED = 1
EXIT_INVALID = 2

VERDICTS = frozenset({"PASS", "FAIL"})
DOC_FIELDS = ("verdict", "findings")
FINDING_FIELDS = ("severity", "title", "file", "line")
# Union of the tags the vendored briefs allow: the grumpy brief adds NIT, the
# security brief stops at LOW. The test suite reads the briefs and checks
# every tag listed there is accepted here.
SEVERITIES = frozenset({"CRITICAL", "HIGH", "MEDIUM", "LOW", "NIT"})
# The only severities that fail the job (owner decision 2026-10-09: block on
# what matters for correctness, security or acceptance). Every other tag in
# SEVERITIES passes and is printed as non-blocking.
BLOCKING_SEVERITIES = frozenset({"CRITICAL", "HIGH", "MEDIUM"})
# Finding text comes from a model that read untrusted PR content; it is shown
# in the Actions log one finding per line, so each field is cut to this size.
MAX_FIELD_CHARS = 200


class InvalidVerdict(Exception):
    """The verdict file cannot be trusted as a reviewer decision."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [key for key, _ in pairs]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise InvalidVerdict(f"duplicate key(s): {', '.join(_clean(k) for k in duplicates)}")
    return dict(pairs)


def _clean(value: object) -> str:
    """One printable line: control, format and separator characters become '?'."""
    text = "".join(ch if ch.isprintable() else "?" for ch in str(value))
    return text[:MAX_FIELD_CHARS]


def _is_text(value: object) -> bool:
    """A non-blank string: whitespace-only counts as blank."""
    return isinstance(value, str) and bool(value.strip())


def _is_valid_finding(item: object) -> bool:
    """Exact finding key set, and every value inside the contract."""
    if not isinstance(item, dict) or set(item) != set(FINDING_FIELDS):
        return False
    line = item["line"]
    return (
        isinstance(item["severity"], str)
        and item["severity"] in SEVERITIES
        and _is_text(item["title"])
        and _is_text(item["file"])
        # bool is a subclass of int, so it is refused explicitly.
        and isinstance(line, int)
        and not isinstance(line, bool)
        and line >= 0
    )


def load(path: Path) -> dict[str, object]:
    """Read and validate the verdict file, or raise InvalidVerdict."""
    if not path.is_file():
        raise InvalidVerdict(f"{path.name} was not written by the reviewer")
    try:
        doc = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise InvalidVerdict(f"{path.name} is not valid JSON ({type(exc).__name__})") from None
    # Exact key sets: the document and every finding carry the contract keys
    # and nothing else, so an unknown or missing key fails closed. Finding
    # values are checked too, so an off-contract value exits 2, never 1.
    is_doc = isinstance(doc, dict) and set(doc) == set(DOC_FIELDS)
    verdict = doc.get("verdict") if is_doc else None
    findings = doc.get("findings") if is_doc else None
    if (
        not isinstance(verdict, str)
        or verdict not in VERDICTS
        or not isinstance(findings, list)
        or not all(_is_valid_finding(item) for item in findings)
    ):
        raise InvalidVerdict(
            f"{path.name} does not match the verdict contract "
            '{"verdict": "PASS"|"FAIL", "findings": [{...}]}'
        )
    return doc


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_verdict.py <p9-verdict.json>", file=sys.stderr)
        return EXIT_INVALID
    try:
        doc = load(Path(argv[1]))
    except InvalidVerdict as exc:
        print(f"P9 verdict: invalid: {exc}", file=sys.stderr)
        return EXIT_INVALID
    verdict = doc["verdict"]
    findings = doc["findings"]
    if verdict == "PASS" and not findings:
        print("P9 verdict: PASS, 0 findings")
        return EXIT_PASS
    blocking = sum(item["severity"] in BLOCKING_SEVERITIES for item in findings)
    header = f"P9 verdict: {verdict}, {len(findings)} finding(s), {blocking} blocking"
    # A FAIL that names nothing gives no reason to pass: fail closed.
    rejected = blocking > 0 or not findings
    if not findings:
        header += "; a FAIL verdict must list its findings"
    stream = sys.stderr if rejected else sys.stdout
    print(header, file=stream)
    for item in findings:
        severity, title, file, line = (_clean(item[key]) for key in FINDING_FIELDS)
        mark = "blocking" if item["severity"] in BLOCKING_SEVERITIES else "non-blocking"
        print(f"  - [{severity}] {title} ({file}:{line}) {mark}", file=stream)
    return EXIT_REJECTED if rejected else EXIT_PASS


if __name__ == "__main__":
    sys.exit(main(sys.argv))
