#!/usr/bin/env python3
"""Check APPROVAL.yaml files for expiry.

Walks all *.yaml files in the approvals directory, classifies each as
EXPIRED / EXPIRING_SOON / OK, prints a markdown summary table, and exits
non-zero if any approval has expired.

Exit 0: all approvals are current (no expired entries).
Exit 1: one or more approvals are EXPIRED or invalid.
Exit 2: config problem, never "nothing to do": the approvals dir or the
        --sources-dir is missing or unreadable, a *.yaml entry is not a
        regular file, or there are zero approval files (terms-analysis#173).
        Messages go to stderr.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

# Config problem: distinct from 1 (expired/invalid approval) so the cause is visible.
EXIT_CONFIG: int = 2


# ---------------------------------------------------------------------------
# Directory listing and log-path rendering (terms-analysis#173).
# This block is byte-identical in health_check.py and check_approvals.py so each
# scheduled job runs standalone as `python scripts/<name>.py`. The parity test in
# tests/unit/test_health_check_empty.py runs both copies and checks the cap matches.
# What counts as a config file: a regular *.yaml file (symlinks to files followed,
# hidden names included, as the source registry's glob("*.yaml") does). Untrusted
# paths reach a terminal or CI log only through display_path (DEV-FUNDAMENTALS F2, F8).
# ---------------------------------------------------------------------------

YAML_SUFFIX: str = ".yaml"

# Long enough to identify any real path; short enough that a hostile name cannot flood a log.
_MAX_DISPLAY_CHARS: int = 200

# Control, format (bidi overrides, zero-width), line/paragraph separators, surrogates
# (undecodable bytes), private-use and unassigned code points are never printed raw.
_ESCAPED_CATEGORIES: frozenset[str] = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"})


class YamlDirError(Exception):
    """The directory cannot be listed: missing, not a directory, or unreadable."""


@dataclass(frozen=True)
class YamlDirListing:
    """Result of :func:`scan_yaml_dir`."""

    files: list[Path]
    """Regular ``*.yaml`` files, sorted by name."""

    non_files: list[Path]
    """``*.yaml`` entries that are not regular files (directories, broken symlinks)."""


def display_path(path: Path | str) -> str:
    """Render an untrusted path for a terminal or log (DEV-FUNDAMENTALS F2, F8).

    Every character in an escaped category becomes a visible ``\\xNN`` or
    ``\\uNNNN`` escape, so the result is one line with no terminal control,
    no bidi reordering and nothing that fails to encode. Output is capped.
    """
    out: list[str] = []
    for ch in str(path):
        if unicodedata.category(ch) in _ESCAPED_CATEGORIES:
            code = ord(ch)
            out.append(f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}")
        else:
            out.append(ch)
    text = "".join(out)
    if len(text) > _MAX_DISPLAY_CHARS:
        text = text[:_MAX_DISPLAY_CHARS] + "...(truncated)"
    return text


def scan_yaml_dir(directory: Path, label: str) -> YamlDirListing:
    """List the ``*.yaml`` entries of ``directory``, failing closed.

    Args:
        directory: The directory to list (symlinks to directories are followed).
        label:     How error messages name the directory, e.g. ``"config dir"``.

    Raises:
        YamlDirError: ``directory`` is missing, is not a directory, or cannot be
            read. ``Path.glob`` returns nothing for an unreadable directory, which
            would look exactly like an empty one, so ``os.scandir`` is used instead.
    """
    shown = display_path(directory)
    if not directory.exists():
        raise YamlDirError(f"{label} {shown} does not exist.")
    if not directory.is_dir():
        raise YamlDirError(f"{label} {shown} is not a directory.")
    try:
        with os.scandir(directory) as entries:
            matched = sorted(
                (entry for entry in entries if entry.name.endswith(YAML_SUFFIX)),
                key=lambda entry: entry.name,
            )
    except OSError as exc:
        # strerror only: the exception's str() repeats the raw path (F8).
        reason = exc.strerror or type(exc).__name__
        raise YamlDirError(f"cannot read {label} {shown}: {reason}.") from exc

    files: list[Path] = []
    non_files: list[Path] = []
    for entry in matched:
        try:
            is_file = entry.is_file()  # follows symlinks; a broken link is not a file
        except OSError:
            is_file = False
        (files if is_file else non_files).append(directory / entry.name)
    return YamlDirListing(files=files, non_files=non_files)


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

    # main() rejects a missing, unreadable or empty dir before this runs (exit 2).
    if not approvals_dir.is_dir():
        return rows, any_expired
    yaml_files = scan_yaml_dir(approvals_dir, "approvals dir").files
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


def _scan(directory: Path, label: str, problems: list[str]) -> list[Path] | None:
    """Return the regular *.yaml files in ``directory``, appending any problem found.

    Returns None when the directory itself cannot be listed.
    """
    try:
        listing = scan_yaml_dir(directory, label)
    except YamlDirError as exc:
        problems.append(str(exc))
        return None
    shown_dir = display_path(directory)
    problems.extend(
        f"{display_path(p.name)} in {label} {shown_dir} is not a regular file."
        for p in listing.non_files
    )
    return listing.files


def _config_problems(sources_dir: Path, approvals_dir: Path) -> list[str]:
    """Return every reason the job cannot do its check; empty means usable.

    Zero approvals is always a problem (terms-analysis#173): with source configs
    present the gated sources are unverified, and with none the scheduled job
    has nothing to verify, which looks exactly like a job that is not wired up.
    """
    problems: list[str] = []
    sources = _scan(sources_dir, "sources dir", problems)
    approvals = _scan(approvals_dir, "approvals dir", problems)
    if approvals is not None and not approvals:
        unverified = (
            f"; {len(sources)} source config(s) in {display_path(sources_dir)} are unverified"
            if sources
            else ""
        )
        problems.append(
            f"no approval files (*.yaml) in approvals dir {display_path(approvals_dir)}{unverified}."
        )
    return problems


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
        "--sources-dir",
        default="config/sources",
        help="Directory containing per-source YAML configs (default: config/sources)",
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
    sources_dir = Path(args.sources_dir)
    warn_days: int = args.warn_days
    today = date.today()

    # Nothing to check is a config error, not success: the daily job must go red
    # instead of passing silently (terms-analysis#173).
    problems = _config_problems(sources_dir, approvals_dir)
    if problems:
        for problem in problems:
            print(f"Error: {problem}", file=sys.stderr)
        return EXIT_CONFIG

    rows, any_expired = check_approvals_dir(approvals_dir, today, warn_days)

    print(_render_table(rows))

    if any_expired:
        print("ERROR: one or more approvals are EXPIRED or invalid.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
