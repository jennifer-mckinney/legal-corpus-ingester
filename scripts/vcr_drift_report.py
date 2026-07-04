"""vcr_drift_report.py - Compare committed VCR cassettes against re-recorded cassettes.

Walks two cassette directories, diffs interactions, and produces a markdown
drift report. Exits 0 when no cassettes changed, non-zero when any drift is
detected.
"""

from __future__ import annotations

import argparse
import difflib
import os
import sys
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Cassette loading
# ---------------------------------------------------------------------------


def load_cassette(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Load a YAML cassette file.

    Returns (data, None) on success or (None, error_message) on failure.
    """
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


def diff_cassette(
    baseline_data: dict[str, Any],
    current_data: dict[str, Any],
) -> dict[str, Any]:
    """Compare two cassette data dicts.

    Returns a summary dict with keys:
        changed (bool)
        uri_drift (int)
        body_drift (int)
        interaction_count_baseline (int)
        interaction_count_current (int)
        details (list[str])
    """
    baseline_ix = _interactions(baseline_data)
    current_ix = _interactions(current_data)

    uri_drift = 0
    body_drift = 0
    method_drift = 0
    details: list[str] = []

    # Compare interaction by interaction (zip stops at shorter list)
    for idx, (b_ix, c_ix) in enumerate(zip(baseline_ix, current_ix)):
        b_uri = (b_ix.get("request") or {}).get("uri", "")
        c_uri = (c_ix.get("request") or {}).get("uri", "")
        if b_uri != c_uri:
            uri_drift += 1
            details.append(
                f"  interaction {idx}: URI changed"
                f" from {b_uri!r} to {c_uri!r}"
            )

        b_body = str((b_ix.get("response") or {}).get("body", {}).get("string", ""))
        c_body = str((c_ix.get("response") or {}).get("body", {}).get("string", ""))
        if b_body != c_body:
            body_drift += 1
            # Produce a compact unified diff snippet (first 5 lines)
            b_lines = b_body.splitlines(keepends=True)
            c_lines = c_body.splitlines(keepends=True)
            diff_lines = list(
                difflib.unified_diff(b_lines, c_lines, n=1, lineterm="")
            )[:10]
            snippet = "".join(diff_lines) if diff_lines else "(binary or empty diff)"
            details.append(
                f"  interaction {idx}: body changed"
                f" ({len(b_body)} bytes -> {len(c_body)} bytes)\n"
                f"    diff snippet:\n"
                + "\n".join(f"    {line}" for line in snippet.splitlines())
            )

        b_status = (b_ix.get("response") or {}).get("status", {}).get("code")
        c_status = (c_ix.get("response") or {}).get("status", {}).get("code")
        if b_status != c_status:
            uri_drift += 1
            details.append(
                f"  interaction {idx}: status code changed"
                f" from {b_status} to {c_status}"
            )

        b_method = (b_ix.get("request") or {}).get("method", "")
        c_method = (c_ix.get("request") or {}).get("method", "")
        if b_method != c_method:
            method_drift += 1
            details.append(
                f"  interaction {idx}: method changed"
                f" from {b_method!r} to {c_method!r}"
            )

    # Interaction count mismatch
    if len(baseline_ix) != len(current_ix):
        details.append(
            f"  interaction count changed:"
            f" {len(baseline_ix)} -> {len(current_ix)}"
        )

    changed = bool(uri_drift or body_drift or method_drift or len(baseline_ix) != len(current_ix))

    return {
        "changed": changed,
        "uri_drift": uri_drift,
        "body_drift": body_drift,
        "method_drift": method_drift,
        "interaction_count_baseline": len(baseline_ix),
        "interaction_count_current": len(current_ix),
        "details": details,
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


def generate_report(
    baseline_dir: Path,
    current_dir: Path,
) -> tuple[str, bool]:
    """Compare baseline and current cassette directories.

    Returns (markdown_report, any_drift).
    """
    lines: list[str] = []
    lines.append("# VCR Drift Report\n")
    lines.append(f"- Baseline: `{baseline_dir}`")
    lines.append(f"- Current: `{current_dir}`")
    lines.append("")

    baseline_cassettes = collect_cassettes(baseline_dir)
    current_cassettes = collect_cassettes(current_dir)

    if not baseline_cassettes and not current_cassettes:
        lines.append("No cassettes found in either directory.")
        return "\n".join(lines), False

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

        summary = diff_cassette(b_data, c_data)
        if summary["changed"]:
            changed.append((rel, summary))
        else:
            unchanged.append(rel)

    any_drift = bool(baseline_only or current_only or changed or parse_errors)

    # Summary counts
    lines.append("## Summary\n")
    lines.append("| Category | Count |")
    lines.append("|----------|-------|")
    lines.append(f"| Unchanged | {len(unchanged)} |")
    lines.append(f"| Changed | {len(changed)} |")
    lines.append(f"| Baseline only (endpoint removed) | {len(baseline_only)} |")
    lines.append(f"| Current only (new endpoint) | {len(current_only)} |")
    lines.append(f"| Parse errors | {len(parse_errors)} |")
    lines.append("")

    if not any_drift:
        lines.append(
            f"All {len(unchanged)} cassette(s) are identical to the baseline."
            " No drift detected."
        )
        return "\n".join(lines), False

    # Unchanged section (brief)
    if unchanged:
        lines.append("## Unchanged cassettes\n")
        for rel in unchanged:
            lines.append(f"- `{rel}`")
        lines.append("")

    # Changed section (detailed)
    if changed:
        lines.append("## Changed cassettes\n")
        for rel, summary in changed:
            lines.append(f"### `{rel}`\n")
            uri_d = summary["uri_drift"]
            body_d = summary["body_drift"]
            method_d = summary.get("method_drift", 0)
            b_count = summary["interaction_count_baseline"]
            c_count = summary["interaction_count_current"]
            parts: list[str] = []
            if uri_d:
                parts.append(f"URI drift in {uri_d} interaction(s)")
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
        lines.append("## Baseline-only cassettes (endpoint may be removed)\n")
        for rel in baseline_only:
            lines.append(f"- `{rel}`")
        lines.append("")

    # Current-only section
    if current_only:
        lines.append("## Current-only cassettes (new endpoint recorded)\n")
        for rel in current_only:
            lines.append(f"- `{rel}`")
        lines.append("")

    # Parse errors section
    if parse_errors:
        lines.append("## Parse errors\n")
        for fname, err in parse_errors:
            lines.append(f"- `{fname}`: {err}")
        lines.append("")

    return "\n".join(lines), True


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
        required=True,
        metavar="DIR",
        help="Directory containing the committed (baseline) cassettes.",
    )
    parser.add_argument(
        "--current",
        required=True,
        metavar="DIR",
        help="Directory containing the freshly re-recorded cassettes.",
    )
    parser.add_argument(
        "--output",
        required=True,
        metavar="FILE",
        help="Path to write the markdown drift report.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the drift report. Returns 0 (no drift) or 1 (drift detected or error)."""
    args = parse_args(argv)

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

    report, any_drift = generate_report(baseline_dir, current_dir)

    try:
        output_path.write_text(report, encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: Could not write report to {output_path}: {exc}", file=sys.stderr)
        return 1

    # Print summary to stdout
    if any_drift:
        print(f"Drift detected. Report written to {output_path}.")
    else:
        print(f"No drift detected. Report written to {output_path}.")

    return 1 if any_drift else 0


if __name__ == "__main__":
    sys.exit(main())
