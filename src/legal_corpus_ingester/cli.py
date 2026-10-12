from __future__ import annotations

import contextlib
import errno
import importlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path

import httpx
import typer

from legal_corpus_ingester.sources.registry import RegistryLoadError, load_all
from legal_corpus_ingester.sources.schema import SourceConfig
from legal_corpus_ingester.pipeline.state import CheckpointStore
from legal_corpus_ingester.provenance.license_audit import (
    hash_content,
    check_license_drift,
    record_license_baseline,
    DriftStatus,
)

# Public exit-code contract (#108, owner ruling 2026-10-12). `ingester --help` prints it and
# automations/*.md document it, so callers key on the numbers: renumbering is a breaking change.
# Each code has exactly one meaning; _EXIT_MEANINGS below holds the help text for each.
#
# The sources dir loads cleanly but holds no source (refresh and status).
EXIT_NO_SOURCES: int = 5
# The --sources-dir path does not exist or is not a directory, on every command that takes
# --sources-dir (#108; the fold-in of 2026-10-12 added fetch, sources list and audit-license to
# refresh and status). It was 1 before; it has its own code so a wrong path is not mistaken for
# a broken config.
EXIT_SOURCES_DIR_MISSING: int = 6
# The sources dir exists but could not be loaded (unreadable, or a config that fails to parse or
# validate). Every --sources-dir command shares it so they cannot drift (terms-analysis#190).
EXIT_LOAD_ERROR: int = 1
# Message stems shared by refresh and status; the tests and health docs key on them.
MSG_LOAD_ERROR = "Error loading sources"
MSG_NO_SOURCES = "No sources configured"
MSG_SOURCES_DIR_MISSING = "Sources directory does not exist or is not a directory"
MSG_NO_RUNS = "No runs recorded."
# Pipeline not wired to the orchestrator yet (terms-analysis#90): must never look like success
EXIT_NOT_WIRED: int = 3
# Single source for "does `ingester refresh` run the pipeline yet?". scripts/health_check.py
# reads it to tell the expected never-run state (no checkpoint can exist yet) apart from a real
# failure. Flip it in the change that wires refresh; test_cli_exit_codes pins it to refresh's
# actual exit code, so wiring refresh without flipping it fails CI (terms-analysis#90).
REFRESH_WIRED: bool = False
# validate-round-trip could not import the terms-analysis consumer, so the bundle was NOT
# verified end to end; must not look like VALID unless --allow-missing-consumer (terms-analysis#90)
EXIT_CONSUMER_SKIPPED: int = 4
# Command-line usage errors only: click's conventional code for an unknown command or option, a
# missing argument or value, and (from main) no subcommand at all. No command body exits with it.
EXIT_USAGE: int = 2
# Help text for each public exit code, rendered as "<code> when <meaning>" clauses by
# _exit_code_clauses into the top-level epilog and the status help. A meaning must not contain
# ";" (the clause separator) or square brackets (rich markup).
_EXIT_MEANINGS: dict[int, str] = {
    EXIT_LOAD_ERROR: (
        "the sources dir exists but cannot be loaded (it is unreadable, or a source config fails"
        " to parse or validate)"
    ),
    EXIT_USAGE: (
        "the command line is a usage error (unknown command or option, missing argument or"
        " value, no command)"
    ),
    EXIT_NOT_WIRED: "the pipeline behind the command is not wired yet (fetch, refresh)",
    EXIT_CONSUMER_SKIPPED: (
        "validate-round-trip could not import the terms-analysis consumer, so the bundle was"
        " not verified end to end"
    ),
    # Scoped: fetch and audit-license call a source missing from an empty dir unknown (1), and
    # `sources list` prints that nothing is configured and exits 0.
    EXIT_NO_SOURCES: "the sources dir loads but holds no source (refresh, status)",
    EXIT_SOURCES_DIR_MISSING: "the --sources-dir path does not exist or is not a directory",
}
# os.stat errno values meaning "this path names no directory": a component is missing or not a
# directory, the name is too long to exist, or a symlink loop resolves to nothing. Any other
# OSError (EACCES on a parent, EIO) is not "missing": the load step then fails on it with
# EXIT_LOAD_ERROR, so an unknown error never passes for a wrong path.
_DIR_MISSING_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ENAMETOOLONG, errno.ELOOP})
# The terms-analysis module validate-round-trip loads. Only a ModuleNotFoundError naming this
# module or one of its parent packages means "consumer not installed".
_CONSUMER_MODULE = "backend.app.services.legal_kb"


