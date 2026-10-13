"""Acceptance tests for jennifer-mckinney/terms-analysis#96: the Cellar REST / Formex 4 fetcher.

EUR-Lex TXT/XML sits behind the AWS WAF, so the 2026-10-13 design-gate ruling moves
``EurLexFetcher`` to Cellar REST, which serves the act as a Formex 4 zip. These tests
pin that ruling as behaviour. Everything runs offline: zips are built here with
``zipfile``/``io`` and HTTP is answered by a fake transport.

Contract the coder implements (the names below are part of it):

``legal_corpus_ingester.fetchers.cellar``
  * ``CellarSettings``: immutable, int fields ``max_zip_bytes``, ``max_members``,
    ``max_member_bytes``, ``max_total_bytes`` and ``max_ratio`` (F13: caps are config),
    plus the two tunables ``timeout_seconds`` and ``rate_limit_rps`` (int or float, never
    bool; round 3, lead ruling on 565e280).
  * ``load_cellar_settings(path: Path | None = None) -> CellarSettings``. The default
    file is ``config/fetchers/cellar.yaml``: a mapping whose keys are exactly the field
    names, each cap a positive int, with ``max_member_bytes <= max_total_bytes``;
    ``0 < timeout_seconds <= 120`` and ``0.01 <= rate_limit_rps <= 0.5`` (finite). Anything
    else raises ``ValueError`` naming the key; a missing file raises
    ``FileNotFoundError``. No message carries an absolute path. The Cellar host, the
    ``/resource/celex/`` base and the redirect pattern are code constants (F3); the
    config never carries them.
  * ``read_formex_zip(body, celex_id, settings) -> tuple[FormexPart, ...]`` validates
    the zip in memory and returns the act, then its annexes, in the manifest's
    ``REF.PHYS`` order (never zip order). ``FormexPart`` has ``name``, ``root`` (the
    root tag) and ``data`` (the member bytes). Central-directory checks (count, names,
    duplicates, file type, method, flags, sizes) run before any member is read.
    Every refusal raises ``SchemaDriftError``.

``EurLexFetcher(settings: CellarSettings | None = None)``
  * GET ``https://publications.europa.eu/resource/celex/<id>`` with
    ``Accept: application/zip;mtype=fmx4``, ``Accept-Language: eng`` and a
    ``legal-corpus-ingester/<version>`` User-Agent; every request carries
    ``timeout_seconds`` for connect, read, write and pool.
  * Rate limit: ONE limiter shared by every Cellar request in the process (all hops, all
    ``fetch`` calls, all fetcher instances, across separate ``asyncio.run`` loops, which
    is how the orchestrator calls ``fetch``). Each request starts at least
    ``1 / rate_limit_rps`` seconds after the previous one, measured with
    ``time.monotonic()`` (looked up on the ``time`` module at call time) and waited out
    with ``asyncio.sleep``.
  * Redirects are handled by hand: exactly one 303 whose single ``Location`` is
    ``http(s)://publications.europa.eu/resource/cellar/<uuid>[.NNNN.NN]/zip``; the
    target is rewritten to https before it is fetched. Then one 200 with media type
    ``application/zip``, zip magic, no ``x-amzn-waf-action`` header, streamed under
    ``max_zip_bytes``, then ``read_formex_zip`` against the requested CELEX.
  * Exceptions: redirect, status, WAF, transport and 429-exhaustion refusals raise
    ``UpstreamNotFoundError``; payload refusals (content type, magic, size, zip
    contents, CELEX) raise ``SchemaDriftError``; a malformed CELEX string raises
    ``ValueError`` before any request. No HTML fallback.

Literals that are NOT config: the request URL, headers and 0.5 rps come from the ruling;
the Location pattern and the member-name pattern come from the security design (F3, F8).
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.metadata
import io
import math
import random
import re
import stat
import struct
import time
import unicodedata
import warnings
import zipfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
import yaml

from legal_corpus_ingester.cleaners.xml_akn import MAX_XML_BYTES
from legal_corpus_ingester.errors import SchemaDriftError, UpstreamNotFoundError
from legal_corpus_ingester.fetchers.eurlex import EurLexFetcher
from legal_corpus_ingester.sources.registry import load_all
from legal_corpus_ingester.types import FetchResult

_REPO = Path(__file__).resolve().parents[2]
_SOURCES_DIR = _REPO / "config" / "sources"
_DEFAULT_CONFIG = _REPO / "config" / "fetchers" / "cellar.yaml"
_CAP_FIELDS: tuple[str, ...] = (
    "max_zip_bytes",
    "max_members",
    "max_member_bytes",
    "max_total_bytes",
    "max_ratio",
)
# Round 3 (lead ruling on 565e280): the two tunables that moved from code to config.
_TUNABLE_FIELDS: tuple[str, ...] = ("timeout_seconds", "rate_limit_rps")
# Validation policy, not config values (these bound what the config may say):
# the ruling promises EUR-Lex at most 0.5 rps, so config may slow the fetcher down but
# never speed it up; below 0.01 rps (100 s per request) or above a 120 s timeout a
# typo would stall `refresh` for the five EU sources.
_TIMEOUT_MAX_SECONDS = 120
_RATE_MIN_RPS = 0.01

# Ruling literals (2026-10-13, #96 section).
CELEX_BASE = "https://publications.europa.eu/resource/celex/"
ACCEPT = "application/zip;mtype=fmx4"
ACCEPT_LANGUAGE = "eng"
WAF_HEADER = "x-amzn-waf-action"
RATE_RPS = 0.5
AI_ACT_CELEX = "32024R1689"
GDPR_CELEX = "02016R0679-20160504"
_UA_RE = re.compile(r"legal-corpus-ingester/(\S+)( \(\+https://\S+\))?")

# Bound for any refusal message (output safety: bounded, ASCII, printable).
_MSG_CAP = 500


def _cellar() -> ModuleType:
    """The module under test, imported lazily so a missing module fails each test on its own."""
    return importlib.import_module("legal_corpus_ingester.fetchers.cellar")


def _assert_safe_message(msg: str, *forbidden: str) -> None:
    assert msg, "refusal message is empty"
    assert msg.isascii() and msg.isprintable(), ascii(msg)[:300]
    assert len(msg) <= _MSG_CAP, len(msg)
    for text in forbidden:
        assert text not in msg, ascii(msg)[:300]


def _break_chars() -> list[str]:
    """Every line-break, Cf, Zl and Zp code point plus NUL and look-alike spaces (generated)."""
    chars = {"\x00", "\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", "　"}
    for cp in range(0x110000):
        ch = chr(cp)
        if unicodedata.category(ch) in {"Cf", "Zl", "Zp"}:
            chars.add(ch)
    return sorted(chars)


# ---------------------------------------------------------------------------
# synthetic Formex 4 packages (shapes mirror the AI Act and GDPR zips probed 2026-10-13)
# ---------------------------------------------------------------------------

_XSI = "http://www.w3.org/2001/XMLSchema-instance"
_SCHEMA = "http://formex.publications.europa.eu/schema/formex-06.02.1-20231031.xd"
_UUID = "0f0e0d0c-0b0a-4908-8706-050403020100"
_AI_PREFIX = "L_202401689EN"
AI_TOC = f"{_AI_PREFIX}.toc.fmx.xml"
AI_DOC = f"{_AI_PREFIX}.doc.fmx.xml"
AI_ACT = f"{_AI_PREFIX}.000101.fmx.xml"
AI_ANNEXES: tuple[str, ...] = (f"{_AI_PREFIX}.012401.fmx.xml", f"{_AI_PREFIX}.012601.fmx.xml")
GDPR_ACT = "CL2016R0679EN0000020.0001.xml"
GDPR_DOC = "CL2016R0679EN0000020.0001.doc.xml"
_DATE = (2024, 7, 12, 0, 0, 0)


def _filler(n: int, seed: int, char: str | None = None) -> str:
    """``n`` chars of statute-like filler. Random hex compresses about 2:1, like real text."""
    if char is not None:
        return char * n
    return random.Random(seed).randbytes(n // 2 + 1).hex()[:n]


def _xml(root: str, inner: str, *, attrs: str = "") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<{root} xmlns:xsi="{_XSI}"{attrs} xsi:noNamespaceSchemaLocation="{_SCHEMA}">'
        f"{inner}</{root}>"
    ).encode("utf-8")


def _no_doc(year: str, number: str) -> str:
    return (
        f'<NO.DOC FORMAT="YN" TYPE="OJ"><YEAR>{year}</YEAR>'
        f"<NO.CURRENT>{number}</NO.CURRENT></NO.DOC>"
    )


def _sized(root: str, head: str, tail: str, size: int, seed: int, char: str | None, attrs: str) -> bytes:
    """A ``root`` document of exactly ``size`` bytes: head + filler + tail."""
    fill = size - len(_xml(root, head + tail, attrs=attrs))
    if fill < 0:
        raise ValueError(f"size {size} is below the skeleton")
    data = _xml(root, head + _filler(fill, seed, char) + tail, attrs=attrs)
    assert len(data) == size
    return data


def formex_act(
    year: str = "2024",
    number: str = "1689",
    *,
    size: int = 4096,
    seed: int = 1,
    bib: str | None = None,
    article_extra: str = "",
    char: str | None = None,
) -> bytes:
    """A Formex ``ACT`` whose ``BIB.INSTANCE/NO.DOC`` carries YEAR and NO.CURRENT."""
    bib = _no_doc(year, number) if bib is None else bib
    head = (
        f"<BIB.INSTANCE>{bib}</BIB.INSTANCE><ENACTING.TERMS>"
        f'<ARTICLE IDENTIFIER="001"><TI.ART>Article 1</TI.ART>{article_extra}<ALINEA>'
    )
    tail = "</ALINEA></ARTICLE></ENACTING.TERMS>"
    return _sized("ACT", head, tail, size, seed, char, ' NNC="YES"')


def formex_cons(
    ref: str = "2016R0679",
    start: str = "20160504",
    *,
    size: int = 4096,
    seed: int = 2,
    info: str | None = None,
) -> bytes:
    """A consolidated ``CONS.ACT`` whose ``INFO.CONSLEG`` carries CONSLEG.REF and START.DATE."""
    if info is None:
        info = f'<INFO.CONSLEG CONSLEG.REF="{ref}" START.DATE="{start}" CONSLEG.DATE="20180523"/>'
    head = (
        f"{info}<CONS.DOC><BIB.INSTANCE.CONS>"
        '<NO.DOC FORMAT="YN" TYPE="OJ"><YEAR>2016</YEAR><NO.CURRENT>679</NO.CURRENT>'
        "<NO.CELEX>32016R0679</NO.CELEX></NO.DOC></BIB.INSTANCE.CONS>"
        "<ENACTING.TERMS><ARTICLE><ALINEA>"
    )
    tail = "</ALINEA></ARTICLE></ENACTING.TERMS></CONS.DOC>"
    return _sized("CONS.ACT", head, tail, size, seed, None, f' xmlns:fmx="{_XSI}"')


def formex_annex(n: int, *, size: int = 2048, seed: int = 3) -> bytes:
    head = (
        f"<BIB.INSTANCE>{_no_doc('2024', '1689')}</BIB.INSTANCE>"
        f"<TITLE><TI><P>ANNEX {n}</P></TI></TITLE><CONTENTS><P>"
    )
    return _sized("ANNEX", head, "</P></CONTENTS>", size, seed, None, "")


def formex_toc(names: list[str]) -> bytes:
    return _xml("PUBLICATION", "".join(f'<ITEM.PUB DOC.INSTANCE="{n}"/>' for n in names))


def formex_manifest(main: str | None, subs: list[str], *, main_extra: str = "", bib: str = "") -> bytes:
    """The ``DOC`` manifest: REF.PHYS under DOC.MAIN.PUB, then one DOC.SUB.PUB per sub."""
    ref = '<REF.PHYS FILE="{}" TYPE="DOC.XML"/>'
    main_el = f'<DOC.MAIN.PUB NO.SEQ="0001">{main_extra}{ref.format(main) if main else ""}</DOC.MAIN.PUB>'
    subs_el = "".join(
        f'<DOC.SUB.PUB NO.SEQ="0001.{i:04d}" TYPE="ANNEX">{ref.format(s)}</DOC.SUB.PUB>'
        for i, s in enumerate(subs, 1)
    )
    return _xml("DOC", f"<BIB.DOC>{bib}</BIB.DOC><FMX>{main_el}{subs_el}</FMX>")


@dataclass
class Member:
    name: str
    data: bytes
    compress_type: int = zipfile.ZIP_DEFLATED
    create_system: int = 0
    external_attr: int = 0


@dataclass
class Pkg:
    """A zip to build: members in ZIP order, and the parts expected back in REF.PHYS order."""

    celex: str
    members: list[Member]
    expected: list[tuple[str, str]]

    def body(self) -> bytes:
        return build_zip(self.members)

    def index(self, name: str) -> int:
        return [m.name for m in self.members].index(name)

    def with_data(self, name: str, data: bytes) -> Pkg:
        members = [replace(m, data=data) if m.name == name else m for m in self.members]
        return replace(self, members=members)


def build_zip(members: list[Member]) -> bytes:
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # duplicate-name vectors are deliberate
        with zipfile.ZipFile(buf, "w") as zf:
            for m in members:
                info = zipfile.ZipInfo(m.name, date_time=_DATE)
                info.compress_type = m.compress_type
                info.create_system = m.create_system
                info.external_attr = m.external_attr
                zf.writestr(info, m.data)
    return buf.getvalue()


def ai_pkg(
    celex: str = AI_ACT_CELEX,
    *,
    year: str = "2024",
    number: str = "1689",
    act: bytes | None = None,
    act_size: int = 4096,
    annexes: tuple[str, ...] = AI_ANNEXES,
    annex_size: int = 2048,
) -> Pkg:
    """AI Act shape: TOC, DOC manifest, ACT, ANNEXes. Annexes are zipped in REVERSE manifest order."""
    subs = list(annexes)
    act = formex_act(year, number, size=act_size) if act is None else act
    members = [
        Member(AI_TOC, formex_toc([AI_DOC, AI_ACT, *subs])),
        Member(AI_DOC, formex_manifest(AI_ACT, subs, bib=_no_doc(year, number))),
        Member(AI_ACT, act),
        *[Member(n, formex_annex(i, size=annex_size, seed=10 + i)) for i, n in reversed(list(enumerate(subs, 1)))],
    ]
    return Pkg(celex, members, [(AI_ACT, "ACT"), *[(n, "ANNEX") for n in subs]])


def gdpr_pkg(
    celex: str = GDPR_CELEX, *, ref: str = "2016R0679", start: str = "20160504", act_size: int = 4096
) -> Pkg:
    """GDPR consolidated shape: CONS.ACT plus a ``.doc.xml`` manifest, no TOC."""
    members = [
        Member(GDPR_ACT, formex_cons(ref, start, size=act_size)),
        Member(GDPR_DOC, formex_manifest(GDPR_ACT, [], main_extra="<NO.CELEX>32016R0679</NO.CELEX>")),
    ]
    return Pkg(celex, members, [(GDPR_ACT, "CONS.ACT")])


_CELEX_SHAPE = re.compile(r"([03])([0-9]{4})([A-Z])([0-9]{4})(?:-([0-9]{8}))?")


def pkg_for(celex: str, *, act_size: int = 4096) -> Pkg:
    """A valid package whose act identifier matches ``celex`` (base or consolidated)."""
    match = _CELEX_SHAPE.fullmatch(celex)
    assert match is not None, f"no synthetic package shape for CELEX {celex!r}"
    sector, year, kind, number, date = match.groups()
    if sector == "3" and date is None:
        return ai_pkg(celex, year=year, number=number, act_size=act_size)
    assert sector == "0" and date is not None, celex
    return gdpr_pkg(celex, ref=f"{year}{kind}{number}", start=date, act_size=act_size)


# Central-directory / local-header patching (zipfile's own struct layouts).
_CD = struct.Struct("<4s4B4HL2L5H2L")
_LH = struct.Struct("<4s2B4HL2L2H")
_EOCD = struct.Struct("<4s4H2LH")
CD_FLAGS, CD_CSIZE, CD_USIZE, CD_NAME_LEN, CD_OFFSET = 5, 10, 11, 12, 18
LH_FLAGS, LH_NAME_LEN = 3, 10


def _cd_offsets(body: bytes) -> list[int]:
    eocd = body.rfind(b"PK\x05\x06")
    fields = _EOCD.unpack_from(body, eocd)
    offset, count, out = fields[6], fields[4], []
    for _ in range(count):
        rec = _CD.unpack_from(body, offset)
        out.append(offset)
        offset += _CD.size + rec[12] + rec[13] + rec[14]
    return out


def patch_zip(
    body: bytes,
    index: int,
    *,
    cd: dict[int, int] | None = None,
    lh: dict[int, int] | None = None,
    name: bytes | None = None,
) -> bytes:
    """Rewrite fields of member ``index`` in the central directory and its local header."""
    out = bytearray(body)
    cd_off = _cd_offsets(body)[index]
    rec = list(_CD.unpack_from(out, cd_off))
    lh_off = rec[CD_OFFSET]
    lrec = list(_LH.unpack_from(out, lh_off))
    for key, value in (cd or {}).items():
        rec[key] = value
    for key, value in (lh or {}).items():
        lrec[key] = value
    _CD.pack_into(out, cd_off, *rec)
    _LH.pack_into(out, lh_off, *lrec)
    if name is not None:
        assert len(name) == rec[CD_NAME_LEN] == lrec[LH_NAME_LEN], "patched name must keep its length"
        out[cd_off + _CD.size : cd_off + _CD.size + len(name)] = name
        out[lh_off + _LH.size : lh_off + _LH.size + len(name)] = name
    return bytes(out)


def zip_facts(body: bytes) -> dict[str, Any]:
    """Measured facts of a synthetic (trusted) zip, used to place cap boundaries."""
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        infos = zf.infolist()
        sizes = [len(zf.read(i)) for i in infos]
    return {
        "zip": len(body),
        "members": len(infos),
        "member": max(sizes),
        "total": sum(sizes),
        "ratio": max(len_ / i.compress_size for len_, i in zip(sizes, infos)),
    }


def zip_members(body: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        return {i.filename: zf.read(i) for i in zf.infolist()}


# ---------------------------------------------------------------------------
# settings (F13: every cap comes from config/fetchers/cellar.yaml)
# ---------------------------------------------------------------------------


def _default_mapping() -> dict[str, Any]:
    data = yaml.safe_load(_DEFAULT_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "config/fetchers/cellar.yaml is not a mapping"
    return data


def settings_with(tmp_path: Path, **overrides: float) -> Any:
    """Load settings from a tmp copy of the default YAML with ``overrides`` applied."""
    path = tmp_path / f"cellar-{len(list(tmp_path.glob('cellar-*.yaml')))}.yaml"
    path.write_text(yaml.safe_dump({**_default_mapping(), **overrides}), encoding="utf-8")
    return _cellar().load_cellar_settings(path)


def test_default_config_loads_with_positive_int_caps() -> None:
    settings = _cellar().load_cellar_settings()
    for name in _CAP_FIELDS:
        value = getattr(settings, name)
        assert type(value) is int and value > 0, f"{name}={value!r}"
    assert settings.max_member_bytes <= settings.max_total_bytes
    # A member is later handed to an XML cleaner; never let the zip cap exceed the cleaner cap.
    assert settings.max_member_bytes <= MAX_XML_BYTES


def test_default_config_path_is_the_repo_file() -> None:
    cellar = _cellar()
    assert cellar.load_cellar_settings() == cellar.load_cellar_settings(_DEFAULT_CONFIG)
    assert set(_default_mapping()) == set(_CAP_FIELDS) | set(_TUNABLE_FIELDS)


def test_settings_are_immutable() -> None:
    settings = _cellar().load_cellar_settings()
    with pytest.raises((AttributeError, TypeError)):
        settings.max_members = 10**9


_BAD_VALUES: tuple[tuple[str, object], ...] = (
    ("zero", 0),
    ("negative", -1),
    ("bool", True),
    ("string", "1024"),
    ("float", 1.5),
    ("null", None),
)


def _tunable_bad_values() -> list[tuple[str, str, object]]:
    """(field, label, value) for the two tunables: type, sign, non-finite and out-of-bound."""
    common: list[tuple[str, object]] = [
        ("zero", 0),
        ("negative", -1),
        ("negative-float", -0.5),
        ("bool", True),
        ("string", "1"),
        ("null", None),
        ("list", [1]),
        ("nan", float("nan")),
        ("inf", float("inf")),
        ("negative-inf", float("-inf")),
    ]
    out = [(name, label, value) for name in _TUNABLE_FIELDS for label, value in common]
    out += [
        ("timeout_seconds", "above-max", _TIMEOUT_MAX_SECONDS + 1),
        ("timeout_seconds", "absurd", 10**9),
        ("rate_limit_rps", "above-ruling-rate", RATE_RPS + 0.01),
        ("rate_limit_rps", "absurd", 1000),
        ("rate_limit_rps", "below-min", _RATE_MIN_RPS * 0.99),
        ("rate_limit_rps", "near-zero", 1e-12),
    ]
    return out


def _config_vectors() -> list[tuple[str, str | None, Callable[[dict[str, Any]], str]]]:
    """(id, field it attacks or None, render(default mapping) -> YAML text)."""
    vectors: list[tuple[str, str | None, Callable[[dict[str, Any]], str]]] = []
    for name in _CAP_FIELDS:
        for label, value in _BAD_VALUES:
            vectors.append(
                (f"{name}-{label}", name, lambda d, n=name, v=value: yaml.safe_dump({**d, n: v}))
            )
        vectors.append(
            (f"{name}-missing", name, lambda d, n=name: yaml.safe_dump({k: v for k, v in d.items() if k != n}))
        )
    for name, label, value in _tunable_bad_values():
        vectors.append((f"{name}-{label}", name, lambda d, n=name, v=value: yaml.safe_dump({**d, n: v})))
    for name in _TUNABLE_FIELDS:
        vectors.append(
            (f"{name}-missing", name, lambda d, n=name: yaml.safe_dump({k: v for k, v in d.items() if k != n}))
        )
    vectors += [
        ("unknown-key", "max_retries", lambda d: yaml.safe_dump({**d, "max_retries": 3})),
        (
            "member-cap-above-total-cap",
            "max_member_bytes",
            lambda d: yaml.safe_dump({**d, "max_member_bytes": d["max_total_bytes"] + 1}),
        ),
        ("not-a-mapping", None, lambda d: yaml.safe_dump(list(d.items()))),
        ("empty-file", None, lambda d: ""),
        ("invalid-yaml", None, lambda d: "max_members: [1,\n"),
    ]
    return vectors


CONFIG_VECTORS = _config_vectors()


def test_config_vectors_cover_every_field() -> None:
    attacked = {f for _id, f, _r in CONFIG_VECTORS if f is not None}
    assert set(_CAP_FIELDS) | set(_TUNABLE_FIELDS) <= attacked
    ids = [v[0] for v in CONFIG_VECTORS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize(("attacked", "render"), [v[1:] for v in CONFIG_VECTORS], ids=[v[0] for v in CONFIG_VECTORS])
def test_bad_config_fails_closed_at_load(tmp_path: Path, attacked: str | None, render: Callable[[dict[str, Any]], str]) -> None:
    path = tmp_path / "cellar.yaml"
    path.write_text(render(_default_mapping()), encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        _cellar().load_cellar_settings(path)
    msg = str(excinfo.value)
    _assert_safe_message(msg, str(tmp_path))
    if attacked is not None:
        assert attacked in msg, f"message does not name the bad key {attacked!r}: {msg!r}"


def test_good_config_override_loads(tmp_path: Path) -> None:
    """Positive control for the vectors above: a valid override round-trips."""
    defaults = _default_mapping()
    settings = settings_with(tmp_path, max_members=defaults["max_members"] + 1)
    assert settings.max_members == defaults["max_members"] + 1


def test_missing_config_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _cellar().load_cellar_settings(tmp_path / "absent.yaml")


def _default_tunable(name: str) -> float:
    data = _default_mapping()
    assert name in data, f"config/fetchers/cellar.yaml has no {name} key (lead ruling: tunables are config)"
    return data[name]


def test_default_config_tunables_load_within_bounds() -> None:
    settings = _cellar().load_cellar_settings()
    for name in _TUNABLE_FIELDS:
        assert getattr(settings, name) == _default_tunable(name), name
    timeout, rate = settings.timeout_seconds, settings.rate_limit_rps
    assert type(timeout) in (int, float) and 0 < timeout <= _TIMEOUT_MAX_SECONDS, timeout
    assert type(rate) in (int, float) and _RATE_MIN_RPS <= rate <= RATE_RPS, rate


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("timeout_seconds", _TIMEOUT_MAX_SECONDS),
        ("timeout_seconds", 7.5),
        ("rate_limit_rps", RATE_RPS),
        ("rate_limit_rps", _RATE_MIN_RPS),
    ],
    ids=["timeout-at-max", "timeout-float", "rate-at-ruling-max", "rate-at-min"],
)
def test_tunable_values_at_the_bounds_load(tmp_path: Path, name: str, value: float) -> None:
    """Positive controls for the tunable vectors: the edges themselves are legal."""
    _default_tunable(name)
    assert getattr(settings_with(tmp_path, **{name: value}), name) == value


# F3: the SSRF allowlist (host, CELEX base, redirect pattern) stays in code. Config is
# operator-editable, so a key there would let a config edit widen where the fetcher goes.
_SSRF_KEY_RE = re.compile(r"host|url|uri|base|location|redirect|pattern|domain|scheme|origin|allow|endpoint", re.I)
_SSRF_KEYS: tuple[tuple[str, object], ...] = (
    ("cellar_host", "evil.test"),
    ("celex_base_url", "https://evil.test/resource/celex/"),
    ("base_url", "http://169.254.169.254/"),
    ("location_pattern", ".*"),
    ("redirect_pattern", "https?://.*"),
    ("allowed_hosts", ["evil.test"]),
)


def test_cellar_config_carries_no_host_url_or_redirect_keys() -> None:
    data = _default_mapping()
    assert [k for k in data if _SSRF_KEY_RE.search(str(k))] == []
    # Every value is a plain number: no string can smuggle a host or a pattern in.
    assert all(type(v) in (int, float) for v in data.values()), data


def test_ssrf_key_regex_catches_every_listed_key() -> None:
    """Contract for the static check above: each attack key below would trip it."""
    assert all(_SSRF_KEY_RE.search(key) for key, _ in _SSRF_KEYS)
    assert not any(_SSRF_KEY_RE.search(f) for f in (*_CAP_FIELDS, *_TUNABLE_FIELDS))


@pytest.mark.parametrize(("key", "value"), _SSRF_KEYS, ids=[k for k, _ in _SSRF_KEYS])
def test_config_cannot_widen_the_ssrf_allowlist(tmp_path: Path, key: str, value: object) -> None:
    path = tmp_path / "cellar.yaml"
    path.write_text(yaml.safe_dump({**_default_mapping(), key: value}), encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        _cellar().load_cellar_settings(path)
    msg = str(excinfo.value)
    _assert_safe_message(msg, str(tmp_path))
    assert key in msg, msg


# ---------------------------------------------------------------------------
# read_formex_zip: one validator for the fetcher, the cassettes and (later) the cleaner
# ---------------------------------------------------------------------------

_ALLOW = None  # expect value meaning "accepted"
_ANY = ""  # expect value meaning "refused, no keyword pinned"
_DOCTYPE_XXE = b'<!DOCTYPE ACT [<!ENTITY e SYSTEM "file:///etc/hostname">]>'
_LAUGHS = (
    b'<!DOCTYPE ACT [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
    b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>'
)
_SYMLINK_ATTR = (stat.S_IFLNK | 0o777) << 16
_REGULAR_ATTR = (stat.S_IFREG | 0o644) << 16
_DIR_ATTR = (stat.S_IFDIR | 0o755) << 16


def _after_decl(data: bytes, insert: bytes) -> bytes:
    decl_end = data.index(b"?>") + 2
    return data[:decl_end] + insert + data[decl_end:]


def _ai_act() -> bytes:
    return ai_pkg().members[2].data


def _extra(pkg: Pkg, *members: Member) -> Pkg:
    return replace(pkg, members=[*pkg.members, *members])


def _drop(pkg: Pkg, name: str) -> Pkg:
    return replace(pkg, members=[m for m in pkg.members if m.name != name])


def _set(pkg: Pkg, name: str, **changes: Any) -> Pkg:
    return replace(pkg, members=[replace(m, **changes) if m.name == name else m for m in pkg.members])


def _manifest_pkg(main: str | None, subs: list[str], *extra: Member) -> Pkg:
    pkg = ai_pkg().with_data(AI_DOC, formex_manifest(main, subs, bib=_no_doc("2024", "1689")))
    return _extra(pkg, *extra)


def _body(pkg: Pkg) -> Callable[[], tuple[bytes, str, list[tuple[str, str]] | None]]:
    return lambda: (pkg.body(), pkg.celex, None)


def _ok(pkg: Pkg) -> Callable[[], tuple[bytes, str, list[tuple[str, str]] | None]]:
    return lambda: (pkg.body(), pkg.celex, pkg.expected)


def _patched(pkg: Pkg, name: str, **kw: Any) -> Callable[[], tuple[bytes, str, None]]:
    return lambda: (patch_zip(pkg.body(), pkg.index(name), **kw), pkg.celex, None)


def _raw(body: bytes, celex: str = AI_ACT_CELEX) -> Callable[[], tuple[bytes, str, None]]:
    return lambda: (body, celex, None)


def _overlap() -> tuple[bytes, str, None]:
    pkg = ai_pkg()
    body = pkg.body()
    first = _CD.unpack_from(body, _cd_offsets(body)[pkg.index(AI_ANNEXES[0])])[CD_OFFSET]
    return patch_zip(body, pkg.index(AI_ANNEXES[1]), cd={CD_OFFSET: first}), pkg.celex, None


def _csize_lie() -> tuple[bytes, str, None]:
    pkg = ai_pkg()
    body = pkg.body()
    return patch_zip(body, pkg.index(AI_ACT), cd={CD_CSIZE: len(body) * 2}), pkg.celex, None


def _usize_lie() -> tuple[bytes, str, None]:
    pkg = ai_pkg()
    return patch_zip(pkg.body(), pkg.index(AI_ACT), cd={CD_USIZE: 100}, lh={9: 100}), pkg.celex, None


def _bad_utf8_name() -> tuple[bytes, str, None]:
    pkg = _extra(ai_pkg(), Member("zz.xml", formex_annex(9)))
    flags = 0x800
    return patch_zip(pkg.body(), len(pkg.members) - 1, cd={CD_FLAGS: flags}, lh={LH_FLAGS: flags}, name=b"\xff\xfe.xml"), pkg.celex, None


_NAME_VECTORS: tuple[tuple[str, str], ...] = (
    ("dotdot", "../x.xml"),
    ("absolute", "/abs.xml"),
    ("backslash", "a\\b.xml"),
    ("subdir", "dir/x.xml"),
    ("directory", "dir/"),
    ("drive", "C:x.xml"),
    ("not-xml", "x.pdf"),
    ("leading-dot", ".x.xml"),
    ("leading-dash", "-x.xml"),
    ("upper-ext", "x.XML"),
    ("over-max-length", "a" * 129 + ".xml"),
    ("trailing-space", "x.xml "),
    ("non-ascii", "é.xml"),
    ("fullwidth-dot", "x．xml"),
)

# (id, family, build, settings overrides(facts) or None, expect)
#   expect: _ALLOW, _ANY, or a case-insensitive regex the SchemaDriftError message must match.
ZIP_VECTORS: list[tuple[str, str, Callable[[], tuple[bytes, str, Any]], Any, str | None]] = [
    # --- accepted shapes (positive controls) ---
    ("ai-act-good", "zip", _ok(ai_pkg()), None, _ALLOW),
    ("gdpr-consolidated-good", "zip", _ok(gdpr_pkg()), None, _ALLOW),
    ("act-stored-method", "zip", _ok(_set(ai_pkg(), AI_ACT, compress_type=zipfile.ZIP_STORED)), None, _ALLOW),
    ("unix-regular-file-mode", "zip", _ok(_set(ai_pkg(), AI_ACT, create_system=3, external_attr=_REGULAR_ATTR)), None, _ALLOW),
    # --- zip container ---
    ("not-a-zip", "zip", _raw(b"<ACT/>"), None, "not a zip"),
    ("empty-body", "zip", _raw(b""), None, "not a zip"),
    ("html-body", "zip", _raw(b"<html><body>Request blocked</body></html>"), None, "not a zip"),
    ("truncated-zip", "zip", lambda: (ai_pkg().body()[:200], AI_ACT_CELEX, None), None, "not a zip"),
    ("bzip2-member", "zip", _body(_set(ai_pkg(), AI_ACT, compress_type=zipfile.ZIP_BZIP2)), None, "compression"),
    ("lzma-member", "zip", _body(_set(ai_pkg(), AI_ACT, compress_type=zipfile.ZIP_LZMA)), None, "compression"),
    ("encrypted-flag", "zip", _patched(ai_pkg(), AI_ACT, cd={CD_FLAGS: 0x1}, lh={LH_FLAGS: 0x1}), None, "encrypted"),
    ("compressed-size-lie", "zip", _csize_lie, None, "compressed size"),
    ("file-size-lie", "zip", _usize_lie, None, _ANY),
    ("overlapping-entries", "zip", _overlap, None, _ANY),
    ("invalid-utf8-name", "zip", _bad_utf8_name, None, _ANY),
    ("symlink-entry", "zip", _body(_set(ai_pkg(), AI_ANNEXES[0], create_system=3, external_attr=_SYMLINK_ATTR)), None, "symlink"),
    ("directory-mode-entry", "zip", _body(_set(ai_pkg(), AI_ANNEXES[0], create_system=3, external_attr=_DIR_ATTR)), None, _ANY),
    ("nested-zip-member", "zip", _body(ai_pkg().with_data(AI_ANNEXES[0], build_zip([Member("x.xml", b"<ACT/>")]))), None, "nested"),
    ("duplicate-name", "zip", _body(_extra(ai_pkg(), Member(AI_ACT, formex_act(number="1688")))), None, "duplicate"),
    ("duplicate-name-casefold", "zip", _body(_extra(ai_pkg(), Member(AI_ACT.lower(), formex_act(number="1688")))), None, "duplicate"),
    # --- member names (F8 pattern ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.xml$) ---
    ("name-at-max-length", "names", _ok(_extra(ai_pkg(), Member("a" * 128 + ".xml", formex_toc([])))), None, _ALLOW),
    *[
        (f"name-{label}", "names", _body(_extra(ai_pkg(), Member(name, formex_annex(9)))), None, "member name")
        for label, name in _NAME_VECTORS
    ],
    # --- member content ---
    ("act-utf8-bom", "content", _ok(ai_pkg(act=b"\xef\xbb\xbf" + _ai_act())), None, _ALLOW),
    ("member-not-xml", "content", _body(ai_pkg().with_data(AI_ANNEXES[0], b"%PDF-1.7\n" + b"x" * 64)), None, "not XML"),
    ("act-nul-byte", "content", _body(ai_pkg(act=_ai_act().replace(b"<ALINEA>", b"<ALINEA>\x00", 1))), None, "malformed"),
    ("act-invalid-utf8", "content", _body(ai_pkg(act=_ai_act().replace(b"<ALINEA>", b"<ALINEA>\xff", 1))), None, "malformed"),
    ("act-cesu-surrogate", "content", _body(ai_pkg(act=_ai_act().replace(b"<ALINEA>", b"<ALINEA>\xed\xa0\x80", 1))), None, "malformed"),
    ("act-truncated-xml", "content", _body(ai_pkg(act=_ai_act()[:-20])), None, "malformed"),
    ("act-doctype-xxe", "content", _body(ai_pkg(act=_after_decl(_ai_act(), _DOCTYPE_XXE))), None, "DOCTYPE"),
    ("act-billion-laughs", "content", _body(ai_pkg(act=_after_decl(_ai_act(), _LAUGHS))), None, "DOCTYPE"),
    ("manifest-doctype", "content", _body(ai_pkg().with_data(AI_DOC, _after_decl(ai_pkg().members[1].data, _DOCTYPE_XXE))), None, "DOCTYPE"),
    ("act-default-namespace", "content", _body(ai_pkg(act=_ai_act().replace(b"<ACT ", b'<ACT xmlns="urn:x" ', 1))), None, "namespace"),
    ("act-prefixed-root", "content", _body(ai_pkg(act=_ai_act().replace(b"<ACT ", b'<f:ACT xmlns:f="urn:x" ', 1).replace(b"</ACT>", b"</f:ACT>"))), None, "namespace"),
    # --- manifest and member selection (F5) ---
    ("zip-order-differs-from-manifest", "manifest", _ok(ai_pkg(annexes=(*AI_ANNEXES, f"{_AI_PREFIX}.012801.fmx.xml"))), None, _ALLOW),
    ("no-manifest", "manifest", _body(_drop(ai_pkg(), AI_DOC)), None, "manifest"),
    ("two-manifests", "manifest", _body(_extra(ai_pkg(), Member("second.doc.fmx.xml", formex_manifest(AI_ACT, list(AI_ANNEXES))))), None, "manifest"),
    ("manifest-without-refs", "manifest", _body(_manifest_pkg(None, [])), None, "manifest"),
    ("ref-to-missing-member", "manifest", _body(_manifest_pkg(AI_ACT, [*AI_ANNEXES, f"{_AI_PREFIX}.012801.fmx.xml"])), None, "missing"),
    ("ref-path-traversal", "manifest", _body(_manifest_pkg(AI_ACT, [*AI_ANNEXES, "../x.xml"])), None, "missing"),
    ("duplicate-ref", "manifest", _body(_manifest_pkg(AI_ACT, [*AI_ANNEXES, AI_ANNEXES[0]])), None, "duplicate"),
    ("unlisted-annex", "manifest", _body(_extra(ai_pkg(), Member(f"{_AI_PREFIX}.012801.fmx.xml", formex_annex(3)))), None, "unlisted"),
    ("unlisted-second-act", "manifest", _body(_extra(ai_pkg(), Member(f"{_AI_PREFIX}.000100.fmx.xml", formex_act()))), None, "unlisted"),
    ("toc-referenced", "manifest", _body(_manifest_pkg(AI_ACT, [*AI_ANNEXES, AI_TOC])), None, "root"),
    ("manifest-referenced", "manifest", _body(_manifest_pkg(AI_ACT, [*AI_ANNEXES, AI_DOC])), None, "root"),
    ("main-ref-is-annex", "manifest", _body(_manifest_pkg(AI_ANNEXES[0], [AI_ACT, AI_ANNEXES[1]])), None, "root"),
    ("sub-ref-is-act", "manifest", _body(_manifest_pkg(AI_ACT, [*AI_ANNEXES, "second.xml"], Member("second.xml", formex_act()))), None, "root"),
    # --- act identifier vs requested CELEX ---
    ("celex-number-without-leading-zero", "celex", _ok(ai_pkg("32016R0679", year="2016", number="679")), None, _ALLOW),
    ("celex-year-mismatch", "celex", _body(ai_pkg(year="2023")), None, "CELEX"),
    ("celex-number-mismatch", "celex", _body(ai_pkg(number="1688")), None, "CELEX"),
    ("celex-requested-other-act", "celex", _raw(ai_pkg().body(), "32024R1688"), None, "CELEX"),
    ("celex-arabic-indic-digits", "celex", _body(ai_pkg(number="١٦٨٩")), None, "CELEX"),
    ("celex-fullwidth-year", "celex", _body(ai_pkg(year="２０２４")), None, "CELEX"),
    ("celex-no-doc-missing", "celex", _body(ai_pkg(act=formex_act(bib=""))), None, "CELEX"),
    ("celex-no-doc-outside-bib", "celex", _body(ai_pkg(act=formex_act(bib="", article_extra=_no_doc("2024", "1689")))), None, "CELEX"),
    ("cons-ref-mismatch", "celex", _body(gdpr_pkg(ref="2016R0680")), None, "CELEX"),
    ("cons-start-date-mismatch", "celex", _body(gdpr_pkg(start="20180525")), None, "CELEX"),
    ("cons-info-missing", "celex", _body(replace(gdpr_pkg(), members=[Member(GDPR_ACT, formex_cons(info="")), gdpr_pkg().members[1]])), None, "CELEX"),
    ("act-for-consolidated-celex", "celex", _raw(ai_pkg(year="2016", number="0679").body(), GDPR_CELEX), None, "CELEX"),
    ("cons-for-base-celex", "celex", _raw(gdpr_pkg().body(), "32016R0679"), None, "CELEX"),
    # --- caps from config, at and over each boundary (F2) ---
    ("members-at-cap", "caps", _ok(ai_pkg()), lambda f: {"max_members": f["members"]}, _ALLOW),
    ("members-over-cap", "caps", _body(ai_pkg()), lambda f: {"max_members": f["members"] - 1}, "member count"),
    ("member-at-cap", "caps", _ok(ai_pkg()), lambda f: {"max_member_bytes": f["member"]}, _ALLOW),
    ("member-over-cap", "caps", _body(ai_pkg()), lambda f: {"max_member_bytes": f["member"] - 1}, "member size"),
    ("total-at-cap", "caps", _ok(ai_pkg()), lambda f: {"max_total_bytes": f["total"], "max_member_bytes": f["member"]}, _ALLOW),
    ("total-over-cap", "caps", _body(ai_pkg()), lambda f: {"max_total_bytes": f["total"] - 1, "max_member_bytes": f["member"]}, "total size"),
    ("zip-at-cap", "caps", _ok(ai_pkg()), lambda f: {"max_zip_bytes": f["zip"]}, _ALLOW),
    ("zip-over-cap", "caps", _body(ai_pkg()), lambda f: {"max_zip_bytes": f["zip"] - 1}, "too large"),
    ("ratio-at-ceiling", "caps", _ok(ai_pkg()), lambda f: {"max_ratio": math.ceil(f["ratio"])}, _ALLOW),
    ("ratio-over-cap", "caps", _body(ai_pkg()), lambda f: {"max_ratio": math.floor(f["ratio"])}, "ratio"),
]
# Generated: every break / format / look-alike character inside an otherwise valid name.
ZIP_VECTORS += [
    (f"name-char-U+{ord(ch):04X}", "names", _body(_extra(ai_pkg(), Member(f"a{ch}b.xml", formex_annex(9)))), None, "member name")
    for ch in _break_chars()
]


def test_zip_vectors_have_allow_and_block_case_per_family() -> None:
    families = {v[1] for v in ZIP_VECTORS}
    for family in families:
        kinds = {v[4] is _ALLOW for v in ZIP_VECTORS if v[1] == family}
        assert kinds == {True, False}, f"family {family!r} needs an allow and a block vector"
    ids = [v[0] for v in ZIP_VECTORS]
    assert len(ids) == len(set(ids)), "duplicate vector ids"


def test_ratio_vectors_straddle_a_non_integer_ratio() -> None:
    """Precondition for ratio-at-ceiling / ratio-over-cap: the floor really is below the ratio."""
    ratio = zip_facts(ai_pkg().body())["ratio"]
    assert 1 <= math.floor(ratio) < ratio < math.ceil(ratio), ratio


def test_zip_order_precondition_annexes_reversed() -> None:
    """The fixture zips annexes in reverse, so returning zip order cannot pass the allow vectors."""
    pkg = ai_pkg()
    zipped = [m.name for m in pkg.members if m.name in AI_ANNEXES]
    assert zipped == list(reversed(AI_ANNEXES))


@pytest.mark.parametrize(
    ("build", "overrides", "expect"),
    [v[2:] for v in ZIP_VECTORS],
    ids=[v[0] for v in ZIP_VECTORS],
)
def test_read_formex_zip(tmp_path: Path, build: Callable[[], tuple[bytes, str, Any]], overrides: Any, expect: str | None) -> None:
    cellar = _cellar()
    body, celex, expected = build()
    settings = settings_with(tmp_path, **overrides(zip_facts(body))) if overrides else cellar.load_cellar_settings()
    if expect is _ALLOW:
        parts = cellar.read_formex_zip(body, celex, settings)
        assert isinstance(parts, tuple)
        assert [(p.name, p.root) for p in parts] == expected
        members = zip_members(body)
        for part in parts:
            assert part.data == members[part.name], part.name
        return
    with pytest.raises(Exception) as excinfo:
        cellar.read_formex_zip(body, celex, settings)
    assert type(excinfo.value) is SchemaDriftError, repr(excinfo.value)[:300]
    msg = str(excinfo.value)
    _assert_safe_message(msg, str(tmp_path))
    if expect:
        assert re.search(expect, msg, re.IGNORECASE), msg


def test_zip_bomb_trips_the_default_ratio_cap() -> None:
    cellar = _cellar()
    settings = cellar.load_cellar_settings()
    size = min(1024 * 1024, settings.max_member_bytes)
    pkg = ai_pkg(act=formex_act(size=size, char="0"))
    body = pkg.body()
    facts = zip_facts(body)
    # Precondition: only the ratio cap can refuse this zip under the default config.
    assert facts["ratio"] > settings.max_ratio
    assert facts["member"] <= settings.max_member_bytes and facts["total"] <= settings.max_total_bytes
    assert facts["members"] <= settings.max_members and facts["zip"] <= settings.max_zip_bytes
    with pytest.raises(SchemaDriftError, match="(?i)ratio"):
        cellar.read_formex_zip(body, pkg.celex, settings)


def test_default_member_count_cap_plus_one_is_refused() -> None:
    cellar = _cellar()
    settings = cellar.load_cellar_settings()
    annexes = tuple(f"{_AI_PREFIX}.{n:06d}.fmx.xml" for n in range(settings.max_members + 1 - 3))
    over = ai_pkg(annexes=annexes, annex_size=600)
    assert len(over.members) == settings.max_members + 1
    with pytest.raises(SchemaDriftError, match="(?i)member count"):
        cellar.read_formex_zip(over.body(), over.celex, settings)
    at_cap = ai_pkg(annexes=annexes[:-1], annex_size=600)
    assert len(at_cap.members) == settings.max_members
    assert [p.name for p in cellar.read_formex_zip(at_cap.body(), at_cap.celex, settings)] == [
        AI_ACT,
        *annexes[:-1],
    ]


# ---------------------------------------------------------------------------
# EurLexFetcher against a fake transport (no network)
# ---------------------------------------------------------------------------

CELLAR_LOCATION = f"http://publications.europa.eu/resource/cellar/{_UUID}.0006.02/zip"
CELLAR_HTTPS = "https://" + CELLAR_LOCATION[len("http://") :]
ZIP_TYPE = "application/zip;charset=UTF-8"  # the Content-Type Cellar sent on 2026-10-13

Route = Callable[[httpx.Request], httpx.Response]


@dataclass
class FakeNet:
    routes: list[Route] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    sleeps: list[tuple[int, float]] = field(default_factory=list)


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> FakeNet:
    fake = FakeNet()

    async def handle(self: Any, request: httpx.Request) -> httpx.Response:
        fake.requests.append(request)
        index = len(fake.requests) - 1
        if index >= len(fake.routes):
            return httpx.Response(418, request=request)  # unexpected extra request
        return fake.routes[index](request)

    def handle_sync(self: Any, request: httpx.Request) -> httpx.Response:
        fake.requests.append(request)
        raise RuntimeError("the fetcher must use the async client")

    async def sleep(delay: float, *_a: object, **_k: object) -> None:
        fake.sleeps.append((len(fake.requests), float(delay)))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_sync)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    return fake


def redirect(location: str | bytes | None = CELLAR_LOCATION, status: int = 303, extra: tuple[tuple[bytes, bytes], ...] = ()) -> Route:
    def route(request: httpx.Request) -> httpx.Response:
        headers = list(extra)
        if location is not None:
            value = location if isinstance(location, bytes) else location.encode("utf-8")
            headers.append((b"location", value))
        return httpx.Response(status, headers=headers, request=request)

    return route


def zip_ok(
    body: bytes, content_type: str | None = ZIP_TYPE, status: int = 200, extra: tuple[tuple[bytes, bytes], ...] = ()
) -> Route:
    def route(request: httpx.Request) -> httpx.Response:
        headers = list(extra)
        if content_type is not None:
            headers.append((b"content-type", content_type.encode("ascii")))
        return httpx.Response(status, headers=headers, content=body, request=request)

    return route


def _run(target: object, settings: Any = None) -> FetchResult:
    fetcher = EurLexFetcher() if settings is None else EurLexFetcher(settings=settings)
    return asyncio.run(fetcher.fetch(target))


def _refused(target: object, expected: type[Exception], settings: Any = None) -> str:
    with pytest.raises(Exception) as excinfo:
        _run(target, settings)
    assert type(excinfo.value) is expected, repr(excinfo.value)[:300]
    msg = str(excinfo.value)
    _assert_safe_message(msg)
    return msg


def test_fetch_happy_path_request_shape_and_result(net: FakeNet) -> None:
    body = ai_pkg().body()
    net.routes = [redirect(), zip_ok(body)]
    result = _run(AI_ACT_CELEX)

    assert [str(r.url) for r in net.requests] == [CELEX_BASE + AI_ACT_CELEX, CELLAR_HTTPS]
    assert all(r.method == "GET" and r.url.scheme == "https" for r in net.requests)
    first = net.requests[0]
    assert first.headers.get_list("accept") == [ACCEPT]
    assert first.headers.get_list("accept-language") == [ACCEPT_LANGUAGE]
    version = importlib.metadata.version("legal-corpus-ingester")
    for req in net.requests:
        agents = req.headers.get_list("user-agent")
        assert len(agents) == 1, agents
        match = _UA_RE.fullmatch(agents[0])
        assert match is not None and match.group(1) == version, agents[0]
        assert "cookie" not in req.headers and "authorization" not in req.headers
        timeouts = req.extensions.get("timeout", {})
        assert timeouts and all(v is not None for v in timeouts.values()), timeouts

    assert result.mime_type == "application/zip"
    assert result.raw_bytes == body
    assert result.source_name == "eurlex"
    assert result.upstream_metadata["celex_id"] == AI_ACT_CELEX
    prov = result.provenance
    assert prov.source_url == CELLAR_HTTPS
    assert prov.upstream_version == AI_ACT_CELEX
    assert prov.content_sha256 == hashlib.sha256(body).hexdigest()
    assert prov.source_name == "eurlex"


def test_fetch_waits_for_the_rate_limit_between_hops(net: FakeNet) -> None:
    net.routes = [redirect(), zip_ok(ai_pkg().body())]
    fetcher = EurLexFetcher()
    asyncio.run(fetcher.fetch(AI_ACT_CELEX))
    assert fetcher._rate_limit_rps == RATE_RPS
    between = sum(delay for count, delay in net.sleeps if count == 1)
    # The fake sleep returns at once, so the wall time between hops is a few ms at most.
    assert between >= 1 / RATE_RPS - 0.25, net.sleeps


# --- one rate limiter for every Cellar request in the process (lead ruling, 565e280) ---


@dataclass
class VirtualClock:
    now: float
    stamps: list[float] = field(default_factory=list)


@pytest.fixture
def vclock(net: FakeNet, monkeypatch: pytest.MonkeyPatch) -> VirtualClock:
    """Virtual time: ``time.monotonic`` reads it and the fake ``asyncio.sleep`` advances
    it, so spacing is measured exactly and no test really sleeps. It starts at the real
    monotonic time, so limiter state from other tests is never far in the future."""
    clock = VirtualClock(now=time.monotonic())

    async def sleep(delay: float, *_a: object, **_k: object) -> None:
        net.sleeps.append((len(net.requests), float(delay)))
        if delay > 0:
            clock.now += float(delay)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(time, "monotonic", lambda: clock.now)
    return clock


def _stamped(clock: VirtualClock, *routes: Route) -> list[Route]:
    """Wrap each route so the virtual time of every request is recorded."""

    def wrap(route: Route) -> Route:
        def stamped(request: httpx.Request) -> httpx.Response:
            clock.stamps.append(clock.now)
            return route(request)

        return stamped

    return [wrap(r) for r in routes]


def _assert_spaced(clock: VirtualClock, rps: float, count: int) -> None:
    assert len(clock.stamps) == count, clock.stamps
    gaps = [b - a for a, b in zip(clock.stamps, clock.stamps[1:])]
    assert all(gap >= 1 / rps - 1e-9 for gap in gaps), f"gaps {gaps} under {1 / rps} s"


def _two_fetches(clock: VirtualClock, net: FakeNet, make: Callable[[], EurLexFetcher]) -> None:
    """Two fetches; ``make`` is called right before each one, so a second instance is
    built after the first fetch (a limiter reset in ``__init__`` cannot hide)."""
    body = ai_pkg().body()
    net.routes = _stamped(clock, redirect(), zip_ok(body), redirect(), zip_ok(body))
    for _ in range(2):
        # One asyncio.run per fetch, as Orchestrator.run does for each source.
        assert asyncio.run(make().fetch(AI_ACT_CELEX)).raw_bytes == body


def test_rate_limit_holds_across_consecutive_fetches(net: FakeNet, vclock: VirtualClock) -> None:
    fetcher = EurLexFetcher()
    _two_fetches(vclock, net, lambda: fetcher)
    _assert_spaced(vclock, _default_tunable("rate_limit_rps"), 4)


def test_rate_limit_is_shared_by_a_second_fetcher_instance(net: FakeNet, vclock: VirtualClock) -> None:
    _two_fetches(vclock, net, EurLexFetcher)
    _assert_spaced(vclock, _default_tunable("rate_limit_rps"), 4)


def test_configured_rate_sets_the_spacing(net: FakeNet, vclock: VirtualClock, tmp_path: Path) -> None:
    default = _default_tunable("rate_limit_rps")
    slower = max(default / 2, _RATE_MIN_RPS)
    assert slower != default
    settings = settings_with(tmp_path, rate_limit_rps=slower)
    _two_fetches(vclock, net, lambda: EurLexFetcher(settings=settings))
    _assert_spaced(vclock, slower, 4)


def test_429_retry_goes_through_the_shared_limiter(net: FakeNet, vclock: VirtualClock, tmp_path: Path) -> None:
    """A retry is a Cellar request too; with a slow configured rate the backoff alone is
    not enough spacing."""
    slower = max(_default_tunable("rate_limit_rps") / 4, _RATE_MIN_RPS)
    settings = settings_with(tmp_path, rate_limit_rps=slower)
    body = ai_pkg().body()
    net.routes = _stamped(vclock, zip_ok(b"", content_type="text/plain", status=429), redirect(), zip_ok(body))
    assert asyncio.run(EurLexFetcher(settings=settings).fetch(AI_ACT_CELEX)).raw_bytes == body
    _assert_spaced(vclock, slower, 3)


@pytest.mark.parametrize("override", [False, True], ids=["default-config", "tmp-config-halved"])
def test_configured_timeout_reaches_every_request(net: FakeNet, tmp_path: Path, override: bool) -> None:
    expected = _default_tunable("timeout_seconds")
    settings = None
    if override:
        expected = expected / 2
        settings = settings_with(tmp_path, timeout_seconds=expected)
    net.routes = [redirect(), zip_ok(ai_pkg().body())]
    _run(AI_ACT_CELEX, settings)
    assert len(net.requests) == 2
    for req in net.requests:
        timeouts = req.extensions.get("timeout", {})
        assert set(timeouts) == {"connect", "read", "write", "pool"}, timeouts
        assert all(v == expected for v in timeouts.values()), (expected, timeouts)


def _eurlex_sources() -> list[Any]:
    registry = load_all(_SOURCES_DIR)
    return [cfg for cfg in registry.values() if cfg.pipeline.fetcher == "fetchers.eurlex.EurLexFetcher"]


@pytest.mark.parametrize("cfg", _eurlex_sources(), ids=lambda c: c.name)
def test_fetch_requests_cellar_celex_url_for_each_source_config(net: FakeNet, cfg: Any) -> None:
    celex = cfg.pipeline.celex_id
    net.routes = [redirect(), zip_ok(pkg_for(celex).body())]
    result = _run(cfg)
    assert str(net.requests[0].url) == CELEX_BASE + celex
    assert result.provenance.upstream_version == celex


def test_ai_act_source_uses_base_celex() -> None:
    """Ruling: the consolidated AI Act id returns 406 from Cellar; the source uses the base act."""
    assert load_all(_SOURCES_DIR)["eurlex_ai_act"].pipeline.celex_id == AI_ACT_CELEX


# --- Location (F3) ---------------------------------------------------------------

_ALLOWED_LOCATIONS: tuple[tuple[str, str], ...] = (
    ("http-with-version", CELLAR_LOCATION),
    ("https-with-version", CELLAR_HTTPS),
    ("http-no-version", f"http://publications.europa.eu/resource/cellar/{_UUID}/zip"),
)


@pytest.mark.parametrize("location", [v[1] for v in _ALLOWED_LOCATIONS], ids=[v[0] for v in _ALLOWED_LOCATIONS])
def test_allowed_location_is_fetched_over_https(net: FakeNet, location: str) -> None:
    net.routes = [redirect(location), zip_ok(ai_pkg().body())]
    result = _run(AI_ACT_CELEX)
    expected = "https://" + location.split("://", 1)[1]
    assert [str(r.url) for r in net.requests] == [CELEX_BASE + AI_ACT_CELEX, expected]
    assert result.provenance.source_url == expected


_CELLAR_PATH = f"/resource/cellar/{_UUID}.0006.02/zip"
_BLOCKED_LOCATIONS: list[tuple[str, bytes]] = [
    (label, value.encode("utf-8"))
    for label, value in (
        ("evil-host", f"http://evil.test{_CELLAR_PATH}"),
        ("lookalike-suffix-host", f"http://publications.europa.eu.evil.test{_CELLAR_PATH}"),
        ("subdomain", f"http://www.publications.europa.eu{_CELLAR_PATH}"),
        ("eur-lex-host", f"https://eur-lex.europa.eu{_CELLAR_PATH}"),
        ("userinfo", f"http://u:p@publications.europa.eu{_CELLAR_PATH}"),
        ("userinfo-host-swap", f"http://publications.europa.eu@evil.test{_CELLAR_PATH}"),
        ("port", f"http://publications.europa.eu:8443{_CELLAR_PATH}"),
        ("query", f"{CELLAR_LOCATION}?x=1"),
        ("fragment", f"{CELLAR_LOCATION}#f"),
        ("trailing-slash", f"{CELLAR_LOCATION}/"),
        ("upper-zip", CELLAR_LOCATION[:-3] + "ZIP"),
        ("upper-uuid", CELLAR_LOCATION.replace(_UUID, _UUID.upper().replace("-", "-"))),
        ("celex-path", f"http://publications.europa.eu/resource/celex/{AI_ACT_CELEX}"),
        ("dotdot", f"http://publications.europa.eu/resource/cellar/../cellar/{_UUID}/zip"),
        ("percent-encoded", f"http://publications.europa.eu/resource/cellar/{_UUID}%2Fzip"),
        ("bad-version-suffix", f"http://publications.europa.eu/resource/cellar/{_UUID}.6.2/zip"),
        ("pdf", f"http://publications.europa.eu/resource/cellar/{_UUID}/pdf"),
        ("double-slash", f"http://publications.europa.eu//resource/cellar/{_UUID}/zip"),
        ("path-params", f"{CELLAR_LOCATION};x"),
        ("ftp", "ftp://publications.europa.eu" + _CELLAR_PATH),
        ("file", "file://" + _CELLAR_PATH),
        ("javascript", "javascript:alert(1)"),
        ("scheme-relative", "//publications.europa.eu" + _CELLAR_PATH),
        ("relative", _CELLAR_PATH),
        ("empty", ""),
        ("leading-space", " " + CELLAR_LOCATION),
        ("trailing-tab", CELLAR_LOCATION + "\t"),
        ("fullwidth-dot-host", f"http://publications．europa.eu{_CELLAR_PATH}"),
    )
] + [
    (f"char-U+{ord(ch):04X}", f"http://publications.europa.eu/resource/cellar/{_UUID}{ch}/zip".encode("utf-8"))
    for ch in _break_chars()
]


def test_location_vectors_have_allow_and_block_cases() -> None:
    assert _ALLOWED_LOCATIONS and _BLOCKED_LOCATIONS
    assert not {v for _l, v in _ALLOWED_LOCATIONS} & {v.decode("utf-8") for _l, v in _BLOCKED_LOCATIONS}
    assert all(v[1] != _UUID.upper() for v in _BLOCKED_LOCATIONS)


@pytest.mark.parametrize("location", [v[1] for v in _BLOCKED_LOCATIONS], ids=[v[0] for v in _BLOCKED_LOCATIONS])
def test_disallowed_location_is_never_followed(net: FakeNet, location: bytes) -> None:
    net.routes = [redirect(location), zip_ok(ai_pkg().body())]
    _refused(AI_ACT_CELEX, UpstreamNotFoundError)
    assert len(net.requests) == 1, [str(r.url) for r in net.requests]


_EVIL_LOCATION = f"http://evil.test{_CELLAR_PATH}".encode()
_OTHER_VALID = _ALLOWED_LOCATIONS[2][1].encode()
_TWO_LOCATIONS: tuple[tuple[str, bytes], ...] = (
    # Both orders, and two individually valid targets, so "take the first" and
    # "take the last" both follow something and go red; only refusing passes.
    ("evil-then-valid", _EVIL_LOCATION),
    ("two-valid-targets", _OTHER_VALID),
)


@pytest.mark.parametrize("first", [v[1] for v in _TWO_LOCATIONS], ids=[v[0] for v in _TWO_LOCATIONS])
def test_two_location_headers_are_refused(net: FakeNet, first: bytes) -> None:
    net.routes = [redirect(CELLAR_LOCATION, extra=((b"location", first),)), zip_ok(ai_pkg().body())]
    _refused(AI_ACT_CELEX, UpstreamNotFoundError)
    assert len(net.requests) == 1


def test_two_location_headers_valid_then_evil_are_refused(net: FakeNet) -> None:
    net.routes = [redirect(_EVIL_LOCATION, extra=((b"location", CELLAR_LOCATION.encode()),)), zip_ok(ai_pkg().body())]
    _refused(AI_ACT_CELEX, UpstreamNotFoundError)
    assert len(net.requests) == 1


def test_303_without_location_is_refused(net: FakeNet) -> None:
    net.routes = [redirect(None)]
    _refused(AI_ACT_CELEX, UpstreamNotFoundError)
    assert len(net.requests) == 1


# --- first-hop status (only one 303 is allowed) -------------------------------------

_WAF = ((WAF_HEADER.encode(), b"challenge"),)
_FIRST_HOP: tuple[tuple[str, Callable[[], Route]], ...] = (
    ("direct-200-zip", lambda: zip_ok(ai_pkg().body())),
    ("301", lambda: redirect(status=301)),
    ("302", lambda: redirect(status=302)),
    ("307", lambda: redirect(status=307)),
    ("308", lambda: redirect(status=308)),
    ("202-waf-challenge", lambda: zip_ok(b"", content_type="text/html", status=202, extra=_WAF)),
    ("303-with-waf-header", lambda: redirect(extra=_WAF)),
    ("404", lambda: zip_ok(b"", content_type="text/html", status=404)),
    ("406", lambda: zip_ok(b"", content_type="text/html", status=406)),
    ("500", lambda: zip_ok(b"", content_type="text/html", status=500)),
)


@pytest.mark.parametrize("route", [v[1] for v in _FIRST_HOP], ids=[v[0] for v in _FIRST_HOP])
def test_first_hop_other_than_one_303_is_refused(net: FakeNet, route: Callable[[], Route]) -> None:
    net.routes = [route(), zip_ok(ai_pkg().body())]
    _refused(AI_ACT_CELEX, UpstreamNotFoundError)
    assert len(net.requests) == 1, [str(r.url) for r in net.requests]


# --- second hop (the zip) (F4) --------------------------------------------------------

_SECOND_HOP: tuple[tuple[str, Callable[[], Route], type[Exception] | None], ...] = (
    ("zip-type-plain", lambda: zip_ok(ai_pkg().body(), content_type="application/zip"), None),
    ("zip-type-with-param", lambda: zip_ok(ai_pkg().body(), content_type="application/zip; charset=UTF-8"), None),
    ("second-303", lambda: redirect(CELLAR_LOCATION), UpstreamNotFoundError),
    ("302", lambda: redirect(CELLAR_LOCATION, status=302), UpstreamNotFoundError),
    ("202", lambda: zip_ok(b"", content_type="text/html", status=202), UpstreamNotFoundError),
    ("200-with-waf-header", lambda: zip_ok(ai_pkg().body(), extra=_WAF), UpstreamNotFoundError),
    ("text-html", lambda: zip_ok(ai_pkg().body(), content_type="text/html"), SchemaDriftError),
    ("application-xml", lambda: zip_ok(ai_pkg().body(), content_type="application/xml"), SchemaDriftError),
    ("zip-prefix-type", lambda: zip_ok(ai_pkg().body(), content_type="application/zipx"), SchemaDriftError),
    ("type-list", lambda: zip_ok(ai_pkg().body(), content_type="text/html, application/zip"), SchemaDriftError),
    ("x-zip-compressed", lambda: zip_ok(ai_pkg().body(), content_type="application/x-zip-compressed"), SchemaDriftError),
    ("no-content-type", lambda: zip_ok(ai_pkg().body(), content_type=None), SchemaDriftError),
    ("html-body-zip-type", lambda: zip_ok(b"<html>blocked</html>"), SchemaDriftError),
    ("empty-body", lambda: zip_ok(b""), SchemaDriftError),
    ("celex-mismatch", lambda: zip_ok(ai_pkg(number="1688").body()), SchemaDriftError),
)


def test_second_hop_vectors_have_allow_and_block_cases() -> None:
    assert {v[2] is None for v in _SECOND_HOP} == {True, False}


@pytest.mark.parametrize(("route", "expected"), [v[1:] for v in _SECOND_HOP], ids=[v[0] for v in _SECOND_HOP])
def test_second_hop(net: FakeNet, route: Callable[[], Route], expected: type[Exception] | None) -> None:
    net.routes = [redirect(), route()]
    if expected is None:
        assert _run(AI_ACT_CELEX).mime_type == "application/zip"
    else:
        _refused(AI_ACT_CELEX, expected)
    assert len(net.requests) == 2, [str(r.url) for r in net.requests]


# --- retries and transport errors --------------------------------------------------

def test_429_is_retried_then_succeeds(net: FakeNet) -> None:
    body = ai_pkg().body()
    net.routes = [zip_ok(b"", content_type="text/plain", status=429), redirect(), zip_ok(body)]
    assert _run(AI_ACT_CELEX).raw_bytes == body
    assert [str(r.url) for r in net.requests] == [CELEX_BASE + AI_ACT_CELEX] * 2 + [CELLAR_HTTPS]


def test_429_exhaustion_is_refused(net: FakeNet) -> None:
    retries = EurLexFetcher()._max_retries
    net.routes = [zip_ok(b"", content_type="text/plain", status=429)] * (retries + 1) + [redirect(), zip_ok(ai_pkg().body())]
    _refused(AI_ACT_CELEX, UpstreamNotFoundError)
    assert len(net.requests) == retries + 1


def _raiser(exc: type[httpx.TransportError]) -> Route:
    def route(request: httpx.Request) -> httpx.Response:
        raise exc("simulated", request=request)

    return route


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ReadTimeout], ids=["connect-error", "read-timeout"])
def test_transport_error_is_refused_without_html_fallback(net: FakeNet, exc: type[httpx.TransportError]) -> None:
    net.routes = [_raiser(exc)] * 8
    _refused(AI_ACT_CELEX, UpstreamNotFoundError)
    assert net.requests
    for req in net.requests:
        assert str(req.url) == CELEX_BASE + AI_ACT_CELEX, str(req.url)


# --- streamed byte cap (F2) --------------------------------------------------------

_CHUNK = 16 * 1024


class CountingStream(httpx.AsyncByteStream):
    """A zip-looking body of ``total`` bytes served in chunks; counts what the client pulled."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.pulled = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        sent = 0
        while sent < self.total:
            chunk = (b"PK\x03\x04" + b"\0" * (_CHUNK - 4)) if sent == 0 else b"\0" * _CHUNK
            sent += len(chunk)
            self.pulled += len(chunk)
            yield chunk

    async def aclose(self) -> None:
        return None


