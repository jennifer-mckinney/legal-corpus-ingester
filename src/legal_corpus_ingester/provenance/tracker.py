from __future__ import annotations
import hashlib
from datetime import datetime, timezone
from legal_corpus_ingester.types import ProvenanceRecord


class ProvenanceTracker:
    def __init__(self, fetcher_class: str) -> None:
        self._fetcher_class = fetcher_class

    def record(
        self,
        source_name: str,
        source_url: str,
        license_spdx: str,
        upstream_version: str | None,
        content: bytes,
    ) -> ProvenanceRecord:
        """Create a ProvenanceRecord with UTC timestamp and SHA256 of content."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sha256 = hashlib.sha256(content).hexdigest()
        return ProvenanceRecord(
            source_url=source_url,
            fetch_timestamp=ts,
            license=license_spdx,
            upstream_version=upstream_version,
            content_sha256=sha256,
            source_name=source_name,
            fetcher_class=self._fetcher_class,
        )