def _exit_code_clauses(codes: tuple[int, ...]) -> str:
    """Render "<code> when <meaning>" clauses, joined by "; ", from the one meanings table."""
    return "; ".join(f"{code} when {_EXIT_MEANINGS[code]}" for code in codes)


def _sources_dir_missing(sources_dir: str) -> bool:
    """True when *sources_dir* names no directory: missing, dangling, a file, a FIFO, and so on.

    Only stat() is used, never open(), so a FIFO or device is classified without blocking. A
    ValueError (NUL, or a lone surrogate the file system encoding cannot carry) means no path on
    disk can have this name, so it is missing too.
    """
    try:
        st = os.stat(sources_dir)
    except ValueError:
        return True
    except OSError as exc:
        return exc.errno in _DIR_MISSING_ERRNOS
    return not stat.S_ISDIR(st.st_mode)


def _require_sources_dir(sources_dir: str) -> Path:
    """Return *sources_dir* as a Path, or exit EXIT_SOURCES_DIR_MISSING when it names no directory.

    Shared by every --sources-dir command, called before its load-error handler. The message never
    echoes the argument: it can be huge or carry control characters, and an absolute path is a
    leak (F2, F8).
    """
    if _sources_dir_missing(sources_dir):
        typer.echo(
            f"{MSG_SOURCES_DIR_MISSING}; pass --sources-dir a directory of *.yaml source configs.",
            err=True,
        )
        raise typer.Exit(code=EXIT_SOURCES_DIR_MISSING)
    return Path(sources_dir)


def _load_sources_dir(sources_dir: Path) -> dict[str, SourceConfig]:
    """Load an existing sources dir for any --sources-dir command; raise on any load failure.

    load_all lists with Path.glob, which silently skips a directory it cannot read, so an
    unreadable dir would pass for one holding no source (EXIT_NO_SOURCES). The listing is opened
    once first so that fails as a load error instead. Only the error type is kept in the
    message: the OSError text carries the path.
    """
    try:
        with os.scandir(sources_dir) as entries:
            next(entries, None)
    except OSError as exc:
        raise RegistryLoadError(f"Sources directory cannot be read ({type(exc).__name__})") from exc
    return load_all(sources_dir)


def _is_consumer_missing(exc: ModuleNotFoundError) -> bool:
    """True only when *exc* names the consumer module itself or one of its parent packages.

    A missing transitive dependency of an installed consumer (exc.name == "numpy") is a
    broken consumer, not an absent one. exc.name None (cause unknown) fails closed.
    """
    name = exc.name
    if not name:
        return False
    return name == _CONSUMER_MODULE or _CONSUMER_MODULE.startswith(name + ".")

# Main app
app = typer.Typer(
    name="ingester",
    help="Legal corpus ingester CLI — fetch, clean, chunk, embed, and publish legal text.",
    # Every public exit code, from the one meanings table (#108).
    epilog=(
        "Exit codes: "
        + _exit_code_clauses(tuple(sorted(_EXIT_MEANINGS)))
        + ". Other command failures (unknown source, license drift, invalid bundle) also exit 1."
    ),
    no_args_is_help=True,
)

# Sources sub-app, grouped under `sources`
sources_app = typer.Typer(
    name="sources",
    help="Manage source configurations.",
    no_args_is_help=True,
)
app.add_typer(sources_app, name="sources")


@app.command()
def init(
    base_path: Path = typer.Option(
        Path("."),
        "--base-path",
        help="Root directory for created directories (defaults to cwd).",
    ),
) -> None:
    """Create config/, out/, and state/ directories under the base path."""
    for dirname in ("config", "out", "state"):
        target = base_path / dirname
        target.mkdir(parents=True, exist_ok=True)
        typer.echo(f"Created: {target.resolve()}")
    typer.echo("Workspace initialised.")


