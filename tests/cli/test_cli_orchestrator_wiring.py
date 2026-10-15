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
Round 9: a run where every source succeeded yet none staged a corpus exits 1 naming them; the
spy Orchestrator stages through the publisher it is given, as the real one does. A current that
is not a relative link to a real version dir counts as missing (republish); bundle errors exit 1
with the exception type only and leave current as it was.
Round 10b (owner, option 2): hosted runners cache INGESTER_STATE_DIR but not out/, so out/current
never exists at run start. After each full publish the cli writes a producer version record
(`cli.VERSION_RECORD_FILE` under the state dir, not detect's file): the version and detect's
fingerprint. Identical content republishes under the recorded version (detect: unchanged, also
across a month boundary); changed content takes the next patch past both the record and the
calver dirs in out/, or `.0` in a new month. A missing record falls back; a bad one exits 1.

The card's `--out <tmp>` is replaced by the `Settings.out_dir` env var (ruling 3).
"""

from __future__ import annotations

import errno
import hashlib
import importlib
import importlib.util
import inspect
import io
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
import zipfile
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
from typer.main import get_command
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
from legal_corpus_ingester.fetchers import eurlex as eurlex_fetcher
from legal_corpus_ingester.fetchers.eurlex import CELEX_BASE_URL
from legal_corpus_ingester.pipeline.approval_gate import (
    _PLACEHOLDER_SHA,
    EXPIRY_WARNING_DAYS,
    check_approval,
    load_approval,
)
from legal_corpus_ingester.pipeline.manifest import MANIFEST_FILENAME, Manifest
from legal_corpus_ingester.pipeline.retention import next_version
from legal_corpus_ingester.pipeline.orchestrator import Orchestrator as RealOrchestrator
from legal_corpus_ingester.pipeline.state import STAGES, CheckpointState, CheckpointStore
from legal_corpus_ingester.provenance import license_audit
from legal_corpus_ingester.provenance.license_audit import DEFAULT_STATE_FILE as AUDIT_BASELINE_FILE
from legal_corpus_ingester.publishers.base import PublishReceipt
from legal_corpus_ingester.sources.registry import load_all
from legal_corpus_ingester.types import Chunk, Corpus, ProvenanceRecord
from tests.cli.helpers import PUBLIC_EXIT_CODES, strip_ansi

runner = CliRunner()

def _invoke(args: list[str]) -> Result:
    # color=True: click strips ANSI escapes when stdout is not a tty, which would hide an escape
    # the CLI echoes from an exception; a real terminal receives it (attack list A, F8).
    return runner.invoke(app, args, color=True)


_REPO = Path(__file__).resolve().parents[2]
_REPO_SOURCES = _REPO / "config" / "sources"
_REPO_APPROVALS = _REPO / "config" / "approvals"
# Ruling 1: Tier 1. Every other repo source stays unsigned (placeholder SHA) in these tests.
_TIER1 = ("eurlex_gdpr", "eurlex_ai_act", "eurlex_dsa")
_CELLAR_HOST = urlparse(CELEX_BASE_URL).hostname
_ENV_PREFIX = str(Settings.model_config["env_prefix"])

# Since #115 the eurlex fetcher returns Cellar Formex 4 zip bytes, but config/sources/eurlex_*.yaml
# still name AKNXMLCleaner, which raises XMLSyntaxError on them. The CLI records clean_failed and
# exits 8, so every test that needs a eurlex refresh to reach "done" fails on an assertion.
# Strict: once the Formex cleaner lands these XPASS and fail, which forces this mark's removal.
_FORMEX_XFAIL = pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="the eurlex fetcher now returns a Formex 4 zip; AKNXMLCleaner cannot read it until the "
    "Formex cleaner lands (jennifer-mckinney/terms-analysis#95)",
)


def _cellar_replays() -> dict[str, tuple[str, bytes]]:
    replays: dict[str, tuple[str, bytes]] = {}
    cassette_dir = _REPO / "tests" / "fixtures" / "cassettes" / "eurlex"
    for path in cassette_dir.glob("*_fetch.yaml"):
        interactions = yaml.safe_load(path.read_text(encoding="utf-8"))["interactions"]
        redirect, download = interactions
        celex = urlparse(redirect["request"]["uri"]).path.rsplit("/", 1)[-1]
        location = redirect["response"]["headers"]["location"][0]
        body = download["response"]["body"]["string"]
        assert isinstance(body, bytes), path
        replays[celex] = (location, body)
    return replays


_CELLAR_REPLAYS = _cellar_replays()


def _fixture_cellar_bodies() -> dict[str, bytes]:
    sources = load_all(_REPO_SOURCES)
    bodies = {celex: body for celex, (_location, body) in _CELLAR_REPLAYS.items()}
    dsa_body = bodies["32022R2065"]
    for name, config in sources.items():
        celex = config.pipeline.celex_id
        if celex in bodies:
            continue
        year, number = celex[1:5], celex[-4:]
        with zipfile.ZipFile(io.BytesIO(dsa_body)) as source:
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as target:
                for info in source.infolist():
                    content = source.read(info.filename)
                    content = content.replace(b"<YEAR>2022</YEAR>", f"<YEAR>{year}</YEAR>".encode())
                    content = content.replace(
                        b"<NO.CURRENT>2065</NO.CURRENT>",
                        f"<NO.CURRENT>{number}</NO.CURRENT>".encode(),
                    )
                    target.writestr(info, content)
        bodies[celex] = output.getvalue()
    return bodies


_CELLAR_BODIES = _fixture_cellar_bodies()


def _cellar_location(celex: str) -> str:
    replay = _CELLAR_REPLAYS.get(celex)
    if replay is not None:
        return replay[0]
    serial = sorted(_CELLAR_BODIES).index(celex) + 1
    return f"http://publications.europa.eu/resource/cellar/00000000-0000-0000-0000-{serial:012d}/zip"

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
_BASELINE_MISSING = PUBLIC_EXIT_CODES["EXIT_LICENSE_BASELINE_MISSING"]
# Lead decision B: the checkpoint stage a requested source without a valid approval records.
_NOT_APPROVED_STAGE = "not_approved"

_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def _refresh_baseline_name() -> str:
    # Round 6 P: a named constant beside DEFAULT_STATE_FILE in provenance.license_audit, so the
    # tests never restate the file name (F13). Missing today: that is the red.
    constant = getattr(license_audit, "REFRESH_BASELINE_FILE", None)
    assert constant is not None, "license_audit names no REFRESH_BASELINE_FILE constant (ruling P)"
    return Path(constant).name


# What the fixture's baseline file holds: valid JSON with no source entry (Round 11 item 3).
_SEEDED_BASELINE = "{}\n"


def _state_untouched(ws: _Workspace) -> bool:
    """The state dir holds exactly what the fixture put there: the seeded baseline, nothing else."""
    return _files(ws.state) == {_refresh_baseline_name(): _digest(_SEEDED_BASELINE.encode())}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _files(root: Path) -> dict[str, str]:
    """Every file and link under *root*: relative path -> content digest or link target.

    Directories are left out (Settings may create the configured dirs), so "nothing written"
    means no file, link or byte changed anywhere under the workspace.
    """
    found: dict[str, str] = {}
    if not root.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                found[rel] = "link -> " + os.readlink(path)
            elif path.is_file():
                found[rel] = _digest(path.read_bytes())
    return found


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
    source_bodies: dict[str, bytes] = field(default_factory=dict)

    @property
    def body_sha(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    def body_for_source(self, name: str) -> bytes:
        celex = load_all(self.sources)[name].pipeline.celex_id
        return self.source_bodies[celex]

    def approval(self, name: str, **overrides: Any) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source_id": name,
            "signed_artifact_sha256": hashlib.sha256(self.body_for_source(name)).hexdigest(),
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
        body=_CELLAR_BODIES["02016R0679-20160504"],
        today=date.today(),
        source_bodies=_CELLAR_BODIES,
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
    # Round 11 (rthoB): a missing refresh licence baseline fails closed unless the run seeds, so
    # every workspace starts as a restored cache does: the baseline file present. "{}" holds no
    # entry yet, so each source is still NEW and allowed (item 3); the Round 11 tests remove it.
    ws.state.mkdir()
    (ws.state / _refresh_baseline_name()).write_text(_SEEDED_BASELINE, encoding="utf-8")
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


# The chunker and embedder fields every spy-staged Manifest carries unless a test overrides one
# source's (the combined bundle has one index, so they must agree across sources).
_SPY_MODELS = ("chunker-under-test", "embedder-under-test", "revision-under-test")


@dataclass
class _Spy:
    constructed: list[inspect.BoundArguments] = field(default_factory=list)
    runs: list[tuple[str, Any]] = field(default_factory=list)
    fail_with: dict[str, BaseException] = field(default_factory=dict)
    # Round 9: a successful spy run stages a one-chunk corpus through the publisher it was given,
    # as the real Orchestrator does. *unstaged* names the sources whose run succeeds yet stages
    # nothing; *models* overrides one source's (chunker_version, embedder_model, embedder_revision).
    unstaged: set[str] = field(default_factory=set)
    models: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    # Sources whose staged text carries one more sentence: the bundle content changes.
    amended: set[str] = field(default_factory=set)

    def run_names(self) -> list[str]:
        return [name for name, _ in self.runs]

    def stage(self, bound: inspect.BoundArguments, source_name: str) -> PublishReceipt | None:
        if source_name in self.unstaged:
            return None
        args = _with_defaults(bound)
        chunker_version, embedder_model, embedder_revision = self.models.get(source_name, _SPY_MODELS)
        text = f"{source_name} text staged by the spy"
        if source_name in self.amended:
            text += " Amended under test."
        provenance = ProvenanceRecord(
            source_url=f"https://{_CELLAR_HOST}/spy/{source_name}",
            fetch_timestamp=_SPY_FETCHED_AT,
            license="CC-BY-4.0",
            upstream_version=None,
            content_sha256=hashlib.sha256(text.encode()).hexdigest(),
            source_name=source_name,
            fetcher_class="tests.cli.SpyFetcher",
        )
        chunk = Chunk(
            text=text,
            section="Article 1",
            metadata={"chunk_index": 0},
            provenance=provenance,
            offset_start=0,
            offset_end=len(text),
        )
        # One deterministic unit row per source, so two identical runs stage identical bytes.
        row = np.random.default_rng(int(provenance.content_sha256[:8], 16)).random((1, _SPY_DIM))
        embeddings = (row / np.linalg.norm(row, axis=1, keepdims=True)).astype(np.float32)
        corpus = Corpus(chunks=[chunk], embeddings=embeddings, corpus_files=[f"{source_name}.xml"])
        manifest = Manifest(
            corpus_version=args["corpus_version"],
            chunker_version=chunker_version,
            embedder_model=embedder_model,
            embedder_revision=embedder_revision,
            sources=[source_name],
            chunk_count=1,
        )
        receipt: PublishReceipt = args["publisher"].publish(corpus, args["publish_target"], manifest)
        return receipt


# The embedding width the real publisher and terms-analysis index expect.
_SPY_DIM = 1024
# One fixed fetch time, so two spy runs of the same source stage the same bytes.
_SPY_FETCHED_AT = "2026-01-01T00:00:00Z"


def _with_defaults(bound: inspect.BoundArguments) -> dict[str, Any]:
    # A copy with the constructor's defaults filled in: what the Orchestrator would actually use.
    full = inspect.BoundArguments(bound.signature, bound.arguments.copy())
    full.apply_defaults()
    return dict(full.arguments)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _Spy:
    record = _Spy()
    # Bind against the real constructor, so the CLI must call it the way the real class accepts.
    signature = inspect.signature(RealOrchestrator)

    class SpyOrchestrator:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.bound = signature.bind(*args, **kwargs)
            record.constructed.append(self.bound)

        def run(self, source_name: str, source_config: object) -> PublishReceipt:
            record.runs.append((source_name, source_config))
            if source_name in record.fail_with:
                raise record.fail_with[source_name]
            receipt = record.stage(self.bound, source_name)
            if receipt is not None:
                return receipt
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
    # CELEX ids Cellar answers 404 for: one source's fetch fails, the others are served.
    missing_celex: set[str] = field(default_factory=set)
    # CELEX id -> ZIP body served instead of the workspace body (one source's text changed).
    bodies: dict[str, bytes] = field(default_factory=dict)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        ws = self.upstream
        if ws is not None:
            if url.host == _CELLAR_HOST and request.method == "GET":
                self.served.append(request)
                if url.path.startswith("/resource/celex/"):
                    celex = url.path.rsplit("/", 1)[-1]
                    if celex in self.missing_celex:
                        return httpx.Response(404, request=request)
                    if celex not in ws.source_bodies:
                        return httpx.Response(404, request=request)
                    return httpx.Response(
                        303, headers={"Location": _cellar_location(celex)}, request=request
                    )
                if url.path.startswith("/resource/cellar/") and url.path.endswith("/zip"):
                    celex = next(
                        (
                            name
                            for name in ws.source_bodies
                            if urlparse(_cellar_location(name)).path == url.path
                        ),
                        None,
                    )
                    if celex is not None:
                        return httpx.Response(
                            200,
                            headers={"Content-Type": "application/zip"},
                            content=self.bodies.get(celex, ws.source_bodies[celex]),
                            request=request,
                        )
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
            r.url.path.rsplit("/", 1)[-1]
            for r in self.served
            if r.url.host == _CELLAR_HOST and r.url.path.startswith("/resource/celex/")
        }

    def embed_requests(self) -> list[httpx.Request]:
        return [r for r in self.served if r.url.host != _CELLAR_HOST]


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
    assert _is_empty(ws.out) and _state_untouched(ws) and _is_empty(ws.cwd)


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
    assert _is_empty(ws.out) and _state_untouched(ws) and _is_empty(ws.cwd)
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
    # Round 9 M8: every Orchestrator gets the run's one version, the one the bundle is published
    # under, so checkpoints and staged manifests never name a default or a different version.
    published = _assert_current_bundle(ws, _repo_names())
    versions = {_with_defaults(bound)["corpus_version"] for bound in spy.constructed}
    assert versions == {published}, (versions, published)
    assert _is_empty(ws.cwd)


@_FORMEX_XFAIL
def test_fetch_end_to_end_uses_settings_for_localai_and_dirs(ws: _Workspace, net: _Net) -> None:
    # The real Orchestrator behind a fake transport: Cellar Formex and LocalAI are served only
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


@_FORMEX_XFAIL
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


@_FORMEX_XFAIL
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


def _record_snapshot(ws: _Workspace) -> bytes | None:
    # Round 10b item 7, for the tests that predate the record: the producer version record's
    # bytes, or None while there is none (or no constant names it yet). A failed run must leave
    # it byte-for-byte as it was.
    constant = getattr(cli, "VERSION_RECORD_FILE", None)
    if constant is None:
        return None
    path = ws.state / Path(constant)
    return path.read_bytes() if path.is_file() else None


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


@_FORMEX_XFAIL
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
    # Round 9 M8, with the real Orchestrator: each source's last checkpoint names the version
    # the run published (health and status read these), never the Orchestrator's default.
    for name in _repo_names():
        checkpoint = CheckpointStore(ws.state).load(name)
        assert checkpoint is not None, name
        assert checkpoint.corpus_version == version, (name, checkpoint.corpus_version, version)
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


@_FORMEX_XFAIL
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
    record = _record_snapshot(ws)

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
    # Round 10b item 7: a run that published nothing leaves the producer version record as it was.
    assert _record_snapshot(ws) == record, "a failed run rewrote the version record"
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


@_FORMEX_XFAIL
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
    # Change one Formex paragraph while keeping a valid ZIP for the Cellar validator.
    output = io.BytesIO()
    changed = False
    with zipfile.ZipFile(io.BytesIO(body)) as source:
        with zipfile.ZipFile(output, "w") as target:
            for info in source.infolist():
                content = source.read(info.filename)
                if (
                    not changed
                    and not info.filename.endswith(".doc.xml")
                    and ".toc." not in info.filename
                ):
                    amended = content.replace(b"</P>", b" Amended under test.</P>", 1)
                    changed = amended != content
                    content = amended
                target.writestr(info, content)
    assert changed, "the fixture has no Formex paragraph to change"
    return output.getvalue()


@_FORMEX_XFAIL
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
    changed = _changed_body(ws.body_for_source(victim))
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


@_FORMEX_XFAIL
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
# Round 9 (2026-10-13). MEDIUM: a run where every source succeeded yet none staged a corpus
# fails closed (exit 1, naming the sources), never a green run that published nothing. And the
# coder's surviving mutants: M5 (any current target accepted), M8 (the run version not passed
# to the Orchestrator), M10 (a missing current read as "unchanged"), plus the bundle error paths.
# ---------------------------------------------------------------------------


def _spy_first_run(ws: _Workspace, spy: _Spy) -> str:
    """A clean spy run that publishes; returns the version out/current points at."""
    first = _invoke(ws.argv("refresh", "--all"))
    assert first.exit_code == 0, _why(first)
    assert spy.run_names() == _repo_names(), _why(first)
    return _assert_current_bundle(ws, _repo_names())


@pytest.mark.parametrize("before", ["current-exists", "no-current"])
def test_refresh_where_no_source_staged_fails_closed_naming_them(
    ws_all: _Workspace, spy: _Spy, net: _Net, before: str
) -> None:
    # Every requested source ran without error and none staged a corpus. Exit 0 here would be
    # indistinguishable from "content unchanged" (detect: changed=false): the silent success
    # the owner rules highest-risk. Ruling: exit 1, a message naming the sources, out/ as it was.
    ws = ws_all
    if before == "current-exists":
        _spy_first_run(ws, spy)
        spy.runs.clear()
    state = _published_state(ws.out) if ws.out.is_dir() else {}
    record = _record_snapshot(ws)
    spy.unstaged = set(_repo_names())
    result = _invoke(ws.argv("refresh", "--all"))
    assert result.exit_code == 1, _why(result)
    assert spy.run_names() == _repo_names(), _why(result)
    for name in _repo_names():
        assert name in result.stderr, (name, _why(result))
    assert (_published_state(ws.out) if ws.out.is_dir() else {}) == state
    assert _record_snapshot(ws) == record, "a run that staged nothing wrote the version record"
    _assert_safe_output(result, str(ws.root), str(ws.root.resolve()))
    assert net.attempts == []
    assert _is_empty(ws.cwd)


def _repoint(ws: _Workspace, target: str) -> None:
    link = ws.out / _CURRENT
    link.unlink()
    link.symlink_to(target)


def _other_month_calver(ws: _Workspace) -> str:
    # A well-formed version name the run's own next_version never picks (another year).
    return f"{ws.today.year - 1}.{ws.today.month}.0"


def _current_missing(ws: _Workspace, v1: str) -> None:
    (ws.out / _CURRENT).unlink()


def _current_to_non_calver_copy(ws: _Workspace, v1: str) -> None:
    shutil.copytree(ws.out / v1, ws.out / "latest", symlinks=True)
    _repoint(ws, "latest")


def _current_to_calver_file(ws: _Workspace, v1: str) -> None:
    (ws.out / _other_month_calver(ws)).write_bytes(b"not a bundle\n")
    _repoint(ws, _other_month_calver(ws))


def _current_to_calver_symlink(ws: _Workspace, v1: str) -> None:
    # A version-named link to the real bundle: the name passes, but it is no bundle dir.
    (ws.out / _other_month_calver(ws)).symlink_to(v1)
    _repoint(ws, _other_month_calver(ws))


def _symlink_inside_bundle(ws: _Workspace, v1: str) -> None:
    # One corpus file swapped for a link to an identical copy outside out/: the bytes a reader
    # gets are the same, but the bundle no longer holds its own content.
    victim = sorted(p for p in (ws.out / v1 / "corpus").rglob("*") if p.is_file())[0]
    outside = ws.root / "outside-copy.txt"
    shutil.copyfile(victim, outside)
    victim.unlink()
    victim.symlink_to(outside)


# How out/ is left between two runs whose staged content is identical -> what run 2 must do.
# "unchanged": publish nothing (4a). "republish": a new, higher version and current -> it.
_CURRENT_BETWEEN_RUNS: dict[str, tuple[Callable[[_Workspace, str], None], str]] = {
    "valid-current": (lambda ws, v1: None, "unchanged"),
    "missing (M10, ruling 8c)": (_current_missing, "republish"),
    "non-calver-copy (M5)": (_current_to_non_calver_copy, "republish"),
    "dot-slash (M5)": (lambda ws, v1: _repoint(ws, f"./{v1}"), "republish"),
    "trailing-slash (M5)": (lambda ws, v1: _repoint(ws, f"{v1}/"), "republish"),
    "parent-hop (M5)": (lambda ws, v1: _repoint(ws, f"../{ws.out.name}/{v1}"), "republish"),
    "calver-file": (_current_to_calver_file, "republish"),
    "calver-symlink": (_current_to_calver_symlink, "republish"),
    "symlink-inside-bundle": (_symlink_inside_bundle, "republish"),
}


def test_current_between_runs_table_has_both_outcomes() -> None:
    # Contract: the positive control (a valid current, nothing published) sits beside the
    # republish rows, so a run that always or never publishes fails at least one row.
    outcomes = [outcome for _, outcome in _CURRENT_BETWEEN_RUNS.values()]
    assert set(outcomes) == {"unchanged", "republish"}, outcomes
    assert outcomes.count("unchanged") == 1


@pytest.mark.parametrize("between", list(_CURRENT_BETWEEN_RUNS))
def test_identical_refresh_republishes_unless_current_is_a_real_bundle(
    ws_all: _Workspace, spy: _Spy, net: _Net, between: str
) -> None:
    # Round 9 M5 and M10, and the cli.py:517 and :534 paths. Only a relative link whose target
    # is one version name naming a real, symlink-free bundle dir is compared against; any other
    # current is treated as missing, so run 2 repoints current at a real bundle (Round 10b: the
    # recorded v1 if intact, else a fresh version).
    ws = ws_all
    v1 = _spy_first_run(ws, spy)
    setup, outcome = _CURRENT_BETWEEN_RUNS[between]
    setup(ws, v1)
    kept = {name: _tree_digest(ws.out / name) for name in _bundle_versions(ws.out)}
    state = _published_state(ws.out)
    outside = {p: p.read_bytes() for p in ws.root.glob("outside-*")}

    second = _invoke(ws.argv("refresh", "--all"))
    assert second.exit_code == 0, _why(second)
    if outcome == "unchanged":
        assert _published_state(ws.out) == state, (sorted(state), sorted(_published_state(ws.out)))
        return
    v2 = _assert_current_bundle(ws, _repo_names())
    # Round 10b item 3: run 1's producer record names v1 with this same content, so current may
    # end on v1 itself (no alert) when that bundle is intact and link-free; otherwise run 2
    # takes a new, higher version. It never points at a tampered bundle or rewrites one.
    if v2 in kept:
        assert v2 == v1, f"run 2 pointed current at {v2}, not the recorded {v1}"
    else:
        assert _calver_key(v2) > _calver_key(v1), (v1, v2)
    assert not any(p.is_symlink() for p in (ws.out / v2).rglob("*")), "current's bundle holds a link"
    assert {name: _tree_digest(ws.out / name) for name in kept} == kept, "run 2 rewrote an old bundle"
    assert {p: p.read_bytes() for p in outside} == outside, "run 2 wrote through a planted link"
    detect, outputs = _bundle_script(ws, "detect")
    assert detect.returncode == 0, detect.stderr
    assert outputs["new_target"] == v2, outputs
    _assert_safe_output(second, str(ws.root), str(ws.root.resolve()))
    assert net.attempts == []


def _mismatch(field_index: int) -> Callable[[_Workspace, _Spy, str], None]:
    def setup(ws: _Workspace, spy: _Spy, v1: str) -> None:
        models = list(_SPY_MODELS)
        # A forged workflow command in the odd value: it must never reach the output.
        models[field_index] = f"{models[field_index]}-other\n::error::forged"
        spy.models[_repo_names()[-1]] = (models[0], models[1], models[2])
        spy.amended = set(_repo_names())

    return setup


def _unstaged(index: int) -> Callable[[_Workspace, _Spy, str], None]:
    def setup(ws: _Workspace, spy: _Spy, v1: str) -> None:
        spy.unstaged = {_repo_names()[index]}
        spy.amended = set(_repo_names())

    return setup


def _file_at_next_version(ws: _Workspace, spy: _Spy, v1: str) -> None:
    # A stray regular file holds the name this run would publish under, and current points at
    # it: not a bundle (cli.py:517), and the bundle write cannot create the dir. Round 10b: the
    # content changes, so the run must take a new version (identical content may now keep v1).
    spy.amended = set(_repo_names())
    stray = next_version(ws.out, ws.today)
    (ws.out / stray).write_bytes(b"not a bundle\n")
    _repoint(ws, stray)


def _out_read_only(ws: _Workspace, spy: _Spy, v1: str) -> None:
    spy.amended = set(_repo_names())
    ws.out.chmod(0o555)


def _root_copy_blocked(ws: _Workspace, spy: _Spy, v1: str) -> None:
    # The hot-reload copy at the out/ root cannot be replaced (a dir holding a dir of the same
    # name sits there). The run must fail before it repoints current, not after.
    spy.amended = set(_repo_names())
    root_copy = ws.out / "legal_kb.npy"
    root_copy.unlink()
    (root_copy / root_copy.name).mkdir(parents=True)


# Round 9 error paths -> (setup, the exception type the run names). Every row: exit 1, the
# type on stderr, no raw message or path, out/current and every published byte as they were.
_BUNDLE_ERRORS: dict[str, tuple[Callable[[_Workspace, _Spy, str], None], type[BaseException]]] = {
    "chunker-mismatch": (_mismatch(0), ValueError),
    "embedder-mismatch": (_mismatch(1), ValueError),
    "revision-mismatch": (_mismatch(2), ValueError),
    "partial-staging-first": (_unstaged(0), ValueError),
    "partial-staging-last": (_unstaged(-1), ValueError),
    "current-to-file-at-next-version": (_file_at_next_version, FileExistsError),
    "out-read-only": (_out_read_only, PermissionError),
    "root-copy-blocked": (_root_copy_blocked, IsADirectoryError),
}


@pytest.mark.parametrize("case", list(_BUNDLE_ERRORS))
def test_bundle_error_exits_one_with_the_type_only_and_keeps_current(
    ws_all: _Workspace, spy: _Spy, net: _Net, case: str
) -> None:
    ws = ws_all
    if case == "out-read-only" and os.geteuid() == 0:
        pytest.fail("root ignores the read-only out/ dir; run the suite as a normal user")
    v1 = _spy_first_run(ws, spy)
    setup, error = _BUNDLE_ERRORS[case]
    try:
        setup(ws, spy, v1)
        state = _published_state(ws.out)
        record = _record_snapshot(ws)
        result = _invoke(ws.argv("refresh", "--all"))
    finally:
        ws.out.chmod(0o755)
    assert result.exit_code == 1, _why(result)
    assert error.__name__ in result.stderr, _why(result)
    assert "Traceback" not in _output(result) and "forged" not in _output(result), _why(result)
    # current, every bundle that was there and the root copies are as they were. A new version
    # dir left behind is not pinned: nothing points at it and the next run numbers past it.
    after = _published_state(ws.out)
    assert {key: after.get(key) for key in state} == state, (sorted(state), sorted(after))
    # Round 10b item 7: the record is written only after the bundle is fully published, so a
    # publish that failed (even after writing its version dir) leaves the old record.
    assert _record_snapshot(ws) == record, "a failed publish rewrote the version record"
    _assert_safe_output(result, str(ws.root), str(ws.root.resolve()))
    assert net.attempts == []


# ---------------------------------------------------------------------------
# Round 10b (owner, 2026-10-13, option 2; grumpy MEDIUM at cli.py:566). refresh.yml caches
# INGESTER_STATE_DIR but not out/, so on a hosted runner out/current never exists at run start.
# The cli keeps a producer version record under the state dir: identical content republishes
# under the recorded version (detect: unchanged), changed content never reuses a version name.
# Every "emptied out/" step below is that runner: out/ gone, the state dir (and detect's own
# record, refresh.yml's record step) kept.
# ---------------------------------------------------------------------------


def _record_path(ws: _Workspace) -> Path:
    """Item 1: the record's place comes from a cli constant, under Settings.state_dir (F13)."""
    constant = getattr(cli, "VERSION_RECORD_FILE", None)
    assert constant is not None, "cli names no VERSION_RECORD_FILE constant (Round 10b item 1)"
    relative = Path(constant)
    assert not relative.is_absolute() and ".." not in relative.parts, constant
    return ws.state / relative


def _record_values(ws: _Workspace) -> set[str]:
    path = _record_path(ws)
    assert path.is_file() and not path.is_symlink(), "refresh wrote no version record in the state dir"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), type(data).__name__
    return {value for value in data.values() if isinstance(value, str)}


