"""Cellar Formex 4 packages: config caps, CELEX ids and the in-memory zip reader.

Cellar REST (``publications.europa.eu/resource/celex/<id>``) answers a request for
``application/zip;mtype=fmx4`` with a zip holding the act, its annexes, a ``DOC``
manifest and sometimes a ``PUBLICATION`` table of contents (terms-analysis#96,
lead ruling 2026-10-13). This module is the single validator for that package:
the fetcher, the cassette checks and (later, #95) the Formex cleaner all call
``read_formex_zip``. Nothing is ever extracted to disk.

Every refusal raises ``SchemaDriftError`` with a short ASCII message that never
echoes untrusted bytes (member contents, unvalidated names, manifest values).
"""
from __future__ import annotations

import io
import re
import stat
import struct
import zipfile
import zlib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml
from lxml import etree

from legal_corpus_ingester.errors import SchemaDriftError

# Repo file holding the caps (F13: caps are config, not code).
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "fetchers" / "cellar.yaml"

# CELEX shape accepted by the fetcher: sector digit, year, document-type letters,
# number, and an optional consolidation date. ASCII only: ``[0-9]`` (never ``\d``,
# which matches every Unicode decimal-digit script).
_CELEX_RE = re.compile(r"([0-9])([0-9]{4})([A-Z]{1,2})([0-9]{4})(?:-([0-9]{8}))?")
_ASCII_DIGITS_RE = re.compile(r"[0-9]+")

# Member names (security design F8): a flat ASCII file name ending in ``.xml``.
_MEMBER_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.xml")

_ALLOWED_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_ZIP_MAGIC = b"PK\x03\x04"
_BOM = b"\xef\xbb\xbf"
_UNIX = 3
_MSDOS_DIRECTORY_ATTR = 0x10
_FLAG_ENCRYPTED = 0x1
_LOCAL_HEADER = struct.Struct("<4s2B4HL2L2H")
_READ_CHUNK = 64 * 1024

_MANIFEST_ROOT = "DOC"
_TOC_ROOT = "PUBLICATION"
_ANNEX_ROOT = "ANNEX"
_BASE_ACT_ROOT = "ACT"
_CONS_ACT_ROOT = "CONS.ACT"

_MSG_CAP = 300


@dataclass(frozen=True)
class CellarSettings:
    """Caps for one Cellar package; loaded from ``config/fetchers/cellar.yaml``."""

    max_zip_bytes: int
    max_members: int
    max_member_bytes: int
    max_total_bytes: int
    max_ratio: int


_CAP_NAMES: tuple[str, ...] = tuple(f.name for f in fields(CellarSettings))


@dataclass(frozen=True)
class FormexPart:
    """One selected member: the act first, then annexes in manifest order."""

    name: str
    root: str
    data: bytes


def _safe(value: object) -> str:
    """ASCII-only, length-capped rendering for an untrusted value inside a message."""
    return ascii(value)[:80]


def _drift(message: str) -> SchemaDriftError:
    return SchemaDriftError(message[:_MSG_CAP])


def load_cellar_settings(path: Path | None = None) -> CellarSettings:
    """Load and validate the caps; any bad value fails closed with ``ValueError``.

    A missing file raises ``FileNotFoundError``. Messages name the bad key but never
    the absolute path of the file.
    """
    target = DEFAULT_CONFIG_PATH if path is None else Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(f"cellar config file not found: {target.name}") from None
    except UnicodeDecodeError:
        raise ValueError("cellar config is not UTF-8 text") from None
    try:
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ValueError("cellar config is not valid YAML") from None
    if not isinstance(data, dict):
        raise ValueError(f"cellar config must be a mapping of {', '.join(_CAP_NAMES)}")
    unknown = sorted(_safe(k) for k in data if k not in _CAP_NAMES)
    if unknown:
        raise ValueError(f"cellar config has unknown keys: {', '.join(unknown)}"[:_MSG_CAP])
    missing = [name for name in _CAP_NAMES if name not in data]
    if missing:
        raise ValueError(f"cellar config is missing keys: {', '.join(missing)}")
    for name in _CAP_NAMES:
        value = data[name]
        # type() not isinstance(): bool is an int subclass and is never a cap.
        if type(value) is not int or value <= 0:
            raise ValueError(f"cellar config {name} must be a positive integer, got {_safe(value)}")
    settings = CellarSettings(**{name: data[name] for name in _CAP_NAMES})
    if settings.max_member_bytes > settings.max_total_bytes:
        raise ValueError("cellar config max_member_bytes must not exceed max_total_bytes")
    return settings


