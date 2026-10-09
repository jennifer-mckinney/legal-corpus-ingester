#!/usr/bin/env python3
"""Health check script for legal-corpus-ingester.

Reads source configs, checks checkpoint state, and writes a markdown
freshness report to out/health/YYYY-MM-DD.md.

Exit 0: all sources are fresh (lag < stale_days).
Exit 1: a source is stale, or has never run although refresh is wired.
Exit 2: the config dir is missing (a broken checkout or wrong working dir, not
        "nothing to do"; terms-analysis#90).
Exit 3 (cli.EXIT_NOT_WIRED): the only problem is never-run sources while
        cli.REFRESH_WIRED is False. No checkpoint can exist yet, so this is the
        known unwired state: not success, and not a broken pipeline either.
        health.yml turns exactly this code into a warning; every other non-zero
        code fails the run. health_verdict() is the one place this is decided.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from legal_corpus_ingester.cli import EXIT_NOT_WIRED, REFRESH_WIRED

# A stale source, or a never-run source once refresh is wired: a real problem.
EXIT_SOURCE_PROBLEM: int = 1
# Config dir missing: distinct from 1 (stale sources) so the cause is visible in the exit code.
EXIT_CONFIG_MISSING: int = 2


def _utc_now() -> datetime:
    """Return current UTC datetime."""
    return datetime.now(tz=UTC)


def _source_names(config_dir: Path) -> list[str]:
    """Return sorted list of source names from config YAML filenames."""
    if not config_dir.is_dir():
        return []
    return sorted(p.stem for p in config_dir.glob("*.yaml"))


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


def health_verdict(statuses: list[str], refresh_wired: bool) -> int:
    """Return the exit code for a set of per-source statuses.

    Fails closed: any status other than "fresh" is a problem, unless every
    problem is "never-run" and refresh is not wired, which is the known
    unwired state (EXIT_NOT_WIRED). Stale always fails.
    """
    problems = {s for s in statuses if s != "fresh"}
    if not problems:
        return 0
    if problems == {"never-run"} and not refresh_wired:
        return EXIT_NOT_WIRED
    return EXIT_SOURCE_PROBLEM


def build_report(
    config_dir: Path,
    state_dir: Path,
    out_dir: Path,
    stale_days: int,
    today: str,
    refresh_wired: bool,
) -> tuple[str, int]:
    """Build the markdown report string and return (report, exit_code).

    exit_code comes from health_verdict(); see the module docstring.
    """
    sources = _source_names(config_dir)

    header = f"# Legal Corpus Health -- {today}\n\n"

    if not sources:
        return header + "No sources configured.\n", 0

    rows: list[str] = []
    statuses: list[str] = []

    for source in sources:
        last_run, stage, lag_hours = _checkpoint_info(state_dir, source)
        status = _status_label(lag_hours, stale_days)
        lag_str = _lag_display(lag_hours)
        statuses.append(status)

        rows.append(f"| {source} | {last_run} | {stage} | {lag_str} | {status} |")

    table = (
        "| Source | Last Run (UTC) | Stage | Lag (days) | Status |\n"
        "|--------|----------------|-------|-----------|--------|\n"
        + "\n".join(rows)
        + "\n"
    )

    verdict = health_verdict(statuses, refresh_wired)
    if verdict == EXIT_NOT_WIRED:
        # Say why the run is not red, in the report itself, so never-run is not read as fine.
        table += (
            "\nRefresh is not wired yet, so no source can have run: never-run is expected"
            f" (exit {EXIT_NOT_WIRED}), not healthy.\n"
        )
    return header + table, verdict


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

    # A missing config dir means the checkout is broken, not that there is nothing
    # to check; fail loudly instead of reporting success (terms-analysis#90).
    if not config_dir.is_dir():
        print(f"Error: config dir {config_dir} does not exist.", file=sys.stderr)
        return EXIT_CONFIG_MISSING

    today = _utc_now().strftime("%Y-%m-%d")
    report, verdict = build_report(
        config_dir=config_dir,
        state_dir=state_dir,
        out_dir=out_dir,
        stale_days=stale_days,
        today=today,
        refresh_wired=REFRESH_WIRED,
    )

    # Write report to out/health/YYYY-MM-DD.md.
    health_dir = out_dir / "health"
    health_dir.mkdir(parents=True, exist_ok=True)
    report_path = health_dir / f"{today}.md"
    report_path.write_text(report)

    # Print to stdout as well.
    print(report, end="")

    return verdict


if __name__ == "__main__":
    sys.exit(main())
