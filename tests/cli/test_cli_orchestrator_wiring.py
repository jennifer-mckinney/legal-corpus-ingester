"""`fetch` and `refresh` run the pipeline Orchestrator (jennifer-mckinney/terms-analysis#93, G2-1).

Lead rulings of 2026-10-13 that override the card text, each pinned below:

1. Tier 1 is three sources (GDPR, AI Act, DSA). `refresh --all` runs only sources whose
   approval is valid; Data Act and DMA stay unsigned until terms-analysis#94 signs them.
2. The CLI never passes `embedder_model` to `Orchestrator(...)` (terms-analysis#98 owns it).
3. The LocalAI URL and the output and state dirs come from `Settings` (env), never literals (F13).
4. Today's red is the not-wired path (exit 3), not an import error: `cli.Orchestrator` is
   patched with ``raising=False`` so the tests collect and run before the import exists.
5. Fail loudly: no source processed, or an orchestrator failure, exits non-zero and names the
   source. `--dry-run` makes no network call and writes nothing.

Round 2 lead decisions (2026-10-13, after the coder's conflicts A and B):

A. `fetch` of an unsigned source exits EXIT_NO_APPROVED_SOURCES (7) with no network call, and
   its message names where the hash to sign comes from (the cassette sidecar manifest).
B. In `refresh`, a requested source with no valid approval is a source FAILURE, not a skip: it
   gets a `not_approved` checkpoint, the other sources still run, and the run exits
   EXIT_SOURCE_FAILED (8) naming every failed source. Exit 7 only when no requested source is
   signed. A partly signed run that exits 0 would be a silent partial success.
   An approval file that cannot be parsed as a mapping fails the whole run (exit 1, ruling 4).

Round 3 (2026-10-13): C. health treats every non-success checkpoint stage as failed
(tests/unit/test_health_check.py). D. exit 7 also writes a `not_approved` checkpoint for every
requested source. E. a dry run exits 8 when any requested source is unsigned, and writes nothing.

Round 4 (2026-10-13): F. a fully signed `refresh --all --dry-run` exits 0 and lists every source
without running one. G. a checkpoint write that fails is a loud source failure, named on stderr.
H. a stage-class or approval load error exits 1 before any checkpoint is written (mutant C10).
I. `fetch --dry-run` of an unsigned source exits 7, offline, as the real fetch does.

Round 8 (2026-10-13, grumpy HIGH at cli.py:415): `refresh` publishes ONE combined calver bundle
per run through TermsAnalysisPublisher: `out/current` is a relative symlink to a `YYYY.M.N` dir
that `scripts/detect_bundle_change.py` accepts. Any failed source means no new bundle and no
repoint. Round 8b: a second run with identical content publishes nothing new (detect: unchanged);
one with changed content makes a newer version that detect reports as changed.

The card's `--out <tmp>` is replaced by the `Settings.out_dir` env var (ruling 3).
"""

from __future__ import annotations

import errno
import hashlib
import importlib
import importlib.util
import inspect
import json
import os
import pkgutil
import re
import shutil
import socket
import subprocess
import sys
import unicodedata
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlparse

import httpx
import numpy as np
import pydantic
import pytest
import yaml
from typer.testing import CliRunner, Result

from legal_corpus_ingester import cli
from legal_corpus_ingester.cli import app
from legal_corpus_ingester.config import Settings
from legal_corpus_ingester.embedders.localai import LocalAIEmbedder
from legal_corpus_ingester.errors import (
    EmbedEndpointError,
    LegalReviewGateError,
    LicenseDriftError,
    UpstreamNotFoundError,
)
from legal_corpus_ingester.fetchers.eurlex import EURLEX_BASE_URL
from legal_corpus_ingester.pipeline.approval_gate import (
    _PLACEHOLDER_SHA,
    EXPIRY_WARNING_DAYS,
    check_approval,
    load_approval,
)
from legal_corpus_ingester.pipeline.manifest import MANIFEST_FILENAME, Manifest
from legal_corpus_ingester.pipeline.orchestrator import Orchestrator as RealOrchestrator
from legal_corpus_ingester.pipeline.state import STAGES, CheckpointState, CheckpointStore
from legal_corpus_ingester.provenance import license_audit
from legal_corpus_ingester.provenance.license_audit import DEFAULT_STATE_FILE as AUDIT_BASELINE_FILE
from legal_corpus_ingester.publishers.base import PublishReceipt
from legal_corpus_ingester.sources.registry import load_all
from tests.cli.helpers import PUBLIC_EXIT_CODES, strip_ansi

runner = CliRunner()

def _invoke(args: list[str]) -> Result:
    # color=True: click strips ANSI escapes when stdout is not a tty, which would hide an escape
    # the CLI echoes from an exception; a real terminal receives it (attack list A, F8).
    return runner.invoke(app, args, color=True)


_REPO = Path(__file__).resolve().parents[2]
_REPO_SOURCES = _REPO / "config" / "sources"
_REPO_APPROVALS = _REPO / "config" / "approvals"
# The AKN sample the cleaner and chunker tests use; served as the EUR-Lex XML body.
_AKN_SAMPLE = _REPO / "tests" / "fixtures" / "corpus" / "gdpr_akn_sample.xml"
# Ruling 1: Tier 1. Every other repo source stays unsigned (placeholder SHA) in these tests.
_TIER1 = ("eurlex_gdpr", "eurlex_ai_act", "eurlex_dsa")
_EURLEX_HOST = urlparse(EURLEX_BASE_URL).hostname
_ENV_PREFIX = str(Settings.model_config["env_prefix"])

# Exit codes whose public meaning is false for "no source ran" and for "a source failed".
# EXIT_NOT_WIRED is the red today; EXIT_NO_SOURCES says the dir "holds no source", which is
# untrue when sources exist but none is approved. 1 stays allowed: the epilog says other
# command failures exit 1 (the exact code needs a ruling, see the report).
_DISHONEST_FAILURE_CODES = frozenset(
    {0}
    | {
        PUBLIC_EXIT_CODES[name]
        for name in (
            "EXIT_USAGE",
            "EXIT_NOT_WIRED",
            "EXIT_CONSUMER_SKIPPED",
            "EXIT_NO_SOURCES",
            "EXIT_SOURCES_DIR_MISSING",
        )
    }
)
_NOT_WIRED = PUBLIC_EXIT_CODES["EXIT_NOT_WIRED"]
_USAGE = PUBLIC_EXIT_CODES["EXIT_USAGE"]
_LOAD_ERROR = PUBLIC_EXIT_CODES["EXIT_LOAD_ERROR"]
_NO_APPROVED = PUBLIC_EXIT_CODES["EXIT_NO_APPROVED_SOURCES"]
_SOURCE_FAILED = PUBLIC_EXIT_CODES["EXIT_SOURCE_FAILED"]
# Lead decision B: the checkpoint stage a requested source without a valid approval records.
_NOT_APPROVED_STAGE = "not_approved"

_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def _refresh_baseline_name() -> str:
    # Round 6 P: a named constant beside DEFAULT_STATE_FILE in provenance.license_audit, so the
    # tests never restate the file name (F13). Missing today: that is the red.
    constant = getattr(license_audit, "REFRESH_BASELINE_FILE", None)
    assert constant is not None, "license_audit names no REFRESH_BASELINE_FILE constant (ruling P)"
    return Path(constant).name


def _repo_names() -> list[str]:
    return list(load_all(_REPO_SOURCES))


def _unsigned() -> list[str]:
    return [name for name in _repo_names() if name not in _TIER1]


def _env(field_name: str) -> str:
    # The env var name pydantic-settings reads, derived from the Settings class (F13).
    assert field_name in Settings.model_fields, field_name
    return f"{_ENV_PREFIX}{field_name}".upper()


def _default(field_name: str) -> Any:
    return Settings.model_fields[field_name].default


# ---------------------------------------------------------------------------
# Workspace: a tmp config tree, env-configured dirs and an empty cwd
# ---------------------------------------------------------------------------


@dataclass
class _Workspace:
    root: Path
    cwd: Path
    config: Path
    sources: Path
    approvals: Path
    out: Path
    state: Path
    localai_url: str
    localai_model: str
    body: bytes
    today: date

    @property
    def body_sha(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    def approval(self, name: str, **overrides: Any) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source_id": name,
            "signed_artifact_sha256": self.body_sha,
            # Past the warning window so a signed approval raises no expiry warning.
            "expiry": (self.today + timedelta(days=EXPIRY_WARNING_DAYS + 30)).isoformat(),
            "approved_by": "reviewer@example.invalid",
            "approved_at": self.today.isoformat(),
        }
        data.update(overrides)
        return data

    def sign(self, name: str, **overrides: Any) -> None:
        text = yaml.safe_dump(self.approval(name, **overrides))
        (self.approvals / f"{name}.yaml").write_text(text, encoding="utf-8")

    def unsign(self, name: str) -> None:
        # The repo's own unsigned template for the source (placeholder SHA).
        shutil.copyfile(_REPO_APPROVALS / f"{name}.yaml", self.approvals / f"{name}.yaml")

    def argv(self, *args: str) -> list[str]:
        return [*args, "--sources-dir", str(self.sources)]


