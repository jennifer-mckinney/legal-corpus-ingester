"""Exit-code contract tests for unwired CLI commands (terms-analysis#90).

The behavioural acceptance tests live in test_cli_not_wired.py (owned by the
acceptance-test author, asserts only `!= 0`). The exact-code assertions below
pin the contract refresh.yml and operators rely on: unwired work exits exactly
EXIT_NOT_WIRED, not 1 (load/unknown-source error) or EXIT_NO_SOURCES.
"""
from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from legal_corpus_ingester import cli

runner = CliRunner()

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCES_DIR = _REPO_ROOT / "config" / "sources"


def test_exit_codes_are_pairwise_distinct() -> None:
    # Collision tripwire for the code mapping refresh.yml / health.yml / operators read;
    # the behavioural equality asserts below cannot catch two constants sharing a value.
    codes = (0, 1, cli.EXIT_NO_SOURCES, cli.EXIT_NOT_WIRED, cli.EXIT_CONSUMER_SKIPPED)
    assert len(set(codes)) == len(codes)


def test_fetch_unwired_exits_exactly_not_wired() -> None:
    assert (_SOURCES_DIR / "eurlex_gdpr.yaml").is_file()
    result = runner.invoke(cli.app, ["fetch", "eurlex_gdpr", "--sources-dir", str(_SOURCES_DIR)])
    assert result.exit_code == cli.EXIT_NOT_WIRED, (result.exit_code, result.stderr)


def test_refresh_unwired_exits_exactly_not_wired() -> None:
    assert any(_SOURCES_DIR.glob("*.yaml"))
    result = runner.invoke(cli.app, ["refresh", "--all", "--sources-dir", str(_SOURCES_DIR)])
    assert result.exit_code == cli.EXIT_NOT_WIRED, (result.exit_code, result.stderr)


def test_refresh_help_still_exits_zero() -> None:
    # `--help` and `fetch --help` are covered by tests/cli/test_cli_smoke.py.
    assert runner.invoke(cli.app, ["refresh", "--help"]).exit_code == 0


def test_refresh_empty_dir_still_exit_no_sources(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["refresh", "--all", "--sources-dir", str(tmp_path)])
    assert result.exit_code == cli.EXIT_NO_SOURCES


def test_refresh_wired_flag_matches_refresh_behaviour() -> None:
    # health_check.py excuses never-run sources only while REFRESH_WIRED is False. If refresh
    # is wired without flipping the flag, never-run would stay excused forever; if the flag is
    # flipped while refresh is still a stub, health goes permanently red. Pin one to the other.
    assert any(_SOURCES_DIR.glob("*.yaml"))
    result = runner.invoke(cli.app, ["refresh", "--all", "--sources-dir", str(_SOURCES_DIR)])
    assert (result.exit_code == cli.EXIT_NOT_WIRED) is (not cli.REFRESH_WIRED), (
        cli.REFRESH_WIRED, result.exit_code, result.stderr,
    )
