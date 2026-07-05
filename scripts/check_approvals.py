#!/usr/bin/env python3
"""Check APPROVAL.yaml files for expiry.

Walks all *.yaml files in the approvals directory, classifies each as
EXPIRED / EXPIRING_SOON / OK, prints a markdown summary table, and exits
non-zero if any approval has expired.

Exit 0: all approvals are current (no expired entries).
Exit 1: one or more approvals are EXPIRED.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Pure functions (exposed for unit tests)
# ---------------------------------------------------------------------------


def classify_approval(expiry: date, today: date, warn_days: int) -> str:
    """Return 'EXPIRED', 'EXPIRING_SOON', or 'OK'.

    Args:
        expiry:    The parsed expiry date from the APPROVAL.yaml.
        today:     The reference date (pass date.today() in production).
        warn_days: Number of days before expiry at which to warn.
    """
    if today >= expiry:
        return "EXPIRED"
    if (expiry - today).days <= warn_days:
        return "EXPIRING_SOON"
    return "OK"


def check_approvals_dir(
    approvals_dir: Path,
    today: date,
    warn_days: int,
) -> tuple[list[dict[str, str | int]], bool]:
    """Walk approvals_dir and classify every *.yaml file.

    Args:
        approvals_dir: Directory containing APPROVAL.yaml files.
        today:         Reference date for expiry calculations.
        warn_days:     Days-to-expiry threshold for EXPIRING_SOON.

    Returns:
        A tuple of (rows, any_expired) where rows is a list of dicts with keys
        source_id, status, expiry, days_remaining; and any_expired is True if at
        least one approval is EXPIRED.
    """
    rows: list[dict[str, str | int]] = []
    any_expired = False

    yaml_files = sorted(approvals_dir.glob("*.yaml"))
    for yaml_path in yaml_files:
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — surface parse errors as rows
            rows.append(
                {
                    "source_id": yaml_path.stem,
                    "status": "ERROR",
                    "expiry": str(exc),
                    "days_remaining": "-",
                }
            )
            any_expired = True
            continue

        if not isinstance(data, dict):
            rows.append(
                {
                    "source_id": yaml_path.stem,
                    "status": "ERROR",
                    "expiry": "malformed YAML (not a dict)",
                    "days_remaining": "-",
                }
            )
            any_expired = True
            continue

        source_id = str(data.get("source_id", yaml_path.stem))
        raw_expiry = data.get("expiry")

        if not raw_expiry:
            rows.append(
                {
                    "source_id": source_id,
                    "status": "ERROR",
                    "expiry": "missing 'expiry' field",
                    "days_remaining": "-",
                }
            )
            any_expired = True
            continue

        try:
            expiry_date = date.fromisoformat(str(raw_expiry))
        except ValueError:
            rows.append(
                {
                    "source_id": source_id,
                    "status": "ERROR",
                    "expiry": f"invalid date: {raw_expiry!r}",
                    "days_remaining": "-",
                }
            )
            any_expired = True
            continue

        # HR9: signed_artifact_sha256 must be present, non-empty, and valid hex format.
        sha256 = str(data.get("signed_artifact_sha256", "")).strip()
        if not sha256:
            rows.append(
                {
                    "source_id": source_id,
                    "status": "ERROR",
                    "expiry": f"sha256-missing (expiry={expiry_date})",
                    "days_remaining": "-",
                }
            )
            any_expired = True
            continue
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            rows.append(
                {
                    "source_id": source_id,
                    "status": "ERROR",
                    "expiry": f"sha256-invalid-format (expiry={expiry_date})",
                    "days_remaining": "-",
                }
            )
            any_expired = True
            continue

        status = classify_approval(expiry_date, today, warn_days)
        raw_days = (expiry_date - today).days
        # For EXPIRED entries, store absolute days past expiry (positive integer).
        days_remaining: str | int = abs(raw_days) if status == "EXPIRED" else raw_days

        if status == "EXPIRED":
            any_expired = True

        rows.append(
            {
                "source_id": source_id,
                "status": status,
                "expiry": str(expiry_date),
                "days_remaining": days_remaining,
            }
        )

    return rows, any_expired


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _render_table(rows: list[dict[str, str | int]]) -> str:
    """Render rows as a markdown table string."""
    header = (
        "| source_id | status | expiry | days_remaining (EXPIRED=days past) |\n"
        "|-----------|--------|--------|------------------------------------|\n"
    )
    lines: list[str] = []
    for row in rows:
        lines.append(
            f"| {row['source_id']} "
            f"| {row['status']} "
            f"| {row['expiry']} "
            f"| {row['days_remaining']} |"
        )
    return header + "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, check approvals, print table, return exit code."""
    parser = argparse.ArgumentParser(
        description="Check APPROVAL.yaml files for expiry.",
    )
    parser.add_argument(
        "--approvals-dir",
        default="config/approvals",
        help="Directory containing APPROVAL.yaml files (default: config/approvals)",
    )
    parser.add_argument(
        "--warn-days",
        type=int,
        default=60,
        help="Warn when expiry is within this many days (default: 60)",
    )
    args = parser.parse_args(argv)

    # Validate --warn-days is non-negative before proceeding.
    if args.warn_days < 0:
        parser.error("--warn-days must be a non-negative integer")

    approvals_dir = Path(args.approvals_dir)
    warn_days: int = args.warn_days
    today = date.today()

    # No directory or no files -- treat as no approvals to check.
    if not approvals_dir.is_dir():
        print("No approvals to check.")
        return 0

    yaml_files = list(approvals_dir.glob("*.yaml"))
    if not yaml_files:
        print("No approvals to check.")
        return 0

    rows, any_expired = check_approvals_dir(approvals_dir, today, warn_days)

    print(_render_table(rows))

    if any_expired:
        print("ERROR: one or more approvals are EXPIRED or invalid.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
