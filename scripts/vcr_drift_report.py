"""vcr_drift_report.py - Compare committed VCR cassettes against re-recorded cassettes.

Walks two cassette directories, diffs interactions, and produces a markdown
drift report. Exits 0 when no cassettes changed, non-zero when any drift is
detected.

Exit codes:
    0  at least one cassette pair was compared and none drifted
    1  drift detected, or a setup/IO error
    2  zero cassette pairs were compared (empty dirs or no overlapping files);
       a canary that compared nothing must never report green (issue #92)

Report bounds (terms-analysis#92 security F1/F2): the report is used verbatim
as a GitHub issue body, which GitHub rejects above 65536 characters, and it
quotes third-party response bytes. So every snippet line, every snippet and
the whole report are byte-capped (with an explicit truncation marker pointing
at the run and its artifact), and all upstream-derived text is rendered inside
code fences / inline code spans that the content cannot break out of.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

# PyYAML is imported lazily in load_cassette so the outcome classifier
# (--classify-outcome / --print-outcome-table) runs on a bare stdlib python3:
# the workflow's issue step must still title an alert when the venv install
# is what broke (terms-analysis#92 r7).


# ---------------------------------------------------------------------------
# Report bounds (security F1)
# ---------------------------------------------------------------------------

# Max characters kept from one unified-diff line before it is cut.
MAX_SNIPPET_LINE_CHARS = 200
# Max UTF-8 bytes of one interaction's diff snippet (all lines together).
MAX_SNIPPET_BYTES = 2000
# Max diff lines kept per snippet.
MAX_SNIPPET_LINES = 10
# Max UTF-8 bytes of the whole report; well below GitHub's 65536-char issue
# body limit so the workflow can append a trailer and still fit.
MAX_REPORT_BYTES = 60000
# Max characters of a parse-error message quoted in the report.
MAX_ERROR_CHARS = 300

TRUNCATION_MARKER = "**Report truncated**"


# ---------------------------------------------------------------------------
# Markdown containment helpers (security F2)
# ---------------------------------------------------------------------------


def _longest_run(text: str, char: str) -> int:
    """Length of the longest run of consecutive `char` in text."""
    return max((len(m) for m in re.findall(re.escape(char) + "+", text)), default=0)


def _fence(text: str, info: str = "") -> str:
    """Wrap text in a backtick code fence the content cannot close early.

    The fence is one backtick longer than the longest backtick run inside the
    content (minimum 3), per CommonMark, so upstream bytes such as `@user`,
    `#1`, links, images, HTML or a stray triple backtick stay literal.
    """
    fence = "`" * max(3, _longest_run(text, "`") + 1)
    return f"{fence}{info}\n{text}\n{fence}"


def _code(value: object) -> str:
    """Render value as a single-line inline code span it cannot break out of."""
    text = " ".join(str(value).split())  # collapse newlines: spans are one line
    ticks = "`" * (_longest_run(text, "`") + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"


def _clip(text: str, limit: int) -> str:
    """Cut text to limit characters, marking the cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + f" ...[truncated {len(text) - limit} chars]"


def _diff_snippet(old: str, new: str) -> tuple[str, bool]:
    """Bounded unified-diff snippet of two bodies (line, line-count, byte caps).

    Returns (snippet, truncated).
    """
    # splitlines() without keepends + lineterm="" keeps one diff entry per
    # line; joining keepends lines with lineterm="" merged them into one line.
    diff_lines = list(
        difflib.unified_diff(old.splitlines(), new.splitlines(), n=1, lineterm="")
    )
    if not diff_lines:
        return "(binary or empty diff)", False
    kept: list[str] = []
    used = 0
    truncated = False
    for line in diff_lines[:MAX_SNIPPET_LINES]:
        clipped = _clip(line, MAX_SNIPPET_LINE_CHARS)
        truncated = truncated or clipped != line
        cost = len(clipped.encode("utf-8")) + 1
        if used + cost > MAX_SNIPPET_BYTES:
            kept.append("...[snippet truncated]")
            return "\n".join(kept), True
        kept.append(clipped)
        used += cost
    if len(diff_lines) > MAX_SNIPPET_LINES:
        kept.append(f"...[truncated {len(diff_lines) - MAX_SNIPPET_LINES} more diff lines]")
        truncated = True
    return "\n".join(kept), truncated