def _make_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signed: tuple[str, ...]) -> _Workspace:
    for key in list(os.environ):
        if key.upper().startswith(_ENV_PREFIX.upper()):
            monkeypatch.delenv(key)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    # An empty cwd: a literal ./out, ./state or a stray .env would show up here.
    monkeypatch.chdir(cwd)
    config = tmp_path / "cfg"
    sources = config / "sources"
    approvals = config / "approvals"
    sources.mkdir(parents=True)
    approvals.mkdir()
    for path in sorted(_REPO_SOURCES.glob("*.yaml")):
        shutil.copyfile(path, sources / path.name)
    localai_url = "http://localai-under-test.invalid:18080/v1"
    localai_model = "embedder-under-test"
    assert localai_url != _default("localai_url") and localai_model != _default("localai_model")
    ws = _Workspace(
        root=tmp_path,
        cwd=cwd,
        config=config,
        sources=sources,
        approvals=approvals,
        out=tmp_path / "out-from-env",
        state=tmp_path / "state-from-env",
        localai_url=localai_url,
        localai_model=localai_model,
        body=_AKN_SAMPLE.read_bytes(),
        today=date.today(),
    )
    monkeypatch.setenv(_env("config_dir"), str(config))
    monkeypatch.setenv(_env("out_dir"), str(ws.out))
    monkeypatch.setenv(_env("state_dir"), str(ws.state))
    monkeypatch.setenv(_env("localai_url"), localai_url)
    monkeypatch.setenv(_env("localai_model"), localai_model)
    for name in _repo_names():
        if name in signed:
            ws.sign(name)
        else:
            ws.unsign(name)
    return ws


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Workspace:
    return _make_workspace(tmp_path, monkeypatch, signed=_TIER1)


@pytest.fixture
def ws_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Workspace:
    # Every repo source signed: under decision B the only workspace where `refresh --all` exits 0.
    return _make_workspace(tmp_path, monkeypatch, signed=tuple(_repo_names()))


# ---------------------------------------------------------------------------
# Spy Orchestrator and network guard
# ---------------------------------------------------------------------------


@dataclass
class _Spy:
    constructed: list[inspect.BoundArguments] = field(default_factory=list)
    runs: list[tuple[str, Any]] = field(default_factory=list)
    fail_with: dict[str, BaseException] = field(default_factory=dict)

    def run_names(self) -> list[str]:
        return [name for name, _ in self.runs]


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _Spy:
    record = _Spy()
    # Bind against the real constructor, so the CLI must call it the way the real class accepts.
    signature = inspect.signature(RealOrchestrator)

    class SpyOrchestrator:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            record.constructed.append(signature.bind(*args, **kwargs))

        def run(self, source_name: str, source_config: object) -> PublishReceipt:
            record.runs.append((source_name, source_config))
            if source_name in record.fail_with:
                raise record.fail_with[source_name]
            digest = hashlib.sha256(source_name.encode()).hexdigest()
            return PublishReceipt(chunk_count=1, matrix_sha256=digest, metadata_sha256=digest)

    # raising=False: cli has no Orchestrator attribute until it is wired (ruling 4).
    monkeypatch.setattr(cli, "Orchestrator", SpyOrchestrator, raising=False)
    return record


@dataclass
class _Net:
    attempts: list[str] = field(default_factory=list)
    served: list[httpx.Request] = field(default_factory=list)
    upstream: _Workspace | None = None
    # URL -> body served to the sync client (audit-license's live licence fetch).
    licence_pages: dict[str, bytes] = field(default_factory=dict)
    # CELEX ids EUR-Lex answers 404 for: one source's fetch fails, the others are served.
    missing_celex: set[str] = field(default_factory=set)
    # CELEX id -> XML body served instead of the workspace body (one source's text changed).
    bodies: dict[str, bytes] = field(default_factory=dict)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        ws = self.upstream
        if ws is not None:
            if url.host == _EURLEX_HOST and "/XML/" in url.path and request.method == "GET":
                self.served.append(request)
                celex = str(url.params.get("uri", "")).removeprefix("CELEX:")
                if celex in self.missing_celex:
                    return httpx.Response(404, request=request)
                return httpx.Response(200, content=self.bodies.get(celex, ws.body), request=request)
            if request.method == "POST" and str(url) == ws.localai_url.rstrip("/") + "/embeddings":
                self.served.append(request)
                payload = json.loads(request.content)
                vector = [1.0] + [0.0] * (LocalAIEmbedder.DIM - 1)
                rows = [{"index": i, "embedding": vector} for i, _ in enumerate(payload["input"])]
                return httpx.Response(200, json={"data": rows}, request=request)
        self.attempts.append(f"{request.method} {url}")
        raise httpx.ConnectError("network blocked by test", request=request)

    def eurlex_celex_ids(self) -> set[str]:
        return {
            str(r.url.params.get("uri", "")).removeprefix("CELEX:")
            for r in self.served
            if r.url.host == _EURLEX_HOST
        }

    def embed_requests(self) -> list[httpx.Request]:
        return [r for r in self.served if r.url.host != _EURLEX_HOST]


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Net]:
    record = _Net()

    async def async_send(client: httpx.AsyncClient, request: httpx.Request, *a: Any, **k: Any) -> httpx.Response:
        return record.handle(request)

    def sync_send(client: httpx.Client, request: httpx.Request, *a: Any, **k: Any) -> httpx.Response:
        if request.method == "GET" and str(request.url) in record.licence_pages:
            record.served.append(request)
            return httpx.Response(200, content=record.licence_pages[str(request.url)], request=request)
        record.attempts.append(f"sync {request.method} {request.url}")
        raise httpx.ConnectError("network blocked by test", request=request)

    def connect(sock: socket.socket, address: Any) -> None:
        record.attempts.append(f"socket {address!r}")
        raise OSError("network blocked by test")

    monkeypatch.setattr(httpx.AsyncClient, "send", async_send)
    monkeypatch.setattr(httpx.Client, "send", sync_send)
    monkeypatch.setattr(socket.socket, "connect", connect)
    yield record


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


def _why(result: Result) -> str:
    # Shows today's not-wired exit code and stderr in every red assertion (ruling 4).
    return (
        f"exit={result.exit_code} exception={result.exception!r} "
        f"stdout={result.stdout[-600:]!r} stderr={result.stderr[-600:]!r}"
    )


def _output(result: Result) -> str:
    return result.stdout + result.stderr


def _is_empty(path: Path) -> bool:
    return not path.exists() or not any(path.iterdir())


def _unsafe_chars(text: str) -> list[str]:
    return [hex(ord(c)) for c in text if c != "\n" and unicodedata.category(c) in _UNSAFE_CATEGORIES]


def _named_with(text: str, name: str, word: str) -> bool:
    """True when one output line names *name* and carries *word* (the reason)."""
    return any(name in line and word in line.lower() for line in text.splitlines())


def _assert_safe_output(result: Result, *secrets: str) -> None:
    # F8 / attack list A: no raw control, format or separator characters, no forged workflow
    # command line, no absolute tmp path, and a traceback never stands in for a message.
    for stream in (result.stdout, result.stderr):
        assert _unsafe_chars(stream) == [], (_unsafe_chars(stream), stream[:400])
        forged = [line for line in stream.splitlines() if line.lstrip().startswith("::")]
        assert forged == [], forged
        for secret in secrets:
            assert secret not in stream, (secret, stream[:400])
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(result.exception)


def _help_text(args: list[str]) -> str:
    result = _invoke(args)
    assert result.exit_code == 0, _why(result)
    return " ".join(strip_ansi(result.output).split())


def _documented(code: int) -> bool:
    text = _help_text(["--help"])
    return re.search(rf"(?<![\w.]){code} when |also exit {code}\b", text) is not None


def _assert_not_approved_checkpoint(ws: _Workspace, name: str) -> None:
    # Decision B: the unsigned source records its own failure under Settings.state_dir, so
    # health and status see a failed run, and the checkpoint carries no untrusted bytes (F8).
    state = CheckpointStore(ws.state).load(name)
    assert state is not None, (name, "no checkpoint under INGESTER_STATE_DIR")
    assert state.source_name == name and state.stage == _NOT_APPROVED_STAGE, state
    assert _unsafe_chars(state.error) == [] and "::" not in state.error, state.error[:200]
    assert str(ws.root) not in state.error and str(ws.root.resolve()) not in state.error


def _assert_failed_loudly(result: Result) -> None:
    assert result.exit_code not in _DISHONEST_FAILURE_CODES, _why(result)
    assert isinstance(result.exception, SystemExit), _why(result)
    assert "not wired" not in _output(result).lower(), _why(result)
    assert _documented(result.exit_code), (result.exit_code, "exit code missing from --help")


# ---------------------------------------------------------------------------
# Card tests (terms-analysis#93), with ruling 1 (Tier 1 = 3)
# ---------------------------------------------------------------------------


def test_fetch_invokes_orchestrator(ws: _Workspace, spy: _Spy, net: _Net) -> None:
    result = _invoke(ws.argv("fetch", "eurlex_gdpr"))
    assert result.exit_code == 0, _why(result)
    assert spy.run_names() == ["eurlex_gdpr"], _why(result)
    assert getattr(spy.runs[0][1], "name", None) == "eurlex_gdpr"
    assert "eurlex_gdpr" in result.stdout, _why(result)
    assert net.attempts == []


def test_refresh_all_invokes_orchestrator_per_source(ws: _Workspace, spy: _Spy, net: _Net) -> None:
    # Ruling 1: three runs (the card said five), one per source with a valid approval.
    # Decision B: the two unsigned sources are failures, so the partly signed run exits 8,
    # names each as failed with the approval reason, and checkpoints it as not_approved.
    result = _invoke(ws.argv("refresh", "--all"))
    assert result.exit_code == _SOURCE_FAILED, _why(result)
    assert sorted(spy.run_names()) == sorted(_TIER1), _why(result)
    for name in _TIER1:
        assert name in result.stdout, (name, _why(result))
    for name in _unsigned():
        assert name not in spy.run_names()
        assert _named_with(_output(result), name, "approv"), (name, _why(result))
        assert _named_with(result.stderr, name, "fail"), (name, _why(result))
        _assert_not_approved_checkpoint(ws, name)
    assert net.attempts == []
    assert _is_empty(ws.cwd)


