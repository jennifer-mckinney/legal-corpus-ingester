from __future__ import annotations
import re
from typing import Literal
from urllib.parse import urlparse
from pydantic import BaseModel, field_validator

# Validates dotted class paths like "module.sub.ClassName"; rejects bare names and empty strings.
_DOTTED_PATH_RE: re.Pattern[str] = re.compile(
    r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+\.[A-Z][a-zA-Z0-9]+$"
)

# Extracted from terms-analysis::schemas.py Jurisdiction Literal — keep in sync
# via scripts/sync_jurisdictions.py (Task 11).
# "EU" is added here as the ingester-native aggregate code for all EU instruments
# (GDPR, EU-AI-ACT, COE-108, etc.) — not present in terms-analysis Jurisdiction.
VALID_JURISDICTIONS: frozenset[str] = frozenset({
    "US-CA",
    "US-FED",
    "US-NY",
    "US-TX",
    "US-VA",
    "US-CO",
    "US-CT",
    "US-IL",
    "US-NJ",
    "US-MN",
    "US-OR",
    "GDPR",
    "UK-GDPR",
    "LGPD",
    "PIPEDA",
    "CA-QC",
    "POPIA",
    "PDPA-KE",
    "DPDP",
    "APPI",
    "PIPA",
    "APP",
    "PDPA-TH",
    "NDPR",
    "ICCPR-17",
    "COE-108",
    "EU-AI-ACT",
    "COE-AI-225",
    "OECD-AI",
    "UNESCO-AI",
    # ingester-native aggregate codes (not in terms-analysis Jurisdiction Literal)
    "EU",   # aggregate for all EU legal instruments (GDPR, EU-AI-ACT, COE-108, COE-AI-225)
    "US",   # aggregate for all US federal + state instruments
    "APAC", # aggregate for APAC instruments (APPI, PIPA, APP, PDPA-TH, PDPA-KE)
})

Cadence = Literal["weekly", "monthly", "quarterly", "event-driven"]


class LicenseConfig(BaseModel):
    spdx: str
    url: str = ""

    @field_validator("url")
    @classmethod
    def url_must_be_https(cls, v: str) -> str:
        # Enforce HTTPS-only to prevent SSRF via http:// or file:// license URLs (HR4)
        if not v:
            return v
        parsed = urlparse(v)
        if parsed.scheme not in ("https",):
            raise ValueError(
                f"license.url must use https:// scheme, got {parsed.scheme!r}"
            )
        return v


class RefreshConfig(BaseModel):
    cadence: Cadence


class PipelineConfig(BaseModel):
    fetcher: str  # dotted class path, e.g. "fetchers.eurlex.EurLexFetcher"
    celex_id: str | None = None  # CELEX document ID for EUR-Lex fetcher
    cleaner: str | None = None  # dotted class path for the cleaner, e.g. "cleaners.xml_akn.AKNXMLCleaner"
    chunker: str | None = None  # dotted class path for the chunker, e.g. "chunkers.sectioned.SectionedChunker"

    @field_validator("fetcher", "cleaner", "chunker")
    @classmethod
    def validate_dotted_class_path(cls, v: str | None) -> str | None:
        # Enforce dotted module.ClassName format to prevent arbitrary string injection
        # into dynamic class loading (HR1, HR3).
        if v is not None and not _DOTTED_PATH_RE.fullmatch(v):
            raise ValueError(
                f"expected a dotted class path (e.g. 'module.sub.ClassName'), got {v!r}"
            )
        return v


class SourceConfig(BaseModel):
    name: str
    jurisdiction: str
    base_url: str
    license: LicenseConfig
    refresh: RefreshConfig
    pipeline: PipelineConfig

    @field_validator("base_url")
    @classmethod
    def base_url_must_be_https(cls, v: str) -> str:
        # Enforce HTTPS-only on base_url to prevent SSRF via http:// or file:// URLs (HR4).
        if not v:
            return v
        parsed = urlparse(v)
        if parsed.scheme not in ("https",):
            raise ValueError(
                f"base_url must use https:// scheme, got {parsed.scheme!r}"
            )
        return v

    @field_validator("name")
    @classmethod
    def name_is_safe(cls, v: str) -> str:
        # Reject path separators and other unsafe chars — prevents path traversal
        # when source_name is used to construct filesystem paths in state/ and corpus/.
        if not re.fullmatch(r"[a-z0-9_-]+", v):
            raise ValueError(
                f"source name {v!r} contains unsafe characters; "
                "only lowercase letters, digits, hyphens, and underscores are allowed"
            )
        return v

    @field_validator("jurisdiction")
    @classmethod
    def validate_jurisdiction(cls, v: str) -> str:
        if v not in VALID_JURISDICTIONS:
            raise ValueError(
                f"Unknown jurisdiction code: {v!r}. "
                f"Valid codes: {sorted(VALID_JURISDICTIONS)}"
            )
        return v
