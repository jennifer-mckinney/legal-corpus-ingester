"""Acceptance tests for jennifer-mckinney/terms-analysis#96: real Cellar cassettes for the EU Tier-1 sources.

EUR-Lex TXT/XML sits behind the AWS WAF, so the 2026-10-13 ruling moves the fetcher
to Cellar REST (see ``tests/unit/test_cellar_fetcher.py`` for the fetcher contract).
Each Tier-1 source YAML under ``config/sources/`` names a ``celex_id``. Its cassette
``tests/fixtures/cassettes/eurlex/<name>_fetch.yaml`` must be a REAL recording of what
the fetcher asks for, hop by hop:

1. GET ``https://publications.europa.eu/resource/celex/<celex_id>`` -> 303 with one
   ``Location`` on ``publications.europa.eu/resource/cellar/<uuid>[.NNNN.NN]/zip``;
2. GET the https form of that Location -> 200 ``application/zip``, a Formex 4 zip that
   passes ``read_formex_zip`` under the default config and whose act, UNZIPPED, is
   larger than 100 KiB (the zip itself is smaller: the AI Act zip is ~171 KB, its act
   ~654 KB).

No response carries the WAF header and no interaction carries credentials (this repo
is public). Next to each cassette, ``<name>_fetch.manifest.txt`` lists the zip and its
members (``zip <sha256> <bytes>`` then ``member <name> <bytes> <sha256> <root>`` in
central-directory order; ``#`` lines are comments). A test keeps the two in sync, so a
re-recorded zip that changed shows up as a reviewable diff.

Single sources of truth (nothing is retyped here):
- source list entries and ``celex_id``: the source YAMLs, through ``sources.registry.load_all``;
- cassette directory, recording config and the sensitive-header fragments:
  ``tests/integration/test_eurlex_fetcher.py``;
- the first-hop URI: produced by running the real ``EurLexFetcher`` with a fake transport;
- the zip validator and its caps: ``fetchers.cellar`` and ``config/fetchers/cellar.yaml``.

The replay tests come first in this file on purpose: under the drift canary's
``--record-mode=rewrite`` they re-record the cassettes, and the static checks that
follow then validate the fresh recording.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import importlib
import io
import re
import zipfile
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import yaml
from lxml import etree

from legal_corpus_ingester.cleaners.xml_akn import AKN_NS
from legal_corpus_ingester.errors import SchemaDriftError
from legal_corpus_ingester.fetchers.eurlex import EurLexFetcher
from legal_corpus_ingester.sources.registry import load_all
from legal_corpus_ingester.sources.schema import SourceConfig
from tests.integration import test_eurlex_fetcher as _eurlex_vcr
from tests.unit.test_cellar_fetcher import (
    AI_ACT_CELEX,
    CELEX_BASE,
    WAF_HEADER,
    _break_chars,
    ai_pkg,
    formex_act,
)

_REPO = Path(__file__).resolve().parents[2]
_SOURCES_DIR = _REPO / "config" / "sources"

# Card #96 scope: the EU Tier-1 sources, by their source-YAML ``name``. Their
# CELEX ids are NOT listed here; they are read from the YAMLs at test time.
TIER1_SOURCES: tuple[str, ...] = ("eurlex_gdpr", "eurlex_ai_act", "eurlex_dsa")

# Card #96 acceptance threshold, measured on the UNZIPPED act (ruling 2026-10-13).
_MIN_RECORDED_BYTES = 100 * 1024

# Security design F9: a committed cassette file is at most 1 MiB.
_MAX_CASSETTE_BYTES = 1024 * 1024

# Cassette file name = <source name without the "<cassette dir>_" prefix> + this.
_CASSETTE_SUFFIX = "_fetch"
_SIDECAR_SUFFIX = ".manifest.txt"

# Security design F3: the only Location a recorded first hop may carry.
_LOCATION_RE = re.compile(
    r"https?://publications\.europa\.eu/resource/cellar/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(\.[0-9]{4}\.[0-9]{2})?/zip",
    re.ASCII,
)
_ACT_ROOTS = frozenset({"ACT", "CONS.ACT"})

# Recording config shared with the existing EUR-Lex VCR test (pytest fixtures).
vcr_config = _eurlex_vcr.vcr_config
vcr_cassette_dir = _eurlex_vcr.vcr_cassette_dir
_SENSITIVE_FRAGMENTS: tuple[str, ...] = _eurlex_vcr._SENSITIVE_RESPONSE_HEADER_FRAGMENTS

# How much of an untrusted value a problem message may quote.
_QUOTE_CAP = 160


def _cellar() -> ModuleType:
    return importlib.import_module("legal_corpus_ingester.fetchers.cellar")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _quote(value: object) -> str:
    """Render an untrusted value as printable ASCII, truncated (output safety)."""
    text = ascii(value)
    return text if len(text) <= _QUOTE_CAP else text[:_QUOTE_CAP] + "...(truncated)"


def _rel(path: Path, root: Path) -> str:
    """Repo-relative path for messages; never an absolute path (F8)."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _cassette_stem(cfg: SourceConfig, cassette_dir: Path) -> str:
    """``eurlex_gdpr`` in cassette dir ``eurlex/`` -> ``gdpr_fetch``."""
    prefix = cassette_dir.name + "_"
    if not cfg.name.startswith(prefix):
        pytest.fail(
            f"source {cfg.name!r} does not start with {prefix!r}; "
            "cannot derive its cassette name"
        )
    return cfg.name[len(prefix):] + _CASSETTE_SUFFIX