def _bounded_join(blocks: list[str], max_bytes: int) -> tuple[str, bool]:
    """Join report blocks with newlines, dropping trailing blocks over budget.

    Blocks are kept whole (a fenced snippet is one block), so a cut never
    leaves a fence open. Returns (text, truncated).
    """
    out: list[str] = []
    used = 0
    for block in blocks:
        cost = len(block.encode("utf-8")) + 1
        if used + cost > max_bytes:
            return "\n".join(out), True
        out.append(block)
        used += cost
    return "\n".join(out), False


def _truncation_trailer(summary: str) -> str:
    """Marker naming where the full detail lives (run URL + artifact)."""
    run_url = os.environ.get("RUN_URL", "").strip()
    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    where = f"workflow run {run_url}" if run_url else "the workflow run"
    # Name the artifact only when the real run id is known; never print a
    # literal placeholder (Critic r2 LOW).
    named = f" {_code(f'vcr-drift-report-{run_id}')}" if run_id else ""
    return (
        f"\n---\n{TRUNCATION_MARKER}: {summary}."
        f" The re-recorded cassettes and this report are in the artifact"
        f"{named} of {where}."
    )


# ---------------------------------------------------------------------------
# Cassette loading
# ---------------------------------------------------------------------------


def load_cassette(path: Path) -> tuple[Any, str | None]:
    """Load a YAML cassette file.

    Returns (data, None) on success or (None, error_message) on failure.
    """
    import yaml  # lazy: see the import-block note

    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if data is None:
            return None, "Empty or whitespace-only YAML file"
        return data, None
    except yaml.YAMLError as exc:
        return None, f"YAML parse error: {exc}"
    except OSError as exc:
        return None, f"IO error: {exc}"


# ---------------------------------------------------------------------------
# Interaction diffing
# ---------------------------------------------------------------------------


