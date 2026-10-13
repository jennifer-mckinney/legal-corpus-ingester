"""Acceptance tests for card #95: AKN cleaner depth.

The card (terms-analysis issue #95, gate G2) says `AKNXMLCleaner` walks only
`akn:section` and takes `akn:p.text`. That silently drops inline children
(`akn:ref`, `akn:i`), their tail text, `akn:article` bodies that sit outside a
section, and annexes (`akn:attachment`). A citation into dropped text fails DA1.

The first three tests are the card's test spec, run against the hand-written
fixture `tests/fixtures/corpus/akn_depth_sample.xml`. The rest cover the
DEV-FUNDAMENTALS (F1-F10) and QUALITY-BAR attack list A for the new walk:
  * the cleaner's output is a structured format (`## heading` lines, read by
    the sectioned chunker's SECTION_RE and by chunk_text's str.splitlines()),
    so deeper extraction must not let body text forge a heading (F2, F3);
  * encoding attacks, huge, many and deep inputs, entity and XInclude
    indirection must fail closed or stay inert (F4, F6, F7);
  * a document with no extractable AKN text is an error, not an empty
    success (F5);
  * error messages carry no raw control bytes, body text or paths (F8).

Single sources of truth (F10, F13): AKN_NS and MAX_XML_BYTES come from the
cleaner module, and SECTION_RE (the heading marker) from the sectioned
chunker. Nothing here restates them. Hostile character sets are generated
from Unicode properties, not listed (QUALITY-BAR R2).
"""
from __future__ import annotations

import sys
import unicodedata
from collections.abc import Callable
from pathlib import Path

import pytest
from lxml import etree

from legal_corpus_ingester.chunkers.factory import ChunkerFactory
from legal_corpus_ingester.chunkers.sectioned import SECTION_RE
from legal_corpus_ingester.cleaners.xml_akn import AKN_NS, MAX_XML_BYTES, AKNXMLCleaner
from legal_corpus_ingester.errors import SchemaDriftError
from legal_corpus_ingester.types import CleanedDocument, FetchResult, ProvenanceRecord

_PROV = ProvenanceRecord(
    source_url="https://eur-lex.europa.eu/eli/reg/2016/679/oj",
    fetch_timestamp="2026-07-04T00:00:00Z",
    license="CC-BY-4.0",
    upstream_version="32016R0679",
    content_sha256="c" * 64,
    source_name="eurlex",
    fetcher_class="fetchers.eurlex.EurLexFetcher",
)

_CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "corpus"
DEPTH_FIXTURE = _CORPUS / "akn_depth_sample.xml"
SHALLOW_FIXTURE = _CORPUS / "gdpr_akn_sample.xml"

# "Failed closed" on malformed bytes: lxml's parse error today, or the
# project's SchemaDriftError if the cleaner chooses to wrap it. Either way no
# CleanedDocument comes back.
_FAIL_CLOSED = (etree.XMLSyntaxError, SchemaDriftError)

# Unicode categories that must never reach an error message raw (F8).
_UNSAFE_MESSAGE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})

# A genuine section present in every synthetic document, so each one has
# real text and one real heading.
_GENUINE = (
    '<section eId="sec_g"><heading>Genuine heading</heading>'
    "<content><p>genuine body text</p></content></section>"
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _clean(raw: bytes) -> CleanedDocument:
    return AKNXMLCleaner().clean(
        FetchResult(
            source_name=_PROV.source_name,
            raw_bytes=raw,
            mime_type="application/xml",
            upstream_metadata={},
            provenance=_PROV,
        )
    )


def _akn(body: str, attachments: str = "", meta: str = "", ns: str | None = AKN_NS) -> bytes:
    """Wrap body markup in a minimal act (default namespace, as EUR-Lex serves it)."""
    xmlns = f' xmlns="{ns}"' if ns is not None else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<akomaNtoso{xmlns}><act name="regulation">{meta}<body>{body}</body>'
        f"{attachments}</act></akomaNtoso>"
    ).encode("utf-8")


def _annex(inner: str) -> str:
    return (
        '<attachments><attachment eId="att_1"><doc name="annex">'
        f"<mainBody>{inner}</mainBody></doc></attachment></attachments>"
    )