@pytest.mark.parametrize("declare_length", [False, True], ids=["chunked", "content-length"])
def test_download_is_capped_while_streaming(net: FakeNet, tmp_path: Path, declare_length: bool) -> None:
    cap = 16 * _CHUNK
    settings = settings_with(tmp_path, max_zip_bytes=cap)
    stream = CountingStream(4 * cap)
    headers = [(b"content-type", b"application/zip")]
    if declare_length:
        headers.append((b"content-length", str(stream.total).encode()))
    net.routes = [redirect(), lambda req: httpx.Response(200, headers=headers, stream=stream, request=req)]
    msg = _refused(AI_ACT_CELEX, SchemaDriftError, settings)
    assert re.search("too large", msg, re.IGNORECASE), msg
    assert stream.pulled <= cap + _CHUNK, f"pulled {stream.pulled} bytes past a {cap}-byte cap"


def test_download_at_the_cap_is_accepted(net: FakeNet, tmp_path: Path) -> None:
    body = ai_pkg().body()
    net.routes = [redirect(), zip_ok(body)]
    assert _run(AI_ACT_CELEX, settings_with(tmp_path, max_zip_bytes=len(body))).raw_bytes == body
    net.requests.clear()
    net.routes = [redirect(), zip_ok(body)]
    _refused(AI_ACT_CELEX, SchemaDriftError, settings_with(tmp_path, max_zip_bytes=len(body) - 1))