@sources_app.command(
    "list",
    help=(
        "List all configured sources (name + jurisdiction).\n\n"
        f"Exit codes: 0 when the sources dir loads (one line per source, or '{MSG_NO_SOURCES}.'"
        " when it holds none); "
        + _exit_code_clauses((EXIT_SOURCES_DIR_MISSING, EXIT_LOAD_ERROR))
        + "."
    ),
)
def sources_list(
    # A plain str, not Path: typer's Path type stats the argument itself and fails on NUL or a
    # lone surrogate before _require_sources_dir can classify it (#108).
    sources_dir: str = typer.Option(
        "config/sources",
        "--sources-dir",
        help="Directory containing *.yaml source configs.",
    ),
) -> None:
    sources_path = _require_sources_dir(sources_dir)
    try:
        registry = _load_sources_dir(sources_path)
    except Exception as exc:
        typer.echo(f"{MSG_LOAD_ERROR}: {exc}", err=True)
        raise typer.Exit(code=EXIT_LOAD_ERROR)

    if not registry:
        typer.echo("No sources configured.")
        return

    # Print a simple table
    name_w = max(len(name) for name in registry) + 2
    typer.echo(f"{'NAME':<{name_w}}  JURISDICTION")
    typer.echo("-" * (name_w + 14))
    for name, cfg in registry.items():
        typer.echo(f"{name:<{name_w}}  {cfg.jurisdiction}")


@app.command(
    help=(
        "Show the last checkpoint stage for each configured source.\n\n"
        f"Exit codes: 0 when sources are configured (one '<name>: <stage>' line per source with a"
        f" checkpoint, or '{MSG_NO_RUNS}' when none has run yet); "
        + _exit_code_clauses((EXIT_NO_SOURCES, EXIT_SOURCES_DIR_MISSING, EXIT_LOAD_ERROR))
        + ". The failure codes match `ingester refresh` on the same sources dir."
    )
)
def status(
    # A plain str, not Path: typer's Path type stats the argument itself and fails on NUL or a
    # lone surrogate before _require_sources_dir can classify it (#108).
    sources_dir: str = typer.Option(
        "config/sources",
        "--sources-dir",
        help="Directory containing *.yaml source configs.",
    ),
    state_dir: Path = typer.Option(
        Path("state"),
        "--state-dir",
        help="Directory containing checkpoint files.",
    ),
) -> None:
    # "Nothing configured" and "config unloadable" are failures, never "No runs recorded."
    # (DEV-FUNDAMENTALS F5, terms-analysis#190). Checkpoints are read only for configured
    # sources: an orphan checkpoint must not make an empty config look configured, and its
    # file name (untrusted) is never echoed.
    sources_path = _require_sources_dir(sources_dir)
    try:
        registry = _load_sources_dir(sources_path)
    except Exception as exc:
        # Only the exception type is shown: the loader's message can carry absolute paths and
        # raw bytes from file names or YAML content (F8). The type is a code identifier.
        typer.echo(
            f"{MSG_LOAD_ERROR} ({type(exc).__name__}); check the *.yaml files in --sources-dir."
            " `ingester sources list` prints the loader's full message.",
            err=True,
        )
        raise typer.Exit(code=EXIT_LOAD_ERROR)

    if not registry:
        typer.echo(f"{MSG_NO_SOURCES} in --sources-dir; nothing to report.", err=True)
        raise typer.Exit(code=EXIT_NO_SOURCES)

    store = CheckpointStore(state_dir)
    any_recorded = False
    for name in registry:
        # Registry names passed SourceConfig's name_is_safe allowlist, so echoing them is safe.
        state = store.load(name)
        if state:
            any_recorded = True
            typer.echo(f"{name}: {state.stage}")

    if not any_recorded:
        typer.echo(MSG_NO_RUNS)


@app.command(
    help=(
        "Fetch a source (use --dry-run to preview without writing).\n\n"
        "Exit codes: 0 when --dry-run prints what would be fetched; "
        + _exit_code_clauses((EXIT_NOT_WIRED, EXIT_SOURCES_DIR_MISSING, EXIT_LOAD_ERROR))
        + ". An unknown source also exits 1."
    )
)
def fetch(
    source: str = typer.Argument(..., help="Name of the source to fetch."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print what would be fetched without writing anything.",
    ),
    # A plain str, not Path: typer's Path type stats the argument itself and fails on NUL or a
    # lone surrogate before _require_sources_dir can classify it (#108).
    sources_dir: str = typer.Option(
        "config/sources",
        "--sources-dir",
        help="Directory containing *.yaml source configs.",
    ),
) -> None:
    sources_path = _require_sources_dir(sources_dir)
    try:
        registry = _load_sources_dir(sources_path)
    except Exception as exc:
        typer.echo(f"{MSG_LOAD_ERROR}: {exc}", err=True)
        raise typer.Exit(code=EXIT_LOAD_ERROR)

    if source not in registry:
        typer.echo(f"Unknown source: {source!r}. Use 'ingester sources list' to see available sources.", err=True)
        raise typer.Exit(code=1)

    cfg = registry[source]

    if dry_run:
        typer.echo(f"DRY RUN: would fetch {source!r} from {cfg.base_url}")
        return

    # Actual fetch not implemented in this thin CLI layer — delegate to pipeline
    typer.echo(f"Fetching {source!r} from {cfg.base_url} ...")
    # Fail loudly: exiting 0 here would report a no-op as success (terms-analysis#90)
    typer.echo("(Full fetch pipeline not yet wired — use orchestrator directly.)", err=True)
    raise typer.Exit(code=EXIT_NOT_WIRED)


