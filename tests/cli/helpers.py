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
