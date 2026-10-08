"""Shared directory checks for the scheduled-job scripts (terms-analysis#173).

health_check.py and check_approvals.py both read a directory of ``*.yaml``
files that an operator controls. This module is the one place that decides:

* what counts as a config file (a regular ``*.yaml`` file, symlinks to files
  followed, hidden names included exactly as the source registry's
  ``glob("*.yaml")`` includes them), and what is a config problem (a missing,
  non-directory or unreadable dir, or a ``*.yaml`` entry that is not a file);
* how an untrusted path is rendered into a terminal or CI log
  (:func:`display_path`), so a crafted directory name cannot forge log lines.

Keeping both rules here stops the two jobs drifting apart (DEV-FUNDAMENTALS F2, F10).
"""
from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path

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