def _assert_record_names_current(ws: _Workspace, version: str) -> None:
    # Items 1 and 2: the record holds the version out/current names and detect's own fingerprint
    # of that bundle (computed by detect_bundle_change.read_bundle, not by a test copy).
    linked, fingerprint = _DETECT.read_bundle(ws.out / _CURRENT)
    assert linked == version, (linked, version)
    values = _record_values(ws)
    assert version in values, f"the record does not name {version}"
    assert fingerprint in values, "the record's digest is not detect's fingerprint of the bundle"


def _set_today(ws: _Workspace, monkeypatch: pytest.MonkeyPatch, day: date) -> None:
    # refresh reads the run date once, via cli's `date.today()`; inject it there. The approvals
    # are re-signed for that day so the gate stays green (only the version math is under test).
    class _InjectedDate(date):
        @classmethod
        def today(cls) -> date:
            return day

    monkeypatch.setattr(cli, "date", _InjectedDate)
    ws.today = day
    for name in _repo_names():
        ws.sign(name)


def _calver(ws: _Workspace, day: date, patch: int) -> str:
    # The version name for *day* and *patch*, in retention's own format (no restated pattern).
    month = next_version(ws.root / "never-created", day).rpartition(".")[0]
    return f"{month}.{patch}"


def _first_of_next_month(day: date) -> date:
    return (day.replace(day=1) + timedelta(days=32)).replace(day=1)


