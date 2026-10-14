"""Shared helpers for CLI tests."""

from __future__ import annotations

import re
from pathlib import Path

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
    # terms-analysis#93 (lead rulings of 2026-10-13): fetch and refresh run the pipeline.
    "EXIT_NO_APPROVED_SOURCES": 7,
    "EXIT_SOURCE_FAILED": 8,
    # terms-analysis#93 Round 11 (rthoB): refresh with no licence baseline and no seed opt-in.
    # Its own code, not 8: no source failed, and every code has exactly one meaning.
    "EXIT_LICENSE_BASELINE_MISSING": 9,
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
    "EXIT_NO_APPROVED_SOURCES": ("valid approval", "nothing ran"),
    "EXIT_SOURCE_FAILED": ("failed", "named on stderr"),
    "EXIT_LICENSE_BASELINE_MISSING": ("baseline", "nothing ran"),
}

# Every command that takes --sources-dir, by the name `ingester --help` shows (#108, owner ruling
# 2026-10-12 "fold in"): the top-level help says 6 means a missing --sources-dir for the whole tool,
# so each of these must classify the path the same way. test_cli_smoke pins this tuple to the
# commands the app actually registers with a --sources-dir option.
SOURCES_DIR_COMMANDS: tuple[str, ...] = ("status", "refresh", "fetch", "sources list", "audit-license")


def sources_dir_argv(command: str, sources_dir: str, root: Path, source: str) -> list[str]:
    """argv that runs *command* on *sources_dir*, with every other path kept under *root*.

    *source* is the source name for fetch and audit-license. audit-license runs --offline with
    its own state file, so no row can reach the network or the repo's state/.
    """
    argv: dict[str, list[str]] = {
        "status": ["status", "--sources-dir", sources_dir, "--state-dir", str(root / "state")],
        "refresh": ["refresh", "--all", "--sources-dir", sources_dir],
        "fetch": ["fetch", source, "--sources-dir", sources_dir],
        "sources list": ["sources", "list", "--sources-dir", sources_dir],
        "audit-license": [
            "audit-license", source, "--offline",
            "--state-file", str(root / "license-hashes.json"),
            "--sources-dir", sources_dir,
        ],
    }
    assert set(argv) == set(SOURCES_DIR_COMMANDS), "argv table out of step with SOURCES_DIR_COMMANDS"
    return argv[command]