def _heading_titles(text: str) -> list[str]:
    """Heading titles as the chunkers see them.

    SECTION_RE is the sectioned chunker's own marker. Lines are split with
    str.splitlines() because chunk_text splits on every boundary it honours
    (U+2028, U+0085 and so on), not just on '\\n'.
    """
    titles: list[str] = []
    for line in text.splitlines():
        match = SECTION_RE.match(line)
        if match:
            titles.append(match.group(1).strip())
    return titles


def _unsafe_chars(message: str) -> list[str]:
    return [c for c in message if unicodedata.category(c) in _UNSAFE_MESSAGE_CATEGORIES]


def _assert_message_safe(exc: BaseException, *must_not_contain: str) -> None:
    message = str(exc)
    unsafe = _unsafe_chars(message)
    if isinstance(exc, etree.XMLSyntaxError):
        # libxml2 formats its own messages as "<reason>\n, line L, column C".
        # That one newline is the library's, not the document's, so it is
        # tolerated here; the hostile character itself is checked by callers.
        unsafe = [c for c in unsafe if c != "\n"]
    assert unsafe == [], f"raw control/format chars in message: {message!r}"
    assert str(Path.home()) not in message, "home path leaked into the error message"
    for needle in must_not_contain:
        assert needle not in message, f"document text {needle!r} echoed in message: {message!r}"


def _xml_escape(text: str) -> str:
    """Escape every non-printable-ASCII character as a numeric char ref.

    A char ref keeps the exact code point: a literal CR would be normalised
    to LF by the parser, &#xD; survives as CR.
    """
    out = []
    for c in text:
        if c.isascii() and c.isprintable() and c not in "<>&\"'":
            out.append(c)
        else:
            out.append(f"&#x{ord(c):X};")
    return "".join(out)


def _xml_legal(c: str) -> bool:
    """Ask the XML parser itself whether the code point is a legal XML Char."""
    try:
        etree.fromstring(f"<a>&#x{ord(c):X};</a>".encode("ascii"))
    except etree.XMLSyntaxError:
        return False
    return True


def _generate() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """One pass over every code point: line breaks, whitespace, Cf, and Cc/Cs."""
    breaks: list[str] = []
    spaces: list[str] = []
    formats: list[str] = []
    controls: list[str] = []
    for cp in range(sys.maxunicode + 1):
        c = chr(cp)
        if len(f"a{c}b".splitlines()) == 2:
            breaks.append(c)
        if c.isspace():
            spaces.append(c)
        category = unicodedata.category(c)
        if category == "Cf":
            formats.append(c)
        elif category in ("Cc", "Cs"):
            controls.append(c)
    return tuple(breaks), tuple(spaces), tuple(formats), tuple(controls)


_BREAKS, _SPACES, _FORMATS, _CONTROLS = _generate()
LINE_BREAKS = tuple(c for c in _BREAKS if _xml_legal(c)) + ("\r\n",)
WHITESPACE = tuple(c for c in _SPACES if _xml_legal(c))
FORMAT_CHARS = tuple(c for c in _FORMATS if _xml_legal(c))
XML_ILLEGAL = tuple(
    c for c in dict.fromkeys(_BREAKS + _SPACES + _CONTROLS + ("￾", "￿"))
    if not _xml_legal(c)
)


# --------------------------------------------------------------------------
# card test spec (#95): the hand-written depth fixture
# --------------------------------------------------------------------------

_POINT_ORDER = (
    "list-intro-sentinel",
    "alpha-point-sentinel",
    "alpha-one-subpoint-sentinel",
    "alpha-two-subpoint-sentinel",
    "bravo-point-sentinel",
    "list-wrapup-sentinel",
)
_DEPTH_SENTINELS = ("Data under Art. 6 lawful basis", *_POINT_ORDER, "ANNEX I sentinel")


def _depth_text() -> str:
    return _clean(DEPTH_FIXTURE.read_bytes()).text


def test_inline_and_tail_text_kept() -> None:
    text = _depth_text()
    assert "Data under Art. 6 lawful basis" in text, f"inline ref or its tail dropped: {text!r}"


def test_annex_text_kept() -> None:
    text = _depth_text()
    assert "ANNEX I sentinel" in text, f"akn:attachment annex dropped: {text!r}"