def test_refresh_all_dry_run_lists_without_writing(ws: _Workspace, spy: _Spy, net: _Net) -> None:
    # Decision B: the dry run exits with the code the real run would (8 here), so a partly
    # signed pre-flight is never green, and it still writes nothing (no not_approved checkpoint).
    result = _invoke(ws.argv("refresh", "--all", "--dry-run"))
    assert result.exit_code == _SOURCE_FAILED, _why(result)
    for name in _TIER1:
        assert name in result.stdout, (name, _why(result))
    for name in _unsigned():
        assert _named_with(_output(result), name, "approv"), (name, _why(result))
    assert spy.constructed == [] and spy.runs == []
    assert net.attempts == []
    assert _is_empty(ws.out) and _is_empty(ws.state) and _is_empty(ws.cwd)


def test_refresh_all_dry_run_fully_signed_exits_zero(ws_all: _Workspace, spy: _Spy, net: _Net) -> None:
    # Round 4 F: the success path of the pre-flight. Exit 0 must mean "every source would run",
    # so it must differ from not-wired (exit 3) and from a no-op that lists nothing.
    ws = ws_all
    result = _invoke(ws.argv("refresh", "--all", "--dry-run"))
    assert result.exit_code == 0, _why(result)
    assert result.exit_code != _NOT_WIRED and "not wired" not in _output(result).lower()
    listed = [line for line in result.stdout.splitlines() if line.startswith("DRY RUN")]
    for name in _repo_names():
        assert sum(name in line.split() for line in listed) == 1, (name, _why(result))
    assert len(listed) == len(_repo_names()), _why(result)
    assert result.stderr == "", _why(result)
    assert spy.constructed == [] and spy.runs == []
    assert net.attempts == []
    assert _is_empty(ws.out) and _is_empty(ws.state) and _is_empty(ws.cwd)
    _assert_safe_output(result, str(ws.root), str(ws.root.resolve()))


# ---------------------------------------------------------------------------
# Rulings 2 and 3: how the Orchestrator is composed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [("fetch", "eurlex_gdpr"), ("refresh", "--all")], ids=["fetch", "refresh"])
def test_cli_never_passes_embedder_model(
    ws_all: _Workspace, spy: _Spy, net: _Net, argv: tuple[str, ...]
) -> None:
    # Ruling 2: terms-analysis#98 owns the embedder model; the CLI leaves the parameter alone.
    # Every source signed (decision B), so exit 0 means every run was composed and succeeded.
    result = _invoke(ws_all.argv(*argv))
    assert result.exit_code == 0, _why(result)
    assert spy.constructed, _why(result)
    for bound in spy.constructed:
        assert "embedder_model" not in bound.arguments, bound.arguments.get("embedder_model")


def test_orchestrator_dirs_come_from_settings(ws_all: _Workspace, spy: _Spy, net: _Net) -> None:
    # Ruling 3 / F13: the publish target and state dir are the env-configured ones.
    # Every source signed (decision B), so all five runs are composed and the run exits 0.
    ws = ws_all
    result = _invoke(ws.argv("refresh", "--all"))
    assert result.exit_code == 0, _why(result)
    assert len(spy.constructed) == len(_repo_names()), _why(result)
    for bound in spy.constructed:
        target = Path(bound.arguments["publish_target"].path).resolve()
        state = Path(bound.arguments["state_dir"]).resolve()
        assert target.is_relative_to(ws.out.resolve()), target
        assert state.is_relative_to(ws.state.resolve()), state
    # Round 5 J: refresh runs the licence-drift gate, with its baseline in the configured state
    # dir (the one file every source shares), never None and never a cwd-relative default.
    baselines = set()
    for bound in spy.constructed:
        baseline = bound.arguments.get("license_state_file")
        assert baseline is not None, "refresh composes the Orchestrator without license_state_file"
        baselines.add(Path(baseline).resolve())
    # Round 6 P: the gate's own file, never audit-license's license-hashes.json.
    assert baselines == {ws.state.resolve() / _refresh_baseline_name()}, baselines
    assert _is_empty(ws.cwd)


def test_fetch_end_to_end_uses_settings_for_localai_and_dirs(ws: _Workspace, net: _Net) -> None:
    # The real Orchestrator behind a fake transport: EUR-Lex XML and LocalAI are served only
    # at the env-configured URL, so a literal default URL or dir fails here.
    net.upstream = ws
    result = _invoke(ws.argv("fetch", "eurlex_gdpr"))
    assert result.exit_code == 0, _why(result)
    assert net.attempts == [], net.attempts
    embeds = net.embed_requests()
    assert embeds, "LocalAI was never called at the Settings URL"
    for request in embeds:
        assert json.loads(request.content)["model"] == ws.localai_model
    state = CheckpointStore(ws.state).load("eurlex_gdpr")
    assert state is not None and state.stage == "done", state
    manifests = sorted(ws.out.rglob(MANIFEST_FILENAME))
    assert manifests, "nothing published under Settings.out_dir"
    manifest = Manifest.load(manifests[0].parent)
    assert "eurlex_gdpr" in manifest.sources
    assert re.search(rf"\b{manifest.chunk_count}\b", result.stdout), _why(result)
    assert _is_empty(ws.cwd)


def test_refresh_all_end_to_end_fetches_only_approved_sources(ws: _Workspace, net: _Net) -> None:
    # Decision B: the signed sources publish, the unsigned ones fail with not_approved, exit 8.
    net.upstream = ws
    result = _invoke(ws.argv("refresh", "--all"))
    assert result.exit_code == _SOURCE_FAILED, _why(result)
    assert net.attempts == [], net.attempts
    registry = load_all(ws.sources)
    expected = {registry[name].pipeline.celex_id for name in _TIER1}
    assert net.eurlex_celex_ids() == expected, net.eurlex_celex_ids()
    store = CheckpointStore(ws.state)
    for name in _TIER1:
        state = store.load(name)
        assert state is not None and state.stage == "done", (name, state)
    for name in _unsigned():
        _assert_not_approved_checkpoint(ws, name)
        assert _named_with(result.stderr, name, "fail"), (name, _why(result))
    # Round 8 item 3: a partly failed run publishes no bundle and makes no out/current.
    assert _bundle_versions(ws.out) == set(), _bundle_versions(ws.out)
    assert not os.path.lexists(ws.out / _CURRENT), "a partly failed run created out/current"
    assert _is_empty(ws.cwd)


_HOSTILE_SPDX = "CC0-1.0\x1b[2J\n::error::forged\u202eevil\u2028"


def _baseline_hash(path: Path, name: str) -> str:
    return str(json.loads(path.read_text(encoding="utf-8"))[name]["hash"])


def test_refresh_spdx_drift_blocks_publish_and_names_the_source(ws_all: _Workspace, net: _Net) -> None:
    # Round 5 J: the real pipeline, twice. Run 1 records each source's licence baseline in the
    # configured state dir; an SPDX change on a signed source then blocks its publish, names it,
    # checkpoints license_failed (round 6 Q) and exits 8 while the others publish.
    ws = ws_all
    net.upstream = ws
    first = _invoke(ws.argv("refresh", "--all"))
    assert first.exit_code == 0, _why(first)
    baseline = ws.state / _refresh_baseline_name()
    assert baseline.is_file(), "refresh recorded no licence baseline in INGESTER_STATE_DIR"
    recorded = json.loads(baseline.read_text(encoding="utf-8"))
    assert set(recorded) == set(_repo_names()), sorted(recorded)

    # The stored SPDX differs from the one the fetcher reports: SPDX_DRIFT for eurlex_gdpr only.
    recorded["eurlex_gdpr"]["spdx"] = _HOSTILE_SPDX
    baseline.write_text(json.dumps(recorded), encoding="utf-8")
    # Round 8 item 3: run 1's bundle is the one consumers see; the blocked run must leave it be.
    _assert_current_bundle(ws, _repo_names())
    before = _published_state(ws.out)
    second = _invoke(ws.argv("refresh", "--all"))
    assert second.exit_code == _SOURCE_FAILED, _why(second)
    assert _named_with(second.stderr, "eurlex_gdpr", "fail"), _why(second)
    assert _published_state(ws.out) == before, "a run with SPDX drift published or repointed a bundle"
    # Round 6 Q (ruling 3): the blocked source checkpoints its own failure, with the reason.
    state = CheckpointStore(ws.state).load("eurlex_gdpr")
    assert state is not None and state.stage == "license_failed", state
    assert state.error.strip(), "the license_failed checkpoint carries no error"
    # The drifted baseline is kept for review, not overwritten by the blocked run.
    assert json.loads(baseline.read_text(encoding="utf-8"))["eurlex_gdpr"]["spdx"] == _HOSTILE_SPDX
    assert "::error::" not in _output(second), _why(second)
    _assert_safe_output(second, str(ws.root), str(ws.root.resolve()))
    assert net.attempts == [], net.attempts
    assert _is_empty(ws.cwd)


# ---------------------------------------------------------------------------
# Round 8: one combined calver bundle per run, the contract refresh.yml's detect and record
# steps read (grumpy HIGH at cli.py:415). The real Orchestrator and publisher run; only EUR-Lex
# and LocalAI are faked, at the HTTP boundary. detect runs as its own process, as in the workflow.
# ---------------------------------------------------------------------------

_DETECT_SCRIPT = _REPO / "scripts" / "detect_bundle_change.py"
# The link name refresh.yml passes to detect and record (`--bundle-link out/current`), and the
# name TermsAnalysisPublisher repoints.
_CURRENT = "current"


