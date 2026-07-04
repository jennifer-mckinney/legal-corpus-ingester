from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ProvenanceRecord:
    source_url: str
    fetch_timestamp: str
    license: str
    upstream_version: Optional[str]
    content_sha256: str
    source_name: str
    fetcher_class: str