def _empty_out(ws: _Workspace) -> None:
    # The hosted runner: out/ is not in the actions/cache path, the state dir is.
    shutil.rmtree(ws.out)


@dataclass(frozen=True)
class _RecordCase:
    # Runs with out/ kept between them (amended source indexes per run), then out/ emptied.
    kept_runs: tuple[frozenset[int], ...]
    # Patches planted as real calver dirs (a copy of run 1's bundle) in the final run's month.
    plant: tuple[int, ...] = ()
    # "same" month, "next-month", or "next-year" (run 1 on 15 Dec, the final run on 1 Jan).
    when: str = "same"
    # "identical" to the last kept run's content, or "changed" (every source amended).
    final: str = "identical"
    drop_record: bool = False
    # "recorded" (the last kept run's version) or the final run's patch in its own month.
    expect: str | int = "recorded"


_NONE: frozenset[int] = frozenset()
_FIRST: frozenset[int] = frozenset({0})

_RECORD_CASES: dict[str, _RecordCase] = {
    "identical-emptied-out (item 3)": _RecordCase((_NONE,)),
    "changed-emptied-out (item 4)": _RecordCase((_NONE,), final="changed", expect=1),
    "identical-after-a-change (item 3)": _RecordCase((_NONE, _FIRST)),
    "changed-after-a-change (item 4)": _RecordCase((_NONE, _FIRST), final="changed", expect=2),
    "out-dir-above-record-changed (item 4)": _RecordCase((_NONE,), plant=(5,), final="changed", expect=6),
    "out-dir-above-record-identical (item 3)": _RecordCase((_NONE,), plant=(5,)),
    "record-above-out-dir-changed (item 4)": _RecordCase((_NONE, _FIRST), plant=(0,), final="changed", expect=2),
    "next-month-identical (item 5)": _RecordCase((_NONE,), when="next-month"),
    "next-month-changed (item 4)": _RecordCase((_NONE, _FIRST), when="next-month", final="changed", expect=0),
    "next-year-identical (item 5)": _RecordCase((_NONE,), when="next-year"),
    "next-year-changed (item 4)": _RecordCase((_NONE,), when="next-year", final="changed", expect=0),
    "record-missing-identical (item 6, control)": _RecordCase((_NONE,), drop_record=True, expect=0),
    "record-missing-changed (item 6, control)": _RecordCase(
        (_NONE,), drop_record=True, final="changed", expect=0
    ),
}


