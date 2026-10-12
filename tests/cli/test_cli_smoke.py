from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
import typer.main
from typer.testing import CliRunner

from legal_corpus_ingester import cli
from legal_corpus_ingester.cli import app
from legal_corpus_ingester.pipeline.state import STAGES, CheckpointState, CheckpointStore
from legal_corpus_ingester.sources.registry import load_all
from tests.cli.helpers import strip_ansi

runner = CliRunner()


def test_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0


def test_init_creates_dirs(tmp_path: object, monkeypatch: object) -> None:  # type: ignore[type-arg]
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert (tmp_path / "config").is_dir()  # type: ignore[operator]
    assert (tmp_path / "out").is_dir()  # type: ignore[operator]
    assert (tmp_path / "state").is_dir()  # type: ignore[operator]


def test_sources_list_no_sources(tmp_path: object, monkeypatch: object) -> None:  # type: ignore[type-arg]
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    (tmp_path / "config" / "sources").mkdir(parents=True)  # type: ignore[operator]
    result = runner.invoke(app, ["sources", "list"])
    assert result.exit_code == 0
    assert "No sources" in result.output


def test_sources_list_help() -> None:
    result = runner.invoke(app, ["sources", "list", "--help"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# status: "nothing configured" is not success (terms-analysis#190, DEV-FUNDAMENTALS F5)
# ---------------------------------------------------------------------------
#
# `ingester status` has four outcomes, and each must be told apart from the others:
#   no_sources  the sources dir loads cleanly but holds no source -> EXIT_NO_SOURCES, stderr says so
#   load_error  the sources dir cannot be loaded                   -> EXIT_LOAD_ERROR, stderr says it
#   never_run   sources configured, no checkpoint yet              -> 0, "No runs recorded."
#   healthy     sources configured, checkpoints present            -> 0, one "<name>: <stage>" line
# never_run stays exit 0 on purpose: the nightly health.yml runs `ingester status` with the repo's
# config/sources on a fresh hosted runner, whose state dir is empty until refresh is wired and state
# persists (ADR-016). Each failure row pins the exact public code (EXIT_NO_SOURCES /
# EXIT_LOAD_ERROR, whose numeric values test_status_help_documents_exit_codes pins) AND checks
# that refresh exits with the same code on the same sources dir (G0-1, terms-analysis#90), so
# neither the shared constant nor either command can drift unnoticed.

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_SOURCE = _REPO_ROOT / "tests" / "fixtures" / "sources" / "eurlex.yaml"
_REPO_SOURCES = _REPO_ROOT / "config" / "sources"
_NO_RUNS = "No runs recorded."
_NO_SOURCES_MSG = "No sources configured"
_LOAD_ERROR_MSG = "Error loading sources"
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
# One character per hostile class, generated into the `name` field of an otherwise valid source.
# The loader must reject each; status must then report a load error without echoing the raw byte.
_HOSTILE_NAME_CHARS = ("\x1b", "\r", "\x0b", "\x0c", "\x85", " ", " ", "​", "‮", "﻿")


def _unsafe_chars(text: str) -> list[str]:
    """Raw control, format, surrogate and line/paragraph-separator characters (newline allowed)."""
    return [hex(ord(c)) for c in text if c != "\n" and unicodedata.category(c) in _UNSAFE_CATEGORIES]


def _fixture_name() -> str:
    (name,) = load_all(_FIXTURE_SOURCE.parent)  # from the config, not a literal
    return name


def _copy_fixture(sources: Path, filename: str = _FIXTURE_SOURCE.name) -> None:
    sources.mkdir(exist_ok=True)
    (sources / filename).write_text(_FIXTURE_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")


def _save_checkpoint(state: Path, name: str, stage: str) -> None:
    CheckpointStore(state).save(CheckpointState(source_name=name, stage=stage, corpus_version="v-test"))


@dataclass(frozen=True)
class _StatusCase:
    id: str
    kind: str  # no_sources | load_error | never_run | healthy
    # build(root) lays out the case under root (the cwd) and returns
    # (--sources-dir argument, --state-dir argument, expected stdout lines for the exit-0 kinds).
    build: Callable[[Path], tuple[str, str, list[str]]]


def _empty_dir(root: Path) -> tuple[str, str, list[str]]:
    (root / "src").mkdir()
    return "src", "state", []


def _lookalike_files_only(root: Path) -> tuple[str, str, list[str]]:
    # Valid source content under names the loader does not read: nothing is configured.
    src = root / "src"
    for filename in ("eurlex.yml", "eurlex.yaml.disabled", "eurlex.yaml~", "README.md"):
        _copy_fixture(src, filename)
    return "src", "state", []


def _symlink_to_empty_dir(root: Path) -> tuple[str, str, list[str]]:
    (root / "real").mkdir()
    (root / "src").symlink_to(root / "real", target_is_directory=True)
    return "src", "state", []


def _orphan_checkpoint(root: Path) -> tuple[str, str, list[str]]:
    # A checkpoint left in the state dir must not make "nothing configured" look configured.
    (root / "src").mkdir()
    _save_checkpoint(root / "state", _fixture_name(), STAGES[-1])
    return "src", "state", []


def _hostile_orphan_checkpoint(root: Path) -> tuple[str, str, list[str]]:
    # Orphan checkpoint whose FILE NAME forges a status line with ESC, newline and U+2028.
    (root / "src").mkdir()
    forged = f"orphan\n\x1b[31m{_fixture_name()}: {STAGES[-1]} x"
    _save_checkpoint(root / "state", forged, STAGES[-1])
    return "src", "state", []


def _missing_dir(root: Path) -> tuple[str, str, list[str]]:
    return "src", "state", []


def _dangling_symlink(root: Path) -> tuple[str, str, list[str]]:
    (root / "src").symlink_to(root / "gone", target_is_directory=True)
    return "src", "state", []


def _sources_path_is_file(root: Path) -> tuple[str, str, list[str]]:
    (root / "src").write_text(_FIXTURE_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    return "src", "state", []


def _one_yaml(content: bytes, filename: str = "a.yaml") -> Callable[[Path], tuple[str, str, list[str]]]:
    def build(root: Path) -> tuple[str, str, list[str]]:
        (root / "src").mkdir()
        (root / "src" / filename).write_bytes(content)
        return "src", "state", []

    return build


def _absolute(
    build: Callable[[Path], tuple[str, str, list[str]]],
) -> Callable[[Path], tuple[str, str, list[str]]]:
    # Same layout, but --sources-dir is passed as an absolute path. The loader's message names
    # that path, so echoing it (or the raw exception) would leak it to stderr (F8, mutant M2).
    def build_absolute(root: Path) -> tuple[str, str, list[str]]:
        sources_arg, state_arg, lines = build(root)
        return str(root / sources_arg), state_arg, lines

    return build_absolute


def _dir_named_yaml(root: Path) -> tuple[str, str, list[str]]:
    (root / "src" / "x.yaml").mkdir(parents=True)
    return "src", "state", []


def _valid_plus_malformed(root: Path) -> tuple[str, str, list[str]]:
    # One good source must not hide a broken one: a partial config is a load error, not a status.
    _copy_fixture(root / "src")
    (root / "src" / "zz.yaml").write_text("", encoding="utf-8")
    return "src", "state", []


def _hostile_name(ch: str) -> Callable[[Path], tuple[str, str, list[str]]]:
    def build(root: Path) -> tuple[str, str, list[str]]:
        (root / "src").mkdir()
        text = _FIXTURE_SOURCE.read_text(encoding="utf-8")
        name = _fixture_name()
        # JSON string syntax is valid YAML, so the escape decodes to the raw character on load.
        hostile = text.replace(f"name: {name}", f"name: {json.dumps(name + ch + 'x')}", 1)
        assert hostile != text, "control: the fixture's name line was not found"
        (root / "src" / "a.yaml").write_text(hostile, encoding="utf-8")
        return "src", "state", []

    return build


def _fixture_never_run(state_exists: bool) -> Callable[[Path], tuple[str, str, list[str]]]:
    def build(root: Path) -> tuple[str, str, list[str]]:
        _copy_fixture(root / "src")
        if state_exists:
            (root / "state").mkdir()
        return "src", "state", [_NO_RUNS]

    return build


def _repo_sources_never_run(root: Path) -> tuple[str, str, list[str]]:
    # Exactly what health.yml runs on a fresh runner: the repo's config, a state dir not yet created.
    return str(_REPO_SOURCES), "state", [_NO_RUNS]


def _fixture_healthy(root: Path) -> tuple[str, str, list[str]]:
    _copy_fixture(root / "src")
    _save_checkpoint(root / "state", _fixture_name(), STAGES[-1])
    return "src", "state", [f"{_fixture_name()}: {STAGES[-1]}"]


def _repo_sources_healthy(root: Path) -> tuple[str, str, list[str]]:
    names = list(load_all(_REPO_SOURCES))
    lines = []
    for i, name in enumerate(names):
        stage = STAGES[i % len(STAGES)]
        _save_checkpoint(root / "state", name, stage)
        lines.append(f"{name}: {stage}")
    return str(_REPO_SOURCES), "state", lines


_STATUS_CASES: tuple[_StatusCase, ...] = (
    _StatusCase("empty-dir", "no_sources", _empty_dir),
    _StatusCase("lookalike-files-only", "no_sources", _lookalike_files_only),
    _StatusCase("symlink-to-empty-dir", "no_sources", _symlink_to_empty_dir),
    _StatusCase("orphan-checkpoint", "no_sources", _orphan_checkpoint),
    _StatusCase("hostile-orphan-checkpoint-filename", "no_sources", _hostile_orphan_checkpoint),
    _StatusCase("missing-dir", "load_error", _missing_dir),
    _StatusCase("dangling-symlink", "load_error", _dangling_symlink),
    _StatusCase("sources-path-is-a-file", "load_error", _sources_path_is_file),
    _StatusCase("empty-yaml", "load_error", _one_yaml(b"")),
    _StatusCase("yaml-is-a-list", "load_error", _one_yaml(b"- a\n- b\n")),
    _StatusCase("invalid-utf8", "load_error", _one_yaml(b"\xff\xfe")),
    _StatusCase("encoded-lone-surrogate", "load_error", _one_yaml(b"name: \xed\xa0\x80\n")),
    _StatusCase("nul-byte", "load_error", _one_yaml(b"name: a\x00b\n")),
    _StatusCase("hostile-yaml-filename", "load_error", _one_yaml(b"", "\x1b[2J x.yaml")),
    _StatusCase("dir-named-yaml", "load_error", _dir_named_yaml),
    _StatusCase("valid-plus-malformed", "load_error", _valid_plus_malformed),
    _StatusCase("absolute-missing-dir", "load_error", _absolute(_missing_dir)),
    _StatusCase("absolute-path-is-a-file", "load_error", _absolute(_sources_path_is_file)),
    _StatusCase("absolute-valid-plus-malformed", "load_error", _absolute(_valid_plus_malformed)),
    _StatusCase("absolute-empty-dir", "no_sources", _absolute(_empty_dir)),
    *(
        _StatusCase(f"hostile-name-U+{ord(ch):04X}", "load_error", _hostile_name(ch))
        for ch in _HOSTILE_NAME_CHARS
    ),
    _StatusCase("never-run-empty-state", "never_run", _fixture_never_run(state_exists=True)),
    _StatusCase("never-run-missing-state", "never_run", _fixture_never_run(state_exists=False)),
    _StatusCase("never-run-repo-config-health-yml", "never_run", _repo_sources_never_run),
    _StatusCase("healthy-fixture", "healthy", _fixture_healthy),
    _StatusCase("healthy-repo-config", "healthy", _repo_sources_healthy),
)


def test_status_cases_cover_every_outcome() -> None:
    # Contract for the table below: every outcome has at least one row, both failure
    # (no_sources, load_error) and success (never_run, healthy), and ids are unique.
    kinds = {c.kind for c in _STATUS_CASES}
    assert kinds == {"no_sources", "load_error", "never_run", "healthy"}, kinds
    ids = [c.id for c in _STATUS_CASES]
    assert len(ids) == len(set(ids)), ids
    assert len(_HOSTILE_NAME_CHARS) == sum(c.id.startswith("hostile-name-") for c in _STATUS_CASES)
    assert {unicodedata.category(ch) for ch in _HOSTILE_NAME_CHARS} >= {"Cc", "Cf", "Zl", "Zp"}
    # Both failure outcomes are also fed an absolute --sources-dir, the input whose path the
    # loader's message carries (F8).
    absolute_kinds = {c.kind for c in _STATUS_CASES if c.id.startswith("absolute-")}
    assert absolute_kinds == {"no_sources", "load_error"}, absolute_kinds


def _assert_no_leak(root: Path, *streams: str) -> None:
    # F8 / attack list A: no absolute path, no raw control or separator characters.
    for stream in streams:
        assert str(root) not in stream and str(root.resolve()) not in stream, stream
        assert _unsafe_chars(stream) == [], (_unsafe_chars(stream), stream)


@pytest.mark.parametrize("case", _STATUS_CASES, ids=[c.id for c in _STATUS_CASES])
def test_status_outcome_by_configuration(
    case: _StatusCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    sources_arg, state_arg, expected_lines = case.build(tmp_path)
    result = runner.invoke(app, ["status", "--sources-dir", sources_arg, "--state-dir", state_arg])

    if case.kind in ("never_run", "healthy"):
        assert result.exit_code == 0, (result.exit_code, result.stdout, result.stderr)
        assert result.stdout.splitlines() == expected_lines
        assert result.stderr == ""
        return

    # Control: refresh, on the same sources dir, defines which failure this row is (G0-1).
    ref = runner.invoke(app, ["refresh", "--all", "--sources-dir", sources_arg])
    assert isinstance(ref.exception, SystemExit), f"control: refresh crashed: {ref.exception!r}"

    # The fix: status never reports nothing-configured or unloadable config as success.
    assert result.exit_code == ref.exit_code, (
        f"status exited {result.exit_code}, refresh exited {ref.exit_code} on the same sources dir; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # An exit through typer.Exit, not a traceback that happens to exit 1.
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert _NO_RUNS not in result.stdout, "status still prints the never-run message"
    _assert_no_leak(tmp_path, result.stdout, result.stderr)

    if case.kind == "no_sources":
        assert ref.exit_code == cli.EXIT_NO_SOURCES, "control: refresh does not see this row as empty"
        # The exact public code, not just parity with refresh: if the shared path drifted for
        # both commands, parity alone would still pass.
        assert result.exit_code == cli.EXIT_NO_SOURCES, (result.exit_code, result.stderr)
        assert _NO_SOURCES_MSG in result.stderr, result.stderr
        assert _LOAD_ERROR_MSG not in result.stderr, "no-sources reported as a load error"
    else:
        assert ref.exit_code == cli.EXIT_LOAD_ERROR, "control: refresh loaded this row"
        assert result.exit_code == cli.EXIT_LOAD_ERROR, (result.exit_code, result.stderr)
        assert _LOAD_ERROR_MSG in result.stderr, result.stderr
        assert _NO_SOURCES_MSG not in result.stderr, "load error reported as no sources"


def test_status_default_paths_in_empty_dir_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Was test_status_no_checkpoints, which pinned the bug (exit 0, "No runs"). The default
    # --sources-dir is relative to the cwd; in an empty directory nothing is configured, and
    # status must fail the way refresh does on the same defaults.
    monkeypatch.chdir(tmp_path)
    ref = runner.invoke(app, ["refresh", "--all"])
    assert isinstance(ref.exception, SystemExit) and ref.exit_code != 0, "control: refresh succeeded"
    result = runner.invoke(app, ["status"])
    assert result.exit_code == ref.exit_code, (result.exit_code, result.stdout, result.stderr)
    # The default config/sources does not exist in an empty cwd: that is the load-error code.
    assert result.exit_code == cli.EXIT_LOAD_ERROR, (result.exit_code, result.stderr)
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert _NO_RUNS not in result.stdout
    _assert_no_leak(tmp_path, result.stdout, result.stderr)


def test_status_help_documents_exit_codes() -> None:
    # Card #190: the CLI help states which outcome exits which code: never-run (sources
    # configured, no checkpoint) is a legitimate 0, nothing configured is EXIT_NO_SOURCES, an
    # unloadable sources dir is EXIT_LOAD_ERROR.
    #
    # The numeric values are a public contract: this help text prints them and
    # automations/refresh.md documents 2 for EXIT_NO_SOURCES, so callers may key on the numbers.
    # They are pinned here, not just compared with each other: renumbering is a breaking change.
    documented = {"EXIT_LOAD_ERROR": 1, "EXIT_NO_SOURCES": 2}
    assert {name: getattr(cli, name) for name in documented} == documented
    outcomes = {
        0: "sources are configured",
        cli.EXIT_NO_SOURCES: "holds no source",
        cli.EXIT_LOAD_ERROR: "cannot be loaded",
    }
    assert len(outcomes) == 3, "two status outcomes share an exit code"

    result = runner.invoke(app, ["status", "--help"])
    assert result.exit_code == 0
    text = " ".join(strip_ansi(result.output).split())
    # Every "<code> when <outcome>" clause in the help, in order. Exactly the three outcome
    # codes, each once and each tied to its own outcome: a dropped, extra or swapped code fails.
    clauses = re.findall(r"(?<![\w.])(\d+) when ([^;]+)", text)
    assert sorted(int(code) for code, _ in clauses) == sorted(outcomes), (clauses, text)
    for code, clause in clauses:
        assert outcomes[int(code)] in clause, (code, clause)


def test_fetch_dry_run_help() -> None:
    result = runner.invoke(app, ["fetch", "--help"])
    assert result.exit_code == 0
    # Assert on the registered parameters, not the help text: the fetch
    # docstring mentions --dry-run, so a text check passes without the option.
    commands = getattr(typer.main.get_command(app), "commands", {})
    assert "fetch" in commands, f"no fetch command: {sorted(commands)}"
    fetch = commands["fetch"]
    options = {opt for param in fetch.params for opt in getattr(param, "opts", [])}
    assert "--dry-run" in options, f"fetch has no --dry-run option: {sorted(options)}"


# ---------------------------------------------------------------------------
# validate-round-trip tests
# ---------------------------------------------------------------------------

_VALID_MANIFEST_YAML = """\
chunk_count: 10
chunker_version: v1.0.0-vendored-from-terms-analysis@abc123
corpus_version: '2026.07.0'
embedder_model: apertus-8b-instruct
embedder_revision: sha256:abc123def456
sources:
- eurlex
"""


def test_validate_round_trip_help() -> None:
    """validate-round-trip --help exits 0."""
    result = runner.invoke(app, ["validate-round-trip", "--help"])
    assert result.exit_code == 0


def test_validate_round_trip_missing_dir(tmp_path: Path) -> None:
    """Non-existent path exits 1."""
    missing = str(tmp_path) + "/no-such-bundle"
    result = runner.invoke(app, ["validate-round-trip", missing])
    assert result.exit_code == 1


def test_validate_round_trip_no_manifest(tmp_path: Path) -> None:
    """Existing dir without MANIFEST.yaml exits 1 and output contains X-Corpus-Mismatch."""
    bundle = tmp_path / "bundle-v1"
    bundle.mkdir()
    result = runner.invoke(app, ["validate-round-trip", str(bundle)])
    assert result.exit_code == 1
    assert "X-Corpus-Mismatch" in result.output


def test_validate_round_trip_valid_bundle(tmp_path: Path) -> None:
    """Valid bundle with MANIFEST.yaml + required subdirs exits 0."""
    bundle = tmp_path / "2026.07.0"
    bundle.mkdir()

    # Write a valid MANIFEST.yaml
    (bundle / "MANIFEST.yaml").write_text(_VALID_MANIFEST_YAML, encoding="utf-8")

    # Create required subdirectories
    for subdir in ("corpus", "index", "provenance"):
        (bundle / subdir).mkdir()

    # terms-analysis is not installed in the test env; the structural-only opt-out is
    # required for exit 0, and the verdict must say the consumer was skipped (terms-analysis#90).
    result = runner.invoke(
        app, ["validate-round-trip", str(bundle), "--allow-missing-consumer"]
    )
    assert result.exit_code == 0, result.output
    assert "VALID (consumer check skipped)" in result.output


# ---------------------------------------------------------------------------
# prune tests
# ---------------------------------------------------------------------------


def _make_bundle(base: Path, name: str) -> Path:
    """Create a minimal bundle directory under base."""
    d = base / name
    d.mkdir(parents=True)
    return d


def test_prune_help() -> None:
    """prune --help exits 0 and mentions expected flags."""
    result = runner.invoke(app, ["prune", "--help"])
    assert result.exit_code == 0
    out = strip_ansi(result.output)
    assert "--dry-run" in out
    assert "--force" in out


def test_prune_no_flags_prints_guidance(tmp_path: Path, monkeypatch: object) -> None:
    """prune with no action flag prints guidance without deleting anything."""
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    out = tmp_path / "out"
    out.mkdir()
    result = runner.invoke(app, ["prune", "--out-dir", str(out)])
    assert result.exit_code == 0
    # With nothing to prune, output should reference "prune" or "bundle"
    assert "prune" in result.output.lower() or "bundle" in result.output.lower()


def test_prune_dry_run(tmp_path: Path, monkeypatch: object) -> None:
    """prune --dry-run lists bundles that would be pruned without deleting them."""
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    out = tmp_path / "out"
    out.mkdir()
    # Create 15 monthly bundles (2024.01 through 2025.03) so bundles outside the
    # 12-month window and not a quarterly anchor qualify for pruning.
    for year, month in [
        (2024, 1), (2024, 2), (2024, 3), (2024, 4),
        (2024, 5), (2024, 6), (2024, 7), (2024, 8),
        (2024, 9), (2024, 10), (2024, 11), (2024, 12),
        (2025, 1), (2025, 2), (2025, 3),
    ]:
        _make_bundle(out, f"{year}.{month:02d}.0")
    result = runner.invoke(app, ["prune", "--out-dir", str(out), "--dry-run"])
    assert result.exit_code == 0
    assert "would prune" in result.output


def test_prune_force_deletes(tmp_path: Path, monkeypatch: object) -> None:
    """prune --force actually removes prunable bundles and keeps the required ones."""
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    out = tmp_path / "out"
    out.mkdir()
    # Same 15-month dataset — bundles outside the retention window will be deleted.
    for year, month in [
        (2024, 1), (2024, 2), (2024, 3), (2024, 4),
        (2024, 5), (2024, 6), (2024, 7), (2024, 8),
        (2024, 9), (2024, 10), (2024, 11), (2024, 12),
        (2025, 1), (2025, 2), (2025, 3),
    ]:
        _make_bundle(out, f"{year}.{month:02d}.0")
    result = runner.invoke(app, ["prune", "--out-dir", str(out), "--force"])
    assert result.exit_code == 0
    assert "pruned" in result.output
    # At least 4 bundles must remain (the 4-most-recent recency rule)
    remaining = [d for d in out.iterdir() if d.is_dir()]
    assert len(remaining) >= 4


# ---------------------------------------------------------------------------
# validate-round-trip — C1: retrieve returns [None, None] exits 1 (Issue #6)
# ---------------------------------------------------------------------------


def test_cli_validate_round_trip_retrieve_returns_none_list_exits_1(tmp_path: Path, monkeypatch: object) -> None:
    """retrieve() returning [None, None] should exit 1 (garbage result)."""
    import types

    # Build a minimal valid bundle directory
    bundle = tmp_path / "2026.07.001"
    bundle.mkdir()
    (bundle / "corpus").mkdir()
    (bundle / "index").mkdir()
    (bundle / "provenance").mkdir()

    # MANIFEST.yaml uses corpus_version (not bundle_version) and requires
    # corpus_version, chunker_version, embedder_model, embedder_revision,
    # sources, chunk_count — matching the Manifest dataclass exactly.
    manifest_yaml = (
        "corpus_version: '2026.07.001'\n"
        "embedder_model: apertus-8b\n"
        "embedder_revision: abc123\n"
        "chunker_version: 1.0.0\n"
        "chunk_count: 0\n"
        "sources: []\n"
    )
    (bundle / "MANIFEST.yaml").write_text(manifest_yaml, encoding="utf-8")

    # Stub a legal_kb module whose retrieve() returns [None, None]
    stub_mod = types.ModuleType("backend")
    stub_app = types.ModuleType("backend.app")
    stub_services = types.ModuleType("backend.app.services")
    stub_legal_kb = types.ModuleType("backend.app.services.legal_kb")

    class _KB:
        def load_from_bundle(self, bundle_dir: Path) -> None:
            pass

        def retrieve(self, query: str) -> list[object]:
            return [None, None]

    stub_legal_kb.LegalKnowledgeBase = _KB  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "backend", stub_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "backend.app", stub_app)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "backend.app.services", stub_services)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "backend.app.services.legal_kb", stub_legal_kb)  # type: ignore[attr-defined]

    result = runner.invoke(app, ["validate-round-trip", str(bundle)])
    assert result.exit_code == 1
    assert "empty result" in result.output


# ---------------------------------------------------------------------------
# audit-license --offline (Issue #7 / Fix C2)
# ---------------------------------------------------------------------------

# Minimal valid source config matching the current SourceConfig schema:
# - name must be lowercase/digits/hyphens/underscores only (no "EUR-Lex")
# - refresh and pipeline are required fields
_EURLEX_YAML = (
    "name: eurlex\n"
    "jurisdiction: EU\n"
    "base_url: https://eur-lex.europa.eu/\n"
    "license:\n"
    "  spdx: CC-BY-4.0\n"
    "  url: https://eur-lex.europa.eu/content/legal-notice/legal-notice.html\n"
    "refresh:\n"
    "  cadence: weekly\n"
    "pipeline:\n"
    "  fetcher: fetchers.eurlex.EurLexFetcher\n"
)


def test_cli_audit_license_offline_reads_baseline(tmp_path: Path) -> None:
    """--offline flag should read stored baseline and skip live fetch."""
    import json

    # Set up minimal sources dir with a source that has a license URL
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir()
    (sources_dir / "eurlex.yaml").write_text(_EURLEX_YAML, encoding="utf-8")

    # Pre-populate a state file
    state_file = tmp_path / "license-hashes.json"
    state_file.write_text(
        json.dumps({
            "eurlex": {
                "hash": "deadbeef12345678",
                "spdx": "CC-BY-4.0",
                "recorded_at": "2026-07-04T00:00:00Z",
            }
        }),
        encoding="utf-8",
    )

    result = runner.invoke(app, [
        "audit-license", "eurlex",
        "--sources-dir", str(sources_dir),
        "--state-file", str(state_file),
        "--offline",
    ])
    assert result.exit_code == 0
    assert "BASELINE_ONLY" in result.output
    assert "deadbeef1234" in result.output


def test_cli_audit_license_offline_no_baseline(tmp_path: Path) -> None:
    """--offline with no stored baseline should exit 0 with NO_BASELINE message."""
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir()
    (sources_dir / "eurlex.yaml").write_text(_EURLEX_YAML, encoding="utf-8")

    state_file = tmp_path / "empty-hashes.json"
    state_file.write_text("{}", encoding="utf-8")

    result = runner.invoke(app, [
        "audit-license", "eurlex",
        "--sources-dir", str(sources_dir),
        "--state-file", str(state_file),
        "--offline",
    ])
    assert result.exit_code == 0
    assert "NO_BASELINE" in result.output