def test_nested_points_kept_in_order() -> None:
    text = _depth_text()
    positions = {s: text.find(s) for s in _POINT_ORDER}
    missing = [s for s, pos in positions.items() if pos < 0]
    assert missing == [], f"point text dropped: {missing} in {text!r}"
    assert list(positions.values()) == sorted(positions.values()), (
        f"points out of document order: {positions}"
    )


def test_depth_fixture_text_appears_exactly_once() -> None:
    text = _depth_text()
    counts = {s: text.count(s) for s in _DEPTH_SENTINELS}
    assert counts == dict.fromkeys(_DEPTH_SENTINELS, 1), counts


def test_depth_fixture_body_precedes_annex() -> None:
    text = _depth_text()
    order = [text.find(s) for s in ("Data under Art. 6 lawful basis",
                                    "bravo-point-sentinel", "ANNEX I sentinel")]
    assert -1 not in order and order == sorted(order), f"document order lost: {order}"


def test_depth_text_reaches_chunks() -> None:
    """Wiring (F12): the extra text has to survive into the chunks that get published."""
    doc = _clean(DEPTH_FIXTURE.read_bytes())
    joined = "\n".join(chunk.text for chunk in ChunkerFactory().chunk(doc))
    for sentinel in ("Data under Art. 6 lawful basis", "bravo-point-sentinel", "ANNEX I sentinel"):
        assert sentinel in joined, f"{sentinel!r} never reached a chunk"


def test_depth_fixture_uses_cleaner_namespace() -> None:
    """Control: the fixture is real AKN 3.0 (the URI the cleaner uses), via a non-default prefix."""
    root = etree.fromstring(DEPTH_FIXTURE.read_bytes())
    assert etree.QName(root).namespace == AKN_NS
    assert root.prefix == "akn"


# --------------------------------------------------------------------------
# structure the card implies: articles, inline-only blocks, headings, order
# --------------------------------------------------------------------------

def test_article_body_outside_section_kept() -> None:
    body = (
        '<article eId="art_1"><num>Article 1</num><heading>Subject-matter</heading>'
        '<paragraph eId="art_1__para_1"><num>1.</num>'
        "<content><p>article-paragraph-sentinel</p></content></paragraph></article>"
        '<article eId="art_2"><num>Article 2</num><heading>Scope</heading>'
        "<content><p>article-direct-content-sentinel</p></content></article>"
    )
    words = _clean(_akn(body)).text.split()
    for sentinel in ("article-paragraph-sentinel", "article-direct-content-sentinel"):
        assert sentinel in words, f"akn:article text outside akn:section dropped: {sentinel}"


@pytest.mark.parametrize(
    ("p_inner", "expected"),
    [
        pytest.param(
            'Data under <ref href="#art_6">Art. 6</ref> lawful basis',
            "Data under Art. 6 lawful basis", id="spaced",
        ),
        pytest.param(
            'Data under\n          <ref href="#art_6">Art. 6</ref>\n          lawful basis',
            "Data under Art. 6 lawful basis", id="pretty-printed",
        ),
        pytest.param(
            'Data under&#13;\n\t<ref href="#art_6">Art. 6</ref>&#13;\n\tlawful basis',
            "Data under Art. 6 lawful basis", id="crlf-and-tabs",
        ),
        pytest.param(
            'Data under <i><ref href="#art_6">Art. 6</ref></i> lawful basis',
            "Data under Art. 6 lawful basis", id="nested-inline",
        ),
        pytest.param(
            'pursuant to Article <ref href="#art_6">6</ref>(1)(a) of this Regulation',
            "pursuant to Article 6(1)(a) of this Regulation", id="no-space-in-source-stays-joined",
        ),
    ],
)
def test_inline_whitespace_normalised(p_inner: str, expected: str) -> None:
    """Words around inline elements neither fuse ("Art. 6lawful") nor gain spaces ("6 (1)")."""
    body = (
        '<section eId="sec_6"><heading>Article 6</heading>'
        f"<content><p>{p_inner}</p></content></section>"
    )
    text = _clean(_akn(body)).text
    assert expected in text, f"expected {expected!r} in {text!r}"