def test_record_case_table_covers_every_axis() -> None:
    # Contract: both contents, all three date steps, both "larger wins" directions, the
    # fallback controls, and both outcomes; a rule that ignores any one axis fails a row.
    cases = list(_RECORD_CASES.values())
    assert {c.final for c in cases} == {"identical", "changed"}
    assert {c.when for c in cases} == {"same", "next-month", "next-year"}
    for when in ("next-month", "next-year"):
        assert {c.final for c in cases if c.when == when} == {"identical", "changed"}, when
    planted = [c for c in cases if c.plant]
    assert any(max(c.plant) >= len(c.kept_runs) for c in planted), "no out/ dir above the record"
    assert any(max(c.plant) < len(c.kept_runs) - 1 for c in planted), "no record above out/ dirs"
    assert {c.final for c in cases if c.drop_record} == {"identical", "changed"}
    assert {isinstance(c.expect, int) for c in cases} == {True, False}


@pytest.mark.parametrize("case", list(_RECORD_CASES))
def test_version_after_an_emptied_out_dir_follows_the_record(
    ws_all: _Workspace, spy: _Spy, net: _Net, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    ws = ws_all
    names = _repo_names()
    assert len(names) >= 2, names
    row = _RECORD_CASES[case]
    start = date(ws.today.year, 12, 15) if row.when == "next-year" else ws.today
    final_day = {
        "same": start,
        "next-month": _first_of_next_month(start),
        "next-year": date(start.year + 1, 1, 1),
    }[row.when]
    _set_today(ws, monkeypatch, start)
    saved = ws.root / "saved-bundle"
    recorded = ""
    for amended in row.kept_runs:
        spy.amended = {names[i] for i in amended}
        run = _invoke(ws.argv("refresh", "--all"))
        assert run.exit_code == 0, _why(run)
        recorded = _assert_current_bundle(ws, names)
        if not saved.exists():
            shutil.copytree(ws.out / recorded, saved, symlinks=True)
        seed, _ = _bundle_script(ws, "record")
        assert seed.returncode == 0, seed.stderr

    _empty_out(ws)
    if row.drop_record:
        _record_path(ws).unlink()
    ws.out.mkdir()
    planted = {}
    for patch in row.plant:
        name = _calver(ws, final_day, patch)
        shutil.copytree(saved, ws.out / name, symlinks=True)
        planted[name] = _tree_digest(ws.out / name)
    _set_today(ws, monkeypatch, final_day)
    if row.final == "changed":
        spy.amended = set(names)

    result = _invoke(ws.argv("refresh", "--all"))
    assert result.exit_code == 0, _why(result)
    version = _assert_current_bundle(ws, names)
    expected = recorded if row.expect == "recorded" else _calver(ws, final_day, int(row.expect))
    assert version == expected, f"published {version}; the record and out/ call for {expected}"
    assert {name: _tree_digest(ws.out / name) for name in planted} == planted, "a planted dir changed"
    _assert_record_names_current(ws, version)
    # refresh.yml's detect step against its own record of the last announced bundle: an alert
    # only for new content (a missing producer record may still reuse a name: the known fallback).
    detect, outputs = _bundle_script(ws, "detect", "--require-state")
    assert detect.returncode == 0, detect.stderr
    changed = row.final == "changed" or version != recorded
    assert outputs == {"changed": str(changed).lower(), "new_target": version}, outputs
    _assert_safe_output(result, str(ws.root), str(ws.root.resolve()))
    assert net.attempts == []
    assert _is_empty(ws.cwd)


@_FORMEX_XFAIL
def test_weekly_runs_with_an_uncached_out_dir_alert_only_on_new_content(
    ws_all: _Workspace, net: _Net
) -> None:
    # The CI case end to end, with the real Orchestrator and publisher and detect in its own
    # process: four weekly runs in one month, out/ emptied before each, the state dir kept.
    # Today every run restarts at .0, so week 3's new content reuses week 1's name.
    ws = ws_all
    net.upstream = ws
    names = _repo_names()
    changed = _changed_body(ws.body_for_source(names[0]))
    weeks = [("identical", "true"), ("identical", "false"), ("changed", "true"), ("identical", "false")]
    versions: list[str] = []
    for week, (content, alert) in enumerate(weeks):
        if week:
            _empty_out(ws)
        if content == "changed":
            net.bodies[load_all(ws.sources)[names[0]].pipeline.celex_id] = changed
            ws.sign(names[0], signed_artifact_sha256=hashlib.sha256(changed).hexdigest())
        result = _invoke(ws.argv("refresh", "--all"))
        assert result.exit_code == 0, (week, _why(result))
        version = _assert_current_bundle(ws, names)
        _assert_record_names_current(ws, version)
        detect, outputs = _bundle_script(ws, "detect", *(["--require-state"] if week else []))
        assert detect.returncode == 0, (week, detect.stderr)
        assert outputs == {"changed": alert, "new_target": version}, (week, outputs, versions)
        if alert == "true":
            assert version not in versions, f"week {week} reused {version} for new content"
            record, _ = _bundle_script(ws, "record")
            assert record.returncode == 0, record.stderr
        versions.append(version)
    assert versions[1] == versions[0] and versions[3] == versions[2], versions
    assert _calver_key(versions[2]) > _calver_key(versions[0]), versions
    assert net.attempts == [], net.attempts
    assert _is_empty(ws.cwd)


def test_version_record_is_its_own_file_under_the_state_dir(
    ws_all: _Workspace, spy: _Spy, net: _Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Item 1: the producer record is not detect's last-published-bundle.json (refresh.yml's
    # record step owns that), nor a baseline or checkpoint, and refresh never writes detect's
    # default location under HOME / XDG_STATE_HOME.
    ws = ws_all
    home = ws.root / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "xdg-state"))
    path = _record_path(ws)
    others = {
        _DETECT.default_state_file().name,
        _refresh_baseline_name(),
        Path(AUDIT_BASELINE_FILE).name,
        *(f"{name}.checkpoint.json" for name in _repo_names()),
    }
    assert path.name not in others, path.name
    version = _spy_first_run(ws, spy)
    _assert_record_names_current(ws, version)
    assert _is_empty(home), sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    assert not _DETECT.default_state_file().exists()


