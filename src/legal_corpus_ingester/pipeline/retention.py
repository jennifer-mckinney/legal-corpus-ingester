"""Bundle retention policy: last 4 weekly + first-of-month x 12 + first-of-quarter forever."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

# YYYY.MM.PATCH calver pattern
_CALVER_RE = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d+)$")


def _parse_calver(name: str) -> date | None:
    """Parse YYYY.MM.PATCH bundle name -> date(year, month, 1), or None."""
    m = _CALVER_RE.fullmatch(name)
    if not m:
        return None
    year, month, _patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(year, month, 1)
    except ValueError:
        return None


def _quarter(d: date) -> tuple[int, int]:
    """Return (year, quarter_number) for a date."""
    return (d.year, (d.month - 1) // 3 + 1)


def plan(
    out_dir: Path,
    current_symlink: Path | None = None,
) -> tuple[list[Path], list[Path]]:
    """Compute retention plan for bundles in out_dir.

    Returns (keep, prune) lists of bundle Path objects.
    Never includes the out/current symlink target in prune.

    Retention policy:
    - Always keep the 4 most-recent bundles (weekly recency).
    - Keep the first bundle of each calendar month for the most-recent 12 distinct months.
    - Keep the first bundle of each calendar quarter, forever.
    """
    if not out_dir.is_dir():
        return ([], [])

    # Determine the current symlink target (never prune it)
    if current_symlink is None:
        current_symlink = out_dir / "current"
    current_target: Path | None = None
    if current_symlink.is_symlink():
        current_target = current_symlink.resolve()

    # Collect all calver bundle dirs: (path, date, patch)
    entries: list[tuple[Path, date, int]] = []
    for child in out_dir.iterdir():
        if child.is_symlink():  # skip symlinks — rmtree refuses them
            continue
        if not child.is_dir():
            continue
        d = _parse_calver(child.name)
        if d is None:
            # Ignore non-calver dirs (current, health, etc.)
            continue
        m = _CALVER_RE.fullmatch(child.name)
        # m is guaranteed non-None since _parse_calver returned non-None
        patch = int(m.group(3))  # type: ignore[union-attr]
        entries.append((child, d, patch))

    if not entries:
        return ([], [])

    # Sort descending by (date, patch) — most recent first
    sorted_bundles: list[tuple[Path, date, int]] = sorted(
        entries, key=lambda e: (e[1], e[2]), reverse=True
    )

    keep_paths: set[Path] = set()

    # Rule 1: keep the 4 most-recent bundles
    for path, _d, _patch in sorted_bundles[:4]:
        keep_paths.add(path)

    # Collect distinct (year, month) combos in descending order (most recent first)
    seen_months: list[tuple[int, int]] = []
    for _path, d, _patch in sorted_bundles:
        ym = (d.year, d.month)
        if ym not in seen_months:
            seen_months.append(ym)

    # Rule 2: keep the oldest bundle (lowest patch) in each of the 12 most-recent months
    recent_12_months = seen_months[:12]
    for ym in recent_12_months:
        # Find all bundles in this month, pick the oldest (lowest patch = ascending sort)
        month_bundles = [
            (path, d, patch)
            for path, d, patch in sorted_bundles
            if (d.year, d.month) == ym
        ]
        # Sort ascending by patch to get the oldest
        month_bundles.sort(key=lambda e: e[2])
        if month_bundles:
            keep_paths.add(month_bundles[0][0])

    # Rule 3: keep the oldest bundle of each quarter, forever
    seen_quarters: dict[tuple[int, int], list[tuple[Path, date, int]]] = {}
    for path, d, patch in sorted_bundles:
        q = _quarter(d)
        seen_quarters.setdefault(q, []).append((path, d, patch))

    for q_bundles in seen_quarters.values():
        # Sort ascending by (date, patch) to get the oldest bundle in the quarter
        q_bundles.sort(key=lambda e: (e[1], e[2]))
        if q_bundles:
            keep_paths.add(q_bundles[0][0])

    # Everything not in keep -> prune, but never prune the current symlink target
    prune_paths: list[Path] = []
    for path, _d, _patch in sorted_bundles:
        if path not in keep_paths:
            # Guard: never prune the active symlink target
            if current_target is None or path.resolve() != current_target:
                prune_paths.append(path)

    return (sorted(keep_paths), sorted(prune_paths))
