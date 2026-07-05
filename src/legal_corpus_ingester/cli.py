from __future__ import annotations

import importlib
import json
import shutil
from pathlib import Path

import httpx
import typer

from legal_corpus_ingester.sources.registry import load_all
from legal_corpus_ingester.pipeline.state import CheckpointStore
from legal_corpus_ingester.provenance.license_audit import (
    hash_content,
    check_license_drift,
    record_license_baseline,
    DriftStatus,
)

# Exit codes for refresh — distinguishes "no sources" from real failure
EXIT_NO_SOURCES: int = 2

# Main app
app = typer.Typer(
    name="ingester",
    help="Legal corpus ingester CLI — fetch, clean, chunk, embed, and publish legal text.",
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


@sources_app.command("list")
def sources_list(
    sources_dir: Path = typer.Option(
        Path("config/sources"),
        "--sources-dir",
        help="Directory containing *.yaml source configs.",
    ),
) -> None:
    """List all configured sources (name + jurisdiction)."""
    try:
        registry = load_all(sources_dir)
    except Exception as exc:
        typer.echo(f"Error loading sources: {exc}", err=True)
        raise typer.Exit(code=1)

    if not registry:
        typer.echo("No sources configured.")
        return

    # Print a simple table
    name_w = max(len(name) for name in registry) + 2
    typer.echo(f"{'NAME':<{name_w}}  JURISDICTION")
    typer.echo("-" * (name_w + 14))
    for name, cfg in registry.items():
        typer.echo(f"{name:<{name_w}}  {cfg.jurisdiction}")


@app.command()
def status(
    sources_dir: Path = typer.Option(
        Path("config/sources"),
        "--sources-dir",
        help="Directory containing *.yaml source configs.",
    ),
    state_dir: Path = typer.Option(
        Path("state"),
        "--state-dir",
        help="Directory containing checkpoint files.",
    ),
) -> None:
    """Show the last checkpoint stage for each configured source."""
    # Load sources; if the directory doesn't exist, still try to read checkpoints
    try:
        registry = load_all(sources_dir)
    except Exception:
        registry = {}

    store = CheckpointStore(state_dir)

    if not registry:
        # Fall back to scanning the state dir directly
        if state_dir.is_dir():
            checkpoints = sorted(state_dir.glob("*.checkpoint.json"))
        else:
            checkpoints = []

        if not checkpoints:
            typer.echo("No runs recorded.")
            return

        for cp_path in checkpoints:
            source_name = cp_path.name.replace(".checkpoint.json", "")
            state = store.load(source_name)
            if state:
                typer.echo(f"{source_name}: {state.stage}")
        return

    any_recorded = False
    for name in registry:
        state = store.load(name)
        if state:
            any_recorded = True
            typer.echo(f"{name}: {state.stage}")

    if not any_recorded:
        typer.echo("No runs recorded.")


@app.command()
def fetch(
    source: str = typer.Argument(..., help="Name of the source to fetch."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print what would be fetched without writing anything.",
    ),
    sources_dir: Path = typer.Option(
        Path("config/sources"),
        "--sources-dir",
        help="Directory containing *.yaml source configs.",
    ),
) -> None:
    """Fetch a source (use --dry-run to preview without writing)."""
    try:
        registry = load_all(sources_dir)
    except Exception as exc:
        typer.echo(f"Error loading sources: {exc}", err=True)
        raise typer.Exit(code=1)

    if source not in registry:
        typer.echo(f"Unknown source: {source!r}. Use 'ingester sources list' to see available sources.", err=True)
        raise typer.Exit(code=1)

    cfg = registry[source]

    if dry_run:
        typer.echo(f"DRY RUN: would fetch {source!r} from {cfg.base_url}")
        return

    # Actual fetch not implemented in this thin CLI layer — delegate to pipeline
    typer.echo(f"Fetching {source!r} from {cfg.base_url} ...")
    typer.echo("(Full fetch pipeline not yet wired — use orchestrator directly.)")


@app.command()
def refresh(
    all_sources: bool = typer.Option(
        False,
        "--all",
        help="Refresh all configured sources.",
    ),
    sources_dir: Path = typer.Option(
        Path("config/sources"),
        "--sources-dir",
        help="Directory containing *.yaml source configs.",
    ),
) -> None:
    """Refresh corpus bundles for all configured sources (or a single source)."""
    try:
        registry = load_all(sources_dir)
    except Exception as exc:
        typer.echo(f"Error loading sources: {exc}", err=True)
        raise typer.Exit(code=1)

    if not registry:
        typer.echo("No sources configured; nothing to refresh.")
        raise typer.Exit(code=EXIT_NO_SOURCES)

    # Full pipeline orchestration not yet wired — placeholder for Phase 1.
    typer.echo(f"Refreshing {len(registry)} source(s): {', '.join(registry)}")
    typer.echo("(Full refresh pipeline not yet wired — use orchestrator directly.)")


@app.command("audit-license")
def audit_license(
    source: str = typer.Argument(..., help="Name of the source to audit."),
    sources_dir: Path = typer.Option(
        Path("config/sources"),
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
    """Check license drift for a source against the stored baseline."""
    # Load source registry
    try:
        registry = load_all(sources_dir)
    except Exception as exc:
        typer.echo(f"Error loading sources: {exc}", err=True)
        raise typer.Exit(code=1)

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

    # 5. Attempt terms-analysis consumer round-trip (graceful skip if not installed)
    try:
        legal_kb_mod = importlib.import_module("backend.app.services.legal_kb")
        kb_cls = getattr(legal_kb_mod, "LegalKnowledgeBase")
        kb = kb_cls()
        kb.load_from_bundle(bundle_dir)
        result = kb.retrieve("test query")
        if not result or not any(getattr(r, "text", r) for r in result):
            typer.echo("X-Corpus-Mismatch: terms-analysis retrieve returned empty result")
            raise typer.Exit(code=1)
        typer.echo(f"terms-analysis round-trip: OK ({len(result)} chunks returned)")
    except ModuleNotFoundError:
        # ModuleNotFoundError (not ImportError) so module-level errors in a partially-installed
        # terms-analysis raise through to the broad except below instead of being silenced.
        typer.echo("terms-analysis not installed; skipping consumer round-trip check.")
    except typer.Exit:
        raise
    except Exception as exc:
        typer.echo(f"X-Corpus-Mismatch: terms-analysis error: {exc}")
        raise typer.Exit(code=1)

    # 6. Full pass
    typer.echo(
        f"bundle {bundle_dir.name}: VALID"
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