def _load_detect() -> ModuleType:
    # The script is not a package; load it by path so the tests read its VERSION_RE, not a copy.
    spec = importlib.util.spec_from_file_location("detect_bundle_change_under_test", _DETECT_SCRIPT)
    assert spec is not None and spec.loader is not None, _DETECT_SCRIPT
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_DETECT = _load_detect()


def _bundle_versions(out: Path) -> set[str]:
    # Real dirs directly under out/ whose name detect accepts as a bundle version.
    if not out.is_dir():
        return set()
    return {
        child.name
        for child in out.iterdir()
        if child.is_dir() and not child.is_symlink() and _DETECT.VERSION_RE.fullmatch(child.name)
    }


def _tree_digest(root: Path) -> str:
    # Every path and byte under root, so an in-place rewrite of a bundle shows up.
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(str(path.relative_to(root)).encode("utf-8", "surrogateescape"))
        if path.is_symlink():
            digest.update(b"->" + os.readlink(path).encode("utf-8", "surrogateescape"))
        elif path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _published_state(out: Path) -> dict[str, str]:
    # What a consumer can see: the current link, each version dir, and the files at the out root
    # (the hot-reload copies). Non-version dirs (per-source staging, if the coder keeps it) are left
    # out on purpose: the ruling lets the coder choose staging or no per-source output.
    state: dict[str, str] = {}
    link = out / _CURRENT
    if os.path.lexists(link):
        state[_CURRENT] = os.readlink(link) if link.is_symlink() else "<not a symlink>"
    for version in _bundle_versions(out):
        state[version] = _tree_digest(out / version)
    for child in out.iterdir() if out.is_dir() else ():
        if child.is_file() and not child.is_symlink():
            state[child.name] = hashlib.sha256(child.read_bytes()).hexdigest()
    return state


def _embedded_texts(net: _Net) -> int:
    # Every text the run sent to LocalAI: an independent count of the chunks the run produced.
    return sum(len(json.loads(request.content)["input"]) for request in net.embed_requests())