@app.command()
def refresh(
    all_sources: bool = typer.Option(
        False,
        "--all",
        help="Refresh all configured sources.",
    ),
    # A plain str, not Path: typer's Path type stats the argument itself and fails on NUL or a
    # lone surrogate before _require_sources_dir can classify it (#108).
    sources_dir: str = typer.Option(
        "config/sources",
        "--sources-dir",
        help="Directory containing *.yaml source configs.",
    ),
) -> None:
    """Refresh corpus bundles for all configured sources (or a single source)."""
    sources_path = _require_sources_dir(sources_dir)
    try:
        registry = _load_sources_dir(sources_path)
    except Exception as exc:
        typer.echo(f"{MSG_LOAD_ERROR}: {exc}", err=True)
        raise typer.Exit(code=EXIT_LOAD_ERROR)

    if not registry:
        typer.echo(f"{MSG_NO_SOURCES}; nothing to refresh.")
        raise typer.Exit(code=EXIT_NO_SOURCES)

    # Full pipeline orchestration not yet wired — placeholder for Phase 1.
    typer.echo(f"Refreshing {len(registry)} source(s): {', '.join(registry)}")
    # Fail loudly: exiting 0 here would report a no-op as success (terms-analysis#90)
    typer.echo("(Full refresh pipeline not yet wired — use orchestrator directly.)", err=True)
    raise typer.Exit(code=EXIT_NOT_WIRED)