# --- the CELEX argument is validated before any request --------------------------


def _non_ascii_digit_celex() -> list[tuple[str, str]]:
    """One CELEX per Unicode decimal-digit script (category Nd, generated), e.g. fullwidth,
    Arabic-Indic and Devanagari. Python's ``\\d`` matches all of them unless ``re.ASCII`` is set.

    Each script yields two ids: every digit swapped, and one swapped digit in an otherwise
    ASCII id (a check that only looks at the first character is not enough).
    """
    out: list[tuple[str, str]] = []
    for cp in range(0x110000):
        ch = chr(cp)
        if cp == 0x30 or unicodedata.category(ch) != "Nd" or unicodedata.digit(ch) != 0:
            continue
        table = str.maketrans("0123456789", "".join(chr(cp + i) for i in range(10)))
        out.append((f"digits-U+{cp:04X}-all", "32024R1689".translate(table)))
        out.append((f"digits-U+{cp:04X}-one", "32024R168" + "9".translate(table)))
    return out


def test_non_ascii_digit_vectors_cover_named_scripts() -> None:
    """Contract: the generated table includes the scripts the lead named, and every entry
    really is a `\\d` match that `re.ASCII` refuses (so the vectors test what they claim)."""
    names = {v[0] for v in _BAD_CELEX}
    for zero in (0xFF10, 0x0660, 0x0966):  # fullwidth, Arabic-Indic, Devanagari
        assert f"digits-U+{zero:04X}-all" in names and f"digits-U+{zero:04X}-one" in names
    unicode_re = re.compile(r"\d{5}[A-Z]\d{4}")
    ascii_re = re.compile(r"\d{5}[A-Z]\d{4}", re.ASCII)
    for name, celex in _BAD_CELEX:
        if name.startswith("digits-U+"):
            assert unicode_re.fullmatch(celex) and not ascii_re.fullmatch(celex), name

_BAD_CELEX: list[tuple[str, str]] = [
    ("empty", ""),
    ("slash", "32024R1689/x"),
    ("dotdot", "../32024R1689"),
    ("query", "32024R1689?x=1"),
    ("fragment", "32024R1689#x"),
    ("percent", "32024R1689%2F"),
    ("space", "32024R 1689"),
    ("trailing-space", "32024R1689 "),
    ("url", "https://evil.test/x"),
    ("lone-surrogate", "32024R\ud8001689"),
    ("lone-low-surrogate-tail", "32024R1689\udc00"),
    # str.isdigit() accepts superscripts (category No) although `\d` does not.
    ("superscript-digits", "³²⁰²⁴R¹⁶⁸⁹"),
] + [(f"char-U+{ord(ch):04X}", f"32024R{ch}1689") for ch in _break_chars()] + _non_ascii_digit_celex()


@pytest.mark.parametrize("celex", [v[1] for v in _BAD_CELEX], ids=[v[0] for v in _BAD_CELEX])
def test_malformed_celex_is_refused_before_any_request(net: FakeNet, celex: str) -> None:
    net.routes = [redirect(), zip_ok(ai_pkg().body())]
    _refused(celex, ValueError)
    assert net.requests == []
