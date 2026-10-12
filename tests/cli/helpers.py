"""Shared helpers for CLI tests."""

from __future__ import annotations

import re

# CSI escape sequences (colour/style), as emitted by Rich/typer help rendering.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences so assertions are independent of colour settings.

    Rich styles each option token separately under CI/GITHUB_ACTIONS, which splits
    strings such as ``--dry-run`` across escape codes; stripping restores them.
    """
    return _ANSI_RE.sub("", text)


# Public exit-code contract of the `ingester` CLI (#108, owner ruling 2026-10-12). The numbers
# are printed by `--help` and documented in automations/*.md, so callers may key on them:
# renumbering is a breaking change, and these values are pinned here on purpose, once, for every
# test that checks a code. EXIT_USAGE is for command-line (argument parse / usage) errors only.
PUBLIC_EXIT_CODES: dict[str, int] = {
    "EXIT_LOAD_ERROR": 1,
    "EXIT_USAGE": 2,
    "EXIT_NOT_WIRED": 3,
    "EXIT_CONSUMER_SKIPPED": 4,
    "EXIT_NO_SOURCES": 5,
    "EXIT_SOURCES_DIR_MISSING": 6,
}

# What each code means, as the phrases a "<code> when <meaning>" help clause must carry. Every
# phrase in a tuple must appear, so the clause for one code cannot describe another's outcome.
EXIT_CODE_MEANINGS: dict[str, tuple[str, ...]] = {
    "EXIT_LOAD_ERROR": ("cannot be loaded",),
    "EXIT_USAGE": ("usage",),
    "EXIT_NOT_WIRED": ("not wired",),
    "EXIT_CONSUMER_SKIPPED": ("consumer",),
    "EXIT_NO_SOURCES": ("holds no source",),
    "EXIT_SOURCES_DIR_MISSING": ("does not exist", "not a directory"),
}