@app.command(
    "audit-license",
    help=(
        "Check license drift for a source against the stored baseline.\n\n"
        "Exit codes: 0 when the license is unchanged, new or has no URL, or (with --offline)"
        " when the stored baseline, or its absence, is shown; "
        + _exit_code_clauses((EXIT_SOURCES_DIR_MISSING, EXIT_LOAD_ERROR))
        + ". An unknown source, license drift or a failed license fetch also exits 1."
    ),
)
def audit_license(
    source: str = typer.Argument(..., help="Name of the source to audit."),
    # A plain str, not Path: typer's Path type stats the argument itself and fails on NUL or a
    # lone surrogate before _require_sources_dir can classify it (#108).
    sources_dir: str = typer.Option(
        "config/sources",
        "--sources-dir",
        help="Directory containing *.yaml source configs.",
    ),
    state_file: Path = typer.Option(
        Path("state/license-hashes.json"),
        "--state-file",
        help="Path to license-hashes.json baseline state file.",
    ),
    update_baseline: bool = typer.Option(
        False,
        "--update-baseline",
        help="Record current hash as new baseline.",
    ),
    offline: bool = typer.Option(
        False,
        "--offline",
        help="Skip live fetch; compare stored baseline only (for restricted-egress CI).",
    ),
) -> None:
    # Load source registry
    sources_path = _require_sources_dir(sources_dir)
    try:
        registry = _load_sources_dir(sources_path)
    except Exception as exc:
        typer.echo(f"{MSG_LOAD_ERROR}: {exc}", err=True)
        raise typer.Exit(code=EXIT_LOAD_ERROR)

    if source not in registry:
        typer.echo(
            f"Unknown source: {source!r}. Use 'ingester sources list' to see available sources.",
            err=True,
        )
        raise typer.Exit(code=1)

    cfg = registry[source]
    license_url: str = cfg.license.url
    spdx: str = cfg.license.spdx

    # No license URL configured — nothing to audit
    if not license_url:
        typer.echo(f"No license URL configured for {source!r}.")
        return

    if offline:
        # --offline: read stored baseline only; skip live fetch.
        stored: dict[str, object] = {}
        if state_file.exists():
            try:
                stored = json.loads(state_file.read_text(encoding="utf-8"))
            except Exception as exc:
                typer.echo(f"Error reading state file {state_file}: {exc}", err=True)
                raise typer.Exit(code=1)
        entry = stored.get(source, {})
        if not entry:
            typer.echo(f"source     : {source}")
            typer.echo(f"spdx       : {spdx}")
            typer.echo("status     : NO_BASELINE -- no stored entry (run without --offline to record)")
            typer.echo("hash[:12]  : n/a")
            return
        # Guard: entry must be a dict; non-dict values indicate state file corruption.
        if not isinstance(entry, dict):
            typer.echo(
                f"Error: state file entry for {source!r} is not a dict (got {type(entry).__name__!r}).",
                err=True,
            )
            raise typer.Exit(code=1)
        entry_dict: dict[str, object] = entry
        stored_hash = str(entry_dict.get("hash", ""))
        stored_spdx = str(entry_dict.get("spdx", spdx))
        typer.echo(f"source     : {source}")
        typer.echo(f"spdx       : {stored_spdx}")
        typer.echo("status     : BASELINE_ONLY -- offline mode; stored hash shown")
        typer.echo(f"hash[:12]  : {stored_hash[:12]}")
        return

    # Fetch the license content synchronously
    try:
        response = httpx.get(license_url, follow_redirects=True, timeout=30.0)
        response.raise_for_status()
    except Exception as exc:
        typer.echo(f"Error fetching license URL {license_url!r}: {exc}", err=True)
        raise typer.Exit(code=1)

    # Hash + compare against baseline
    current_hash = hash_content(response.text)
    drift_status = check_license_drift(source, current_hash, spdx, state_file)

    # Status descriptions per DriftStatus value
    status_messages: dict[DriftStatus, str] = {
        DriftStatus.NO_CHANGE: "license unchanged",
        DriftStatus.NEW: "new baseline (not yet recorded)",
        DriftStatus.DRIFT: "license content changed (SPDX same)",
        DriftStatus.SPDX_DRIFT: "SPDX changed -- publish blocked per HR8",
    }

    # Print status table
    hash_short = current_hash[:12]
    status_label = status_messages[drift_status]
    typer.echo(f"source     : {source}")
    typer.echo(f"spdx       : {spdx}")
    typer.echo(f"status     : {drift_status.value} -- {status_label}")
    typer.echo(f"hash[:12]  : {hash_short}")

    # Optionally record as new baseline — blocked when drift is already detected
    if update_baseline:
        if drift_status in (DriftStatus.DRIFT, DriftStatus.SPDX_DRIFT):
            typer.echo(
                "error: cannot update baseline while drift is detected; "
                "resolve the drift first.",
                err=True,
            )
            raise typer.Exit(code=1)
        record_license_baseline(source, current_hash, spdx, state_file)
        typer.echo("baseline updated.")

    # Exit 1 for DRIFT and SPDX_DRIFT
    if drift_status in (DriftStatus.DRIFT, DriftStatus.SPDX_DRIFT):
        raise typer.Exit(code=1)


