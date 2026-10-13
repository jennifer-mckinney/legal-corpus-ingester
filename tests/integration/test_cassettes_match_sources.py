"""Acceptance tests for terms-analysis#96: real EUR-Lex cassettes for the EU Tier-1 sources.

Each Tier-1 source YAML under ``config/sources/`` names a ``celex_id``. Its
cassette ``tests/fixtures/cassettes/eurlex/<name>_fetch.yaml`` must be a REAL
recording of the URI the fetcher requests for that source: status 200, an
Akoma Ntoso body of real-document size, and no credentials (this repo is public).

Single sources of truth (nothing is retyped here):
- source list entries and ``celex_id``: the source YAMLs, through ``sources.registry.load_all``;
- cassette directory, recording config and the sensitive-header fragments:
  ``tests/integration/test_eurlex_fetcher.py`` (re-exported below so a re-record
  from this module is scrubbed exactly like the existing one);
- the requested URI: produced by running the real ``EurLexFetcher`` with a fake transport;
- the AKN namespace and the XML size cap: ``cleaners.xml_akn``.

The replay tests come first in this file on purpose: under the drift canary's
``--record-mode=rewrite`` they re-record the cassettes, and the static checks
that follow then validate the fresh recording.
"""
from __future__ import annotations

import asyncio
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import yaml
from lxml import etree

from legal_corpus_ingester.cleaners.xml_akn import AKN_NS, MAX_XML_BYTES
from legal_corpus_ingester.fetchers.eurlex import EurLexFetcher
from legal_corpus_ingester.sources.registry import load_all
from legal_corpus_ingester.sources.schema import SourceConfig
from tests.integration import test_eurlex_fetcher as _eurlex_vcr

_REPO = Path(__file__).resolve().parents[2]
_SOURCES_DIR = _REPO / "config" / "sources"

# Card #96 scope: the EU Tier-1 sources, by their source-YAML ``name``. Their
# CELEX ids are NOT listed here; they are read from the YAMLs at test time.
TIER1_SOURCES: tuple[str, ...] = ("eurlex_gdpr", "eurlex_ai_act", "eurlex_dsa")

# Card #96 acceptance threshold: a recorded document body is > 100 KB. The
# synthetic GDPR fixture is ~1.5 KB; every real Tier-1 statute is far larger.
_MIN_RECORDED_BYTES = 100 * 1024

# Cassette file name = <source name without the "<cassette dir>_" prefix> + this.
_CASSETTE_SUFFIX = "_fetch"

# Recording config shared with the existing EUR-Lex VCR test (pytest fixtures).
vcr_config = _eurlex_vcr.vcr_config
vcr_cassette_dir = _eurlex_vcr.vcr_cassette_dir
_SENSITIVE_FRAGMENTS: tuple[str, ...] = _eurlex_vcr._SENSITIVE_RESPONSE_HEADER_FRAGMENTS

# How much of an untrusted value a problem message may quote.
_QUOTE_CAP = 160


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


def _fetcher_request_urls(cfg: SourceConfig) -> list[str]:
    """Run the real EurLexFetcher for ``cfg`` with a fake transport; return the URLs it requested."""
    seen: list[str] = []

    async def _fake_fetch(self: EurLexFetcher, url: str, **_kwargs: object) -> httpx.Response:
        seen.append(url)
        return httpx.Response(200, content=b"<x/>", request=httpx.Request("GET", url))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(EurLexFetcher, "_fetch_with_retry", _fake_fetch)
        asyncio.run(EurLexFetcher().fetch(cfg))
    return seen


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
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return None, [f"{source}: cassette {rel} is unreadable ({type(exc).__name__})"]
    return data, []


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


def _uri_problems(data: Any, source: str, expected_url: str) -> list[str]:
    items, problems = _interactions(data, source)
    for i, item in enumerate(items):
        req = item["request"]
        uri = req.get("uri")
        if req.get("method") != "GET":
            problems.append(f"{source}: interaction {i} method {_quote(req.get('method'))} is not GET")
        if uri != expected_url:
            problems.append(
                f"{source}: interaction {i} uri {_quote(uri)} is not the fetcher's "
                f"request {_quote(expected_url)}"
            )
    return problems


def _status_problems(data: Any, source: str) -> list[str]:
    items, problems = _interactions(data, source)
    for i, item in enumerate(items):
        status = item["response"].get("status")
        code = status.get("code") if isinstance(status, dict) else None
        # type() not isinstance(): True and 200.0 are not a recorded HTTP 200.
        if type(code) is not int or code != 200:
            problems.append(
                f"{source}: interaction {i} status {_quote(code)} is not 200 "
                "(202 with an empty body is the EUR-Lex WAF challenge, not the document)"
            )
    return problems