@dataclass(frozen=True)
class Celex:
    """A parsed CELEX id (ASCII digits only)."""

    text: str
    sector: str
    year: str
    kind: str
    number: str
    date: str | None


def parse_celex(celex: object) -> Celex:
    """Parse a CELEX id or raise ``ValueError`` with a safe message."""
    if not isinstance(celex, str):
        raise ValueError(f"CELEX id must be a string, got {type(celex).__name__}")
    match = _CELEX_RE.fullmatch(celex)
    if match is None:
        raise ValueError(f"malformed CELEX id {_safe(celex)}")
    sector, year, kind, number, date = match.groups()
    return Celex(celex, sector, year, kind, number, date)


# ---------------------------------------------------------------------------
# zip container
# ---------------------------------------------------------------------------


def _open_zip(body: bytes, settings: CellarSettings) -> tuple[zipfile.ZipFile, list[zipfile.ZipInfo]]:
    if len(body) > settings.max_zip_bytes:
        raise _drift(f"Cellar zip too large ({len(body)} bytes, cap {settings.max_zip_bytes})")
    if not body.startswith(_ZIP_MAGIC):
        raise _drift("Cellar body is not a zip (no local-header magic)")
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
        infos = archive.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, EOFError, OSError, struct.error):
        # ValueError covers UnicodeDecodeError on a UTF-8-flagged member name.
        raise _drift("Cellar body is not a zip (unreadable central directory)") from None
    if len(infos) > settings.max_members:
        archive.close()
        raise _drift(f"Cellar zip member count {len(infos)} exceeds cap {settings.max_members}")
    return archive, infos


def _check_entry(info: zipfile.ZipInfo, seen: set[str]) -> None:
    """Name, flags, compression and file type of one central-directory entry."""
    if _MEMBER_NAME_RE.fullmatch(info.filename) is None:
        raise _drift("Cellar zip member name is not a flat ASCII .xml file name")
    folded = info.filename.casefold()
    if folded in seen:
        raise _drift(f"Cellar zip has a duplicate member name {info.filename}")
    seen.add(folded)
    if info.flag_bits & _FLAG_ENCRYPTED:
        raise _drift(f"Cellar zip member {info.filename} is encrypted")
    if info.compress_type not in _ALLOWED_COMPRESSION:
        raise _drift(f"Cellar zip member {info.filename} uses a compression other than stored/deflate")
    if info.create_system == _UNIX:
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise _drift(f"Cellar zip member {info.filename} is a symlink")
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
            raise _drift(f"Cellar zip member {info.filename} is not a regular file")
    if info.external_attr & _MSDOS_DIRECTORY_ATTR:
        raise _drift(f"Cellar zip member {info.filename} is a directory")


def _check_layout(body: bytes, archive: zipfile.ZipFile, infos: list[zipfile.ZipInfo]) -> None:
    """Every member's data lies inside the file region, and no two members overlap."""
    start_dir = archive.start_dir
    spans: list[tuple[int, int]] = []
    for info in infos:
        offset = info.header_offset
        if offset < 0 or offset + _LOCAL_HEADER.size > start_dir:
            raise _drift(f"Cellar zip member {info.filename} header lies outside the file region")
        header = _LOCAL_HEADER.unpack_from(body, offset)
        if header[0] != _ZIP_MAGIC:
            raise _drift(f"Cellar zip member {info.filename} has no local header")
        data_start = offset + _LOCAL_HEADER.size + header[10] + header[11]
        data_end = data_start + info.compress_size
        if data_end > start_dir:
            raise _drift(f"Cellar zip member {info.filename} compressed size runs past the file region")
        spans.append((offset, data_end))
    spans.sort()
    for (_, end), (start, _) in zip(spans, spans[1:]):
        if start < end:
            raise _drift("Cellar zip has overlapping member entries")