def _interactions(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the interactions list from a cassette, or an empty list."""
    if not isinstance(data, dict):
        return []
    interactions = data.get("interactions")
    if not isinstance(interactions, list):
        return []
    return interactions


def _interactions_error(data: Any) -> str | None:
    """Why a loaded cassette has nothing to compare, or None if it does.

    A pair of cassettes with no interactions would otherwise "compare equal"
    and count as compared, letting the canary go green having compared zero
    HTTP interactions (grumpy #2).
    """
    if not isinstance(data, dict):
        return f"cassette is not a mapping (got {type(data).__name__})"
    if "interactions" not in data:
        return "cassette has no 'interactions' key"
    interactions = data["interactions"]
    if not isinstance(interactions, list):
        return f"'interactions' is not a list (got {type(interactions).__name__})"
    if not interactions:
        return "no interactions in cassette"
    # diff_cassette() calls .get() on each interaction, its request/response
    # and the response status, so any of those that is present but not a
    # mapping is a parse error here, never an AttributeError that crashes the
    # whole report (Copilot PR #25). Absent or null request/response keep the
    # existing tolerant comparison; a present status must be a mapping.
    for idx, ix in enumerate(interactions):
        if not isinstance(ix, dict):
            return f"interaction {idx} is not a mapping (got {type(ix).__name__})"
        for part in ("request", "response"):
            value = ix.get(part)
            if value is not None and not isinstance(value, dict):
                return f"interaction {idx} {part} is not a mapping (got {type(value).__name__})"
        response = ix.get("response")
        if isinstance(response, dict) and "status" in response:
            status = response["status"]
            if not isinstance(status, dict):
                return (
                    f"interaction {idx} response status is not a mapping"
                    f" (got {type(status).__name__})"
                )
    return None


def _body_text(response: Any) -> str:
    """Response body as text; tolerates a bare-string or odd-typed body."""
    body = (response or {}).get("body", "") if isinstance(response, dict) else ""
    if isinstance(body, dict):
        return str(body.get("string", ""))
    return "" if body is None else str(body)


def diff_cassette(
    baseline_data: dict[str, Any],
    current_data: dict[str, Any],
) -> dict[str, Any]:
    """Compare two cassette data dicts.

    Returns a summary dict with keys:
        changed (bool)
        uri_drift (int)
        status_drift (int)
        body_drift (int)
        method_drift (int)
        interaction_count_baseline (int)
        interaction_count_current (int)
        details (list[str])
    """
    baseline_ix = _interactions(baseline_data)
    current_ix = _interactions(current_data)

    uri_drift = 0
    status_drift = 0
    body_drift = 0
    method_drift = 0
    truncated = False
    details: list[str] = []

    # Compare interaction by interaction (zip stops at shorter list)
    for idx, (b_ix, c_ix) in enumerate(zip(baseline_ix, current_ix)):
        b_uri = (b_ix.get("request") or {}).get("uri", "")
        c_uri = (c_ix.get("request") or {}).get("uri", "")
        if b_uri != c_uri:
            uri_drift += 1
            details.append(
                f"- interaction {idx}: URI changed"
                f" from {_code(_clip(repr(b_uri), MAX_SNIPPET_LINE_CHARS))}"
                f" to {_code(_clip(repr(c_uri), MAX_SNIPPET_LINE_CHARS))}"
            )

        b_body = _body_text(b_ix.get("response"))
        c_body = _body_text(c_ix.get("response"))
        if b_body != c_body:
            body_drift += 1
            # Bounded snippet, fenced so upstream bytes render literally.
            snippet, cut = _diff_snippet(b_body, c_body)
            truncated = truncated or cut
            details.append(
                f"- interaction {idx}: body changed"
                f" ({len(b_body)} bytes -> {len(c_body)} bytes), diff snippet:\n\n"
                + _fence(snippet, "diff")
                + "\n"
            )

        # Status changes are their own drift class, not URI drift (grumpy #6).
        b_status = (b_ix.get("response") or {}).get("status", {}).get("code")
        c_status = (c_ix.get("response") or {}).get("status", {}).get("code")
        if b_status != c_status:
            status_drift += 1
            details.append(
                f"- interaction {idx}: status code changed"
                f" from {_code(b_status)} to {_code(c_status)}"
            )

        b_method = (b_ix.get("request") or {}).get("method", "")
        c_method = (c_ix.get("request") or {}).get("method", "")
        if b_method != c_method:
            method_drift += 1
            details.append(
                f"- interaction {idx}: method changed"
                f" from {_code(_clip(repr(b_method), MAX_SNIPPET_LINE_CHARS))}"
                f" to {_code(_clip(repr(c_method), MAX_SNIPPET_LINE_CHARS))}"
            )

    # Interaction count mismatch
    if len(baseline_ix) != len(current_ix):
        details.append(
            f"- interaction count changed:"
            f" {len(baseline_ix)} -> {len(current_ix)}"
        )

    changed = bool(
        uri_drift
        or status_drift
        or body_drift
        or method_drift
        or len(baseline_ix) != len(current_ix)
    )

    return {
        "changed": changed,
        "uri_drift": uri_drift,
        "status_drift": status_drift,
        "body_drift": body_drift,
        "method_drift": method_drift,
        "interaction_count_baseline": len(baseline_ix),
        "interaction_count_current": len(current_ix),
        "details": details,
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# Directory walking
# ---------------------------------------------------------------------------


def collect_cassettes(root: Path) -> dict[str, Path]:
    """Recursively collect all *.yaml files under root.

    Returns a dict mapping relative path string to absolute Path.
    """
    result: dict[str, Path] = {}
    for dirpath, _dirs, files in os.walk(root):
        for fname in files:
            if fname.endswith(".yaml"):
                full = Path(dirpath) / fname
                rel = str(full.relative_to(root))
                result[rel] = full
    return result


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


# Exit code when no cassette pair was actually compared (distinct from drift).
EXIT_NOTHING_COMPARED = 2


# ---------------------------------------------------------------------------
# Issue outcomes (terms-analysis#92 r7): the ONE source of truth for how the
# workflow's "Open issue on failure" step titles and labels an alert. The
# workflow calls --classify-outcome; automations/vcr-drift.md embeds the
# --print-outcome-table output; tests/unit/test_vcr_outcome_table.py fails CI
# if either drifts from this table.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssueOutcome:
    """How one class of canary failure is reported as a GitHub issue."""

    key: str
    title: str  # str.format template; {date} is the UTC run date
    labels: tuple[str, ...]
    attach_report: bool  # issue body is drift-report.md (else a failed-run note)


# Every label an outcome can carry, with the colour/description the issue step
# passes to `gh label create --force`. The issue step creates exactly the
# labels the classifier returns (--field=label_specs), so adding a label here
# or to an outcome can never leave `gh issue create` pointing at a missing
# label (grumpy r7 #2). tests/unit/test_vcr_outcome_table.py pins that every
# outcome label has a spec.
LABEL_SPECS: dict[str, tuple[str, str]] = {
    "corpus-drift": ("B60205", "VCR drift canary"),
    "needs-review": ("FBCA04", "Needs human review"),
}
# The label the issue step's dedupe lookup searches (`gh issue list --label`)
# for an already-open alert to comment on instead of opening a duplicate. It
# must be on every outcome, and the workflow literal must equal it (both pinned
# by tests/unit/test_vcr_outcome_table.py).
DEDUPE_LABEL = "corpus-drift"

_ISSUE_LABELS = ("corpus-drift", "needs-review")

OUTCOME_DRIFT = IssueOutcome("drift", "VCR drift detected {date}", _ISSUE_LABELS, True)
OUTCOME_NOTHING_COMPARED = IssueOutcome(
    "nothing_compared", "VCR drift canary compared 0 cassettes {date}", _ISSUE_LABELS, True
)
OUTCOME_BROKEN = IssueOutcome("broken", "VCR drift canary broken {date}", _ISSUE_LABELS, False)

_RC_DRIFT = "1"
_RC_NOTHING = str(EXIT_NOTHING_COMPARED)

# (report step exit code, drift-report.md present) -> (outcome, why).
# The exit code is the step output string; "" means the report step never
# recorded one (it did not run, or died before `echo rc=`).
ISSUE_OUTCOMES: dict[tuple[str, bool], tuple[IssueOutcome, str]] = {
    (_RC_DRIFT, True): (OUTCOME_DRIFT, "One or more cassettes changed."),
    (_RC_NOTHING, True): (
        OUTCOME_NOTHING_COMPARED,
        "No cassette pair was compared; a canary that compared nothing is never green.",
    ),
    (_RC_DRIFT, False): (
        OUTCOME_BROKEN,
        "Setup/IO error (missing directory, unwritable report) before any report was written.",
    ),
    (_RC_NOTHING, False): (
        OUTCOME_BROKEN,
        "Not produced by the script (exit 2 always writes a report); treated as broken.",
    ),
    ("0", True): (
        OUTCOME_BROKEN,
        "Report found no drift, but a later step (for example the artifact upload) failed.",
    ),
    ("0", False): (OUTCOME_BROKEN, "Report step passed but its report is gone; a later step failed."),
    ("", True): (
        OUTCOME_BROKEN,
        "Report step did not record an exit code (stale or partial report); treated as broken.",
    ),
    ("", False): (
        OUTCOME_BROKEN,
        "Report step never ran: re-record failed, the skip guard failed, or an earlier step broke.",
    ),
}
# Any exit code not in the table (e.g. 3, 127, garbage) is the canary breaking.
DEFAULT_OUTCOME = OUTCOME_BROKEN
_DEFAULT_WHY = "Any other exit code: the report script crashed or was not run as expected."


def classify_outcome(rc: str, report_exists: bool) -> IssueOutcome:
    """Map the report step's exit code and report presence to an issue outcome."""
    entry = ISSUE_OUTCOMES.get((rc.strip(), report_exists))
    return entry[0] if entry else DEFAULT_OUTCOME


def classify_why(rc: str, report_exists: bool) -> str:
    """The table's explanation for an outcome; used as the alert body when no
    report is attached, so the body never contradicts the row (grumpy r7 #3)."""
    entry = ISSUE_OUTCOMES.get((rc.strip(), report_exists))
    return entry[1] if entry else _DEFAULT_WHY


def label_specs(outcome: IssueOutcome) -> str:
    """Tab-separated `name\tcolor\tdescription` lines, one per outcome label."""
    return "\n".join(f"{name}\t{LABEL_SPECS[name][0]}\t{LABEL_SPECS[name][1]}" for name in outcome.labels)


def render_outcome_table() -> str:
    """Markdown table of ISSUE_OUTCOMES, embedded verbatim in the runbook."""
    lines = [
        "| Report step exit code | drift-report.md | Outcome | Issue title | Labels | Why |",
        "|-----------------------|-----------------|---------|-------------|--------|-----|",
    ]
    rows = [
        (rc or "(empty)", "present" if present else "absent", outcome, why)
        for (rc, present), (outcome, why) in ISSUE_OUTCOMES.items()
    ]
    rows.append(("any other", "either", DEFAULT_OUTCOME, _DEFAULT_WHY))
    for rc_cell, report_cell, outcome, why in rows:
        title = outcome.title.format(date="YYYY-MM-DD")
        labels = ", ".join(f"`{label}`" for label in outcome.labels)
        lines.append(
            f"| {rc_cell} | {report_cell} | `{outcome.key}` | \"{title}\" | {labels} | {why} |"
        )
    return "\n".join(lines) + "\n"


def generate_report(
    baseline_dir: Path,
    current_dir: Path,
) -> tuple[str, bool]:
    """Compare baseline and current cassette directories.

    Returns (markdown_report, any_drift). Thin wrapper kept for backward
    compatibility; use generate_report_with_count() to also learn how many
    cassette pairs were compared.
    """
    report, any_drift, _compared = generate_report_with_count(baseline_dir, current_dir)
    return report, any_drift


def generate_report_with_count(
    baseline_dir: Path,
    current_dir: Path,
) -> tuple[str, bool, int]:
    """Compare baseline and current cassette directories.

    Returns (markdown_report, any_drift, pairs_compared). pairs_compared counts
    cassettes present in both trees that were loaded and diffed successfully.
    """
    lines: list[str] = []
    lines.append("# VCR Drift Report\n")
    lines.append(f"- Baseline: {_code(baseline_dir)}")
    lines.append(f"- Current: {_code(current_dir)}")
    lines.append("")

    baseline_cassettes = collect_cassettes(baseline_dir)
    current_cassettes = collect_cassettes(current_dir)

    if not baseline_cassettes and not current_cassettes:
        lines.append("No cassettes found in either directory.")
        lines.append("Cassette pairs compared: 0")
        return "\n".join(lines), False, 0

    all_keys = sorted(set(baseline_cassettes) | set(current_cassettes))

    baseline_only: list[str] = []
    current_only: list[str] = []
    changed: list[tuple[str, dict[str, Any]]] = []
    unchanged: list[str] = []
    parse_errors: list[tuple[str, str]] = []

    for rel in all_keys:
        in_baseline = rel in baseline_cassettes
        in_current = rel in current_cassettes

        if in_baseline and not in_current:
            baseline_only.append(rel)
            continue

        if in_current and not in_baseline:
            current_only.append(rel)
            continue

        # Present in both - load and diff
        b_data, b_err = load_cassette(baseline_cassettes[rel])
        if b_err:
            parse_errors.append((f"baseline/{rel}", b_err))
            continue

        c_data, c_err = load_cassette(current_cassettes[rel])
        if c_err:
            parse_errors.append((f"current/{rel}", c_err))
            continue

        # A cassette with nothing to compare is a parse error, never a
        # "compared" pair (grumpy #2).
        b_empty = _interactions_error(b_data)
        if b_empty:
            parse_errors.append((f"baseline/{rel}", b_empty))
            continue
        c_empty = _interactions_error(c_data)
        if c_empty:
            parse_errors.append((f"current/{rel}", c_empty))
            continue

        # _interactions_error() returning None guarantees both are dicts.
        summary = diff_cassette(cast(dict[str, Any], b_data), cast(dict[str, Any], c_data))
        if summary["changed"]:
            changed.append((rel, summary))
        else:
            unchanged.append(rel)

    any_drift = bool(baseline_only or current_only or changed or parse_errors)
    # Positive evidence: how many pairs were really diffed (issue #92).
    compared = len(changed) + len(unchanged)

    # Summary counts
    lines.append("## Summary\n")
    lines.append("| Category | Count |")
    lines.append("|----------|-------|")
    lines.append(f"| Unchanged | {len(unchanged)} |")
    lines.append(f"| Changed | {len(changed)} |")
    lines.append(f"| Baseline only (not re-recorded this run) | {len(baseline_only)} |")
    lines.append(f"| Current only (new endpoint) | {len(current_only)} |")
    lines.append(f"| Parse errors | {len(parse_errors)} |")
    lines.append("")
    lines.append(f"Cassette pairs compared: {compared}")
    lines.append("")

    if not any_drift:
        lines.append(
            f"All {len(unchanged)} cassette(s) are identical to the baseline."
            " No drift detected."
        )
        return "\n".join(lines), False, compared

    # Unchanged section (brief)
    if unchanged:
        lines.append("## Unchanged cassettes\n")
        for rel in unchanged:
            lines.append(f"- {_code(rel)}")
        lines.append("")

    # Changed section (detailed)
    if changed:
        lines.append("## Changed cassettes\n")
        for rel, summary in changed:
            lines.append(f"### {_code(rel)}\n")
            uri_d = summary["uri_drift"]
            status_d = summary.get("status_drift", 0)
            body_d = summary["body_drift"]
            method_d = summary.get("method_drift", 0)
            b_count = summary["interaction_count_baseline"]
            c_count = summary["interaction_count_current"]
            parts: list[str] = []
            if uri_d:
                parts.append(f"URI drift in {uri_d} interaction(s)")
            if status_d:
                parts.append(f"status drift in {status_d} interaction(s)")
            if body_d:
                parts.append(f"body structure drift in {body_d} interaction(s)")
            if method_d:
                parts.append(f"method drift in {method_d} interaction(s)")
            if b_count != c_count:
                parts.append(
                    f"interaction count changed ({b_count} -> {c_count})"
                )
            lines.append(f"{len(parts)} change type(s): {'; '.join(parts)}.\n")
            for detail in summary["details"]:
                lines.append(detail)
            lines.append("")

    # Baseline-only section
    if baseline_only:
        lines.append("## Baseline-only cassettes (not re-recorded this run: test skipped, renamed, or made no request)\n")
        for rel in baseline_only:
            lines.append(f"- {_code(rel)}")
        lines.append("")

    # Current-only section
    if current_only:
        lines.append("## Current-only cassettes (new endpoint recorded)\n")
        for rel in current_only:
            lines.append(f"- {_code(rel)}")
        lines.append("")

    # Parse errors section
    if parse_errors:
        lines.append("## Parse errors\n")
        for fname, err in parse_errors:
            lines.append(f"- {_code(fname)}: {_code(_clip(err, MAX_ERROR_CHARS))}")
        lines.append("")

    snippets_cut = any(summary.get("truncated") for _rel, summary in changed)
    return _bounded_report(lines, snippets_cut), True, compared


def _bounded_report(blocks: list[str], snippets_cut: bool = False) -> str:
    """Join report blocks, capping the result below MAX_REPORT_BYTES (F1).

    Appends a truncation trailer (run URL + artifact name) whenever any
    snippet or the report itself was cut, so the reader knows detail is
    missing and where to find it.
    """
    full = "\n".join(blocks)
    total = len(full.encode("utf-8"))
    if total <= MAX_REPORT_BYTES and not snippets_cut:
        return full
    if snippets_cut:
        # Only append the snippets-cut trailer when it still fits the cap;
        # otherwise fall through to the reserve-and-bound path below.
        trailer = _truncation_trailer("one or more diff snippets were cut")
        if total + len(trailer.encode("utf-8")) <= MAX_REPORT_BYTES:
            return full + trailer
    # Reserve room for the trailer (its numbers are at most len(str(total))
    # digits each), then keep whole blocks only so no fence is left open.
    probe = _truncation_trailer(f"showing {total} of {total} bytes")
    budget = MAX_REPORT_BYTES - len(probe.encode("utf-8"))
    text, _cut = _bounded_join(blocks, budget)
    shown = len(text.encode("utf-8"))
    return text + _truncation_trailer(f"showing {shown} of {total} bytes")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Compare committed VCR cassettes against re-recorded cassettes "
            "and produce a markdown drift report."
        )
    )
    parser.add_argument(
        "--baseline",
        metavar="DIR",
        help="Directory containing the committed (baseline) cassettes.",
    )
    parser.add_argument(
        "--current",
        metavar="DIR",
        help="Directory containing the freshly re-recorded cassettes.",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="Path to write the markdown drift report.",
    )
    # Outcome classifier modes (ISSUE_OUTCOMES); they need no cassette dirs.
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--classify-outcome",
        action="store_true",
        help="Print the issue outcome FIELD for --rc/--report-exists and exit 0.",
    )
    mode.add_argument(
        "--print-outcome-table",
        action="store_true",
        help="Print the ISSUE_OUTCOMES markdown table (embedded in the runbook).",
    )
    parser.add_argument("--rc", default="", help="Report step exit code ('' if none).")
    parser.add_argument(
        "--report-exists", choices=("0", "1"), default="0", help="1 if drift-report.md exists."
    )
    parser.add_argument("--date", default="", help="Date substituted into the title.")
    parser.add_argument(
        "--field",
        choices=("title", "key", "labels", "label_specs", "attach_report", "why"),
        default="title",
        help=(
            "Which outcome field --classify-outcome prints (labels comma-joined; "
            "label_specs one name<TAB>color<TAB>description line per label)."
        ),
    )
    args = parser.parse_args(argv)
    if not (args.classify_outcome or args.print_outcome_table):
        missing = [f"--{n}" for n in ("baseline", "current", "output") if getattr(args, n) is None]
        if missing:
            parser.error(f"the following arguments are required: {', '.join(missing)}")
    return args