@app.command("validate-round-trip")
def validate_round_trip(
    bundle_dir: Path = typer.Argument(..., help="Path to the bundle directory to validate."),
    allow_missing_consumer: bool = typer.Option(
        False,
        "--allow-missing-consumer",
        help=(
            "Treat a missing terms-analysis consumer as a pass (structural checks only)."
            f" Without this flag a missing consumer exits {EXIT_CONSUMER_SKIPPED}."
        ),
    ),
) -> None:
    """Validate a published bundle is structurally sound and consumer-compatible."""
    from legal_corpus_ingester.pipeline.manifest import Manifest, ManifestError

    # 1. Confirm bundle_dir is an existing directory
    if not bundle_dir.is_dir():
        typer.echo(f"Error: {bundle_dir} is not a directory.", err=True)
        raise typer.Exit(code=1)

    # 2. Load manifest
    try:
        manifest = Manifest.load(bundle_dir)
    except ManifestError as exc:
        typer.echo(f"X-Corpus-Mismatch: manifest-missing-or-invalid ({exc})")
        raise typer.Exit(code=1)

    # 3. Validate required fields are non-empty
    required_fields = {
        "embedder_model": manifest.embedder_model,
        "embedder_revision": manifest.embedder_revision,
        "chunker_version": manifest.chunker_version,
    }
    for field_name, field_value in required_fields.items():
        if not field_value:
            typer.echo(f"X-Corpus-Mismatch: {field_name}-empty")
            raise typer.Exit(code=1)

    # 4. Check required subdirectories exist
    required_dirs = ("corpus", "index", "provenance")
    for dir_name in required_dirs:
        if not (bundle_dir / dir_name).is_dir():
            typer.echo(f"X-Corpus-Mismatch: {dir_name}-directory-missing")
            raise typer.Exit(code=1)

    # 5. Attempt terms-analysis consumer round-trip. A missing consumer is NOT a pass by
    # default: it exits EXIT_CONSUMER_SKIPPED unless --allow-missing-consumer (terms-analysis#90).
    consumer_checked = False
    # The import has its own try so only the import can mean "not installed". A
    # ModuleNotFoundError for a transitive dependency, or one raised lazily inside
    # load_from_bundle/retrieve, is a broken consumer: exit 1 even with --allow-missing-consumer.
    try:
        legal_kb_mod = importlib.import_module(_CONSUMER_MODULE)
    except ModuleNotFoundError as exc:
        if not _is_consumer_missing(exc):
            typer.echo(f"X-Corpus-Mismatch: terms-analysis error: {exc}")
            raise typer.Exit(code=1)
        if not allow_missing_consumer:
            typer.echo(
                "Error: terms-analysis not installed; consumer round-trip NOT checked."
                " Re-run where terms-analysis is importable, or pass --allow-missing-consumer"
                " for a structural-only check.",
                err=True,
            )
            raise typer.Exit(code=EXIT_CONSUMER_SKIPPED)
        typer.echo("terms-analysis not installed; skipping consumer round-trip check.")
        legal_kb_mod = None
    except Exception as exc:
        typer.echo(f"X-Corpus-Mismatch: terms-analysis error: {exc}")
        raise typer.Exit(code=1)

    if legal_kb_mod is not None:
        try:
            kb_cls = getattr(legal_kb_mod, "LegalKnowledgeBase")
            kb = kb_cls()
            kb.load_from_bundle(bundle_dir)
            result = kb.retrieve("test query")
            if not result or not any(getattr(r, "text", r) for r in result):
                typer.echo("X-Corpus-Mismatch: terms-analysis retrieve returned empty result")
                raise typer.Exit(code=1)
            typer.echo(f"terms-analysis round-trip: OK ({len(result)} chunks returned)")
            consumer_checked = True
        except typer.Exit:
            raise
        except Exception as exc:
            typer.echo(f"X-Corpus-Mismatch: terms-analysis error: {exc}")
            raise typer.Exit(code=1)

    # 6. Pass; the label says whether the consumer check actually ran
    verdict = "VALID" if consumer_checked else "VALID (consumer check skipped)"
    typer.echo(
        f"bundle {bundle_dir.name}: {verdict}"
        f" (embedder={manifest.embedder_model},"
        f" chunker={manifest.chunker_version},"
        f" chunks={manifest.chunk_count})"
    )


@app.command()
def prune(
    out_dir: Path = typer.Option(
        Path("out"),
        "--out-dir",
        help="Directory containing versioned bundles.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print what would be pruned without deleting.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Actually delete prunable bundles (required to delete).",
    ),
) -> None:
    """Apply retention policy to bundle output directory."""
    from legal_corpus_ingester.pipeline.retention import plan

    keep, prune_list = plan(out_dir)

    typer.echo(f"{len(keep)} bundle(s) to keep, {len(prune_list)} to prune.")

    if not prune_list:
        typer.echo("Nothing to prune.")
        return

    if dry_run:
        for p in prune_list:
            typer.echo(f"  would prune: {p}")
        return

    if not force:
        typer.echo("Use --dry-run to preview or --force to delete.")
        return

    # --force: delete each prunable bundle tree (symlinks are never rmtree'd)
    for p in prune_list:
        if p.is_symlink():
            typer.echo(f"  skipped symlink: {p.name}", err=True)
            continue
        shutil.rmtree(p)
        typer.echo(f"  pruned: {p.name}")

    typer.echo("Done.")


def main(argv: list[str] | None = None) -> None:
    """Entry point for the `ingester` console script and `python -m legal_corpus_ingester.cli`.

    Both go through here, so they behave the same on the same input (terms-analysis#173).

    With no subcommand, Typer's `no_args_is_help` prints help to STDOUT, which a
    caller capturing stdout could mistake for real output. Print the help to
    stderr instead and exit with the usage-error code, so "did nothing" is never
    confused with a run. Everything else is the Typer app unchanged.
    """
    args = sys.argv[1:] if argv is None else argv
    if not args:
        with contextlib.redirect_stdout(sys.stderr):
            try:
                app(args=["--help"])
            except SystemExit:
                # --help always ends in SystemExit(0); the exit code is set below.
                pass
        typer.echo("Error: Missing command.", err=True)
        raise SystemExit(EXIT_USAGE)
    app(args=args)


if __name__ == "__main__":
    main()
