"""G0-1 acceptance tests: unwired `fetch` / `refresh` must not report success.

Tracking card: jennifer-mckinney/terms-analysis#90.

Until the orchestrator is wired (G2-1), `ingester fetch <source>` and
`ingester refresh --all` do no work. They must exit non-zero and say so on
stderr, so a scheduled job (refresh.yml) cannot go green while doing nothing.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from legal_corpus_ingester.cli import app

# Resolve the real source configs relative to the repo root so the test does
# not depend on the pytest working directory.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCES_DIR = _REPO_ROOT / "config" / "sources"

_NOT_WIRED = "not yet wired"

runner = CliRunner()


def test_fetch_unwired_exits_nonzero() -> None:
    # Guard: the source must exist, otherwise a non-zero exit would come from
    # "Unknown source" and the test would pass for the wrong reason.
    assert (_SOURCES_DIR / "eurlex_gdpr.yaml").is_file()

    result = runner.invoke(
        app, ["fetch", "eurlex_gdpr", "--sources-dir", str(_SOURCES_DIR)]
    )

    assert result.exit_code != 0, (
        f"unwired fetch reported success (exit 0); stdout={result.stdout!r}"
    )
    assert _NOT_WIRED in result.stderr, (
        f"'{_NOT_WIRED}' not on stderr; stderr={result.stderr!r}"
    )


def test_refresh_unwired_exits_nonzero() -> None:
    # Guard: an empty registry exits EXIT_NO_SOURCES, which is a different
    # (already non-zero) path; make sure configs are present.
    assert any(_SOURCES_DIR.glob("*.yaml"))

    result = runner.invoke(
        app, ["refresh", "--all", "--sources-dir", str(_SOURCES_DIR)]
    )

    assert result.exit_code != 0, (
        f"unwired refresh reported success (exit 0); stdout={result.stdout!r}"
    )
    assert _NOT_WIRED in result.stderr, (
        f"'{_NOT_WIRED}' not on stderr; stderr={result.stderr!r}"
    )
