from __future__ import annotations

from pathlib import Path

import typer

from legal_corpus_ingester.sources.registry import load_all
from legal_corpus_ingester.pipeline.state import CheckpointStore

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