def _print_outcome_field(args: argparse.Namespace) -> int:
    outcome = classify_outcome(args.rc, args.report_exists == "1")
    if args.field == "title":
        print(outcome.title.format(date=args.date))
    elif args.field == "key":
        print(outcome.key)
    elif args.field == "labels":
        print(",".join(outcome.labels))
    elif args.field == "label_specs":
        print(label_specs(outcome))
    elif args.field == "why":
        print(classify_why(args.rc, args.report_exists == "1"))
    else:
        print("1" if outcome.attach_report else "0")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the drift report.

    Returns 0 (>=1 pair compared, no drift), 1 (drift or error), or
    EXIT_NOTHING_COMPARED (zero cassette pairs compared).
    """
    args = parse_args(argv)
    if args.print_outcome_table:
        sys.stdout.write(render_outcome_table())
        return 0
    if args.classify_outcome:
        return _print_outcome_field(args)

    baseline_dir = Path(args.baseline)
    current_dir = Path(args.current)
    output_path = Path(args.output)

    # Validate directories
    errors: list[str] = []
    if not baseline_dir.is_dir():
        errors.append(f"Baseline directory not found: {baseline_dir}")
    if not current_dir.is_dir():
        errors.append(f"Current directory not found: {current_dir}")

    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    report, any_drift, compared = generate_report_with_count(baseline_dir, current_dir)

    try:
        output_path.write_text(report, encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: Could not write report to {output_path}: {exc}", file=sys.stderr)
        return 1

    # Zero pairs compared means the canary checked nothing: never exit 0.
    if compared == 0:
        print(
            "ERROR: Compared 0 cassettes (no cassette pairs; directories empty or no overlapping"
            f" cassette files). Baseline: {baseline_dir}, current: {current_dir}."
            f" Report written to {output_path}.",
            file=sys.stderr,
        )
        return EXIT_NOTHING_COMPARED

    # Print summary to stdout
    if any_drift:
        print(
            f"Drift detected ({compared} cassette pair(s) compared)."
            f" Report written to {output_path}."
        )
        return 1

    print(
        f"No drift detected ({compared} cassette pair(s) compared)."
        f" Report written to {output_path}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
