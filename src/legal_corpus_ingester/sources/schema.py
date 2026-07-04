from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, field_validator

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


class RefreshConfig(BaseModel):
    cadence: Cadence


class PipelineConfig(BaseModel):
    fetcher: str  # dotted class path, e.g. "fetchers.eurlex.EurLexFetcher"


class SourceConfig(BaseModel):
    name: str
    jurisdiction: str
    base_url: str
    license: LicenseConfig
    refresh: RefreshConfig
    pipeline: PipelineConfig

    @field_validator("jurisdiction")
    @classmethod
    def validate_jurisdiction(cls, v: str) -> str:
        if v not in VALID_JURISDICTIONS:
            raise ValueError(
                f"Unknown jurisdiction code: {v!r}. "
                f"Valid codes: {sorted(VALID_JURISDICTIONS)}"
            )
        return v