def _body_bytes(response: dict[str, Any], source: str, i: int) -> tuple[bytes | None, list[str]]:
    body = response.get("body")
    raw = body.get("string") if isinstance(body, dict) else None
    if isinstance(raw, bytes):
        return raw, []
    if isinstance(raw, str):
        try:
            return raw.encode("utf-8"), []
        except UnicodeEncodeError:
            return None, [f"{source}: interaction {i} body is not encodable as UTF-8"]
    return None, [f"{source}: interaction {i} has no recorded body string"]


def _body_problems(data: Any, source: str) -> list[str]:
    items, problems = _interactions(data, source)
    for i, item in enumerate(items):
        raw, errs = _body_bytes(item["response"], source, i)
        if raw is None:
            problems.extend(errs)
            continue
        size = len(raw)
        if size <= _MIN_RECORDED_BYTES:
            problems.append(
                f"{source}: interaction {i} body is {size} bytes, not > {_MIN_RECORDED_BYTES}; "
                "synthetic or truncated, not a recorded document"
            )
            continue
        if size > MAX_XML_BYTES:
            problems.append(
                f"{source}: interaction {i} body is {size} bytes, above the cleaner cap {MAX_XML_BYTES}"
            )
            continue
        # Same hardening as AKNXMLCleaner: no entities, no network, no huge tree.
        parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
        try:
            root = etree.fromstring(raw, parser)
        except etree.XMLSyntaxError as exc:
            problems.append(f"{source}: interaction {i} body is not well-formed XML ({_quote(str(exc))})")
            continue
        if root.getroottree().docinfo.doctype:
            problems.append(f"{source}: interaction {i} body declares a DOCTYPE; a recorded AKN document has none")
        namespace = etree.QName(root).namespace
        if namespace != AKN_NS:
            problems.append(
                f"{source}: interaction {i} root namespace {_quote(namespace)} is not the AKN namespace"
            )
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
    assert result.mime_type == "application/xml"
    assert result.provenance.upstream_version == cfg.pipeline.celex_id
    data, problems = _read_cassette(_cassette_path(cfg, cassette_dir), source, _REPO)
    if not problems:
        problems = _body_problems(data, source)
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
# positive control: the Tier-1 configs load and name a CELEX id the fetcher uses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", TIER1_SOURCES)
def test_tier1_source_config_loads_with_celex_id(
    source: str, registry: dict[str, SourceConfig], cassette_dir: Path
) -> None:
    assert source in registry, f"{source}: no such source in config/sources"
    celex_id = registry[source].pipeline.celex_id
    assert celex_id, f"{source}: pipeline.celex_id is empty"
    urls = _fetcher_request_urls(registry[source])
    assert len(urls) == 1 and celex_id in urls[0], f"{source}: fetcher requested {urls!r}"
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
def test_cassette_uri_matches_source_celex(
    source: str, registry: dict[str, SourceConfig], cassette_dir: Path
) -> None:
    cfg = registry[source]
    (expected_url,) = _fetcher_request_urls(cfg)
    data, problems = _read_cassette(_cassette_path(cfg, cassette_dir), source, _REPO)
    if not problems:
        problems = _uri_problems(data, source, expected_url)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("source", TIER1_SOURCES)
def test_cassette_is_recorded_not_synthetic(
    source: str, registry: dict[str, SourceConfig], cassette_dir: Path
) -> None:
    data, problems = _read_cassette(_cassette_path(registry[source], cassette_dir), source, _REPO)
    if not problems:
        problems = _body_problems(data, source)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("source", TIER1_SOURCES)
def test_cassette_status_is_200(
    source: str, registry: dict[str, SourceConfig], cassette_dir: Path
) -> None:
    data, problems = _read_cassette(_cassette_path(registry[source], cassette_dir), source, _REPO)
    if not problems:
        problems = _status_problems(data, source)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("source", TIER1_SOURCES)
def test_cassette_carries_no_credentials(
    source: str, registry: dict[str, SourceConfig], cassette_dir: Path
) -> None:
    data, problems = _read_cassette(_cassette_path(registry[source], cassette_dir), source, _REPO)
    if not problems:
        problems = _credential_problems(data, source)
    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------------------
# the checks above bite: vectors of crafted cassettes (allow and block per check)
# ---------------------------------------------------------------------------

_V_SOURCE = "vector_source"
_V_URL = "https://eur-lex.europa.eu/legal-content/EN/TXT/XML/?uri=CELEX:0VECTOR-20200101"