@pytest.mark.parametrize("argv", [("fetch", "eurlex_gdpr"), ("refresh", "--all", "--dry-run")], ids=["fetch", "dry-run"])
def test_runs_that_publish_no_bundle_write_no_record(
    ws_all: _Workspace, spy: _Spy, net: _Net, argv: tuple[str, ...]
) -> None:
    # Item 7's other side: no bundle published, so no record (positive control: the first test
    # above shows a publishing run does write one).
    ws = ws_all
    result = _invoke(ws.argv(*argv))
    assert result.exit_code == 0, _why(result)
    path = _record_path(ws)
    assert not os.path.lexists(path), f"{argv[0]} wrote a version record without a bundle"


def _swap(old: str, new: str) -> Callable[[Path, bytes, str, str], None]:
    # Replace one JSON string value in the record as written (the version or the digest).
    def mutate(path: Path, valid: bytes, version: str, fingerprint: str) -> None:
        source = {"version": version, "digest": fingerprint}[old]
        token = json.dumps(source).encode()
        assert valid.count(token) == 1, f"the record does not hold the {old} once as JSON"
        path.write_bytes(valid.replace(token, new.encode()))

    return mutate


def _bytes(data: Callable[[bytes], bytes]) -> Callable[[Path, bytes, str, str], None]:
    def mutate(path: Path, valid: bytes, version: str, fingerprint: str) -> None:
        path.write_bytes(data(valid))

    return mutate


def _record_is_dir(path: Path, valid: bytes, version: str, fingerprint: str) -> None:
    path.unlink()
    path.mkdir()


def _record_unreadable(path: Path, valid: bytes, version: str, fingerprint: str) -> None:
    path.chmod(0o000)