def test_compact_block_siblings_do_not_fuse() -> None:
    """Minified XML (no whitespace between elements, as EUR-Lex cassettes are) keeps blocks apart."""
    body = (
        '<article eId="art_2"><paragraph eId="art_2__para_1"><list eId="l1">'
        '<point eId="pa"><content><p>first-point-words</p></content></point>'
        '<point eId="pb"><content><p>second-point-words</p></content></point>'
        '</list></paragraph><paragraph eId="art_2__para_2">'
        "<content><p>third-paragraph-words</p></content></paragraph></article>"
    )
    text = _clean(_akn(body)).text
    words = text.split()
    sequence = ("first-point-words", "second-point-words", "third-paragraph-words")
    for word in sequence:
        assert word in words, f"{word!r} missing or fused with a neighbour: {text!r}"
    indexes = [words.index(w) for w in sequence]
    assert indexes == sorted(indexes)


@pytest.mark.parametrize(
    ("p_inner", "sentinel"),
    [
        pytest.param('<ref href="#art_9">only-ref-sentinel</ref>', "only-ref-sentinel", id="ref"),
        pytest.param("<i>only-italic-sentinel</i>", "only-italic-sentinel", id="italic"),
        pytest.param("<b><i>only-nested-inline-sentinel</i></b>", "only-nested-inline-sentinel",
                     id="nested-inline"),
    ],
)
def test_section_with_only_inline_children_kept(p_inner: str, sentinel: str) -> None:
    body = (
        '<section eId="sec_1"><heading>Inline-only section</heading>'
        f"<content><p>{p_inner}</p></content></section>"
    )
    text = _clean(_akn(body)).text
    assert sentinel in text.split(), f"block with only inline children dropped: {text!r}"
    assert "Inline-only section" in _heading_titles(text)


def test_heading_inline_children_kept() -> None:
    body = (
        '<section eId="sec_6"><heading>Article <ref href="#art_6">6</ref> — Lawfulness of '
        "processing</heading><content><p>heading-test body</p></content></section>"
    )
    assert _heading_titles(_clean(_akn(body)).text) == ["Article 6 — Lawfulness of processing"]


def test_comments_and_processing_instructions_not_emitted() -> None:
    body = (
        '<section eId="sec_1"><heading>Comment test</heading><content>'
        "<p>Visible <!-- comment-sentinel --> continued<?akn-pi pi-sentinel?> end</p>"
        "</content></section>"
    )
    text = _clean(_akn(body)).text
    assert "Visible continued end" in text, f"text after a comment or PI dropped: {text!r}"
    assert "comment-sentinel" not in text and "pi-sentinel" not in text


def test_meta_block_not_emitted() -> None:
    """Control: akn:meta is metadata, not legal text, even when it holds akn:p."""
    meta = (
        '<meta><identification source="#EU"><FRBRWork><FRBRthis value="CELEX:32016R0679"/>'
        '</FRBRWork></identification><notes source="#EU"><note eId="n1">'
        "<p>meta-note-sentinel</p></note></notes></meta>"
    )
    text = _clean(_akn(_GENUINE, meta=meta)).text
    assert "meta-note-sentinel" not in text
    assert "genuine body text" in text


_ONCE_CASES = [
    pytest.param(
        '<section eId="o"><heading>outer-heading-sentinel</heading>'
        "<intro><p>outer-intro-sentinel</p></intro>"
        '<section eId="o__i"><heading>inner-heading-sentinel</heading>'
        "<content><p>inner-body-sentinel</p></content></section></section>",
        "",
        ("outer-heading-sentinel", "outer-intro-sentinel",
         "inner-heading-sentinel", "inner-body-sentinel"),
        (),
        id="nested-sections",
    ),
    pytest.param(
        '<section eId="s"><heading>Inline once</heading><content>'
        '<p>before <ref href="#x">ref-once-sentinel</ref> after-once-sentinel</p>'
        "</content></section>",
        "",
        ("ref-once-sentinel", "after-once-sentinel"),
        (),
        id="inline-ref",
    ),
    pytest.param(
        '<section eId="s"><heading>sec-heading-sentinel</heading>'
        '<article eId="a"><heading>art-heading-sentinel</heading>'
        '<paragraph eId="a__p"><content><p>art-in-sec-body-sentinel</p></content></paragraph>'
        "</article></section>",
        "",
        ("sec-heading-sentinel", "art-in-sec-body-sentinel"),
        ("art-heading-sentinel",),
        id="article-inside-section",
    ),
    pytest.param(
        '<article eId="a"><paragraph eId="a__p"><list eId="l">'
        '<point eId="pa"><intro><p>point-a-intro-sentinel</p></intro>'
        '<point eId="pa_i"><content><p>point-a-i-body-sentinel</p></content></point></point>'
        '<point eId="pb"><content><p>point-b-body-sentinel</p></content></point>'
        "</list></paragraph></article>",
        "",
        ("point-a-intro-sentinel", "point-a-i-body-sentinel", "point-b-body-sentinel"),
        (),
        id="nested-points",
    ),
    pytest.param(
        _GENUINE,
        _annex('<section eId="ax_s"><heading>annex-section-heading-sentinel</heading>'
               "<content><p>annex-section-body-sentinel</p></content></section>"),
        ("annex-section-heading-sentinel", "annex-section-body-sentinel"),
        (),
        id="section-inside-annex",
    ),
]