def _assert_current_bundle(
    ws: _Workspace, expected_sources: list[str], embedded: int | None = None
) -> str:
    """Round 8 item 2. Returns the version out/current points at."""
    link = ws.out / _CURRENT
    listing = sorted(p.name for p in ws.out.iterdir()) if ws.out.is_dir() else []
    assert link.is_symlink(), f"refresh made no out/{_CURRENT} symlink; out/ holds {listing}"
    target = os.readlink(link)
    assert not os.path.isabs(target), "out/current is an absolute link; it must survive a moved out/"
    # One plain version name: no slash, no '..', nothing detect would refuse.
    assert _DETECT.VERSION_RE.fullmatch(target), f"out/current -> {target!r} is not a version name"
    bundle = ws.out / target
    assert bundle.is_dir() and not bundle.is_symlink(), f"out/current -> {target!r} is not a dir"
    assert bundle.resolve().parent == ws.out.resolve()
    checksums = bundle / "checksums.txt"
    assert (bundle / MANIFEST_FILENAME).is_file() and checksums.is_file(), sorted(
        p.name for p in bundle.iterdir()
    )
    assert (bundle / "index").is_dir(), sorted(p.name for p in bundle.iterdir())
    # The checksums describe this bundle's own bytes, every listed file inside the bundle.
    lines = [line for line in checksums.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines, "checksums.txt lists nothing"
    for line in lines:
        sha, _, rel = line.partition("  ")
        listed = (bundle / rel).resolve()
        assert listed.is_relative_to(bundle.resolve()) and listed.is_file(), line
        assert hashlib.sha256(listed.read_bytes()).hexdigest() == sha, f"stale checksum: {line}"
    manifest = Manifest.load(bundle)
    assert manifest.corpus_version == target, (manifest.corpus_version, target)
    assert sorted(manifest.sources) == sorted(expected_sources), manifest.sources
    # The index holds one row per chunk the MANIFEST counts; with *embedded* (every text the run
    # embedded), that is every source's chunks, not one source's corpus under every name.
    rows = np.load(bundle / "index" / "legal_kb.npy", allow_pickle=False).shape[0]
    assert rows == manifest.chunk_count, (rows, manifest.chunk_count)
    if embedded is not None:
        assert manifest.chunk_count == embedded, (manifest.chunk_count, embedded)
    return target


def _bundle_script(
    ws: _Workspace, command: str, *extra: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    """Run detect_bundle_change.py as refresh.yml does: its own process, out/current, a state file."""
    work = ws.root / "detect-work"
    work.mkdir(exist_ok=True)
    github_output = work / "github-output"
    github_output.unlink(missing_ok=True)
    args = [
        sys.executable,
        str(_DETECT_SCRIPT),
        command,
        "--bundle-link",
        str(ws.out / _CURRENT),
        "--state-file",
        str(work / "last-published-bundle.json"),
        *extra,
    ]
    if command == "detect":
        args += ["--github-output", str(github_output)]
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(_ENV_PREFIX.upper())}
    # Never the real state dir, even if --state-file handling regresses.
    env["XDG_STATE_HOME"] = str(work / "xdg-state")
    proc = subprocess.run(
        args, cwd=ws.cwd, env=env, capture_output=True, text=True, timeout=120, check=False
    )
    outputs: dict[str, str] = {}
    if github_output.is_file():
        for line in github_output.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                outputs[key] = value
    return proc, outputs


def _calver_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def test_refresh_all_publishes_one_bundle_that_detect_accepts(ws_all: _Workspace, net: _Net) -> None:
    # Round 8 items 1 and 2: the workflow's own sequence (refresh, seed record, detect).
    ws = ws_all
    net.upstream = ws
    result = _invoke(ws.argv("refresh", "--all"))
    assert result.exit_code == 0, _why(result)
    assert net.attempts == [], net.attempts
    # The consumer first, as the workflow runs it: no record yet, so the first bundle is news
    # (detect without --require-state). Exit 1 here is the grumpy HIGH: no bundle to read.
    first, outputs = _bundle_script(ws, "detect")
    assert first.returncode == 0, f"detect exit {first.returncode}: {first.stderr.strip()}"
    version = _assert_current_bundle(ws, _repo_names(), embedded=_embedded_texts(net))
    assert _bundle_versions(ws.out) == {version}, _bundle_versions(ws.out)
    assert outputs == {"changed": "true", "new_target": version}, outputs
    seed, _ = _bundle_script(ws, "record")
    assert seed.returncode == 0, seed.stderr
    # refresh.yml's detect step: --require-state, record present, same bundle, no news.
    again, outputs = _bundle_script(ws, "detect", "--require-state")
    assert again.returncode == 0, again.stderr
    assert outputs == {"changed": "false", "new_target": version}, outputs
    assert _is_empty(ws.cwd)


# Round 8 item 3: one source fails, by a different stage per row, first or last in the run.
_PARTIAL_FAILURES = {
    "not-approved-first": ("unsign", 0),
    "fetch-404-last": ("missing", -1),
}


@pytest.mark.parametrize("failure", sorted(_PARTIAL_FAILURES))
def test_failed_source_publishes_nothing_and_keeps_current(
    ws_all: _Workspace, net: _Net, failure: str
) -> None:
    ws = ws_all
    net.upstream = ws
    first = _invoke(ws.argv("refresh", "--all"))
    assert first.exit_code == 0, _why(first)
    version = _assert_current_bundle(ws, _repo_names())
    seed, _ = _bundle_script(ws, "record")
    assert seed.returncode == 0, seed.stderr
    before = _published_state(ws.out)

    how, position = _PARTIAL_FAILURES[failure]
    victim = _repo_names()[position]
    if how == "unsign":
        ws.unsign(victim)
    else:
        net.missing_celex.add(load_all(ws.sources)[victim].pipeline.celex_id)
    second = _invoke(ws.argv("refresh", "--all"))
    assert second.exit_code == _SOURCE_FAILED, _why(second)
    assert _named_with(second.stderr, victim, "fail"), _why(second)
    # No new version dir, no rewrite of the old one, no repoint, no new hot-reload copies.
    assert _published_state(ws.out) == before, (sorted(before), sorted(_published_state(ws.out)))
    assert os.readlink(ws.out / _CURRENT) == version
    # The workflow's detect after the failed run sees the announced bundle, unchanged.
    detect, outputs = _bundle_script(ws, "detect", "--require-state")
    assert detect.returncode == 0, detect.stderr
    assert outputs == {"changed": "false", "new_target": version}, outputs
    assert net.attempts == [], net.attempts
    assert _is_empty(ws.cwd)


def test_partial_failure_table_has_a_row_per_failure_stage_and_both_ends() -> None:
    hows = {how for how, _ in _PARTIAL_FAILURES.values()}
    positions = {position for _, position in _PARTIAL_FAILURES.values()}
    assert hows == {"unsign", "missing"} and positions == {0, -1}, _PARTIAL_FAILURES


def _first_run_recorded(ws: _Workspace, net: _Net) -> tuple[str, str]:
    """Run 1 of item 4: a clean refresh, then the workflow's record step. Returns (v1, digest)."""
    net.upstream = ws
    first = _invoke(ws.argv("refresh", "--all"))
    assert first.exit_code == 0, _why(first)
    v1 = _assert_current_bundle(ws, _repo_names())
    seed, _ = _bundle_script(ws, "record")
    assert seed.returncode == 0, seed.stderr
    return v1, _tree_digest(ws.out / v1)


def test_identical_second_refresh_publishes_nothing_new(ws_all: _Workspace, net: _Net) -> None:
    # Round 8b item 4a: the same fixtures again. Same chunk texts and the same corpus/ and index/
    # bytes, so no new version, no repoint, bundle 1's bytes as they were, and no weekly
    # "new bundle" alert: detect against run 1's record says changed=false.
    ws = ws_all
    v1, _ = _first_run_recorded(ws, net)
    before = _published_state(ws.out)
    second = _invoke(ws.argv("refresh", "--all"))
    assert second.exit_code == 0, _why(second)
    assert _published_state(ws.out) == before, (sorted(before), sorted(_published_state(ws.out)))
    assert _bundle_versions(ws.out) == {v1}, _bundle_versions(ws.out)
    assert os.readlink(ws.out / _CURRENT) == v1
    detect, outputs = _bundle_script(ws, "detect", "--require-state")
    assert detect.returncode == 0, detect.stderr
    assert outputs == {"changed": "false", "new_target": v1}, outputs
    assert net.attempts == [], net.attempts
    assert _is_empty(ws.cwd)


def _changed_body(body: bytes) -> bytes:
    # One more sentence inside the first paragraph: the cleaned text, and so a chunk, changes.
    changed = body.replace(b"</p>", b" Amended under test.</p>", 1)
    assert changed != body, "the fixture has no paragraph to change"
    return changed


@pytest.mark.parametrize("between_runs", ["keep-current", "current-removed"])
def test_changed_second_refresh_publishes_a_new_version_detect_reports_changed(
    ws_all: _Workspace, net: _Net, between_runs: str
) -> None:
    # Round 8b item 4b: one source's text changed (and re-signed for its new artifact sha).
    # "current-removed": a lost link must not make run 2 reuse run 1's version dir (the calver
    # comes from what is on disk, not from the link alone).
    ws = ws_all
    v1, v1_digest = _first_run_recorded(ws, net)
    victim = _repo_names()[0]
    changed = _changed_body(ws.body)
    net.bodies[load_all(ws.sources)[victim].pipeline.celex_id] = changed
    ws.sign(victim, signed_artifact_sha256=hashlib.sha256(changed).hexdigest())
    if between_runs == "current-removed":
        (ws.out / _CURRENT).unlink()

    second = _invoke(ws.argv("refresh", "--all"))
    assert second.exit_code == 0, _why(second)
    v2 = _assert_current_bundle(ws, _repo_names())
    assert v2 != v1, f"run 2 republished version {v1}"
    # Newer sorts later, as retention orders bundles by (year, month, patch).
    assert _calver_key(v2) > _calver_key(v1), (v1, v2)
    assert _bundle_versions(ws.out) == {v1, v2}, _bundle_versions(ws.out)
    assert _tree_digest(ws.out / v1) == v1_digest, f"run 2 rewrote bundle {v1}"

    detect, outputs = _bundle_script(ws, "detect", "--require-state")
    assert detect.returncode == 0, detect.stderr
    assert outputs == {"changed": "true", "new_target": v2}, outputs
    record, _ = _bundle_script(ws, "record")
    assert record.returncode == 0, record.stderr
    settled, outputs = _bundle_script(ws, "detect", "--require-state")
    assert settled.returncode == 0, settled.stderr
    assert outputs == {"changed": "false", "new_target": v2}, outputs
    assert net.attempts == [], net.attempts
    assert _is_empty(ws.cwd)


def test_refresh_replaces_a_planted_absolute_current_link(ws_all: _Workspace, net: _Net) -> None:
    # Indirection: out/current left pointing outside out/ (absolute) by an old tool or an
    # attacker. A successful run repoints it to its own relative version; the outside dir is
    # never written through the link.
    ws = ws_all
    net.upstream = ws
    outside = ws.root / "outside-out"
    outside.mkdir()
    (outside / "checksums.txt").write_text("planted\n", encoding="utf-8")
    planted = _tree_digest(outside)
    ws.out.mkdir(parents=True)
    (ws.out / _CURRENT).symlink_to(outside.resolve())
    result = _invoke(ws.argv("refresh", "--all"))
    assert result.exit_code == 0, _why(result)
    _assert_current_bundle(ws, _repo_names())
    assert _tree_digest(outside) == planted, "refresh wrote through the planted current link"
    detect, outputs = _bundle_script(ws, "detect")
    assert detect.returncode == 0, detect.stderr
    assert outputs["changed"] == "true", outputs


# ---------------------------------------------------------------------------
# Round 6 P: one state dir, two baseline files. audit-license hashes the licence page and the
# refresh gate hashes the act's text; sharing a file would make each overwrite the other.
# ---------------------------------------------------------------------------

_LICENCE_PAGE = b"<html><body>Licence notice served to audit-license under test</body></html>"


def _serve_licence_pages(ws: _Workspace, net: _Net) -> None:
    net.upstream = ws
    for cfg in load_all(ws.sources).values():
        if cfg.license.url:
            net.licence_pages[cfg.license.url] = _LICENCE_PAGE


def test_baseline_files_are_two_named_constants() -> None:
    # The ruled name, stated once here; every other test reads the constant.
    assert _refresh_baseline_name() == "refresh-license-baseline.json"
    assert _refresh_baseline_name() != AUDIT_BASELINE_FILE.name


def test_refresh_and_audit_license_never_touch_each_others_baseline(ws_all: _Workspace, net: _Net) -> None:
    ws = ws_all
    _serve_licence_pages(ws, net)
    audit_file = ws.state / AUDIT_BASELINE_FILE.name
    refresh_file = ws.state / _refresh_baseline_name()
    # audit-license records its own baseline first, by default under INGESTER_STATE_DIR.
    first_audit = _invoke(ws.argv("audit-license", "eurlex_gdpr", "--update-baseline"))
    assert first_audit.exit_code == 0, _why(first_audit)
    assert audit_file.is_file(), "audit-license did not record under INGESTER_STATE_DIR"
    audit_bytes = audit_file.read_bytes()

    refresh = _invoke(ws.argv("refresh", "--all"))
    assert refresh.exit_code == 0, _why(refresh)
    assert audit_file.read_bytes() == audit_bytes, "refresh rewrote audit-license's baseline"
    assert refresh_file.is_file(), "refresh recorded no baseline of its own"
    refresh_bytes = refresh_file.read_bytes()

    # Offline: the stored hash comes from the state-dir file, not a cwd-relative decoy.
    stored = json.loads(audit_bytes)["eurlex_gdpr"]["hash"]
    decoy = ws.cwd / AUDIT_BASELINE_FILE
    decoy.parent.mkdir(parents=True)
    decoy_hash = "d" * 64
    assert decoy_hash[:12] != stored[:12]
    decoy.write_text(json.dumps({"eurlex_gdpr": {"hash": decoy_hash, "spdx": "CC-BY-4.0"}}), encoding="utf-8")
    offline = _invoke(ws.argv("audit-license", "eurlex_gdpr", "--offline"))
    assert offline.exit_code == 0, _why(offline)
    assert stored[:12] in offline.stdout and decoy_hash[:12] not in offline.stdout, _why(offline)
    shutil.rmtree(ws.cwd / AUDIT_BASELINE_FILE.parts[0])

    # A live audit that writes (a new source's baseline) leaves the refresh file untouched.
    live = _invoke(ws.argv("audit-license", "eurlex_dsa", "--update-baseline"))
    assert live.exit_code == 0, _why(live)
    assert "eurlex_dsa" in json.loads(audit_file.read_text(encoding="utf-8"))
    assert refresh_file.read_bytes() == refresh_bytes, "audit-license rewrote the refresh baseline"
    assert net.attempts == [], net.attempts
    assert _is_empty(ws.cwd)


def test_unchanged_document_never_warns_text_drift_across_audit_runs(ws_all: _Workspace, net: _Net) -> None:
    # The ping-pong regression: audit-license, refresh, audit-license, refresh, all on an
    # unchanged document and licence page. With one shared file each tool reads the other's hash:
    # refresh warns text drift and the next audit sees DRIFT, on every run.
    ws = ws_all
    _serve_licence_pages(ws, net)
    results = []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for _ in range(2):
            results.append(_invoke(ws.argv("audit-license", "eurlex_gdpr", "--update-baseline")))
            results.append(_invoke(ws.argv("refresh", "--all")))
    assert (ws.state / _refresh_baseline_name()).is_file(), "refresh recorded no baseline"
    drift = [str(w.message) for w in caught if "License text changed" in str(w.message)]
    assert drift == [], drift
    for result in results:
        assert result.exit_code == 0, _why(result)
    assert net.attempts == [], net.attempts


def test_refresh_dry_run_real_pipeline_stays_offline(ws: _Workspace, net: _Net) -> None:
    # No spy: the real composition must not reach the network or create a dir on --dry-run.
    # Decision B: the dry run reports the exit code the real run would give (8: two unsigned).
    result = _invoke(ws.argv("refresh", "--all", "--dry-run"))
    assert result.exit_code == _SOURCE_FAILED, _why(result)
    assert net.attempts == [], net.attempts
    for name in _TIER1:
        assert name in result.stdout, (name, _why(result))
    assert _is_empty(ws.out) and _is_empty(ws.state) and _is_empty(ws.cwd)


def test_fetched_artifact_must_match_the_signed_sha(ws: _Workspace, net: _Net) -> None:
    # A signed approval for a different artifact: the fetched body must not be embedded or
    # published (the SHA check can only run after the fetch).
    ws.sign("eurlex_gdpr", signed_artifact_sha256=hashlib.sha256(b"another artifact").hexdigest())
    net.upstream = ws
    result = _invoke(ws.argv("fetch", "eurlex_gdpr"))
    _assert_failed_loudly(result)
    assert "eurlex_gdpr" in _output(result), _why(result)
    assert net.embed_requests() == []
    assert list(ws.out.rglob(MANIFEST_FILENAME)) == []
    state = CheckpointStore(ws.state).load("eurlex_gdpr")
    assert state is None or state.stage != "done", state
    _assert_safe_output(result, str(ws.root))


# ---------------------------------------------------------------------------
# Ruling 5: "nothing ran" is a loud failure, distinct from "not wired"
# ---------------------------------------------------------------------------


def test_refresh_with_no_valid_approval_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: _Spy, net: _Net
) -> None:
    # Every approval is the repo's unsigned template, as on main today.
    ws = _make_workspace(tmp_path, monkeypatch, signed=())
    # Dry run first: it writes nothing (ruling E keeps its exit code equal to the real run's).
    dry = _invoke(ws.argv("refresh", "--all", "--dry-run"))
    assert _is_empty(ws.state), "a dry run wrote a checkpoint"
    real = _invoke(ws.argv("refresh", "--all"))
    # Ruling D: exit 7 still records a not_approved checkpoint for every requested source,
    # so the state matches an exit-8 run and health reports each as failed (ruling C).
    for name in _repo_names():
        _assert_not_approved_checkpoint(ws, name)
    for result in (real, dry):
        _assert_failed_loudly(result)
        for name in _repo_names():
            assert _named_with(_output(result), name, "approv"), (name, _why(result))
        _assert_safe_output(result, str(ws.root))
    assert dry.exit_code == real.exit_code, (_why(dry), _why(real))
    # Decision B: exit 7 only when no requested source is signed.
    assert real.exit_code == _NO_APPROVED, _why(real)
    assert spy.constructed == [] and spy.runs == []
    assert net.attempts == []
    assert _is_empty(ws.out) and _is_empty(ws.cwd)


