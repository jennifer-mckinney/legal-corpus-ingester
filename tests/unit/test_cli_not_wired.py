"""G0-1 acceptance tests: `fetch` / `refresh` must not report success when they do no work.

Tracking cards: jennifer-mckinney/terms-analysis#90 (G0-1), #93 (G2-1).

G0-1 pinned the unwired stub: exit non-zero and say so on stderr, so a scheduled job
(refresh.yml) cannot go green while doing nothing. G2-1 wired both commands to the
orchestrator; the same guarantee now holds for the case where no source holds a valid
approval: nothing runs, the exit is non-zero and stderr names the approval as the reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from legal_corpus_ingester.cli import app

# Resolve the real source configs relative to the repo root so the test does
# not depend on the pytest working directory.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCES_DIR = _REPO_ROOT / "config" / "sources"

# The reason stderr must give when nothing ran (terms-analysis#93).
_NOTHING_RAN_REASON = "approval"

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_approvals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An empty approvals dir from env, not the repo's: no source may run, and a signed repo
    # approval can never turn these tests into live fetches.
    (tmp_path / "approvals").mkdir()
    monkeypatch.setenv("INGESTER_CONFIG_DIR", str(tmp_path))


def test_fetch_unwired_exits_nonzero() -> None:
    # Guard: the source must exist, otherwise a non-zero exit would come from
    # "Unknown source" and the test would pass for the wrong reason.
    assert (_SOURCES_DIR / "eurlex_gdpr.yaml").is_file()

    result = runner.invoke(
        app, ["fetch", "eurlex_gdpr", "--sources-dir", str(_SOURCES_DIR)]
    )

    assert result.exit_code != 0, (
        f"fetch with no approval reported success (exit 0); stdout={result.stdout!r}"
    )
    assert _NOTHING_RAN_REASON in result.stderr, (
        f"'{_NOTHING_RAN_REASON}' not on stderr; stderr={result.stderr!r}"
    )


def test_refresh_unwired_exits_nonzero() -> None:
    # Guard: an empty registry exits EXIT_NO_SOURCES, which is a different
    # (already non-zero) path; make sure configs are present.
    assert any(_SOURCES_DIR.glob("*.yaml"))

    result = runner.invoke(
        app, ["refresh", "--all", "--sources-dir", str(_SOURCES_DIR)]
    )

    assert result.exit_code != 0, (
        f"refresh with no approval reported success (exit 0); stdout={result.stdout!r}"
    )
    assert _NOTHING_RAN_REASON in result.stderr, (
        f"'{_NOTHING_RAN_REASON}' not on stderr; stderr={result.stderr!r}"
    )