@pytest.mark.parametrize(("body", "attachments", "exactly_once", "at_most_once"), _ONCE_CASES)
def test_text_appears_exactly_once(
    body: str, attachments: str, exactly_once: tuple[str, ...], at_most_once: tuple[str, ...]
) -> None:
    """Walking a parent and its child must not emit the child's text twice."""
    text = _clean(_akn(body, attachments)).text
    counts = {s: text.count(s) for s in exactly_once}
    assert counts == dict.fromkeys(exactly_once, 1), f"{counts} in {text!r}"
    for sentinel in at_most_once:
        assert text.count(sentinel) <= 1, f"{sentinel!r} duplicated in {text!r}"


# --------------------------------------------------------------------------
# attack list A, hostile content: forged "## " headings (F2, F3)
# --------------------------------------------------------------------------
# Every place the deeper walk reads text from is a field. The same table
# drives the positive control (benign text in the field is kept) and every
# hostile family (text in the field never adds a heading line), so a field
# can't be added without both.

_MARKER = "payload-marker text"

FIELDS: dict[str, tuple[str, str]] = {
    "p-text": (
        _GENUINE + '<section eId="sec_f"><heading>Field section</heading>'
        "<content><p>{payload}</p></content></section>",
        "",
    ),
    "inline-tail": (
        _GENUINE + '<section eId="sec_f"><heading>Field section</heading>'
        '<content><p>lead words <ref href="#x">Art. 6</ref>{payload}</p></content></section>',
        "",
    ),
    "inline-text": (
        _GENUINE + '<section eId="sec_f"><heading>Field section</heading>'
        '<content><p>lead words <ref href="#x">{payload}</ref> trail words</p></content></section>',
        "",
    ),
    "heading": (
        _GENUINE + '<section eId="sec_f"><heading>{payload}</heading>'
        "<content><p>field body words</p></content></section>",
        "",
    ),
    "article-point": (
        _GENUINE + '<article eId="art_f"><paragraph eId="art_f__para_1"><list eId="l">'
        '<point eId="pa"><content><p>{payload}</p></content></point></list></paragraph></article>',
        "",
    ),
    "annex": (_GENUINE, _annex("<p>{payload}</p>")),
}


def _field_doc(field: str, payload: str) -> bytes:
    body, attachments = FIELDS[field]
    escaped = _xml_escape(payload)
    return _akn(body.replace("{payload}", escaped), attachments.replace("{payload}", escaped))


def _line_break_payloads() -> list[str]:
    return [f"lead words{lb}## {_MARKER}" for lb in LINE_BREAKS]


def _block_start_payloads() -> list[str]:
    # Whatever leads the block, and whatever separates "##" from the words:
    # after whitespace normalisation any of these could start a line "## ...".
    return [f"{prefix}##{sep}{_MARKER}" for prefix in ("", *WHITESPACE) for sep in WHITESPACE]


def _format_char_payloads() -> list[str]:
    # Regression guard: a fix that strips Cf must not uncover "## " behind it.
    return [f"{cf}## {_MARKER}" for cf in FORMAT_CHARS]