def _cassette_path(cfg: SourceConfig, cassette_dir: Path) -> Path:
    return cassette_dir / f"{_cassette_stem(cfg, cassette_dir)}.yaml"


def _sidecar_path(cfg: SourceConfig, cassette_dir: Path) -> Path:
    return cassette_dir / f"{_cassette_stem(cfg, cassette_dir)}{_SIDECAR_SUFFIX}"


def _expected_celex_url(cfg: SourceConfig) -> str:
    """Run the real EurLexFetcher for ``cfg`` against a 404 transport; return its first request URL."""
    seen: list[str] = []

    async def _handle(self: Any, request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404, request=request)

    async def _no_sleep(*_a: object, **_k: object) -> None:
        return None

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _handle)
        mp.setattr(asyncio, "sleep", _no_sleep)
        try:
            asyncio.run(EurLexFetcher().fetch(cfg))
        except Exception:  # noqa: BLE001 - a 404 refusal is expected; only the URL matters
            pass
    if not seen:
        pytest.fail(f"{cfg.name}: the fetcher made no request")
    return seen[0]


def _read_cassette(path: Path, source: str, root: Path) -> tuple[Any, list[str]]:
    """Load a cassette. Every failure is a problem that names the source."""
    rel = _rel(path, root)
    if path.is_symlink():
        return None, [f"{source}: cassette {rel} is a symlink; commit the recorded file itself"]
    if not path.is_file():
        return None, [
            f"{source}: no cassette at {rel}; record it from the live source "
            "(pytest tests/integration/test_cassettes_match_sources.py --record-mode=once)"
        ]
    size = path.stat().st_size
    if size > _MAX_CASSETTE_BYTES:
        return None, [f"{source}: cassette {rel} is {size} bytes, above {_MAX_CASSETTE_BYTES}"]
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return None, [f"{source}: cassette {rel} is unreadable ({type(exc).__name__})"]
    return data, []


def _read_sidecar(path: Path, source: str, root: Path) -> tuple[bytes | None, list[str]]:
    rel = _rel(path, root)
    if path.is_symlink():
        return None, [f"{source}: zip manifest {rel} is a symlink; commit the file itself"]
    if not path.is_file():
        return None, [f"{source}: no zip manifest at {rel}; write it when recording the cassette"]
    return path.read_bytes(), []


def _interactions(data: Any, source: str) -> tuple[list[dict[str, Any]], list[str]]:
    """The interactions list, or problems. Zero interactions is never a pass (F5)."""
    if not isinstance(data, dict):
        return [], [f"{source}: cassette is not a mapping (got {type(data).__name__})"]
    items = data.get("interactions")
    if not isinstance(items, list) or not items:
        return [], [f"{source}: cassette has no recorded interactions"]
    for i, item in enumerate(items):
        if not (
            isinstance(item, dict)
            and isinstance(item.get("request"), dict)
            and isinstance(item.get("response"), dict)
        ):
            return [], [f"{source}: interaction {i} lacks a request/response mapping"]
    return items, []


def _header_values(side: dict[str, Any], name: str) -> list[Any]:
    """Every recorded value of header ``name``, matched case-insensitively across keys."""
    headers = side.get("headers")
    if not isinstance(headers, dict):
        return []
    values: list[Any] = []
    for key, value in headers.items():
        if str(key).lower() == name:
            values.extend(value if isinstance(value, list) else [value])
    return values


def _status(response: dict[str, Any]) -> object:
    status = response.get("status")
    return status.get("code") if isinstance(status, dict) else None


def _https(location: str) -> str:
    return "https://" + location.split("://", 1)[1]


def _shape_problems(data: Any, source: str, expected_url: str) -> list[str]:
    """Hop by hop: 303 to a Cellar zip Location, then 200 application/zip from its https form."""
    items, problems = _interactions(data, source)
    if not items:
        return problems
    if len(items) != 2:
        return [f"{source}: cassette has {len(items)} interactions, not 2 (303 then 200 zip)"]
    first, second = items
    for i, item in enumerate(items):
        if item["request"].get("method") != "GET":
            problems.append(f"{source}: interaction {i} method {_quote(item['request'].get('method'))} is not GET")
        if _header_values(item["response"], WAF_HEADER):
            problems.append(f"{source}: interaction {i} response carries the WAF header; re-record it")
    if first["request"].get("uri") != expected_url:
        problems.append(
            f"{source}: interaction 0 uri {_quote(first['request'].get('uri'))} is not the fetcher's "
            f"request {_quote(expected_url)}"
        )
    # type() not isinstance(): True and 303.0 are not a recorded HTTP status.
    code = _status(first["response"])
    if type(code) is not int or code != 303:
        problems.append(f"{source}: interaction 0 status {_quote(code)} is not 303")
    locations = _header_values(first["response"], "location")
    target: str | None = None
    if len(locations) != 1:
        problems.append(f"{source}: interaction 0 has {len(locations)} Location values, not 1")
    elif not isinstance(locations[0], str) or not _LOCATION_RE.fullmatch(locations[0]):
        problems.append(f"{source}: interaction 0 Location {_quote(locations[0])} is not a Cellar zip URL")
    else:
        target = _https(locations[0])
    if target is not None and second["request"].get("uri") != target:
        problems.append(
            f"{source}: interaction 1 uri {_quote(second['request'].get('uri'))} is not the https "
            f"Location {_quote(target)}"
        )
    code = _status(second["response"])
    if type(code) is not int or code != 200:
        problems.append(
            f"{source}: interaction 1 status {_quote(code)} is not 200 "
            "(202 with an empty body is the WAF challenge, not the document)"
        )
    types_ = _header_values(second["response"], "content-type")
    media = types_[0].split(";", 1)[0].strip().lower() if len(types_) == 1 and isinstance(types_[0], str) else None
    if media != "application/zip":
        problems.append(f"{source}: interaction 1 Content-Type {_quote(types_)} is not application/zip")
    return problems


