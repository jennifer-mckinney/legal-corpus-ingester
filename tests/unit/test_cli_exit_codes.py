"""Exit-code contract tests for unwired CLI commands (terms-analysis#90).

The behavioural acceptance tests live in test_cli_not_wired.py (owned by the
acceptance-test author, asserts only `!= 0`). The exact-code assertions below
pin the contract refresh.yml and operators rely on: unwired work exits exactly
EXIT_NOT_WIRED, not 1 (load/unknown-source error) or EXIT_NO_SOURCES.

#108 (owner ruling 2026-10-12) splits the overloaded 2: EXIT_USAGE stays 2 for argparse/usage
errors only, EXIT_NO_SOURCES moves to 5, and a sources path that does not exist or is not a
directory gets its own EXIT_SOURCES_DIR_MISSING (6). The values live once, in
tests/cli/helpers.PUBLIC_EXIT_CODES; the tests below pin cli, the help and the docs to it.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

import pytest
from typer.testing import CliRunner

from legal_corpus_ingester import cli
from tests.cli.helpers import PUBLIC_EXIT_CODES

runner = CliRunner()

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCES_DIR = _REPO_ROOT / "config" / "sources"


def test_exit_codes_are_pairwise_distinct() -> None:
    # Collision tripwire for the code mapping refresh.yml / health.yml / operators read;
    # the behavioural equality asserts below cannot catch two constants sharing a value.
    # #108: all six named codes plus success, so EXIT_USAGE no longer shares 2 with anything.
    codes = (0, *(getattr(cli, name) for name in PUBLIC_EXIT_CODES))
    assert len(set(codes)) == len(codes), dict(zip(("success", *PUBLIC_EXIT_CODES), codes))


def test_public_exit_code_values_are_pinned() -> None:
    # #108: the numbers are a public contract (help text, automations/*.md, callers). Renumbering
    # is a breaking change, so the values are pinned, not just compared with each other.
    actual = {name: getattr(cli, name, None) for name in PUBLIC_EXIT_CODES}
    assert actual == PUBLIC_EXIT_CODES


def test_every_cli_exit_constant_is_in_the_public_table() -> None:
    # Contract for the table: a new EXIT_* constant in cli must be added to PUBLIC_EXIT_CODES
    # (and so to the help and doc checks), and only one name may own the usage code.
    declared = {
        name: value for name, value in vars(cli).items()
        if name.startswith("EXIT_") and isinstance(value, int)
    }
    assert set(declared) <= set(PUBLIC_EXIT_CODES), set(declared) - set(PUBLIC_EXIT_CODES)
    usage = PUBLIC_EXIT_CODES["EXIT_USAGE"]
    owners = sorted(name for name, value in declared.items() if value == usage)
    assert owners == ["EXIT_USAGE"], owners


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


# --- #108: 2 means a usage error, and nothing else -----------------------------------------

# Usage errors click/typer raises before any command body runs. Green today (positive
# controls): the split must not move them off EXIT_USAGE.
_USAGE_ERRORS: dict[str, list[str]] = {
    "unknown-command": ["no-such-command"],
    "unknown-option": ["status", "--no-such-option"],
    "missing-argument": ["fetch"],
    "flag-given-a-value": ["refresh", "--all=x"],
    "option-missing-value": ["status", "--sources-dir"],
    "no-subcommand": [],
}


@pytest.mark.parametrize("args", list(_USAGE_ERRORS.values()), ids=list(_USAGE_ERRORS))
def test_usage_errors_exit_usage(args: list[str]) -> None:
    if args:
        result = runner.invoke(cli.app, args)
        assert result.exit_code == PUBLIC_EXIT_CODES["EXIT_USAGE"], (result.exit_code, result.output)
    # The console-script path (main) gives the same code, including for no arguments at all.
    with pytest.raises(SystemExit) as exc:
        cli.main(list(args))
    assert exc.value.code == PUBLIC_EXIT_CODES["EXIT_USAGE"], exc.value.code


def _empty(root: Path) -> str:
    (root / "empty").mkdir()
    return str(root / "empty")


def _missing(root: Path) -> str:
    return str(root / "does-not-exist")


def _bad_yaml(root: Path) -> str:
    (root / "bad").mkdir()
    (root / "bad" / "broken.yaml").write_text("id: [unclosed\n", encoding="utf-8")
    return str(root / "bad")


def _configured(root: Path) -> str:
    return str(_SOURCES_DIR)


_SOURCES_BUILDERS: dict[str, Callable[[Path], str]] = {
    "empty-dir": _empty,
    "missing-dir": _missing,
    "bad-yaml": _bad_yaml,
    "configured": _configured,
}

_NON_USAGE_RUNS: list[tuple[str, Callable[[Path], list[str]]]] = [
    *(
        (f"status-{key}", lambda r, b=b: ["status", "--sources-dir", b(r), "--state-dir", str(r / "state")])
        for key, b in _SOURCES_BUILDERS.items()
    ),
    *(
        (f"refresh-{key}", lambda r, b=b: ["refresh", "--all", "--sources-dir", b(r)])
        for key, b in _SOURCES_BUILDERS.items()
    ),
    ("fetch-unknown-source", lambda r: ["fetch", "no_such_source", "--sources-dir", str(_SOURCES_DIR)]),
    ("fetch-not-wired", lambda r: ["fetch", "eurlex_gdpr", "--sources-dir", str(_SOURCES_DIR)]),
    ("validate-missing-bundle", lambda r: ["validate-round-trip", str(r / "no-bundle")]),
]


@pytest.mark.parametrize("build", [b for _, b in _NON_USAGE_RUNS], ids=[i for i, _ in _NON_USAGE_RUNS])
def test_only_usage_errors_exit_usage(
    build: Callable[[Path], list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #108: a well-formed command line never exits EXIT_USAGE, whatever the outcome; and every
    # exit is 0 or a documented public code. Red today for empty-dir: EXIT_NO_SOURCES is still 2.
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, build(tmp_path))
    assert isinstance(result.exception, SystemExit) or result.exception is None, repr(result.exception)
    assert result.exit_code != PUBLIC_EXIT_CODES["EXIT_USAGE"], (result.exit_code, result.stderr)
    assert result.exit_code in {0, *PUBLIC_EXIT_CODES.values()}, (result.exit_code, result.stderr)


# --- #108: the docs state the same numbers as the code -------------------------------------

_DOC_GLOBS = (
    "automations/*.md", "README.md", ".claude/**/*.md", "docs/*.md", "docs/adr/*.md",
    ".github/workflows/*.yml", "scripts/**/*.py", "scripts/**/*.sh", "src/**/*.py",
)
# "5 (`EXIT_NO_SOURCES`" and "EXIT_NO_SOURCES (5)", with or without backticks / a cli. prefix.
_CODE_THEN_NAME = re.compile(r"(?<![\w.])(\d+)\s*\(\s*`?(?:cli\.)?(EXIT_[A-Z_]+)")
_NAME_THEN_CODE = re.compile(r"(?:cli\.)?`?(EXIT_[A-Z_]+)`?\s*\((\d+)\)")


def _doc_files() -> list[Path]:
    files = {p for g in _DOC_GLOBS for p in _REPO_ROOT.glob(g) if p.is_file()}
    # Untracked evidence and session handoffs are history, not documentation.
    return sorted(p for p in files if "evidence" not in p.relative_to(_REPO_ROOT).parts)


def _documented_codes(text: str) -> list[tuple[str, int]]:
    pairs = [(name, int(code)) for code, name in _CODE_THEN_NAME.findall(text)]
    pairs += [(name, int(code)) for name, code in _NAME_THEN_CODE.findall(text)]
    return pairs


def test_documented_exit_codes_match_cli() -> None:
    # Every "<code> (EXIT_NAME)" in the docs, workflows and scripts states the public value.
    # Names outside the public table (scripts' own EXIT_CONFIG_MISSING etc.) are other
    # namespaces and are not checked here.
    seen = 0
    wrong = []
    for path in _doc_files():
        for name, code in _documented_codes(path.read_text(encoding="utf-8")):
            seen += 1
            if name in PUBLIC_EXIT_CODES and code != PUBLIC_EXIT_CODES[name]:
                wrong.append(f"{path.relative_to(_REPO_ROOT)}: {name} documented as {code}")
    assert seen > 0, "control: the scan matched no documented exit code at all"
    assert wrong == [], wrong


@pytest.mark.parametrize("doc", ["automations/health-check.md", "automations/refresh.md"])
def test_runbooks_document_no_sources_and_dir_missing(doc: str) -> None:
    # #108: both runbooks that list the CLI's codes name 5 and 6 with their constants.
    pairs = set(_documented_codes((_REPO_ROOT / doc).read_text(encoding="utf-8")))
    for name in ("EXIT_NO_SOURCES", "EXIT_SOURCES_DIR_MISSING"):
        assert (name, PUBLIC_EXIT_CODES[name]) in pairs, (doc, name, sorted(pairs))


def test_docs_do_not_call_a_missing_dir_a_load_error() -> None:
    # #108 moved "missing or not a directory" off EXIT_LOAD_ERROR. No sentence or clause in the
    # markdown docs may still tie the two together (health-check.md said exactly that).
    stale = []
    for path in _doc_files():
        if path.suffix != ".md":
            continue
        text = " ".join(path.read_text(encoding="utf-8").split())
        for clause in re.split(r";|\. ", text):
            if "EXIT_LOAD_ERROR" in clause and ("not a directory" in clause or "missing" in clause):
                stale.append(f"{path.relative_to(_REPO_ROOT)}: {clause.strip()[:160]}")
    assert stale == [], stale