FAMILIES: dict[str, Callable[[], list[str]]] = {
    "line-break": _line_break_payloads,
    "block-start": _block_start_payloads,
    "format-char-prefix": _format_char_payloads,
}


def test_hostile_generators_cover_known_members() -> None:
    """The generators must not silently produce nothing (F5 applied to the tests)."""
    for c in ("\n", "\r", "\x85", " ", " ", "\r\n"):
        assert c in LINE_BREAKS, repr(c)
    for c in (" ", "\t", " ", "　", " "):
        assert c in WHITESPACE, repr(c)
    for c in ("­", "​", "‮", "⁦", "﻿"):
        assert c in FORMAT_CHARS, repr(c)
    for c in ("\x00", "\x0b", "\x0c", "\x1c", "\ud800", "\udfff", "￾"):
        assert c in XML_ILLEGAL, repr(c)
    for name, family in FAMILIES.items():
        assert len(family()) > 0, name


@pytest.mark.parametrize("field", sorted(FIELDS))
def test_field_text_kept(field: str) -> None:
    """Positive control per field: benign text in the field reaches the cleaned text."""
    text = _clean(_field_doc(field, _MARKER)).text
    assert _MARKER in text, f"{field}: benign text dropped: {text!r}"
    assert "Genuine heading" in _heading_titles(text)


@pytest.mark.parametrize("family", sorted(FAMILIES))
@pytest.mark.parametrize("field", sorted(FIELDS))
def test_field_text_cannot_forge_heading(field: str, family: str) -> None:
    """Untrusted text in any field never adds a "## " line, and is still kept."""
    baseline = len(_heading_titles(_clean(_field_doc(field, _MARKER)).text))
    payloads = FAMILIES[family]()
    print(f"{field}/{family}: {len(payloads)} generated payloads")
    failures: list[str] = []
    for payload in payloads:
        try:
            text = _clean(_field_doc(field, payload)).text
        except Exception as exc:  # noqa: BLE001 - collect, report every payload
            failures.append(f"{payload!r}: raised {type(exc).__name__}")
            continue
        titles = _heading_titles(text)
        if len(titles) != baseline:
            failures.append(f"{payload!r}: {len(titles)} headings, expected {baseline}: {titles!r}")
        elif _MARKER.split()[0] not in text:
            failures.append(f"{payload!r}: payload text dropped")
    assert failures == [], (
        f"{field}/{family}: {len(failures)} of {len(payloads)} payloads failed; first 5: "
        + "; ".join(failures[:5])
    )


# --------------------------------------------------------------------------
# attack list A, encoding: illegal XML chars, lone surrogates, NUL, bad UTF-8 (F4, F8)
# --------------------------------------------------------------------------

def _invalid_utf8_sequences() -> list[bytes]:
    lone_bytes = [bytes([b]) for b in range(0x80, 0x100)]  # never valid on their own
    surrogates = [chr(cp).encode("utf-8", "surrogatepass") for cp in (0xD800, 0xDBFF, 0xDC00, 0xDFFF)]
    malformed = [b"\xc0\xaf", b"\xe0\x80\xaf", b"\xe2\x82", b"\xf4\x90\x80\x80", b"\xf8\x88\x80\x80\x80"]
    return lone_bytes + surrogates + malformed


def test_xml_illegal_chars_fail_closed() -> None:
    """Control (green today): every XML-illegal code point, as a char ref or raw, is refused."""
    failures: list[str] = []
    cases: list[tuple[str, bytes]] = []
    for c in XML_ILLEGAL:
        cases.append((f"ref {c!r}", _field_doc("p-text", f"before{c}after")))
        cases.append((f"raw {c!r}", _akn(_GENUINE).replace(
            b"genuine body text", f"before{c}after".encode("utf-8", "surrogatepass"))))
    print(f"{len(cases)} generated illegal-char cases")
    for label, raw in cases:
        try:
            _clean(raw)
        except _FAIL_CLOSED as exc:
            _assert_message_safe(exc, "genuine body text", "before", "after")
            continue
        failures.append(label)
    assert failures == [], f"{len(failures)} of {len(cases)} accepted: {failures[:5]}"


