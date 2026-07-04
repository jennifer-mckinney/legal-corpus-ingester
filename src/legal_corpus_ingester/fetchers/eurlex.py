from __future__ import annotations
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse
import httpx
from legal_corpus_ingester.fetchers.base import BaseFetcher
from legal_corpus_ingester.errors import UpstreamNotFoundError
from legal_corpus_ingester.types import FetchResult, ProvenanceRecord

EURLEX_BASE_URL = "https://eur-lex.europa.eu"
EURLEX_LICENSE = "CC-BY-4.0"
# Same-origin enforcement: redirects must stay on this host.
_EURLEX_HOST = "eur-lex.europa.eu"


class EurLexFetcher(BaseFetcher):
    """Fetches EU law documents from EUR-Lex by CELEX ID."""

    def __init__(self) -> None:
        super().__init__(rate_limit_rps=0.5, max_retries=3, backoff_base=2.0)

    async def fetch(self, source_config: object) -> FetchResult:
        """Fetch a CELEX document and return a FetchResult.

        source_config may be a bare CELEX ID string (e.g. "32016R0679") for
        direct/test usage, or a SourceConfig whose pipeline.celex_id carries
        the identifier when called through the Orchestrator.
        """
        if isinstance(source_config, str):
            celex_id: str = source_config
        else:
            try:
                celex_id = source_config.pipeline.celex_id  # type: ignore[attr-defined]
            except AttributeError as exc:
                raise TypeError(
                    "EurLexFetcher.fetch: expected a CELEX ID string or a SourceConfig "
                    f"with pipeline.celex_id, got {type(source_config)}"
                ) from exc
            if not celex_id:
                raise TypeError(
                    "EurLexFetcher.fetch: SourceConfig.pipeline.celex_id is empty or None"
                )
        # Try XML first (Akoma Ntoso), fall back to HTML
        xml_url = (
            f"{EURLEX_BASE_URL}/legal-content/EN/TXT/XML/"
            f"?uri=CELEX:{celex_id}"
        )
        try:
            response = await self._fetch_with_retry(
                xml_url,
                headers={"Accept": "application/xml, text/xml;q=0.9"},
                timeout=30.0,
                follow_redirects=True,
            )
            # Same-origin check: reject responses that redirected outside EUR-Lex.
            if urlparse(str(response.url)).netloc != _EURLEX_HOST:
                raise UpstreamNotFoundError(
                    f"Redirect led outside EUR-Lex: {response.url}"
                )
            mime_type = "application/xml"
        except UpstreamNotFoundError:
            raise
        except (httpx.HTTPError, OSError):
            # Fall back to HTML on network/OS errors only; other exceptions propagate
            html_url = (
                f"{EURLEX_BASE_URL}/legal-content/EN/TXT/HTML/"
                f"?uri=CELEX:{celex_id}"
            )
            response = await self._fetch_with_retry(
                html_url,
                timeout=30.0,
                follow_redirects=True,
            )
            # Same-origin check on HTML fallback response as well.
            if urlparse(str(response.url)).netloc != _EURLEX_HOST:
                raise UpstreamNotFoundError(
                    f"Redirect led outside EUR-Lex: {response.url}"
                )
            mime_type = "text/html"

        raw_bytes = response.content
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sha256 = hashlib.sha256(raw_bytes).hexdigest()

        provenance = ProvenanceRecord(
            source_url=str(response.url) if hasattr(response, "url") else xml_url,
            fetch_timestamp=ts,
            license=EURLEX_LICENSE,
            upstream_version=celex_id,
            content_sha256=sha256,
            source_name="eurlex",
            fetcher_class="fetchers.eurlex.EurLexFetcher",
        )

        return FetchResult(
            source_name="eurlex",
            raw_bytes=raw_bytes,
            mime_type=mime_type,
            upstream_metadata={"celex_id": celex_id},
            provenance=provenance,
        )