def _zip_body(data: Any, source: str) -> tuple[bytes | None, list[str]]:
    items, problems = _interactions(data, source)
    if not items:
        return None, problems
    if len(items) != 2:
        return None, [f"{source}: cassette has {len(items)} interactions, not 2; no zip to check"]
    body = items[1]["response"].get("body")
    raw = body.get("string") if isinstance(body, dict) else None
    if not isinstance(raw, bytes):
        # vcrpy stores undecodable bodies as bytes; a str body is text, never a deflated zip.
        return None, [f"{source}: interaction 1 body is {type(raw).__name__}, not recorded bytes"]
    return raw, []


def _parse_hardened(data: bytes) -> Any:
    """Same hardening as AKNXMLCleaner: no entities, no network, no huge tree."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    return etree.fromstring(data, parser)


def _body_problems(data: Any, source: str, celex: str) -> list[str]:
    raw, problems = _zip_body(data, source)
    if raw is None:
        return problems
    cellar = _cellar()
    try:
        parts = cellar.read_formex_zip(raw, celex, cellar.load_cellar_settings())
    except SchemaDriftError as exc:
        return [f"{source}: interaction 1 zip is refused by read_formex_zip ({_quote(str(exc))})"]
    if not parts:
        return [f"{source}: interaction 1 zip has no act"]
    act = parts[0]
    if act.root not in _ACT_ROOTS:
        problems.append(f"{source}: first part root {_quote(act.root)} is not ACT or CONS.ACT")
    size = len(act.data)
    if size <= _MIN_RECORDED_BYTES:
        problems.append(
            f"{source}: unzipped act is {size} bytes, not > {_MIN_RECORDED_BYTES}; "
            "synthetic or truncated, not a recorded statute"
        )
    # Independent of read_formex_zip: parse the act the way the cleaner will.
    try:
        root = _parse_hardened(act.data)
    except etree.XMLSyntaxError as exc:
        problems.append(f"{source}: act is not well-formed XML ({_quote(str(exc))})")
        return problems
    if root.getroottree().docinfo.doctype:
        problems.append(f"{source}: act declares a DOCTYPE; a Formex act has none")
    qname = etree.QName(root)
    if qname.namespace is not None or qname.localname != act.root:
        problems.append(f"{source}: act root {_quote(root.tag)} is not the un-namespaced {_quote(act.root)}")
    return problems


def _is_sensitive(name: object) -> bool:
    lowered = str(name).lower()
    return any(frag in lowered for frag in _SENSITIVE_FRAGMENTS)


def _credential_problems(data: Any, source: str) -> list[str]:
    items, problems = _interactions(data, source)
    for i, item in enumerate(items):
        for side in ("request", "response"):
            headers = item[side].get("headers") or {}
            if not isinstance(headers, dict):
                problems.append(f"{source}: interaction {i} {side} headers are not a mapping")
                continue
            for name in headers:
                if _is_sensitive(name):
                    problems.append(
                        f"{source}: interaction {i} {side} header {_quote(name)} must not be committed"
                    )
        uri = item["request"].get("uri")
        if isinstance(uri, str) and (urlsplit(uri).username or urlsplit(uri).password):
            problems.append(f"{source}: interaction {i} uri carries credentials")
    return problems


def _sidecar_lines(body: bytes) -> list[str]:
    """The manifest lines a zip must be listed with (no comments). The zip must already be valid."""
    lines = [f"zip {hashlib.sha256(body).hexdigest()} {len(body)}"]
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        for info in zf.infolist():  # central-directory order
            member = zf.read(info)
            root = etree.QName(_parse_hardened(member)).localname
            lines.append(f"member {info.filename} {len(member)} {hashlib.sha256(member).hexdigest()} {root}")
    return lines


def _sidecar_problems(data: Any, sidecar: bytes | None, source: str, celex: str) -> list[str]:
    if sidecar is None:
        return [f"{source}: no zip manifest"]
    problems: list[str] = []
    if not sidecar.isascii():
        problems.append(f"{source}: zip manifest is not ASCII")
    text = sidecar.decode("ascii", errors="replace")
    if not text.endswith("\n"):
        problems.append(f"{source}: zip manifest does not end with a newline")
    lines = text[:-1].split("\n") if text.endswith("\n") else text.split("\n")
    for n, line in enumerate(lines, 1):
        if not line:
            problems.append(f"{source}: zip manifest line {n} is blank")
        elif not line.isprintable():
            problems.append(f"{source}: zip manifest line {n} has a control or format character")
        elif line != line.rstrip():
            problems.append(f"{source}: zip manifest line {n} has trailing whitespace")
    if problems:
        return problems
    raw, problems = _zip_body(data, source)
    if raw is None:
        return problems
    cellar = _cellar()
    try:
        cellar.read_formex_zip(raw, celex, cellar.load_cellar_settings())
    except SchemaDriftError as exc:
        return [f"{source}: recorded zip is refused, so its manifest cannot be checked ({_quote(str(exc))})"]
    listed = [line for line in lines if not line.startswith("#")]
    expected = _sidecar_lines(raw)
    if listed != expected:
        for n, (got, want) in enumerate(zip(listed + [""] * len(expected), expected + [""] * len(listed))):
            if got != want:
                return [
                    f"{source}: zip manifest entry {n + 1} is {_quote(got)}, the recorded zip gives "
                    f"{_quote(want)}; re-write the manifest from the cassette"
                ]
    return []


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def registry() -> dict[str, SourceConfig]:
    return load_all(_SOURCES_DIR)


@pytest.fixture(scope="module")
def cassette_dir(vcr_cassette_dir: str) -> Path:
    path = Path(vcr_cassette_dir)
    return path if path.is_absolute() else _REPO / path


# ---------------------------------------------------------------------------
# replay: the real fetcher, driven by each source config, against its cassette
# (record_mode defaults to "none" in CI; the drift canary re-records these)
# ---------------------------------------------------------------------------


def _replay(request: pytest.FixtureRequest, registry: dict[str, SourceConfig], cassette_dir: Path, source: str) -> None:
    cfg = registry[source]
    marker = request.node.get_closest_marker("default_cassette")
    assert marker is not None and marker.args[0] == _cassette_stem(cfg, cassette_dir), (
        f"{source}: default_cassette {marker.args[0] if marker else None!r} != "
        f"derived {_cassette_stem(cfg, cassette_dir)!r}"
    )
    result = asyncio.run(EurLexFetcher().fetch(cfg))
    assert result.mime_type == "application/zip"
    assert result.provenance.upstream_version == cfg.pipeline.celex_id
    data, problems = _read_cassette(_cassette_path(cfg, cassette_dir), source, _REPO)
    if not problems:
        raw, problems = _zip_body(data, source)
        if raw is not None:
            assert hashlib.sha256(result.raw_bytes).hexdigest() == hashlib.sha256(raw).hexdigest()
            problems = _body_problems(data, source, cfg.pipeline.celex_id)
    assert not problems, "\n".join(problems)


@pytest.mark.vcr
@pytest.mark.default_cassette("gdpr_fetch")
def test_fetcher_replays_eurlex_gdpr_cassette(
    request: pytest.FixtureRequest, registry: dict[str, SourceConfig], cassette_dir: Path
) -> None:
    _replay(request, registry, cassette_dir, "eurlex_gdpr")


@pytest.mark.vcr
@pytest.mark.default_cassette("ai_act_fetch")
def test_fetcher_replays_eurlex_ai_act_cassette(
    request: pytest.FixtureRequest, registry: dict[str, SourceConfig], cassette_dir: Path
) -> None:
    _replay(request, registry, cassette_dir, "eurlex_ai_act")


@pytest.mark.vcr
@pytest.mark.default_cassette("dsa_fetch")
def test_fetcher_replays_eurlex_dsa_cassette(
    request: pytest.FixtureRequest, registry: dict[str, SourceConfig], cassette_dir: Path
) -> None:
    _replay(request, registry, cassette_dir, "eurlex_dsa")


# ---------------------------------------------------------------------------
# positive control: the Tier-1 configs load and the fetcher asks Cellar for their CELEX id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", TIER1_SOURCES)
def test_tier1_source_config_loads_with_celex_id(
    source: str, registry: dict[str, SourceConfig], cassette_dir: Path
) -> None:
    assert source in registry, f"{source}: no such source in config/sources"
    celex_id = registry[source].pipeline.celex_id
    assert celex_id, f"{source}: pipeline.celex_id is empty"
    assert _expected_celex_url(registry[source]) == CELEX_BASE + celex_id
    assert _cassette_stem(registry[source], cassette_dir)


# ---------------------------------------------------------------------------
# card #96 acceptance, per Tier-1 source
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", TIER1_SOURCES)
def test_cassette_exists_per_tier1_source(
    source: str, registry: dict[str, SourceConfig], cassette_dir: Path
) -> None:
    _data, problems = _read_cassette(_cassette_path(registry[source], cassette_dir), source, _REPO)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("source", TIER1_SOURCES)
def test_cassette_hops_match_cellar_shape(
    source: str, registry: dict[str, SourceConfig], cassette_dir: Path
) -> None:
    cfg = registry[source]
    expected_url = _expected_celex_url(cfg)
    data, problems = _read_cassette(_cassette_path(cfg, cassette_dir), source, _REPO)
    if not problems:
        problems = _shape_problems(data, source, expected_url)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("source", TIER1_SOURCES)
def test_cassette_is_recorded_formex_not_synthetic(
    source: str, registry: dict[str, SourceConfig], cassette_dir: Path
) -> None:
    cfg = registry[source]
    data, problems = _read_cassette(_cassette_path(cfg, cassette_dir), source, _REPO)
    if not problems:
        problems = _body_problems(data, source, cfg.pipeline.celex_id)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("source", TIER1_SOURCES)
def test_cassette_carries_no_credentials(
    source: str, registry: dict[str, SourceConfig], cassette_dir: Path
) -> None:
    data, problems = _read_cassette(_cassette_path(registry[source], cassette_dir), source, _REPO)
    if not problems:
        problems = _credential_problems(data, source)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("source", TIER1_SOURCES)
def test_zip_manifest_matches_cassette(
    source: str, registry: dict[str, SourceConfig], cassette_dir: Path
) -> None:
    cfg = registry[source]
    data, problems = _read_cassette(_cassette_path(cfg, cassette_dir), source, _REPO)
    sidecar, sidecar_problems = _read_sidecar(_sidecar_path(cfg, cassette_dir), source, _REPO)
    problems += sidecar_problems
    if not problems:
        problems = _sidecar_problems(data, sidecar, source, cfg.pipeline.celex_id)
    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------------------
# the checks above bite: vectors of crafted cassettes (allow and block per check)
# ---------------------------------------------------------------------------

_V_SOURCE = "vector_source"
_V_CELEX = AI_ACT_CELEX
_V_URL = CELEX_BASE + _V_CELEX
_V_UUID = "1a2b3c4d-0000-4111-8222-333344445555"
_V_LOCATION = f"http://publications.europa.eu/resource/cellar/{_V_UUID}.0006.02/zip"
_V_ZIP_URL = _https(_V_LOCATION)
_ZIP_TYPE = "application/zip;charset=UTF-8"


@functools.cache
def _real_size_zip() -> bytes:
    """A synthetic zip shaped like a recording: act > 100 KiB unzipped, zip < 100 KiB."""
    body = ai_pkg(act_size=_MIN_RECORDED_BYTES + 50 * 1024).body()
    assert len(body) < _MIN_RECORDED_BYTES, len(body)
    return body


def _cassette(body: Any) -> dict[str, Any]:
    return {
        "interactions": [
            {
                "request": {"method": "GET", "uri": _V_URL, "headers": {"Accept": ["application/zip;mtype=fmx4"]}},
                "response": {
                    "status": {"code": 303, "message": "See Other"},
                    "headers": {"Location": [_V_LOCATION]},
                    "body": {"string": b""},
                },
            },
            {
                "request": {"method": "GET", "uri": _V_ZIP_URL, "headers": {}},
                "response": {
                    "status": {"code": 200, "message": "OK"},
                    "headers": {"Content-Type": [_ZIP_TYPE]},
                    "body": {"string": body},
                },
            },
        ],
        "version": 1,
    }


def _good() -> dict[str, Any]:
    return _cassette(_real_size_zip())


def _good_sidecar(comment: str = "# recorded from Cellar\n") -> bytes:
    return (comment + "\n".join(_sidecar_lines(_real_size_zip())) + "\n").encode("ascii")


Vector = Callable[[], tuple[Any, bytes | None]]


def _ok() -> tuple[Any, bytes | None]:
    return _good(), _good_sidecar()


def _with(path: tuple[Any, ...], value: Any) -> Vector:
    def build() -> tuple[Any, bytes | None]:
        data = _good()
        node: Any = data
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        return data, _good_sidecar()

    return build


def _body_is(body: Any) -> Vector:
    return lambda: (_cassette(body), _good_sidecar())


def _sidecar_is(render: Callable[[bytes], bytes | None]) -> Vector:
    return lambda: (_good(), render(_good_sidecar()))


def _swap_lines(text: bytes) -> bytes:
    lines = text.split(b"\n")
    lines[2], lines[3] = lines[3], lines[2]
    return b"\n".join(lines)


def _tamper_hex(text: bytes, line_no: int) -> bytes:
    lines = text.split(b"\n")
    fields = lines[line_no].split(b" ")
    idx = 1 if fields[0] == b"zip" else 3
    fields[idx] = (b"0" if fields[idx][:1] != b"0" else b"1") + fields[idx][1:]
    lines[line_no] = b" ".join(fields)
    return b"\n".join(lines)


_I0 = ("interactions", 0)
_I1 = ("interactions", 1)
_CHECKS: dict[str, Callable[[Any, bytes | None], list[str]]] = {
    "shape": lambda d, _s: _shape_problems(d, _V_SOURCE, _V_URL),
    "body": lambda d, _s: _body_problems(d, _V_SOURCE, _V_CELEX),
    "credentials": lambda d, _s: _credential_problems(d, _V_SOURCE),
    "sidecar": lambda d, s: _sidecar_problems(d, s, _V_SOURCE, _V_CELEX),
}
_EVIL_LOCATION = _V_LOCATION.replace("publications.europa.eu", "publications.europa.eu.evil.test")
_WAF = {WAF_HEADER: ["challenge"]}
_DOCTYPE = b'<!DOCTYPE ACT [<!ENTITY e SYSTEM "file:///etc/hostname">]>'


def _act_variant(transform: Callable[[bytes], bytes]) -> Callable[[], bytes]:
    def build() -> bytes:
        act = formex_act(size=_MIN_RECORDED_BYTES + 50 * 1024)
        return ai_pkg(act=transform(act)).body()

    return build


def _lazy_body(make: Callable[[], bytes]) -> Vector:
    return lambda: (_cassette(make()), _good_sidecar())


# (id, check, build, blocked)
VECTORS: list[tuple[str, str, Vector, bool]] = [
    # --- shape: 303 then 200 zip ---
    ("shape-good", "shape", _ok, False),
    ("shape-https-location", "shape", lambda: _swap_location(_V_LOCATION.replace("http:", "https:", 1)), False),
    ("shape-one-interaction", "shape", _with(("interactions",), _good()["interactions"][1:]), True),
    ("shape-three-interactions", "shape", _with(("interactions",), _good()["interactions"] + _good()["interactions"][1:]), True),
    ("shape-eurlex-uri", "shape", _with(_I0 + ("request", "uri"), "https://eur-lex.europa.eu/legal-content/EN/TXT/XML/?uri=CELEX:32024R1689"), True),
    ("shape-celex-suffix-forgery", "shape", _with(_I0 + ("request", "uri"), _V_URL + "1"), True),
    ("shape-http-first-hop", "shape", _with(_I0 + ("request", "uri"), _V_URL.replace("https:", "http:")), True),
    ("shape-first-status-200", "shape", _with(_I0 + ("response", "status"), {"code": 200}), True),
    ("shape-first-status-302", "shape", _with(_I0 + ("response", "status"), {"code": 302}), True),
    ("shape-first-status-string", "shape", _with(_I0 + ("response", "status"), {"code": "303"}), True),
    ("shape-first-status-float", "shape", _with(_I0 + ("response", "status"), {"code": 303.0}), True),
    ("shape-location-missing", "shape", _with(_I0 + ("response", "headers"), {}), True),
    ("shape-location-two-keys", "shape", _with(_I0 + ("response", "headers"), {"Location": [_V_LOCATION], "location": [_V_LOCATION]}), True),
    ("shape-location-two-values", "shape", _with(_I0 + ("response", "headers"), {"Location": [_V_LOCATION, _EVIL_LOCATION]}), True),
    ("shape-location-evil-host", "shape", _with(_I0 + ("response", "headers"), {"Location": [_EVIL_LOCATION]}), True),
    ("shape-location-query", "shape", _with(_I0 + ("response", "headers"), {"Location": [_V_LOCATION + "?x=1"]}), True),
    ("shape-location-newline", "shape", _with(_I0 + ("response", "headers"), {"Location": [_V_LOCATION + "\n"]}), True),
    ("shape-location-not-string", "shape", _with(_I0 + ("response", "headers"), {"Location": [None]}), True),
    ("shape-second-uri-not-rewritten", "shape", _with(_I1 + ("request", "uri"), _V_LOCATION), True),
    ("shape-second-uri-other-uuid", "shape", _with(_I1 + ("request", "uri"), _V_ZIP_URL.replace("1a2b", "9a2b")), True),
    ("shape-second-status-202", "shape", _with(_I1 + ("response", "status"), {"code": 202}), True),
    ("shape-second-status-bool", "shape", _with(_I1 + ("response", "status"), {"code": True}), True),
    ("shape-content-type-html", "shape", _with(_I1 + ("response", "headers"), {"Content-Type": ["text/html"]}), True),
    ("shape-content-type-zipx", "shape", _with(_I1 + ("response", "headers"), {"Content-Type": ["application/zipx"]}), True),
    ("shape-content-type-missing", "shape", _with(_I1 + ("response", "headers"), {}), True),
    ("shape-content-type-twice", "shape", _with(_I1 + ("response", "headers"), {"Content-Type": [_ZIP_TYPE, "text/html"]}), True),
    ("shape-waf-header-second", "shape", _with(_I1 + ("response", "headers"), {"Content-Type": [_ZIP_TYPE], **_WAF}), True),
    ("shape-waf-header-first-upper", "shape", _with(_I0 + ("response", "headers"), {"Location": [_V_LOCATION], WAF_HEADER.upper(): ["block"]}), True),
    ("shape-second-post", "shape", _with(_I1 + ("request", "method"), "POST"), True),
    ("shape-first-head", "shape", _with(_I0 + ("request", "method"), "HEAD"), True),
    # --- body: a Formex zip whose UNZIPPED act is > 100 KiB ---
    ("body-good", "body", _ok, False),
    ("body-act-at-threshold", "body", _lazy_body(lambda: ai_pkg(act_size=_MIN_RECORDED_BYTES).body()), True),
    ("body-big-zip-small-act", "body", _lazy_body(lambda: ai_pkg(act_size=4096, annex_size=_MIN_RECORDED_BYTES + 20 * 1024).body()), True),
    ("body-akn-xml-not-zip", "body", _body_is(f'<akomaNtoso xmlns="{AKN_NS}"><act/></akomaNtoso>'.encode() + b" " * _MIN_RECORDED_BYTES), True),
    ("body-empty", "body", _body_is(b""), True),
    ("body-str", "body", _lazy_body(lambda: _real_size_zip().decode("latin-1")), True),  # type: ignore[arg-type,return-value]
    ("body-missing", "body", _with(_I1 + ("response", "body"), None), True),
    ("body-act-doctype", "body", _lazy_body(_act_variant(lambda a: a.replace(b"?>", b"?>" + _DOCTYPE, 1))), True),
    ("body-act-akn-namespace", "body", _lazy_body(_act_variant(lambda a: a.replace(b"<ACT ", f'<ACT xmlns="{AKN_NS}" '.encode(), 1))), True),
    ("body-celex-mismatch", "body", _lazy_body(lambda: ai_pkg(number="1688", act_size=_MIN_RECORDED_BYTES + 50 * 1024).body()), True),
    ("body-annex-as-act", "body", _lazy_body(lambda: ai_pkg(act=formex_act(size=4096).replace(b"ACT", b"ANNEX")).body()), True),
    # --- credentials (this repo is public) ---
    ("creds-clean", "credentials", _ok, False),
    ("creds-set-cookie", "credentials", _with(_I1 + ("response", "headers"), {"Set-Cookie": ["aws-waf-token=t"]}), True),
    ("creds-set-cookie-lower", "credentials", _with(_I0 + ("response", "headers"), {"set-cookie": ["s=1"]}), True),
    ("creds-request-cookie", "credentials", _with(_I0 + ("request", "headers"), {"Cookie": ["s=1"]}), True),
    ("creds-authorization", "credentials", _with(_I1 + ("request", "headers"), {"Authorization": ["Bearer t"]}), True),
    ("creds-www-authenticate", "credentials", _with(_I1 + ("response", "headers"), {"WWW-Authenticate": ["Basic"]}), True),
    ("creds-auth-token", "credentials", _with(_I1 + ("response", "headers"), {"X-Auth-Token": ["t"]}), True),
    ("creds-uri-userinfo", "credentials", _with(_I1 + ("request", "uri"), _V_ZIP_URL.replace("https://", "https://u:p@")), True),
    ("creds-headers-not-mapping", "credentials", _with(_I1 + ("response", "headers"), ["Set-Cookie: s=1"]), True),
    # --- zip manifest sidecar ---
    ("sidecar-good", "sidecar", _ok, False),
    ("sidecar-no-comment", "sidecar", _sidecar_is(lambda s: _good_sidecar(comment="")), False),
    ("sidecar-missing", "sidecar", _sidecar_is(lambda s: None), True),
    ("sidecar-empty", "sidecar", _sidecar_is(lambda s: b""), True),
    ("sidecar-zip-sha-tampered", "sidecar", _sidecar_is(lambda s: _tamper_hex(s, 1)), True),
    ("sidecar-member-sha-tampered", "sidecar", _sidecar_is(lambda s: _tamper_hex(s, 4)), True),
    ("sidecar-zip-size-tampered", "sidecar", _sidecar_is(lambda s: re.sub(rb"^(zip \S+ )(\d+)", lambda m: m.group(1) + str(int(m.group(2)) + 1).encode(), s, flags=re.M)), True),
    ("sidecar-root-wrong", "sidecar", _sidecar_is(lambda s: s.replace(b" ANNEX\n", b" ACT\n", 1)), True),
    ("sidecar-member-line-missing", "sidecar", _sidecar_is(lambda s: b"\n".join(s.split(b"\n")[:-2]) + b"\n"), True),
    ("sidecar-extra-line", "sidecar", _sidecar_is(lambda s: s + s.split(b"\n")[2] + b"\n"), True),
    ("sidecar-duplicate-zip-line", "sidecar", _sidecar_is(lambda s: s.split(b"\n")[1] + b"\n" + s), True),
    ("sidecar-reordered", "sidecar", _sidecar_is(_swap_lines), True),
    ("sidecar-uppercase-hex", "sidecar", _sidecar_is(lambda s: re.sub(rb"[0-9a-f]{64}", lambda m: m.group(0).upper(), s, count=1)), True),
    ("sidecar-crlf", "sidecar", _sidecar_is(lambda s: s.replace(b"\n", b"\r\n")), True),
    ("sidecar-trailing-space", "sidecar", _sidecar_is(lambda s: s.replace(b"\n", b" \n", 1)), True),
    ("sidecar-blank-line", "sidecar", _sidecar_is(lambda s: s.replace(b"\n", b"\n\n", 1)), True),
    ("sidecar-no-final-newline", "sidecar", _sidecar_is(lambda s: s[:-1]), True),
    ("sidecar-bom", "sidecar", _sidecar_is(lambda s: b"\xef\xbb\xbf" + s), True),
    ("sidecar-non-ascii-comment", "sidecar", _sidecar_is(lambda s: s.replace(b"# recorded", "# récorded".encode())), True),
    ("sidecar-nul-in-comment", "sidecar", _sidecar_is(lambda s: s.replace(b"# recorded", b"# rec\x00orded")), True),
    ("sidecar-zip-refused", "sidecar", lambda: (_cassette(ai_pkg(number="1688").body()), _good_sidecar()), True),
]


def _swap_location(location: str) -> tuple[Any, bytes | None]:
    data = _good()
    data["interactions"][0]["response"]["headers"] = {"location": [location]}
    data["interactions"][1]["request"]["uri"] = _https(location)
    return data, _good_sidecar()


# Generated: every break / format / look-alike character in a comment line of the manifest.
VECTORS += [
    (f"sidecar-char-U+{ord(ch):04X}", "sidecar", _sidecar_is(lambda s, c=ch: s.replace(b"# recorded", f"# rec{c}orded".encode("utf-8", "surrogatepass"))), True)
    for ch in _break_chars()
]
# Generated: every break / format character inside the recorded first-hop CELEX URI.
VECTORS += [
    (f"shape-uri-char-U+{ord(ch):04X}", "shape", _with(_I0 + ("request", "uri"), _V_URL.replace("R1689", f"R{ch}1689")), True)
    for ch in _break_chars()
]
# Structure: a cassette that recorded nothing never passes any check (F5).
for _check in _CHECKS:
    VECTORS += [
        (f"{_check}-not-mapping", _check, lambda: (["interactions"], _good_sidecar()), True),
        (f"{_check}-no-interactions-key", _check, lambda: ({"version": 1}, _good_sidecar()), True),
        (f"{_check}-empty-interactions", _check, lambda: ({"interactions": []}, _good_sidecar()), True),
        (f"{_check}-null-interactions", _check, lambda: ({"interactions": None}, _good_sidecar()), True),
        (f"{_check}-interaction-not-mapping", _check, lambda: ({"interactions": ["x"]}, _good_sidecar()), True),
        (f"{_check}-second-interaction-bad", _check, _with(("interactions", 1), None), True),
    ]


def test_vectors_have_allow_and_block_case_per_check() -> None:
    for check in _CHECKS:
        kinds = {blocked for _id, c, _b, blocked in VECTORS if c == check}
        assert kinds == {True, False}, f"check {check!r} needs an allow and a block vector"
    ids = [v[0] for v in VECTORS]
    assert len(ids) == len(set(ids)), "duplicate vector ids"


def test_vector_zip_is_shaped_like_a_recording() -> None:
    """Precondition for the body vectors: the 100 KiB bar is on the act, not on the zip."""
    body = _real_size_zip()
    assert len(body) < _MIN_RECORDED_BYTES
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        assert max(i.file_size for i in zf.infolist()) > _MIN_RECORDED_BYTES


@pytest.mark.parametrize(("check", "build", "blocked"), [v[1:] for v in VECTORS], ids=[v[0] for v in VECTORS])
def test_check_rejects_hostile_cassette(check: str, build: Vector, blocked: bool) -> None:
    data, sidecar = build()
    problems = _CHECKS[check](data, sidecar)
    assert bool(problems) is blocked, problems
    for msg in problems:
        assert msg.startswith(f"{_V_SOURCE}: "), msg
        assert msg.isascii() and msg.isprintable(), _quote(msg)
        assert len(msg) < 2 * (_QUOTE_CAP + len("...(truncated)")) + 200, len(msg)


def test_sidecar_line_format_is_pinned() -> None:
    """The manifest format is a contract with whoever records: pin it on a tiny zip."""
    act = formex_act(size=1024)
    body = ai_pkg(act=act, annexes=()).body()
    lines = _sidecar_lines(body)
    assert lines[0] == f"zip {hashlib.sha256(body).hexdigest()} {len(body)}"
    assert [line.split(" ")[1] for line in lines[1:]] == [
        "L_202401689EN.toc.fmx.xml",
        "L_202401689EN.doc.fmx.xml",
        "L_202401689EN.000101.fmx.xml",
    ]
    assert lines[3] == f"member L_202401689EN.000101.fmx.xml 1024 {hashlib.sha256(act).hexdigest()} ACT"
    assert [line.rsplit(" ", 1)[1] for line in lines[1:]] == ["PUBLICATION", "DOC", "ACT"]


def test_missing_cassette_message_names_source_without_absolute_path(tmp_path: Path) -> None:
    data, problems = _read_cassette(tmp_path / "eurlex" / "nope_fetch.yaml", "eurlex_dsa", tmp_path)
    assert data is None
    assert problems == [
        "eurlex_dsa: no cassette at eurlex/nope_fetch.yaml; record it from the live source "
        "(pytest tests/integration/test_cassettes_match_sources.py --record-mode=once)"
    ]
    assert str(tmp_path) not in problems[0]


def test_symlinked_cassette_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real.yaml"
    real.write_text(yaml.safe_dump(_good()), encoding="utf-8")
    link = tmp_path / "gdpr_fetch.yaml"
    link.symlink_to(real)
    data, problems = _read_cassette(link, "eurlex_gdpr", tmp_path)
    assert data is None
    assert problems == ["eurlex_gdpr: cassette gdpr_fetch.yaml is a symlink; commit the recorded file itself"]


@pytest.mark.parametrize("extra", [0, 1], ids=["at-cap", "over-cap"])
def test_cassette_file_size_cap(tmp_path: Path, extra: int) -> None:
    path = tmp_path / "big_fetch.yaml"
    text = "version: 1\n# "
    path.write_text(text + "x" * (_MAX_CASSETTE_BYTES + extra - len(text)), encoding="ascii")
    assert path.stat().st_size == _MAX_CASSETTE_BYTES + extra
    _data, problems = _read_cassette(path, "eurlex_dsa", tmp_path)
    if extra:
        assert problems == [f"eurlex_dsa: cassette big_fetch.yaml is {_MAX_CASSETTE_BYTES + 1} bytes, above {_MAX_CASSETTE_BYTES}"]
    else:
        assert problems == []


def test_sidecar_missing_and_symlink_are_refused(tmp_path: Path) -> None:
    sidecar, problems = _read_sidecar(tmp_path / "eurlex" / "gdpr_fetch.manifest.txt", "eurlex_gdpr", tmp_path)
    assert sidecar is None
    assert problems == ["eurlex_gdpr: no zip manifest at eurlex/gdpr_fetch.manifest.txt; write it when recording the cassette"]
    real = tmp_path / "real.txt"
    real.write_bytes(_good_sidecar())
    link = tmp_path / "gdpr_fetch.manifest.txt"
    link.symlink_to(real)
    sidecar, problems = _read_sidecar(link, "eurlex_gdpr", tmp_path)
    assert sidecar is None
    assert problems == ["eurlex_gdpr: zip manifest gdpr_fetch.manifest.txt is a symlink; commit the file itself"]


@pytest.mark.parametrize(
    "content",
    [b": : invalid\n", b"\xff\xfe not utf-8", b"interactions: [\n"],
    ids=["yaml-error", "invalid-utf8", "truncated"],
)
def test_unreadable_cassette_names_source(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "dsa_fetch.yaml"
    path.write_bytes(content)
    data, problems = _read_cassette(path, "eurlex_dsa", tmp_path)
    assert data is None
    assert len(problems) == 1 and problems[0].startswith("eurlex_dsa: cassette dsa_fetch.yaml is unreadable (")
    assert str(tmp_path) not in problems[0]


def test_good_vector_round_trips_through_yaml(tmp_path: Path) -> None:
    """Positive control: a well-formed recording passes every check after a YAML round trip."""
    path = tmp_path / "ok_fetch.yaml"
    path.write_text(yaml.safe_dump(_good()), encoding="utf-8")
    data, problems = _read_cassette(path, _V_SOURCE, tmp_path)
    assert problems == []
    assert isinstance(data["interactions"][1]["response"]["body"]["string"], bytes)
    for check, fn in _CHECKS.items():
        assert fn(data, _good_sidecar()) == [], check