def test_invalid_utf8_fails_closed() -> None:
    """Control (green today): invalid UTF-8 byte sequences are refused with a safe message."""
    failures: list[str] = []
    sequences = _invalid_utf8_sequences()
    for seq in sequences:
        raw = _akn(_GENUINE).replace(b"genuine body text", b"before" + seq + b"after")
        try:
            _clean(raw)
        except _FAIL_CLOSED as exc:
            _assert_message_safe(exc, "genuine body text")
            continue
        failures.append(repr(seq))
    assert failures == [], f"{len(failures)} of {len(sequences)} accepted: {failures[:5]}"


# --------------------------------------------------------------------------
# attack list A, size: huge, many, deep (F6)
# --------------------------------------------------------------------------

def test_size_cap_boundary() -> None:
    """Control: exactly MAX_XML_BYTES is cleaned; one byte over is refused before parsing."""
    base = _akn(_GENUINE)
    # Pad with whitespace runs split by empty comments: one 20 MB text node
    # would trip libxml2's own 10 MB text-node limit before the cleaner's cap.
    run = " " * 999_993 + "<!---->"
    needed = MAX_XML_BYTES - len(base)
    at_cap = _akn(_GENUINE + run * (needed // len(run)) + " " * (needed % len(run)))
    assert len(at_cap) == MAX_XML_BYTES
    assert "genuine body text" in _clean(at_cap).text
    with pytest.raises(SchemaDriftError) as exc:
        _clean(at_cap + b" ")
    _assert_message_safe(exc.value, "genuine body text")


def test_huge_single_line_kept_whole() -> None:
    """Control (green today): a 3 MB paragraph comes through intact, word for word."""
    words = 1_000_000
    body = f'<section eId="s"><heading>Huge</heading><content><p>{"zq " * words}</p></content></section>'
    assert _clean(_akn(body)).text.split().count("zq") == words


def test_many_inline_children_kept_in_order() -> None:
    count = 5_000
    p_inner = "".join(
        f'tok{i:05d}a <ref href="#r{i}">tok{i:05d}b</ref> ' for i in range(count)
    )
    body = f'<section eId="s"><heading>Many</heading><content><p>{p_inner}</p></content></section>'
    tokens = [w for w in _clean(_akn(body)).text.split() if w.startswith("tok")]
    expected = [f"tok{i:05d}{s}" for i in range(count) for s in ("a", "b")]
    assert tokens == expected, f"{len(tokens)} of {len(expected)} inline tokens, in order"


def test_many_points_kept_in_order() -> None:
    count = 5_000
    points = "".join(
        f'<point eId="p{i}"><content><p>pt{i:05d}</p></content></point>' for i in range(count)
    )
    body = f'<article eId="a"><paragraph eId="a__p"><list eId="l">{points}</list></paragraph></article>'
    tokens = [w for w in _clean(_akn(body)).text.split() if w.startswith("pt")]
    assert tokens == [f"pt{i:05d}" for i in range(count)]


def test_deeply_nested_points_kept_in_order() -> None:
    """200 nested points: within the parser's depth limit, so every level must be kept."""
    depth = 200
    opening = "".join(f'<point eId="d{i}"><intro><p>lvl{i:03d}</p></intro>' for i in range(depth))
    body = f'<article eId="a"><paragraph eId="a__p">{opening}{"</point>" * depth}</paragraph></article>'
    tokens = [w for w in _clean(_akn(body)).text.split() if w.startswith("lvl")]
    assert tokens == [f"lvl{i:03d}" for i in range(depth)]


def test_depth_beyond_parser_limit_fails_closed() -> None:
    """Control (green today): huge_tree stays off, so pathological nesting is refused."""
    depth = 10_000
    body = f'<section eId="s"><heading>Deep</heading><content><p>{"<i>" * depth}x{"</i>" * depth}</p></content></section>'
    with pytest.raises(_FAIL_CLOSED):
        _clean(_akn(body))


# --------------------------------------------------------------------------
# attack list A, indirection: entities and XInclude (F7)
# --------------------------------------------------------------------------

def test_external_entity_and_xinclude_not_followed(tmp_path: Path) -> None:
    """Control: no file content reaches the text, whether the cleaner refuses or ignores."""
    secret = tmp_path / "secret.txt"
    secret.write_text("xxe-file-sentinel", encoding="utf-8")
    uri = secret.as_uri()
    raw = (
        f'<?xml version="1.0"?><!DOCTYPE akomaNtoso [<!ENTITY xxe SYSTEM "{uri}">]>'
        f'<akomaNtoso xmlns="{AKN_NS}" xmlns:xi="http://www.w3.org/2001/XInclude">'
        '<act name="regulation"><body><section eId="s"><heading>Indirection</heading><content>'
        "<p>before-entity &xxe; after-entity</p>"
        f'<p>before-include <xi:include href="{uri}" parse="text"/> after-include</p>'
        "</content></section></body></act></akomaNtoso>"
    ).encode("utf-8")
    try:
        text = _clean(raw).text
    except _FAIL_CLOSED as exc:
        _assert_message_safe(exc, "xxe-file-sentinel")
        return
    assert "xxe-file-sentinel" not in text


def test_internal_entities_not_expanded() -> None:
    """Control: a billion-laughs DTD does not inflate the output."""
    entities = '<!ENTITY lol0 "lol">' + "".join(
        f'<!ENTITY lol{i} "{f"&lol{i - 1};" * 10}">' for i in range(1, 10)
    )
    raw = (
        f'<?xml version="1.0"?><!DOCTYPE akomaNtoso [{entities}]>'
        f'<akomaNtoso xmlns="{AKN_NS}"><act name="regulation"><body><section eId="s">'
        "<heading>Laughs</heading><content><p>before-laughs &lol9; after-laughs</p>"
        "</content></section></body></act></akomaNtoso>"
    ).encode("utf-8")
    try:
        text = _clean(raw).text
    except _FAIL_CLOSED:
        return
    assert len(text) < len(raw) and text.count("lol") <= 1, f"entity expanded: {len(text)} chars"


# --------------------------------------------------------------------------
# F5 / F1: no extractable AKN 3.0 text is an error, never an empty success
# --------------------------------------------------------------------------

_SENTINEL_SECTION = (
    '<section eId="s"><heading>Drift heading</heading>'
    "<content><p>unrecognised-body-sentinel</p></content></section>"
)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(_akn(_SENTINEL_SECTION, ns="http://www.akomantoso.org/2.0"), id="akn-2.0-namespace"),
        pytest.param(_akn(_SENTINEL_SECTION, ns=None), id="no-namespace"),
        pytest.param(
            (
                '<?xml version="1.0"?><akn:akomaNtoso xmlns:akn="http://example.invalid/not-akn">'
                '<akn:act name="regulation"><akn:body><akn:section eId="s">'
                "<akn:heading>Drift heading</akn:heading><akn:content>"
                "<akn:p>unrecognised-body-sentinel</akn:p></akn:content></akn:section>"
                "</akn:body></akn:act></akn:akomaNtoso>"
            ).encode("utf-8"),
            id="akn-prefix-bound-to-other-uri",
        ),
        pytest.param(_akn(""), id="empty-body"),
        pytest.param(
            _akn('<section eId="s"><heading> </heading><content><p> \n\t </p></content></section>'),
            id="whitespace-only-blocks",
        ),
        pytest.param(
            _akn("", meta='<meta><notes source="#EU"><note eId="n1">'
                          "<p>unrecognised-body-sentinel</p></note></notes></meta>"),
            id="meta-only",
        ),
    ],
)
def test_no_extractable_text_raises_schema_drift(raw: bytes) -> None:
    with pytest.raises(SchemaDriftError) as exc:
        _clean(raw)
    _assert_message_safe(exc.value, "unrecognised-body-sentinel")


# --------------------------------------------------------------------------
# positive controls: legitimate shallow documents are unchanged
# --------------------------------------------------------------------------

def test_shallow_fixture_output_unchanged() -> None:
    """Control (green today): the existing GDPR sample cleans to exactly what it does now."""
    doc = _clean(SHALLOW_FIXTURE.read_bytes())
    assert doc.text == (
        "## Article 1 — Subject-matter and objectives\n\n"
        "This Regulation lays down rules relating to the protection of natural persons "
        "with regard to the processing of personal data.\n\n"
        "## Article 2 — Material scope\n\n"
        "This Regulation applies to the processing of personal data wholly or partly "
        "by automated means."
    )
    assert doc.has_sections is True
    assert doc.headers["jurisdiction"] == "GDPR"