def test_fetch_of_unsigned_source_refuses(ws: _Workspace, spy: _Spy, net: _Net) -> None:
    # `fetch` must not bypass the legal gate that `refresh --all` applies (needs a ruling).
    name = _unsigned()[0]
    # Round 4 I: the dry run reports what the real fetch would do (exit 7), offline, and
    # writes nothing (ruling E's principle; ruling D applies to the real run only).
    dry = _invoke(ws.argv("fetch", name, "--dry-run"))
    assert dry.exit_code == _NO_APPROVED, _why(dry)
    assert _named_with(_output(dry), name, "approv"), _why(dry)
    assert spy.constructed == [] and spy.runs == [] and net.attempts == [], _why(dry)
    assert _is_empty(ws.out) and _is_empty(ws.state) and _is_empty(ws.cwd), _why(dry)
    _assert_safe_output(dry, str(ws.root), str(ws.root.resolve()))
    result = _invoke(ws.argv("fetch", name))
    _assert_failed_loudly(result)
    assert result.exit_code == _NO_APPROVED, _why(result)  # decision A
    _assert_not_approved_checkpoint(ws, name)  # ruling D: the requested source is recorded
    assert name not in spy.run_names()
    assert _named_with(_output(result), name, "approv"), _why(result)
    assert net.attempts == []


_HUGE = "A" * 1_000_000
_COMBINED = "\x1b[2J\x1b[31m red\n::error::forged\u202eevil\u2028next\u2029para\x00nul\ud800\udcff"

# Hostile text carried by the exception message, one class per row (attack list A).
_PAYLOADS: dict[str, str] = {
    "ansi-escape": "\x1b[2J\x1b[31mcleared",
    "workflow-command": "ok\n::error file=x::forged annotation",
    "carriage-return": "progress\rDONE: published",
    "bidi-override": "abc\u202edef",
    "line-separator": "one\u2028two\u2029three",
    "nul": "a\x00b",
    "lone-surrogate": "x\ud800y",
    "invalid-utf8-escape": "x\udcffy",
    "huge": _HUGE,
}

# Each exception type the pipeline can raise, carrying every hostile class at once.
_EXCEPTIONS: dict[str, Callable[[str, Path], BaseException]] = {
    "upstream-not-found": lambda p, root: UpstreamNotFoundError(p),
    "embed-endpoint": lambda p, root: EmbedEndpointError(p),
    "license-drift": lambda p, root: LicenseDriftError("eurlex_gdpr", old_spdx=p, new_spdx=p),
    "legal-review-gate": lambda p, root: LegalReviewGateError(p),
    "os-error-with-path": lambda p, root: OSError(13, p, str(root / "secret" / "state.json")),
    "value-error": lambda p, root: ValueError(p),
    "runtime-error": lambda p, root: RuntimeError(p),
    "key-error": lambda p, root: KeyError(p),
}

_FAILURE_CASES = [
    *[pytest.param(RuntimeError, payload, id=f"payload-{key}") for key, payload in _PAYLOADS.items()],
    *[pytest.param(key, _COMBINED, id=f"exception-{key}") for key in _EXCEPTIONS],
]


@pytest.mark.parametrize(("kind", "payload"), _FAILURE_CASES)
def test_orchestrator_failure_exits_non_zero_naming_the_source(
    ws: _Workspace, spy: _Spy, net: _Net, kind: Any, payload: str
) -> None:
    exc = RuntimeError(payload) if kind is RuntimeError else _EXCEPTIONS[kind](payload, ws.root)
    spy.fail_with["eurlex_gdpr"] = exc
    result = _invoke(ws.argv("refresh", "--all"))
    _assert_failed_loudly(result)
    assert result.exit_code == _SOURCE_FAILED, _why(result)
    assert "eurlex_gdpr" in spy.run_names(), _why(result)
    assert "eurlex_gdpr" in _output(result), _why(result)
    _assert_safe_output(result, str(ws.root), str(ws.root.resolve()))
    # Size: the message is bounded, not proportional to the exception text.
    assert len(_output(result)) < len(_HUGE) // 10, len(_output(result))


def test_failure_tables_cover_every_hostile_class() -> None:
    # Contract: every unsafe category is fed through the payload table, and the combined
    # payload carries each of them too, so no exception row is benign.
    fed = {unicodedata.category(c) for p in _PAYLOADS.values() for c in p}
    assert _UNSAFE_CATEGORIES <= fed, _UNSAFE_CATEGORIES - fed
    combined = {unicodedata.category(c) for c in _COMBINED}
    assert _UNSAFE_CATEGORIES <= combined, _UNSAFE_CATEGORIES - combined
    assert "\n::" in _COMBINED and len(_HUGE) > 100_000


# ---------------------------------------------------------------------------
# Approval states: which sources refresh --all runs (ruling 1)
# ---------------------------------------------------------------------------

_HOSTILE_TEXT = "\x1b[2J\n::error::forged\u202e\u2028"


def _state(ws: _Workspace, name: str, key: str) -> str | None:
    """APPROVAL.yaml text for *name* in approval state *key* (None: no file)."""
    t = ws.today
    texts: dict[str, Callable[[], str | None]] = {
        "signed": lambda: yaml.safe_dump(ws.approval(name)),
        "placeholder": lambda: yaml.safe_dump(ws.approval(name, signed_artifact_sha256=_PLACEHOLDER_SHA)),
        "sha-empty": lambda: yaml.safe_dump(ws.approval(name, signed_artifact_sha256="")),
        "sha-not-hex": lambda: yaml.safe_dump(ws.approval(name, signed_artifact_sha256="z" * 64)),
        "expired-yesterday": lambda: yaml.safe_dump(ws.approval(name, expiry=(t - timedelta(days=1)).isoformat())),
        "expires-today": lambda: yaml.safe_dump(ws.approval(name, expiry=t.isoformat())),
        "expiry-missing": lambda: yaml.safe_dump(
            {k: v for k, v in ws.approval(name).items() if k != "expiry"}
        ),
        "expiry-hostile": lambda: yaml.safe_dump(ws.approval(name, expiry="2027-13-01" + _HOSTILE_TEXT)),
        "source-id-other": lambda: yaml.safe_dump(ws.approval(name, source_id=_TIER1[1])),
        "source-id-hostile": lambda: yaml.safe_dump(ws.approval(name, source_id=name + _HOSTILE_TEXT)),
        "missing-file": lambda: None,
        "empty-file": lambda: "",
        "not-a-mapping": lambda: "- a\n- b\n",
        "invalid-yaml": lambda: "source_id: [unclosed\n",
    }
    return texts[key]()


# Decision B: what `refresh --all` does with one source's approval in each state.
_RUNS = "runs"  # valid approval: the source runs
_SOURCE_FAILURE = "source-failure"  # no valid approval: not_approved checkpoint, others run, exit 8
_RUN_LOAD_ERROR = "load-error"  # file unreadable as a mapping: the whole run fails, exit 1 (ruling 4)

_APPROVAL_OUTCOME: dict[str, str] = {
    "signed": _RUNS,
    "placeholder": _SOURCE_FAILURE,
    "sha-empty": _SOURCE_FAILURE,
    "sha-not-hex": _SOURCE_FAILURE,
    "expired-yesterday": _SOURCE_FAILURE,
    "expires-today": _SOURCE_FAILURE,
    "expiry-missing": _SOURCE_FAILURE,
    "expiry-hostile": _SOURCE_FAILURE,
    "source-id-other": _SOURCE_FAILURE,
    "source-id-hostile": _SOURCE_FAILURE,
    "missing-file": _SOURCE_FAILURE,
    "empty-file": _RUN_LOAD_ERROR,
    "not-a-mapping": _RUN_LOAD_ERROR,
    "invalid-yaml": _RUN_LOAD_ERROR,
}


