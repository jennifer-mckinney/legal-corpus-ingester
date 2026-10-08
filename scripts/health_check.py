#!/usr/bin/env python3
"""Health check script for legal-corpus-ingester.

Reads source configs, checks checkpoint state, and writes a markdown
freshness report to out/health/YYYY-MM-DD.md.

Exit 0: all sources are fresh (lag < stale_days).
Exit 1: one or more sources are stale or have never run.
Exit 2: config problem, never "nothing to do": the config dir is missing
        (terms-analysis#90), unreadable or holds zero source configs, a *.yaml
        entry is not a regular file, or a source YAML is empty or not a
        mapping (terms-analysis#173). Messages go to stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

# Config problem (missing/unreadable/empty dir, bad source YAML): distinct from 1
# (stale sources) so the cause is visible in the exit code.
EXIT_CONFIG_MISSING: int = 2


# ---------------------------------------------------------------------------
# Directory listing and log-path rendering (terms-analysis#173).
# This block is byte-identical in health_check.py and check_approvals.py so each
# scheduled job runs standalone as `python scripts/<name>.py`. A test in
# tests/unit/test_health_check_empty.py compares the two copies byte for byte.
# What counts as a config file: a regular *.yaml file (symlinks to files followed,
# hidden names included, as the source registry's glob("*.yaml") does). Untrusted
# text (file names, YAML values, checkpoint fields) reaches a terminal, CI log or
# report only through display_path, and a markdown table cell only through
# table_cell (DEV-FUNDAMENTALS F2, F8).
# ---------------------------------------------------------------------------

YAML_SUFFIX: str = ".yaml"

# Long enough to identify any real path; short enough that a hostile name cannot flood a log.
_MAX_DISPLAY_CHARS: int = 200

# Control, format (bidi overrides, zero-width), line/paragraph separators, surrogates
# (undecodable bytes), private-use and unassigned code points are never printed raw.
_ESCAPED_CATEGORIES: frozenset[str] = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"})


class YamlDirError(Exception):
    """The directory cannot be used: missing, not a directory, unreadable, or empty."""


@dataclass(frozen=True)
class YamlDirListing:
    """Result of :func:`scan_yaml_dir`."""

    files: list[Path]
    """Regular ``*.yaml`` files, sorted by name."""

    non_files: list[Path]
    """``*.yaml`` entries that are not regular files (directories, broken symlinks)."""


def display_path(path: Path | str) -> str:
    """Render untrusted text for a terminal or log (DEV-FUNDAMENTALS F2, F8).

    Every character in an escaped category becomes a visible fixed-width escape
    (``\\xNN``, ``\\uNNNN``, or ``\\UNNNNNNNN`` above U+FFFF) and a literal
    backslash becomes ``\\\\``, so each escape reads one way only. The result is
    one line with no terminal control, no bidi reordering and nothing that fails
    to encode. Output is capped, cut only between whole escapes.
    """
    out: list[str] = []
    used = 0
    for ch in str(path):
        code = ord(ch)
        if ch == "\\":
            token = "\\\\"
        elif unicodedata.category(ch) not in _ESCAPED_CATEGORIES:
            token = ch
        elif code <= 0xFF:
            token = f"\\x{code:02x}"
        elif code <= 0xFFFF:
            token = f"\\u{code:04x}"
        else:
            token = f"\\U{code:08x}"
        # Cut only between whole escapes, so the output never ends inside a partial one.
        if used + len(token) > _MAX_DISPLAY_CHARS:
            return "".join(out) + "...(truncated)"
        out.append(token)
        used += len(token)
    return "".join(out)


def table_cell(value: object) -> str:
    """Render untrusted text as one markdown table cell (DEV-FUNDAMENTALS F2).

    display_path makes it one escaped line; ``|`` then becomes ``\\|`` so the
    value cannot add or close a cell.
    """
    return display_path(str(value)).replace("|", "\\|")


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


def _utc_now() -> datetime:
    """Return current UTC datetime."""
    return datetime.now(tz=UTC)


def _source_names(config_dir: Path) -> list[str]:
    """Return sorted list of source names from config YAML filenames.

    Uses the shared listing rule, so only regular *.yaml files count.

    Raises:
        YamlDirError: config_dir is missing, not a directory, unreadable, or holds
            no source configs. Nothing to check is never a clean report (#173).
    """
    names = sorted(p.stem for p in scan_yaml_dir(config_dir, "config dir").files)
    if not names:
        raise YamlDirError(
            f"No sources configured: config dir {display_path(config_dir)} "
            "has no *.yaml source configs."
        )
    return names


def _config_problems(config_dir: Path) -> list[str]:
    """Return every reason the config dir cannot be checked; empty means usable.

    Zero source configs is a problem, not success: a scheduled job with nothing
    configured is indistinguishable from one that is not wired up (terms-analysis#173).
    """
    try:
        listing = scan_yaml_dir(config_dir, "config dir")
    except YamlDirError as exc:
        return [str(exc)]

    shown_dir = display_path(config_dir)
    problems = [
        f"{display_path(p.name)} in config dir {shown_dir} is not a regular file."
        for p in listing.non_files
    ]
    for path in listing.files:
        shown = display_path(path.name)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            reason = getattr(exc, "strerror", None) or type(exc).__name__
            problems.append(f"cannot read source config {shown} in {shown_dir}: {reason}.")
            continue
        except yaml.YAMLError as exc:
            # Line number only: the parser's message can quote untrusted file bytes (F8).
            mark = getattr(exc, "problem_mark", None)
            where = f" (line {mark.line + 1})" if mark is not None else ""
            problems.append(f"source config {shown} in {shown_dir} is not valid YAML{where}.")
            continue
        if not isinstance(data, dict) or not data:
            problems.append(
                f"source config {shown} in {shown_dir} is empty or not a YAML mapping."
            )
    if not listing.files:
        problems.append(f"No sources configured: config dir {shown_dir} has no *.yaml source configs.")
    return problems


def _checkpoint_info(state_dir: Path, source: str) -> tuple[str, str, float | None]:
    """Return (last_run_utc, stage, lag_hours) for a source checkpoint.

    last_run_utc is an ISO-8601 string or "never".
    stage is the last recorded stage string or "-".
    lag_hours is None if the checkpoint does not exist.
    """
    checkpoint = state_dir / f"{source}.checkpoint.json"
    if not checkpoint.exists():
        return "never", "-", None

    try:
        mtime = os.path.getmtime(checkpoint)
    except OSError:
        return "never", "-", None
    last_run_dt = datetime.fromtimestamp(mtime, tz=UTC)
    last_run_utc = last_run_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    stage = "-"
    try:
        data = json.loads(checkpoint.read_text())
        stage = str(data.get("stage", "-"))
    except (json.JSONDecodeError, OSError):
        pass

    now = _utc_now()
    lag_hours = (now - last_run_dt).total_seconds() / 3600.0
    return last_run_utc, stage, lag_hours


def _latest_bundle(out_dir: Path) -> str | None:
    """Return the most recent versioned bundle directory name, or None."""
    if not out_dir.is_dir():
        return None
    candidates = [
        p
        for p in out_dir.iterdir()
        if p.is_dir() and p.name != "current" and p.name != "health"
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime).name


def _status_label(lag_hours: float | None, stale_days: int) -> str:
    """Return a human-readable status string."""
    if lag_hours is None:
        return "never-run"
    lag_days = lag_hours / 24.0
    if lag_days >= stale_days:
        return "stale"
    return "fresh"


def _lag_display(lag_hours: float | None) -> str:
    """Return lag formatted as fractional days, or '-' for never-run."""
    if lag_hours is None:
        return "-"
    return f"{lag_hours / 24.0:.1f}"


def build_report(
    config_dir: Path,
    state_dir: Path,
    out_dir: Path,
    stale_days: int,
    today: str,
) -> tuple[str, bool]:
    """Build the markdown report string and return (report, any_problem).

    any_problem is True if any source is stale or never-run. Every table cell
    goes through table_cell: names and stages come from untrusted files (F2).

    Raises:
        YamlDirError: from _source_names; there is no report without sources.
    """
    sources = _source_names(config_dir)

    header = f"# Legal Corpus Health -- {today}\n\n"

    rows: list[str] = []
    any_problem = False

    for source in sources:
        last_run, stage, lag_hours = _checkpoint_info(state_dir, source)
        status = _status_label(lag_hours, stale_days)
        lag_str = _lag_display(lag_hours)

        if status in ("stale", "never-run"):
            any_problem = True

        cells = (source, last_run, stage, lag_str, status)
        rows.append("| " + " | ".join(table_cell(cell) for cell in cells) + " |")

    table = (
        "| Source | Last Run (UTC) | Stage | Lag (days) | Status |\n"
        "|--------|----------------|-------|-----------|--------|\n"
        + "\n".join(rows)
        + "\n"
    )

    return header + table, any_problem


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Check freshness of legal corpus sources.",
    )
    parser.add_argument(
        "--config-dir",
        default="config/sources",
        help="Directory containing per-source YAML files (default: config/sources)",
    )
    parser.add_argument(
        "--state-dir",
        default="state",
        help="Directory containing checkpoint JSON files (default: state)",
    )
    parser.add_argument(
        "--out-dir",
        default="out",
        help="Output directory for bundles and health reports (default: out)",
    )
    parser.add_argument(
        "--stale-days",
        type=int,
        default=8,
        help="Number of days before a source is considered stale (default: 8)",
    )
    args = parser.parse_args(argv)

    config_dir = Path(args.config_dir)
    state_dir = Path(args.state_dir)
    out_dir = Path(args.out_dir)
    stale_days: int = args.stale_days

    # A missing, unreadable or empty config dir means the checkout is broken, not
    # that there is nothing to check; fail loudly instead of reporting success
    # (terms-analysis#90, #173). No report is written: there is nothing true to say.
    problems = _config_problems(config_dir)
    if problems:
        for problem in problems:
            print(f"Error: {problem}", file=sys.stderr)
        return EXIT_CONFIG_MISSING

    today = _utc_now().strftime("%Y-%m-%d")
    try:
        report, any_problem = build_report(
            config_dir=config_dir,
            state_dir=state_dir,
            out_dir=out_dir,
            stale_days=stale_days,
            today=today,
        )
    except YamlDirError as exc:
        # The dir changed after the check above: still a config error, never success.
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_MISSING

    # Write report to out/health/YYYY-MM-DD.md.
    health_dir = out_dir / "health"
    health_dir.mkdir(parents=True, exist_ok=True)
    report_path = health_dir / f"{today}.md"
    report_path.write_text(report)

    # Print to stdout as well.
    print(report, end="")

    return 1 if any_problem else 0


if __name__ == "__main__":
    sys.exit(main())