def _akn_body(size: int) -> str:
    """A well-formed AKN document of exactly ``size`` UTF-8 bytes.

    Text is split into many short <p> elements, like a real statute, so a
    cap-sized body stays under libxml2's per-text-node limit.
    """
    head = f'<akomaNtoso xmlns="{AKN_NS}"><act><body><p>'
    tail = "</p></body></act></akomaNtoso>"
    unit = "x" * 993 + "</p><p>"
    fill = size - len(head) - len(tail)
    return head + unit * (fill // len(unit)) + "x" * (fill % len(unit)) + tail


def _good() -> dict[str, Any]:
    return {
        "interactions": [
            {
                "request": {"method": "GET", "uri": _V_URL, "headers": {"Accept": ["application/xml"]}},
                "response": {
                    "status": {"code": 200, "message": "OK"},
                    "headers": {"Content-Type": ["application/xml"]},
                    "body": {"string": _akn_body(_MIN_RECORDED_BYTES + 1)},
                },
            }
        ],
        "version": 1,
    }


def _with(path: tuple[Any, ...], value: Any) -> Callable[[], Any]:
    def build() -> Any:
        data = _good()
        node: Any = data
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        return data

    return build


_REQ = ("interactions", 0, "request")
_RESP = ("interactions", 0, "response")
_CHECKS: dict[str, Callable[[Any], list[str]]] = {
    "uri": lambda d: _uri_problems(d, _V_SOURCE, _V_URL),
    "status": lambda d: _status_problems(d, _V_SOURCE),
    "body": lambda d: _body_problems(d, _V_SOURCE),
    "credentials": lambda d: _credential_problems(d, _V_SOURCE),
}
_XXE = (
    '<!DOCTYPE a [<!ENTITY e SYSTEM "file:///etc/hostname">]>'
    f'<akomaNtoso xmlns="{AKN_NS}"><p>&e;' + "x" * _MIN_RECORDED_BYTES + "</p></akomaNtoso>"
)

# (id, check, build, blocked)
VECTORS: list[tuple[str, str, Callable[[], Any], bool]] = [
    ("uri-exact", "uri", _good, False),
    ("uri-synthetic-celex", "uri", _with(_REQ + ("uri",), _V_URL.replace("0VECTOR-20200101", "3VECTOR")), True),
    ("uri-celex-suffix-forgery", "uri", _with(_REQ + ("uri",), _V_URL + "1"), True),
    ("uri-extra-param", "uri", _with(_REQ + ("uri",), _V_URL + "&uri=CELEX:evil"), True),
    ("uri-lookalike-host", "uri", _with(_REQ + ("uri",), _V_URL.replace("europa.eu", "europa.eu.evil.test")), True),
    ("uri-http-scheme", "uri", _with(_REQ + ("uri",), _V_URL.replace("https:", "http:")), True),
    ("uri-html-fallback", "uri", _with(_REQ + ("uri",), _V_URL.replace("/XML/", "/HTML/")), True),
    ("uri-missing", "uri", _with(_REQ + ("uri",), None), True),
    ("uri-post", "uri", _with(_REQ + ("method",), "POST"), True),
    ("status-200", "status", _good, False),
    ("status-202-waf-challenge", "status", _with(_RESP + ("status",), {"code": 202, "message": "Accepted"}), True),
    ("status-302", "status", _with(_RESP + ("status",), {"code": 302, "message": "Found"}), True),
    ("status-string", "status", _with(_RESP + ("status",), {"code": "200"}), True),
    ("status-bool", "status", _with(_RESP + ("status",), {"code": True}), True),
    ("status-float", "status", _with(_RESP + ("status",), {"code": 200.0}), True),
    ("status-missing", "status", _with(_RESP + ("status",), None), True),
    ("body-real-size", "body", _good, False),
    ("body-bytes", "body", _with(_RESP + ("body",), {"string": _akn_body(_MIN_RECORDED_BYTES + 1).encode()}), False),
    ("body-at-threshold", "body", _with(_RESP + ("body",), {"string": _akn_body(_MIN_RECORDED_BYTES)}), True),
    ("body-synthetic-1k", "body", _with(_RESP + ("body",), {"string": _akn_body(1024)}), True),
    ("body-empty-waf", "body", _with(_RESP + ("body",), {"string": ""}), True),
    ("body-at-cleaner-cap", "body", _with(_RESP + ("body",), {"string": _akn_body(MAX_XML_BYTES)}), False),
    ("body-over-cleaner-cap", "body", _with(_RESP + ("body",), {"string": _akn_body(MAX_XML_BYTES + 1)}), True),
    ("body-html-not-akn", "body", _with(_RESP + ("body",), {"string": "<html>" + "x" * _MIN_RECORDED_BYTES + "</html>"}), True),
    ("body-other-namespace", "body", _with(_RESP + ("body",), {"string": _akn_body(_MIN_RECORDED_BYTES + 1).replace(AKN_NS, AKN_NS + "x")}), True),
    ("body-namespace-in-text-only", "body", _with(_RESP + ("body",), {"string": f"<doc>{AKN_NS}" + "x" * _MIN_RECORDED_BYTES + "</doc>"}), True),
    ("body-doctype-xxe", "body", _with(_RESP + ("body",), {"string": _XXE}), True),
    ("body-nul", "body", _with(_RESP + ("body",), {"string": _akn_body(_MIN_RECORDED_BYTES + 1).replace("x", "\x00", 1)}), True),
    ("body-invalid-utf8", "body", _with(_RESP + ("body",), {"string": b"\xff\xfe" + _akn_body(_MIN_RECORDED_BYTES + 1).encode()}), True),
    ("body-lone-surrogate", "body", _with(_RESP + ("body",), {"string": _akn_body(_MIN_RECORDED_BYTES + 1).replace("x", "\ud800", 1)}), True),
    ("body-missing", "body", _with(_RESP + ("body",), None), True),
    ("creds-clean", "credentials", _good, False),
    ("creds-set-cookie", "credentials", _with(_RESP + ("headers",), {"Set-Cookie": ["aws-waf-token=t"]}), True),
    ("creds-set-cookie-lower", "credentials", _with(_RESP + ("headers",), {"set-cookie": ["s=1"]}), True),
    ("creds-request-cookie", "credentials", _with(_REQ + ("headers",), {"Cookie": ["s=1"]}), True),
    ("creds-authorization", "credentials", _with(_REQ + ("headers",), {"Authorization": ["Bearer t"]}), True),
    ("creds-www-authenticate", "credentials", _with(_RESP + ("headers",), {"WWW-Authenticate": ["Basic"]}), True),
    ("creds-auth-token", "credentials", _with(_RESP + ("headers",), {"X-Auth-Token": ["t"]}), True),
    ("creds-uri-userinfo", "credentials", _with(_REQ + ("uri",), _V_URL.replace("https://", "https://u:p@")), True),
    ("creds-headers-not-mapping", "credentials", _with(_RESP + ("headers",), ["Set-Cookie: s=1"]), True),
]
# Structure: a cassette that recorded nothing never passes any check (F5).
for _check in _CHECKS:
    VECTORS += [
        (f"{_check}-not-mapping", _check, lambda: ["interactions"], True),
        (f"{_check}-no-interactions-key", _check, lambda: {"version": 1}, True),
        (f"{_check}-empty-interactions", _check, lambda: {"interactions": []}, True),
        (f"{_check}-null-interactions", _check, lambda: {"interactions": None}, True),
        (f"{_check}-interaction-not-mapping", _check, lambda: {"interactions": ["x"]}, True),
        (f"{_check}-second-interaction-bad", _check, _with(("interactions",), _good()["interactions"] + [None]), True),
    ]


def test_vectors_have_allow_and_block_case_per_check() -> None:
    for check in _CHECKS:
        kinds = {blocked for _id, c, _b, blocked in VECTORS if c == check}
        assert kinds == {True, False}, f"check {check!r} needs an allow and a block vector"
    ids = [v[0] for v in VECTORS]
    assert len(ids) == len(set(ids)), "duplicate vector ids"


@pytest.mark.parametrize(("check", "build", "blocked"), [v[1:] for v in VECTORS], ids=[v[0] for v in VECTORS])
def test_check_rejects_hostile_cassette(check: str, build: Callable[[], Any], blocked: bool) -> None:
    problems = _CHECKS[check](build())
    assert bool(problems) is blocked, problems
    for msg in problems:
        assert msg.startswith(f"{_V_SOURCE}: "), msg
        assert msg.isascii() and msg.isprintable(), _quote(msg)
        assert len(msg) < 2 * (_QUOTE_CAP + len("...(truncated)")) + 200, len(msg)


def _break_chars() -> list[str]:
    """Every line-break and Cf code point, plus NUL (generated, not listed)."""
    chars = {"\x00", "\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "}
    for cp in range(0x110000):
        ch = chr(cp)
        if unicodedata.category(ch) in {"Cf", "Zl", "Zp"}:
            chars.add(ch)
    return sorted(chars)


def test_uri_check_rejects_every_break_and_format_char_in_celex() -> None:
    chars = _break_chars()
    print(f"generated {len(chars)} hostile uri variants")
    passed = []
    for ch in chars:
        forged = _V_URL.replace("0VECTOR-", f"0VECTOR{ch}-")
        found = _uri_problems(_with(_REQ + ("uri",), forged)(), _V_SOURCE, _V_URL)
        if not found:
            passed.append(f"U+{ord(ch):04X}")
        assert all(m.isascii() and m.isprintable() for m in found), _quote(found)
    assert not passed, f"forged CELEX ids accepted: {passed}"


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
    """Positive control: a well-formed recording passes every check after load."""
    path = tmp_path / "ok_fetch.yaml"
    path.write_text(yaml.safe_dump(_good()), encoding="utf-8")
    data, problems = _read_cassette(path, _V_SOURCE, tmp_path)
    assert problems == []
    for check, fn in _CHECKS.items():
        assert fn(data) == [], check