def _write_state(ws: _Workspace, name: str, key: str) -> Path:
    path = ws.approvals / f"{name}.yaml"
    text = _state(ws, name, key)
    if text is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def test_approval_table_matches_the_real_gate(ws: _Workspace) -> None:
    # Contract (green today): each row's outcome is the one the real gate gives against the
    # served artifact, and every outcome has at least one row.
    assert set(_APPROVAL_OUTCOME.values()) == {_RUNS, _SOURCE_FAILURE, _RUN_LOAD_ERROR}
    for key, outcome in _APPROVAL_OUTCOME.items():
        path = _write_state(ws, "eurlex_gdpr", key)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            if outcome == _RUNS:
                check_approval(path, "eurlex_gdpr", ws.body_sha, today=ws.today)
            elif outcome == _RUN_LOAD_ERROR:
                assert path.exists(), key
                with pytest.raises((LegalReviewGateError, yaml.YAMLError)):
                    load_approval(path)
            elif path.exists():
                load_approval(path)  # parses as a mapping: the failure is this source's alone
                with pytest.raises(LegalReviewGateError):
                    check_approval(path, "eurlex_gdpr", ws.body_sha, today=ws.today)


@pytest.mark.parametrize("key", list(_APPROVAL_OUTCOME))
def test_refresh_runs_a_source_only_with_a_valid_approval(
    ws_all: _Workspace, spy: _Spy, net: _Net, key: str
) -> None:
    # Decision B, one source varied with the other four signed.
    ws = ws_all
    _write_state(ws, "eurlex_gdpr", key)
    others = [name for name in _repo_names() if name != "eurlex_gdpr"]
    outcome = _APPROVAL_OUTCOME[key]
    if outcome == _RUN_LOAD_ERROR:
        # Round 4 H: an unsigned source checked before the malformed file must not leave a
        # not_approved checkpoint behind; the load error (exit 1) writes nothing (ruling 4).
        ws.unsign(others[0])
        assert _repo_names().index(others[0]) < _repo_names().index("eurlex_gdpr")
    result = _invoke(ws.argv("refresh", "--all"))
    if outcome == _RUNS:
        assert result.exit_code == 0, _why(result)
        assert sorted(spy.run_names()) == sorted(_repo_names()), _why(result)
    elif outcome == _SOURCE_FAILURE:
        assert result.exit_code == _SOURCE_FAILED, _why(result)
        assert sorted(spy.run_names()) == sorted(others), _why(result)
        assert _named_with(_output(result), "eurlex_gdpr", "approv"), _why(result)
        assert _named_with(result.stderr, "eurlex_gdpr", "fail"), _why(result)
        _assert_not_approved_checkpoint(ws, "eurlex_gdpr")
    else:
        assert result.exit_code == _LOAD_ERROR, _why(result)
        assert spy.constructed == [] and spy.runs == [], _why(result)
        assert _named_with(_output(result), "eurlex_gdpr", "approv"), _why(result)
        assert _is_empty(ws.state), "a load error left a checkpoint behind (round 4 H)"
    _assert_safe_output(result, str(ws.root), str(ws.root.resolve()))
    assert net.attempts == []


@pytest.mark.parametrize("key", ["placeholder", "missing-file"])
def test_fetch_of_unsigned_source_names_where_the_hash_comes_from(
    ws: _Workspace, spy: _Spy, net: _Net, key: str
) -> None:
    # Decision A: `fetch` of an unsigned source exits 7 with no network call, and the operator
    # is told where the hash to sign comes from: the cassette sidecar manifest (#96).
    _write_state(ws, "eurlex_gdpr", key)
    result = _invoke(ws.argv("fetch", "eurlex_gdpr"))
    assert result.exit_code == _NO_APPROVED, _why(result)
    _assert_not_approved_checkpoint(ws, "eurlex_gdpr")  # ruling D
    assert spy.constructed == [] and spy.runs == []
    assert any(
        "cassette" in line.lower() and "sha256" in line.lower() for line in _output(result).splitlines()
    ), _why(result)
    _assert_safe_output(result, str(ws.root), str(ws.root.resolve()))
    assert net.attempts == []


def test_refresh_continues_past_failed_sources_and_names_each(ws: _Workspace, spy: _Spy, net: _Net) -> None:
    # Mutant M12 (stop at the first failed source). Two signed sources fail, so whichever runs
    # first, a loop that stops there misses the other signed runs; every failure is named.
    failing = _TIER1[1:]
    for name in failing:
        spy.fail_with[name] = RuntimeError(f"{name} upstream down")
    result = _invoke(ws.argv("refresh", "--all"))
    assert result.exit_code == _SOURCE_FAILED, _why(result)
    assert sorted(spy.run_names()) == sorted(_TIER1), _why(result)
    for name in (*failing, *_unsigned()):
        assert _named_with(result.stderr, name, "fail"), (name, _why(result))
    assert _TIER1[0] in result.stdout and _TIER1[0] not in result.stderr, _why(result)
    _assert_safe_output(result, str(ws.root), str(ws.root.resolve()))
    assert net.attempts == []


# Round 4 G: where the checkpoint write fails -> (signed sources, failing stage, expected exit).
# "unsigned": the not_approved record of a partly signed run (a source failure, exit 8).
# "signed": the success checkpoint of a real run where every source is signed, so the write
# failure is the only failure and a silent path would exit 0.
# "all-unsigned": the not_approved record on the exit-7 path (decision B keeps exit 7 there).
_CHECKPOINT_WRITE_FAILS: dict[str, tuple[tuple[str, ...], str, str]] = {
    "unsigned": (_TIER1, "not_approved", "EXIT_SOURCE_FAILED"),
    "signed": (tuple(_repo_names()), STAGES[-1], "EXIT_SOURCE_FAILED"),
    "all-unsigned": ((), "not_approved", "EXIT_NO_APPROVED_SOURCES"),
}


@pytest.mark.parametrize("where", list(_CHECKPOINT_WRITE_FAILS))
def test_checkpoint_write_failure_is_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: _Net, where: str
) -> None:
    signed, failing_stage, exit_name = _CHECKPOINT_WRITE_FAILS[where]
    assert failing_stage in (_NOT_APPROVED_STAGE, *STAGES), failing_stage
    ws = _make_workspace(tmp_path, monkeypatch, signed=signed)
    net.upstream = ws  # the real pipeline: the Orchestrator writes its own checkpoints
    real_save = CheckpointStore.save
    refused: list[str] = []

    def failing_save(store: CheckpointStore, state: CheckpointState) -> None:
        if state.stage == failing_stage:
            refused.append(state.source_name)
            # Hostile strerror carrying a local path: never echoed (F8).
            raise OSError(errno.ENOSPC, f"disk full {ws.root}{_HOSTILE_TEXT}")
        real_save(store, state)

    monkeypatch.setattr(CheckpointStore, "save", failing_save)
    result = _invoke(ws.argv("refresh", "--all"))
    expected = sorted(n for n in _repo_names() if (n in signed) == (failing_stage != _NOT_APPROVED_STAGE))
    assert sorted(refused) == expected, (refused, _why(result))  # the injection hit every one
    assert result.exit_code == PUBLIC_EXIT_CODES[exit_name], _why(result)
    _assert_failed_loudly(result)
    lines = result.stderr.splitlines()
    for name in expected:
        assert _named_with(result.stderr, name, "fail"), (name, _why(result))
        if failing_stage == _NOT_APPROVED_STAGE:
            # The operator learns this source's state record is missing: a line of its own,
            # not only the closing summary that lists every failed source.
            others = [n for n in _repo_names() if n != name]
            own = [ln for ln in lines if name in ln and not any(o in ln for o in others)]
            assert any("checkpoint" in ln.lower() for ln in own), (name, _why(result))
        state = CheckpointStore(ws.state).load(name)
        assert state is None or state.stage != failing_stage, state
    # Message honesty: no line may claim the state dir records each failure when a write failed.
    claims = [ln for ln in lines if "record each failure" in ln.lower()]
    assert claims == [], (claims, _why(result))
    _assert_safe_output(result, str(ws.root), str(ws.root.resolve()))
    assert net.attempts == []