def _check_declared(infos: list[zipfile.ZipInfo], settings: CellarSettings) -> None:
    """Caps on the sizes the central directory declares, before any byte is inflated."""
    total = 0
    for info in infos:
        if info.file_size > settings.max_member_bytes:
            raise _drift(f"Cellar zip member size of {info.filename} exceeds cap {settings.max_member_bytes}")
        if info.file_size > settings.max_ratio * info.compress_size:
            raise _drift(f"Cellar zip member {info.filename} compression ratio exceeds cap {settings.max_ratio}")
        total += info.file_size
        if total > settings.max_total_bytes:
            raise _drift(f"Cellar zip total size exceeds cap {settings.max_total_bytes}")


def _read_members(
    archive: zipfile.ZipFile, infos: list[zipfile.ZipInfo], settings: CellarSettings
) -> dict[str, bytes]:
    """Inflate every member under the caps, counting real bytes (not declared sizes)."""
    out: dict[str, bytes] = {}
    total = 0
    for info in infos:
        buf = bytearray()
        try:
            with archive.open(info) as handle:
                while True:
                    chunk = handle.read(_READ_CHUNK)
                    if not chunk:
                        break
                    buf += chunk
                    total += len(chunk)
                    if len(buf) > settings.max_member_bytes:
                        raise _drift(
                            f"Cellar zip member size of {info.filename} exceeds cap {settings.max_member_bytes}"
                        )
                    if total > settings.max_total_bytes:
                        raise _drift(f"Cellar zip total size exceeds cap {settings.max_total_bytes}")
                    if len(buf) > settings.max_ratio * max(info.compress_size, 1):
                        raise _drift(
                            f"Cellar zip member {info.filename} compression ratio exceeds cap {settings.max_ratio}"
                        )
        except (zipfile.BadZipFile, EOFError, OSError, ValueError, zlib.error):
            raise _drift(f"Cellar zip member {info.filename} is corrupt (CRC, size or header)") from None
        if len(buf) != info.file_size:
            raise _drift(f"Cellar zip member {info.filename} size differs from its declared size")
        out[info.filename] = bytes(buf)
    return out


# ---------------------------------------------------------------------------
# member content
# ---------------------------------------------------------------------------


def _parser() -> etree.XMLParser:
    # Same hardening as the XML cleaner: no entities, no DTD, no network, no huge tree.
    return etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, recover=False
    )


def _root_of(name: str, data: bytes) -> tuple[str, Any]:
    """Validate one member as plain Formex XML; return its root local name and element."""
    if data.startswith(b"PK"):
        raise _drift(f"Cellar zip member {name} is a nested archive")
    text = data[len(_BOM):] if data.startswith(_BOM) else data
    if not text.startswith(b"<"):
        raise _drift(f"Cellar zip member {name} is not XML")
    if b"\x00" in text:
        raise _drift(f"Cellar zip member {name} is malformed (NUL byte)")
    try:
        text.decode("utf-8")
    except UnicodeDecodeError:
        raise _drift(f"Cellar zip member {name} is malformed (not UTF-8)") from None
    # A DOCTYPE is refused outright: Formex members carry none (XXE, entity bombs).
    if b"<!DOCTYPE" in text:
        raise _drift(f"Cellar zip member {name} declares a DOCTYPE")
    try:
        root = etree.fromstring(data, _parser())
    except etree.XMLSyntaxError:
        raise _drift(f"Cellar zip member {name} is malformed XML") from None
    docinfo = root.getroottree().docinfo
    if docinfo.doctype or docinfo.internalDTD is not None:
        raise _drift(f"Cellar zip member {name} declares a DOCTYPE")
    qname = etree.QName(root)
    if qname.namespace is not None or root.prefix is not None:
        raise _drift(f"Cellar zip member {name} root is in a namespace")
    return qname.localname, root


# ---------------------------------------------------------------------------
# manifest and member selection
# ---------------------------------------------------------------------------


def _ref_files(manifest: Any, path: str) -> list[str]:
    return [str(el.get("FILE", "")) for el in manifest.iterfind(path)]