def _record_dangling(path: Path, valid: bytes, version: str, fingerprint: str) -> None:
    path.unlink()
    path.symlink_to(path.parent / "no-such-record")


def _record_linked_copy(path: Path, valid: bytes, version: str, fingerprint: str) -> None:
    # A valid record, but reached through a link out of the state dir: indirection, refused.
    outside = path.parent.parent / "outside-record"
    outside.write_bytes(valid)
    path.unlink()
    path.symlink_to(outside)


_FULLWIDTH = str.maketrans("0123456789", "".join(chr(0xFF10 + d) for d in range(10)))
_VERSION_FORGERIES = {
    "version-parent-hop": lambda v: json.dumps("../" + v),
    "version-forged-line": lambda v: json.dumps(v + "\n::error::forged"),
    "version-line-separator": lambda v: json.dumps(v + " "),
    "version-trailing-newline": lambda v: json.dumps(v + "\n"),
    "version-trailing-slash": lambda v: json.dumps(v + "/"),
    "version-not-calver": lambda v: json.dumps("latest"),
    # \d in a str pattern matches these; detect's [0-9] does not.
    "version-fullwidth-digits": lambda v: json.dumps(v.translate(_FULLWIDTH), ensure_ascii=False),
    "version-lone-surrogate": lambda v: json.dumps(v + "\ud800"),
    "version-number": lambda v: json.dumps(int(v.replace(".", ""))),
}
_DIGEST_FORGERIES = {
    "digest-not-hex": lambda f: json.dumps("not-a-digest\n::error::forged"),
    "digest-short": lambda f: json.dumps(f[:-1]),
    "digest-number": lambda f: "0",
}


def _forged(kind: str, make: Callable[[str], str]) -> Callable[[Path, bytes, str, str], None]:
    def mutate(path: Path, valid: bytes, version: str, fingerprint: str) -> None:
        value = make(version if kind == "version" else fingerprint)
        _swap(kind, value)(path, valid, version, fingerprint)

    return mutate


