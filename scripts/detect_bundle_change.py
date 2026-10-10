#!/usr/bin/env python3
"""Decide whether a refresh published a new corpus bundle (terms-analysis#90).

The refresh workflow used to compare ``readlink out/current`` before and after
``ingester refresh``. ``actions/checkout`` runs ``git clean -ffdx`` and wipes
the gitignored ``out/`` first, so the "before" value was always empty and every
successful refresh counted as a change. This script compares the bundle that
the refresh just published against a record of the last bundle that was
announced. That record lives in a state file outside the checkout (by default
``$XDG_STATE_HOME`` or ``~/.local/state`` on the runner), so it survives the
clean. The record persists only as long as the runner's home directory does;
a fresh runner starts with no record.

Change is decided by content, not by name. The fingerprint is the SHA256 of the
bundle's ``checksums.txt`` lines, leaving out ``MANIFEST.yaml`` because that
file carries run metadata. A new version name also counts as a change.

Subcommands:
  detect  Print ``changed=true|false`` and ``new_target=<version>`` and append
          them to ``--github-output`` if given. Does not write the state file.
  record  Write the current bundle's version and fingerprint to the state file.
          The workflow runs this only after the alert step succeeds, so a failed
          alert is retried on the next run.

Exit 0: decision made (detect) or record written (record).
Exit 1: refresh reported success but no valid bundle is published (missing
        ``current`` link, bad version name, missing ``checksums.txt``), or the
        state file is unreadable. These are loud failures, never "no change".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# Calver bundle name written by TermsAnalysisPublisher, e.g. "2026.07.0".
# Gating the format also stops newline or shell metacharacters reaching GITHUB_OUTPUT.
VERSION_RE = re.compile(r"^[0-9]{4}\.[0-9]+\.[0-9]+$")
# Excluded from the fingerprint: it records run metadata, not corpus content.
_FINGERPRINT_EXCLUDE = frozenset({"MANIFEST.yaml"})


def default_state_file() -> Path:
    """``$XDG_STATE_HOME`` (or ``~/.local/state``), outside any checkout."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "legal-corpus-ingester" / "last-published-bundle.json"


class BundleError(Exception):
    """The published bundle or the state file is not in a usable state."""


def read_bundle(link: Path) -> tuple[str, str]:
    """Return ``(version, fingerprint)`` for the bundle *link* points at."""
    if not link.is_symlink():
        raise BundleError(f"{link} is not a symlink; refresh published no bundle")
    version = os.readlink(link)
    if not VERSION_RE.match(version):
        raise BundleError(f"unexpected bundle version format: {version!r}")
    checksums = link.parent / version / "checksums.txt"
    if not checksums.is_file():
        raise BundleError(f"{checksums} is missing; bundle is incomplete")
    lines = []
    for raw in checksums.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        _sha, _, rel = raw.partition("  ")
        if rel.strip() in _FINGERPRINT_EXCLUDE:
            continue
        lines.append(raw.strip())
    if not lines:
        raise BundleError(f"{checksums} lists no content files")
    fingerprint = hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()
    return version, fingerprint


def read_state(state_file: Path) -> dict[str, str] | None:
    """Return the last recorded bundle, or None when nothing was ever recorded."""
    if not state_file.exists():
        return None
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # A corrupt record must not be read as "no change": fail loudly.
        raise BundleError(f"cannot read state file {state_file}: {exc}") from exc
    if not isinstance(data, dict) or not {"version", "fingerprint"} <= data.keys():
        raise BundleError(f"state file {state_file} lacks version/fingerprint")
    return {"version": str(data["version"]), "fingerprint": str(data["fingerprint"])}


def is_changed(current: tuple[str, str], previous: dict[str, str] | None) -> bool:
    """A first publish, a new version name or new content is a change."""
    if previous is None:
        return True
    version, fingerprint = current
    return version != previous["version"] or fingerprint != previous["fingerprint"]


def write_state(state_file: Path, version: str, fingerprint: str) -> None:
    """Write the record atomically so a crash never leaves a half-written file."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_name(state_file.name + ".tmp")
    tmp.write_text(
        json.dumps({"version": version, "fingerprint": fingerprint}) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, state_file)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("detect", "record"))
    parser.add_argument("--bundle-link", type=Path, default=Path("out/current"))
    parser.add_argument(
        "--state-file",
        type=Path,
        default=default_state_file(),
        help="Record of the last announced bundle (default: %(default)s)",
    )
    parser.add_argument("--github-output", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        version, fingerprint = read_bundle(args.bundle_link)
        if args.command == "record":
            write_state(args.state_file, version, fingerprint)
            print(f"recorded bundle {version} ({fingerprint[:12]})")
            return 0
        changed = is_changed((version, fingerprint), read_state(args.state_file))
    except BundleError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    lines = [f"changed={'true' if changed else 'false'}", f"new_target={version}"]
    for line in lines:
        print(line)
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