def _select(roots: dict[str, tuple[str, Any]]) -> tuple[str, list[str]]:
    """Pick the act and annexes from the single ``DOC`` manifest's ``REF.PHYS`` list."""
    manifests = [name for name, (root, _el) in roots.items() if root == _MANIFEST_ROOT]
    if len(manifests) != 1:
        raise _drift(f"Cellar zip must hold exactly one DOC manifest, found {len(manifests)}")
    manifest_name = manifests[0]
    manifest = roots[manifest_name][1]
    main_refs = _ref_files(manifest, ".//DOC.MAIN.PUB/REF.PHYS")
    sub_refs = _ref_files(manifest, ".//DOC.SUB.PUB/REF.PHYS")
    if len(main_refs) != 1:
        raise _drift(f"Cellar manifest must list exactly one main document, found {len(main_refs)}")
    refs = main_refs + sub_refs
    if len(set(refs)) != len(refs):
        raise _drift("Cellar manifest lists a duplicate REF.PHYS file")
    for ref in refs:
        if ref not in roots:
            raise _drift(f"Cellar manifest references a missing member {_safe(ref)}")
    act, annexes = main_refs[0], sub_refs
    if roots[act][0] not in (_BASE_ACT_ROOT, _CONS_ACT_ROOT):
        raise _drift(f"Cellar main document {act} root is not ACT or CONS.ACT")
    for annex in annexes:
        if roots[annex][0] != _ANNEX_ROOT:
            raise _drift(f"Cellar sub document {annex} root is not ANNEX")
    listed = {manifest_name, *refs}
    for name, (root, _el) in roots.items():
        if name not in listed and root != _TOC_ROOT:
            raise _drift(f"Cellar zip member {name} is unlisted in the manifest")
    return act, annexes


def _ascii_digits(value: str | None) -> str | None:
    if value is None or _ASCII_DIGITS_RE.fullmatch(value) is None:
        return None
    return value


def _check_identity(act_root: str, act: Any, celex: Celex) -> None:
    """The act's own identifier must name the requested CELEX (F5)."""
    if celex.date is None:
        if celex.sector != "3" or act_root != _BASE_ACT_ROOT:
            raise _drift(f"Cellar act root {act_root} does not fit base CELEX {celex.text}")
        no_doc = act.find("BIB.INSTANCE/NO.DOC")
        year = _ascii_digits(no_doc.findtext("YEAR") if no_doc is not None else None)
        number = _ascii_digits(no_doc.findtext("NO.CURRENT") if no_doc is not None else None)
        if year is None or number is None:
            raise _drift(f"Cellar act carries no ASCII BIB.INSTANCE/NO.DOC for CELEX {celex.text}")
        if year != celex.year or int(number) != int(celex.number):
            raise _drift(f"Cellar act identifier does not match the requested CELEX {celex.text}")
        return
    if celex.sector != "0" or act_root != _CONS_ACT_ROOT:
        raise _drift(f"Cellar act root {act_root} does not fit consolidated CELEX {celex.text}")
    info = act.find("INFO.CONSLEG")
    if info is None:
        raise _drift(f"Cellar consolidated act carries no INFO.CONSLEG for CELEX {celex.text}")
    if info.get("CONSLEG.REF") != f"{celex.year}{celex.kind}{celex.number}" or info.get(
        "START.DATE"
    ) != celex.date:
        raise _drift(f"Cellar consolidated act identifier does not match the requested CELEX {celex.text}")


def read_formex_zip(body: bytes, celex_id: str, settings: CellarSettings) -> tuple[FormexPart, ...]:
    """Validate a Cellar Formex 4 zip in memory; return the act, then annexes in manifest order."""
    try:
        celex = parse_celex(celex_id)
    except ValueError:
        raise _drift(f"requested CELEX {_safe(celex_id)} is malformed") from None
    archive, infos = _open_zip(body, settings)
    with archive:
        seen: set[str] = set()
        for info in infos:
            _check_entry(info, seen)
        _check_layout(body, archive, infos)
        _check_declared(infos, settings)
        data = _read_members(archive, infos, settings)
    roots = {name: _root_of(name, member) for name, member in data.items()}
    act, annexes = _select(roots)
    _check_identity(roots[act][0], roots[act][1], celex)
    return tuple(FormexPart(name, roots[name][0], data[name]) for name in (act, *annexes))