# How the record is left before an identical-content run -> the type the run names, or None
# for the positive control (the record as the cli wrote it: exit 0).
_BAD_RECORDS: dict[str, tuple[Callable[[Path, bytes, str, str], None], str | None]] = {
    "as-written (control)": (lambda path, valid, version, fingerprint: None, None),
    "empty": (_bytes(lambda valid: b""), ""),
    "truncated": (_bytes(lambda valid: valid[: len(valid) // 2]), ""),
    "not-json": (_bytes(lambda valid: b"version 2026.1.0\n"), ""),
    "json-array": (_bytes(lambda valid: b"[]\n"), ""),
    "json-null": (_bytes(lambda valid: b"null\n"), ""),
    "no-fields": (_bytes(lambda valid: b"{}\n"), ""),
    "invalid-utf8": (_bytes(lambda valid: b"\xff\xfe" + valid), ""),
    "nul-prefixed": (_bytes(lambda valid: b"\x00" + valid), ""),
    **{name: (_forged("version", make), "") for name, make in _VERSION_FORGERIES.items()},
    **{name: (_forged("digest", make), "") for name, make in _DIGEST_FORGERIES.items()},
    "record-is-a-dir": (_record_is_dir, "IsADirectoryError"),
    "record-unreadable": (_record_unreadable, "PermissionError"),
    "dangling-symlink": (_record_dangling, ""),
    "symlink-to-a-valid-copy": (_record_linked_copy, ""),
}


def test_bad_record_table_has_a_control_and_every_hostile_class() -> None:
    outcomes = [error for _, error in _BAD_RECORDS.values()]
    assert outcomes.count(None) == 1, "exactly one positive control"
    names = set(_BAD_RECORDS)
    for needed in ("truncated", "invalid-utf8", "nul-prefixed", "version-forged-line",
                   "version-line-separator", "version-fullwidth-digits", "version-lone-surrogate",
                   "version-parent-hop", "digest-not-hex", "record-is-a-dir", "record-unreadable",
                   "dangling-symlink", "symlink-to-a-valid-copy"):
        assert needed in names, needed


_TYPE_TOKEN = re.compile(r"\b[A-Z][A-Za-z]*(?:Error|Exception)\b")


@pytest.mark.parametrize("out_dir", ["out-emptied", "out-kept"])
@pytest.mark.parametrize("case", list(_BAD_RECORDS))
def test_bad_version_record_fails_closed_with_the_type_only(
    ws_all: _Workspace, spy: _Spy, net: _Net, case: str, out_dir: str
) -> None:
    # Item 6: a malformed or unreadable record is never read as "no record" (that would restart
    # the patch and could reuse a name): exit 1, the type only, nothing published, record kept.
    # Identical content, so a run that skips the record when out/current is valid is caught too.
    ws = ws_all
    if case == "record-unreadable" and os.geteuid() == 0:
        pytest.fail("root ignores a 000 record; run the suite as a normal user")
    v1 = _spy_first_run(ws, spy)
    path = _record_path(ws)
    assert path.is_file(), "refresh wrote no version record in the state dir"
    valid = path.read_bytes()
    _, fingerprint = _DETECT.read_bundle(ws.out / _CURRENT)
    if out_dir == "out-emptied":
        _empty_out(ws)
    mutate, error = _BAD_RECORDS[case]
    try:
        mutate(path, valid, v1, fingerprint)
        link = os.readlink(path) if path.is_symlink() else None
        # The unreadable row: its bytes are the valid record (read back after the chmod is undone).
        readable = path.is_file() and not path.is_symlink() and os.access(path, os.R_OK)
        raw = path.read_bytes() if readable else valid if case == "record-unreadable" else None
        state = _published_state(ws.out) if ws.out.is_dir() else {}
        result = _invoke(ws.argv("refresh", "--all"))
    finally:
        if path.exists() and not path.is_symlink():
            path.chmod(0o644 if path.is_file() else 0o755)
    if error is None:
        assert result.exit_code == 0, _why(result)
        version = _assert_current_bundle(ws, _repo_names())
        assert version == v1, (version, v1)
        _assert_record_names_current(ws, v1)
        return
    assert result.exit_code == 1, _why(result)
    types = _TYPE_TOKEN.findall(result.stderr)
    assert types, f"no exception type named: {_why(result)}"
    if error:
        assert error in types, (error, types)
    assert "Traceback" not in _output(result) and "forged" not in _output(result), _why(result)
    assert "not-a-digest" not in _output(result) and "latest" not in _output(result), _why(result)
    assert (_published_state(ws.out) if ws.out.is_dir() else {}) == state, "a bad record still published"
    assert (os.readlink(path) if path.is_symlink() else None) == link
    if raw is not None:
        assert path.read_bytes() == raw, "the run rewrote the bad record"
    _assert_safe_output(result, str(ws.root), str(ws.root.resolve()))
    assert net.attempts == []
    assert _is_empty(ws.cwd)


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


@_FORMEX_XFAIL
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


@_FORMEX_XFAIL
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
    assert _is_empty(ws.out) and _state_untouched(ws) and _is_empty(ws.cwd)


def test_fetched_artifact_must_match_the_signed_sha(ws: _Workspace, net: _Net) -> None:
    # The valid Cellar redirect and ZIP are fetched and validated, but their signed SHA differs,
    # so the artifact must not be embedded or published.
    ws.sign("eurlex_gdpr", signed_artifact_sha256=hashlib.sha256(b"another artifact").hexdigest())
    net.upstream = ws
    result = _invoke(ws.argv("fetch", "eurlex_gdpr"))
    _assert_failed_loudly(result)
    assert "eurlex_gdpr" in _output(result), _why(result)
    celex = load_all(ws.sources)["eurlex_gdpr"].pipeline.celex_id
    cellar_requests = [request for request in net.served if request.url.host == _CELLAR_HOST]
    assert [request.url.path for request in cellar_requests] == [
        f"/resource/celex/{celex}",
        urlparse(_cellar_location(celex)).path,
    ], cellar_requests
    assert "LegalReviewGateError" in result.stderr, _why(result)
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
    assert _state_untouched(ws), "a dry run wrote a checkpoint"
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
    assert _is_empty(ws.out) and _state_untouched(ws) and _is_empty(ws.cwd), _why(dry)
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
        assert _state_untouched(ws), "a load error left a checkpoint behind (round 4 H)"
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


# Only "signed" runs every eurlex source to "done"; the other rows pass today.
@pytest.mark.parametrize(
    "where",
    [
        pytest.param(where, marks=_FORMEX_XFAIL) if where == "signed" else where
        for where in _CHECKPOINT_WRITE_FAILS
    ],
)
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
    assert _state_untouched(ws), "a dry run left a checkpoint behind"
    result = _invoke(ws.argv("refresh", "--all"))
    assert result.exit_code == _LOAD_ERROR, _why(result)
    assert spy.constructed == [] and spy.runs == [], _why(result)
    assert _state_untouched(ws), f"a stage-class load error left a checkpoint behind ({signed} signed)"
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
    assert _is_empty(ws.out) and _state_untouched(ws) and _is_empty(ws.cwd)


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


# ---------------------------------------------------------------------------
# Round 11 (rthoB): a missing refresh licence baseline fails closed unless the run seeds
# ---------------------------------------------------------------------------
# A cache miss empties INGESTER_STATE_DIR. Before Round 11 the licence-drift gate then read every
# source as NEW, published, and recorded the current SPDX as the new baseline: a silent
# re-baseline of a legal control. These tests pin the contract (items 1-7 of the ruling).


def _seed_flag() -> str:
    """Item 2: the seed opt-in is one cli constant naming an option of `refresh` (F13)."""
    flag = getattr(cli, "REFRESH_SEED_FLAG", None)
    assert isinstance(flag, str), "cli names no REFRESH_SEED_FLAG constant (Round 11 item 2)"
    assert flag.startswith("--") and len(flag) > 2, flag
    command = get_command(app)
    refresh_cmd = command.commands["refresh"]  # type: ignore[attr-defined]
    options = {opt for param in refresh_cmd.params for opt in param.opts}
    assert flag in options, (flag, sorted(options))
    return flag


def _upstream_spdx() -> str:
    """The SPDX the EUR-Lex fetcher reports today (every repo source is EUR-Lex)."""
    return str(eurlex_fetcher.EURLEX_LICENSE)


def _set_upstream_spdx(monkeypatch: pytest.MonkeyPatch, spdx: str) -> None:
    """The licence upstream now declares: what the fetcher reports on the next run."""
    monkeypatch.setattr(eurlex_fetcher, "EURLEX_LICENSE", spdx)


def _baseline(ws: _Workspace) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((ws.state / _refresh_baseline_name()).read_text(encoding="utf-8"))
    return data


def _remove_file(ws: _Workspace) -> None:
    (ws.state / _refresh_baseline_name()).unlink()


def _remove_state_dir(ws: _Workspace) -> None:
    # The whole cache missed: no checkpoint, no record, no baseline.
    shutil.rmtree(ws.state)


def _dangling_link(ws: _Workspace) -> None:
    path = ws.state / _refresh_baseline_name()
    path.unlink()
    path.symlink_to(ws.root / "no-such-baseline.json")


def _directory_in_its_place(ws: _Workspace) -> None:
    path = ws.state / _refresh_baseline_name()
    path.unlink()
    path.mkdir()


# Item 1, every way "the baseline file is missing" reaches the CLI (attack list A: indirection).
_MISSING_BASELINE: dict[str, Callable[[_Workspace], None]] = {
    "file-removed": _remove_file,
    "state-dir-removed": _remove_state_dir,
    "dangling-link": _dangling_link,
    "directory-in-its-place": _directory_in_its_place,
}


def _assert_baseline_gate(
    ws: _Workspace, result: Result, spy: _Spy | None, net: _Net, approved: tuple[str, ...] | None = None
) -> None:
    """Item 1 and 4: a named, documented non-zero exit, before any source ran, reason only.

    *approved* are the sources the gate stopped (default: all). An unsigned source may still be
    reported as not approved; an approved one must never be blamed for the missing baseline.
    """
    _assert_failed_loudly(result)
    assert result.exit_code == _BASELINE_MISSING, _why(result)
    assert getattr(cli, "EXIT_LICENSE_BASELINE_MISSING", None) == _BASELINE_MISSING, "no named constant"
    assert result.exit_code in cli._EXIT_MEANINGS, _why(result)
    if spy is not None:
        assert spy.constructed == [] and spy.runs == [], ("a source ran", _why(result))
    assert net.attempts == [] and net.served == [], ("a source was fetched", _why(result))
    text = _output(result)
    # Item 4: the reason is named, and it is not the per-source failure message.
    assert "baseline" in text.lower(), _why(result)
    for name in _repo_names() if approved is None else approved:
        assert not _named_with(text, name, "fail"), (name, "the gate blamed a source", _why(result))
    assert "Traceback" not in text, _why(result)
    # No path values: neither the workspace nor the state dir is echoed.
    _assert_safe_output(result, str(ws.root), str(ws.root.resolve()), str(ws.state))


@pytest.mark.parametrize("how", sorted(_MISSING_BASELINE))
@pytest.mark.parametrize("prior", ["first-run", "after-a-run"])
@pytest.mark.parametrize("signed", ["all", "tier1"])
def test_missing_refresh_baseline_fails_closed_before_any_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: _Spy, net: _Net, how: str, prior: str, signed: str
) -> None:
    ws = _make_workspace(tmp_path, monkeypatch, signed=tuple(_repo_names()) if signed == "all" else _TIER1)
    if prior == "after-a-run":
        # A run before the cache miss: checkpoints, and with every source signed a published
        # bundle, a version record and out/current that the blocked run must leave alone.
        earlier = _invoke(ws.argv("refresh", "--all"))
        if signed == "all":
            assert earlier.exit_code == 0, _why(earlier)
            _assert_current_bundle(ws, _repo_names())
            assert _record_path(ws).is_file(), "the earlier run wrote no version record"
        spy.constructed.clear()
        spy.runs.clear()
    _MISSING_BASELINE[how](ws)
    before = _files(ws.root)
    published = _published_state(ws.out)

    # Item 5: the dry run exits as the real run will, offline, and writes nothing.
    dry = _invoke(ws.argv("refresh", "--all", "--dry-run"))
    assert _files(ws.root) == before, ("a dry run wrote", _why(dry))
    result = _invoke(ws.argv("refresh", "--all"))
    _assert_baseline_gate(ws, result, spy, net, None if signed == "all" else _TIER1)
    assert dry.exit_code == result.exit_code, ("the dry run does not mirror the real run", _why(dry))
    _assert_safe_output(dry, str(ws.root), str(ws.root.resolve()), str(ws.state))
    # Nothing written: no checkpoint (not even not_approved), no baseline, no version record,
    # out/current and every bundle as they were.
    assert _files(ws.root) == before, sorted(set(_files(ws.root).items()) ^ set(before.items()))
    assert _published_state(ws.out) == published, "the blocked run published or repointed a bundle"
    for name in _repo_names():
        if prior == "first-run" or how == "state-dir-removed":
            assert CheckpointStore(ws.state).load(name) is None, (name, "the blocked run checkpointed")


@pytest.mark.parametrize("how", ["file-removed", "state-dir-removed"])
def test_the_seed_flag_runs_with_a_missing_baseline(ws_all: _Workspace, spy: _Spy, net: _Net, how: str) -> None:
    # Item 2: seeding is the one explicit way past a missing baseline; its dry run mirrors it.
    ws = ws_all
    _MISSING_BASELINE[how](ws)
    flag = _seed_flag()
    before = _files(ws.root)
    dry = _invoke(ws.argv("refresh", "--all", "--dry-run", flag))
    assert dry.exit_code == 0, _why(dry)
    assert spy.constructed == [] and net.attempts == [], _why(dry)
    assert _files(ws.root) == before, ("a seeding dry run wrote", _why(dry))
    result = _invoke(ws.argv("refresh", "--all", flag))
    assert result.exit_code == 0, _why(result)
    assert spy.run_names() == _repo_names(), _why(result)
    _assert_current_bundle(ws, _repo_names())
    _assert_safe_output(result, str(ws.root), str(ws.root.resolve()))


@pytest.mark.parametrize("content", ["empty-object", "unconfigured-source-only"])
def test_a_baseline_without_the_source_is_new_and_allowed(
    ws_all: _Workspace, spy: _Spy, net: _Net, content: str
) -> None:
    # Item 3: only the whole file missing fails closed. Green today (no gate yet): the control
    # that the gate keys on the file, not on a per-source entry.
    ws = ws_all
    entries = {} if content == "empty-object" else {"retired_source": {"hash": "0" * 64, "spdx": _upstream_spdx()}}
    assert not set(entries) & set(_repo_names())
    (ws.state / _refresh_baseline_name()).write_text(json.dumps(entries), encoding="utf-8")
    result = _invoke(ws.argv("refresh", "--all"))
    assert result.exit_code == 0, _why(result)
    assert spy.run_names() == _repo_names(), _why(result)


@_FORMEX_XFAIL
def test_a_newly_approved_source_is_new_against_an_existing_baseline(ws: _Workspace, net: _Net) -> None:
    # Item 3, the real pipeline: Tier 1 records its baseline entries, then the rest are signed.
    # Their entries are missing from an existing file, so they are NEW and publish without a seed.
    net.upstream = ws
    first = _invoke(ws.argv("refresh", "--all"))
    assert first.exit_code == _SOURCE_FAILED, _why(first)
    assert set(_baseline(ws)) == set(_TIER1), sorted(_baseline(ws))
    for name in _unsigned():
        ws.sign(name)
    second = _invoke(ws.argv("refresh", "--all"))
    assert second.exit_code == 0, _why(second)
    assert set(_baseline(ws)) == set(_repo_names()), sorted(_baseline(ws))
    _assert_current_bundle(ws, _repo_names())


def test_real_refresh_with_a_missing_baseline_writes_nothing(ws_all: _Workspace, net: _Net) -> None:
    # Item 1 on the real composition (no spy): no fetch, no embed call, no file.
    ws = ws_all
    net.upstream = ws
    _remove_file(ws)
    before = _files(ws.root)
    dry = _invoke(ws.argv("refresh", "--all", "--dry-run"))
    result = _invoke(ws.argv("refresh", "--all"))
    _assert_baseline_gate(ws, result, None, net)
    assert dry.exit_code == result.exit_code, _why(dry)
    assert _files(ws.root) == before, sorted(set(_files(ws.root).items()) ^ set(before.items()))
    assert not os.path.lexists(ws.state / _refresh_baseline_name()), "the blocked run wrote a baseline"
    assert not _record_path(ws).exists(), "the blocked run wrote a version record"


# A second SPDX for the drift scenario; any value but the configured one.
_OTHER_SPDX = "CC0-1.0"


@_FORMEX_XFAIL
def test_cache_miss_never_rebaselines_a_changed_licence_without_the_seed(
    ws_all: _Workspace, net: _Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    # rthoB end to end on the real pipeline. Baseline SPDX A; upstream now reports B.
    ws = ws_all
    net.upstream = ws
    first = _invoke(ws.argv("refresh", "--all"))
    assert first.exit_code == 0, _why(first)
    spdx_a = _upstream_spdx()
    assert spdx_a != _OTHER_SPDX
    assert {n: e["spdx"] for n, e in _baseline(ws).items()} == dict.fromkeys(_repo_names(), spdx_a)
    _set_upstream_spdx(monkeypatch, _OTHER_SPDX)

    # Control: with the cache intact, the drift gate blocks B (the gate a cache miss bypassed).
    kept = _invoke(ws.argv("refresh", "--all"))
    assert kept.exit_code == _SOURCE_FAILED, _why(kept)
    for name in _repo_names():
        assert _named_with(kept.stderr, name, "fail"), (name, _why(kept))

    # The cache misses: a hosted runner starts with no state and no out/.
    shutil.rmtree(ws.state)
    shutil.rmtree(ws.out)
    before = _files(ws.root)
    net.attempts.clear()  # the earlier runs fetched; the gated run must not
    net.served.clear()
    unseeded = _invoke(ws.argv("refresh", "--all"))
    _assert_baseline_gate(ws, unseeded, None, net)
    assert _files(ws.root) == before, "the unseeded run after a cache miss wrote something"
    assert not os.path.lexists(ws.out / _CURRENT), "the unseeded run published B"
    dry = _invoke(ws.argv("refresh", "--all", "--dry-run"))
    assert dry.exit_code == unseeded.exit_code and _files(ws.root) == before, _why(dry)

    # The documented trade-off: seeding accepts the current SPDX (B) as the baseline.
    seeded = _invoke(ws.argv("refresh", "--all", _seed_flag()))
    assert seeded.exit_code == 0, _why(seeded)
    _assert_current_bundle(ws, _repo_names())
    assert {n: e["spdx"] for n, e in _baseline(ws).items()} == dict.fromkeys(_repo_names(), _OTHER_SPDX)
    # And the next scheduled run (no seed) uses that baseline: no drift, no gate.
    after = _invoke(ws.argv("refresh", "--all"))
    assert after.exit_code == 0, _why(after)


@_FORMEX_XFAIL
def test_the_seed_flag_never_overrides_drift_on_an_existing_baseline(
    ws_all: _Workspace, net: _Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Item 1 makes the seed an exception to the missing-file gate only. A seed dispatch on an
    # intact cache must not accept a changed SPDX: the baseline keeps A and the source is blocked.
    ws = ws_all
    net.upstream = ws
    name = "eurlex_gdpr"
    first = _invoke(ws.argv("refresh", "--all"))
    assert first.exit_code == 0, _why(first)
    recorded = _baseline(ws)
    _set_upstream_spdx(monkeypatch, _OTHER_SPDX)
    published = _published_state(ws.out)
    seeded = _invoke(ws.argv("refresh", "--all", _seed_flag()))
    assert seeded.exit_code == _SOURCE_FAILED, _why(seeded)
    assert _named_with(seeded.stderr, name, "fail"), _why(seeded)
    assert _baseline(ws) == recorded, "a seed run re-baselined a drifted licence"
    assert _published_state(ws.out) == published, "a seed run published past SPDX drift"
    state = CheckpointStore(ws.state).load(name)
    assert state is not None and state.stage == "license_failed", state


@pytest.mark.parametrize("how", ["file-removed", "state-dir-removed"])
def test_fetch_dry_run_mirrors_fetch_with_a_missing_baseline(ws: _Workspace, spy: _Spy, net: _Net, how: str) -> None:
    # Item 5: whatever fetch does with a missing baseline, its dry run exits the same way and
    # writes nothing.
    _MISSING_BASELINE[how](ws)
    before = _files(ws.root)
    dry = _invoke(ws.argv("fetch", "eurlex_gdpr", "--dry-run"))
    assert _files(ws.root) == before, ("a fetch dry run wrote", _why(dry))
    assert spy.constructed == [] and net.attempts == [], _why(dry)
    real = _invoke(ws.argv("fetch", "eurlex_gdpr"))
    assert dry.exit_code == real.exit_code, (_why(dry), _why(real))


_REFRESH_DOC = _REPO / "automations" / "refresh.md"


def _paragraphs(text: str) -> list[str]:
    return [" ".join(block.split()) for block in re.split(r"\n\s*\n", text) if block.strip()]


def test_refresh_doc_says_seeding_accepts_the_current_licence() -> None:
    # Item 6. (a) every "re-baseline" claim covers the licence baseline by name; (b) the seed
    # procedure says seeding accepts the current SPDX, so the person seeding checks it first;
    # (c) the manual path names the opt-in.
    text = _REFRESH_DOC.read_text(encoding="utf-8")
    name = _refresh_baseline_name()
    paragraphs = _paragraphs(text)
    claims = [p for p in paragraphs if re.search(r"re-?baseline", p, re.IGNORECASE)]
    assert claims, "control: refresh.md makes no re-baseline claim at all"
    uncovered = [p[:240] for p in claims if name not in p]
    assert uncovered == [], uncovered
    seed = [
        p
        for p in paragraphs
        if "seed_bundle_record" in p
        and name in p
        and "SPDX" in p
        and re.search(r"\b(confirm|check|verif|review)", p, re.IGNORECASE)
    ]
    assert seed, "refresh.md: no seed paragraph says seeding accepts the current SPDX as the baseline"
    assert _seed_flag() in text, "refresh.md does not name the seed flag for a manual run"