# ---------------------------------------------------------------------------
# Hostile config: a dotted class path must not import code from outside the package
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", ["fetcher", "cleaner", "chunker"])
def test_config_class_path_cannot_import_outside_the_package(
    ws: _Workspace, spy: _Spy, net: _Net, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    # The schema's dotted-path pattern admits any importable module ("pkg.mod.Class"). A
    # canary package on sys.path writes a marker when imported; the CLI must refuse it.
    package = f"lcicanary_{stage}"
    marker = ws.root / f"{package}.imported"
    pkg_dir = ws.root / "pkgs" / package
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "evil.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n"
        "class Evil:\n    pass\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(ws.root / "pkgs"))
    monkeypatch.delitem(sys.modules, package, raising=False)
    monkeypatch.delitem(sys.modules, f"{package}.evil", raising=False)

    data = yaml.safe_load((_REPO_SOURCES / "eurlex_gdpr.yaml").read_text(encoding="utf-8"))
    data["name"] = "eurlex_canary"
    data["pipeline"][stage] = f"{package}.evil.Evil"
    for path in ws.sources.glob("*.yaml"):
        path.unlink()
    (ws.sources / "eurlex_canary.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    assert list(load_all(ws.sources)) == ["eurlex_canary"]  # the schema admits it today
    ws.sign("eurlex_canary")

    result = _invoke(ws.argv("refresh", "--all"))
    assert not marker.exists(), "config imported a module from outside the package"
    assert "eurlex_canary" not in spy.run_names()
    _assert_failed_loudly(result)
    assert "eurlex_canary" in _output(result), _why(result)
    _assert_safe_output(result, str(ws.root), str(ws.root.resolve()))
    assert net.attempts == []


_PKG = "legal_corpus_ingester"
_STAGES = ("fetcher", "cleaner", "chunker")


def _stage_rows() -> list[Any]:
    """Wrong-stage and imported-not-defined class paths, built from the repo config (F13).

    Each stage's subpackage is the first part of its configured class path. Families:
    M13a, a real class from another stage's subpackage; M13b, the stage's own module and class
    under another stage's prefix (the module exists only in the stage's own subpackage); M14,
    a class a stage module only imports (its __module__ is elsewhere).
    """
    pipeline = yaml.safe_load((_REPO_SOURCES / "eurlex_gdpr.yaml").read_text(encoding="utf-8"))["pipeline"]
    configured = {stage: str(pipeline[stage]) for stage in _STAGES}
    subpackage = {stage: configured[stage].split(".")[0] for stage in _STAGES}
    rows: list[Any] = []
    for stage in _STAGES:
        _, module, cls = configured[stage].split(".")
        for other in _STAGES:
            if other == stage:
                continue
            rows.append(pytest.param(stage, configured[other], id=f"m13a-{stage}-{configured[other]}"))
            wrong_prefix = f"{subpackage[other]}.{module}.{cls}"
            rows.append(pytest.param(stage, wrong_prefix, id=f"m13b-{stage}-{wrong_prefix}"))
        package = importlib.import_module(f"{_PKG}.{subpackage[stage]}")
        for info in pkgutil.iter_modules(package.__path__):
            module_name = f"{_PKG}.{subpackage[stage]}.{info.name}"
            mod = importlib.import_module(module_name)
            for attr, value in sorted(vars(mod).items()):
                if (
                    isinstance(value, type)
                    and value.__module__ != module_name
                    and re.fullmatch(r"[A-Z][a-zA-Z0-9]+", attr)
                ):
                    dotted = f"{subpackage[stage]}.{info.name}.{attr}"
                    rows.append(pytest.param(stage, dotted, id=f"m14-{stage}-{dotted}"))
    return rows


_STAGE_ROWS = _stage_rows()


def test_stage_rows_cover_every_family_for_every_stage() -> None:
    # Contract: each stage has at least one row per mutant family, so no family is silently empty.
    for stage in _STAGES:
        families = {row.id.split("-")[0] for row in _STAGE_ROWS if row.values[0] == stage}
        assert {"m13a", "m13b", "m14"} <= families, (stage, families)


# Round 5 gap: "mixed" leaves one source unsigned; "none" unsigns every source, the path that
# exits 7 and checkpoints every source today. A load error outranks both (ruling 4): exit 1.
_SIGNED_MIXES = ("mixed", "none")


@pytest.mark.parametrize("signed", _SIGNED_MIXES)
@pytest.mark.parametrize(("stage", "dotted"), _STAGE_ROWS)
def test_config_class_path_must_name_a_class_defined_in_its_own_stage(
    ws_all: _Workspace, spy: _Spy, net: _Net, stage: str, dotted: str, signed: str
) -> None:
    # Mutants M13 (a class from another stage's subpackage) and M14 (a class only imported into
    # the named module). The schema admits each path; the CLI must refuse the run before any
    # Orchestrator is composed, naming the source, with exit 1 (StageClassError).
    ws = ws_all
    path = ws.sources / "eurlex_gdpr.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["pipeline"][stage] = dotted
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert load_all(ws.sources)["eurlex_gdpr"].pipeline.model_dump()[stage] == dotted
    # Round 4 H (mutant C10): with an unsigned source in the run, the stage-class load error
    # still exits 1 before any not_approved checkpoint is written (ruling 4).
    for name in _unsigned() if signed == "mixed" else _repo_names():
        ws.unsign(name)
    # Lead ruling 5: the dry run exits with the code the real run would, and writes nothing.
    dry = _invoke(ws.argv("refresh", "--all", "--dry-run"))
    assert dry.exit_code == _LOAD_ERROR, _why(dry)
    assert _is_empty(ws.state), "a dry run left a checkpoint behind"
    result = _invoke(ws.argv("refresh", "--all"))
    assert result.exit_code == _LOAD_ERROR, _why(result)
    assert spy.constructed == [] and spy.runs == [], _why(result)
    assert _is_empty(ws.state), f"a stage-class load error left a checkpoint behind ({signed} signed)"
    assert "eurlex_gdpr" in _output(result), _why(result)
    _assert_safe_output(result, str(ws.root), str(ws.root.resolve()))
    assert net.attempts == []


# ---------------------------------------------------------------------------
# F13: a bad Settings value fails closed at load, before anything runs
# ---------------------------------------------------------------------------

_BAD_SETTINGS: list[tuple[str, str]] = [
    ("localai_url", ""),
    ("localai_url", "   "),
    ("localai_url", "ftp://evilmarker.test/v1"),
    ("localai_url", "file:///etc/passwd"),
    ("localai_url", "evilmarker.test/v1"),
    ("localai_url", "http:///v1"),
    ("localai_url", "http://evilmarker.test/v1\r\nX-Injected: evilmarker"),
    ("localai_url", "http://evilmarker.test\u2028/v1"),
    ("localai_url", "http://evilmarker.test/v1\x00"),
    ("localai_url", "http://evilmarker.test/\ud800"),
    ("localai_model", ""),
    ("localai_model", "model\nX-Injected: evilmarker"),
    ("out_dir", ""),
    ("out_dir", "out\x00dir"),
    ("state_dir", ""),
    ("config_dir", ""),
]

_GOOD_SETTINGS: list[tuple[str, str]] = [
    ("localai_url", "https://localai.test/v1"),
    ("localai_url", "http://127.0.0.1:18080/v1"),
    ("localai_model", "embedder-under-test"),
    ("out_dir", "relative/out"),
    ("state_dir", "relative/state"),
    ("config_dir", "relative/config"),
]


def test_settings_tables_have_positive_and_negative_rows() -> None:
    bad = {name for name, _ in _BAD_SETTINGS}
    good = {name for name, _ in _GOOD_SETTINGS}
    assert bad == good, bad ^ good
    assert bad <= set(Settings.model_fields)


@pytest.mark.parametrize(("name", "value"), _GOOD_SETTINGS + [(n, "<default>") for n in ("localai_url",)])
def test_settings_accept_legitimate_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    # Positive control (green today): the defaults and ordinary values load.
    monkeypatch.chdir(tmp_path)
    kwargs = {} if value == "<default>" else {name: value}
    settings = Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]
    expected = _default(name) if value == "<default>" else value
    assert str(getattr(settings, name)) == str(expected)


@pytest.mark.parametrize(("name", "value"), _BAD_SETTINGS, ids=[f"{n}-{i}" for i, (n, _) in enumerate(_BAD_SETTINGS)])
def test_settings_reject_bad_values_at_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(pydantic.ValidationError):
        Settings(_env_file=None, **{name: value})  # type: ignore[call-arg]


# The rows an environment variable can carry (no NUL or lone surrogate in a POSIX environ).
_BAD_ENV = [(n, v) for n, v in _BAD_SETTINGS if "\x00" not in v and not any(0xD800 <= ord(c) <= 0xDFFF for c in v)]


@pytest.mark.parametrize(("name", "value"), _BAD_ENV, ids=[f"{n}-{i}" for i, (n, _) in enumerate(_BAD_ENV)])
def test_cli_with_bad_setting_fails_closed_before_running(
    ws: _Workspace, spy: _Spy, net: _Net, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(_env(name), value)
    result = _invoke(ws.argv("refresh", "--all"))
    assert result.exit_code not in (0, _NOT_WIRED, _USAGE), _why(result)
    assert isinstance(result.exception, SystemExit), _why(result)
    assert spy.constructed == [] and spy.runs == []
    assert net.attempts == []
    assert name in _output(result).lower(), _why(result)
    _assert_safe_output(result, str(ws.root))
    assert not any(line.startswith("X-Injected") for line in _output(result).splitlines())
    assert _is_empty(ws.cwd) and _is_empty(ws.out)


# ---------------------------------------------------------------------------
# Positive controls: behaviour that must not change (green today)
# ---------------------------------------------------------------------------


def test_tier1_partition_matches_the_repo_config() -> None:
    names = _repo_names()
    assert set(_TIER1) <= set(names), names
    assert len(_TIER1) == 3 and len(_unsigned()) == len(names) - 3
    # Ruling 1 rests on these being unsigned in the repo.
    for name in _unsigned():
        data = yaml.safe_load((_REPO_APPROVALS / f"{name}.yaml").read_text(encoding="utf-8"))
        assert data["signed_artifact_sha256"] == _PLACEHOLDER_SHA, name


def test_fetch_dry_run_stays_offline_and_writes_nothing(ws: _Workspace, spy: _Spy, net: _Net) -> None:
    result = _invoke(ws.argv("fetch", "eurlex_gdpr", "--dry-run"))
    assert result.exit_code == 0, _why(result)
    assert "eurlex_gdpr" in result.stdout
    assert spy.constructed == [] and spy.runs == []
    assert net.attempts == []
    assert _is_empty(ws.out) and _is_empty(ws.state) and _is_empty(ws.cwd)


def test_fetch_unknown_source_exits_one_without_running(ws: _Workspace, spy: _Spy, net: _Net) -> None:
    # The epilog: other command failures (unknown source) exit 1.
    result = _invoke(ws.argv("fetch", "eurlex_not_configured"))
    assert result.exit_code == PUBLIC_EXIT_CODES["EXIT_LOAD_ERROR"], _why(result)
    assert spy.constructed == [] and spy.runs == []
    assert net.attempts == []


def test_refresh_empty_sources_dir_still_exits_no_sources(ws: _Workspace, spy: _Spy, net: _Net) -> None:
    for path in ws.sources.glob("*.yaml"):
        path.unlink()
    result = _invoke(ws.argv("refresh", "--all"))
    assert result.exit_code == PUBLIC_EXIT_CODES["EXIT_NO_SOURCES"], _why(result)
    assert spy.constructed == [] and spy.runs == []
    assert net.attempts == []
