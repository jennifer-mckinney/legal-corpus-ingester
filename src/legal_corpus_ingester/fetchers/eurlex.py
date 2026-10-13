"""EU law from the Publications Office Cellar as Formex 4 zip packages (terms-analysis#96).

EUR-Lex HTML/XML sits behind a WAF, so the fetcher uses Cellar REST instead:

1. ``GET https://publications.europa.eu/resource/celex/<CELEX>`` asking for
   ``application/zip;mtype=fmx4`` answers ``303`` with a ``Location`` on
   ``/resource/cellar/<uuid>[.NNNN.NN]/zip``.
2. After the rate-limit pause, that one Location (rewritten to https) is fetched and
   must return ``200 application/zip``; the body is streamed under a byte cap and
   then validated in memory by ``cellar.read_formex_zip``.

Exactly one redirect is followed, and only to the fixed Cellar path. Any other status,
a WAF header, a second Location or a body that is not the requested act is refused;
the fetcher never retries a WAF challenge and never falls back to another host.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import re
from datetime import datetime, timezone

import httpx

from legal_corpus_ingester.errors import SchemaDriftError, UpstreamNotFoundError
from legal_corpus_ingester.fetchers.base import BaseFetcher
from legal_corpus_ingester.fetchers.cellar import CellarSettings, load_cellar_settings, parse_celex, read_formex_zip
from legal_corpus_ingester.types import FetchResult, ProvenanceRecord

# Protocol constants of the Cellar REST interface (fixed by the upstream, not tunable).
CELEX_BASE_URL = "https://publications.europa.eu/resource/celex/"
EURLEX_LICENSE = "CC-BY-4.0"
_ACCEPT = "application/zip;mtype=fmx4"
_ACCEPT_LANGUAGE = "eng"
_ZIP_MEDIA_TYPE = "application/zip"
_WAF_HEADER = b"x-amzn-waf-action"
_TIMEOUT_SECONDS = 30.0
# The only redirect target followed: the Cellar item path for one zip manifestation.
# Matched against the raw header bytes, so no decoding or URL normalisation can widen it.
_LOCATION_RE = re.compile(
    rb"https?://publications\.europa\.eu/resource/cellar/"
    rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    rb"(?:\.[0-9]{4}\.[0-9]{2})?/zip"
)
_DIGITS_RE = re.compile(rb"[0-9]{1,19}")
_PROJECT_URL = "https://github.com/jennifer-mckinney/legal-corpus-ingester"


def _user_agent() -> str:
    version = importlib.metadata.version("legal-corpus-ingester")
    return f"legal-corpus-ingester/{version} (+{_PROJECT_URL})"


def _raw_values(response: httpx.Response, name: bytes) -> list[bytes]:
    return [value for key, value in response.headers.raw if key.lower() == name]


class EurLexFetcher(BaseFetcher):
    """Fetches EU acts by CELEX id from Cellar as validated Formex 4 zips."""

    def __init__(self, settings: CellarSettings | None = None) -> None:
        super().__init__(rate_limit_rps=0.5, max_retries=3, backoff_base=2.0)
        self._settings = settings if settings is not None else load_cellar_settings()

    def _client(self) -> httpx.AsyncClient:
        # A fresh client per hop: no cookie or connection state carries across hops,
        # proxy environment variables are ignored, and every timeout is set.
        return httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(_TIMEOUT_SECONDS),
            headers={"User-Agent": _user_agent()},
        )

    async def fetch(self, source_config: object) -> FetchResult:
        """Fetch one act. ``source_config`` is a CELEX id string or a SourceConfig
        whose ``pipeline.celex_id`` carries it (the Orchestrator path)."""
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
                raise TypeError("EurLexFetcher.fetch: SourceConfig.pipeline.celex_id is empty or None")
        # Validated before any request: the id is spliced into the URL path.
        parse_celex(celex_id)

        location = await self._resolve(CELEX_BASE_URL + celex_id)
        # Rate limit between the two requests (read via the module so tests can stub it).
        await asyncio.sleep(1 / self._rate_limit_rps)
        raw_bytes = await self._download(location)
        read_formex_zip(raw_bytes, celex_id, self._settings)

        provenance = ProvenanceRecord(
            source_url=location,
            fetch_timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            license=EURLEX_LICENSE,
            upstream_version=celex_id,
            content_sha256=hashlib.sha256(raw_bytes).hexdigest(),
            source_name="eurlex",
            fetcher_class="fetchers.eurlex.EurLexFetcher",
        )
        return FetchResult(
            source_name="eurlex",
            raw_bytes=raw_bytes,
            mime_type=_ZIP_MEDIA_TYPE,
            upstream_metadata={"celex_id": celex_id},
            provenance=provenance,
        )

    async def _resolve(self, url: str) -> str:
        """Hop 1: one 303 to the Cellar zip path; return that target as https."""
        try:
            async with self._client() as client:
                for attempt in range(self._max_retries + 1):
                    async with client.stream(
                        "GET", url, headers={"Accept": _ACCEPT, "Accept-Language": _ACCEPT_LANGUAGE}
                    ) as response:
                        status = response.status_code
                        waf = bool(_raw_values(response, _WAF_HEADER))
                        locations = _raw_values(response, b"location")
                    if status == 429 and not waf:
                        if attempt < self._max_retries:
                            await asyncio.sleep(self._backoff_base * (2**attempt))
                            continue
                        raise UpstreamNotFoundError(
                            f"Cellar rate limited the CELEX request after {self._max_retries} retries"
                        )
                    break
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise UpstreamNotFoundError(f"Cellar CELEX request failed ({type(exc).__name__})") from None
        if waf:
            raise UpstreamNotFoundError("Cellar answered with a WAF challenge; not retried")
        if status != 303:
            raise UpstreamNotFoundError(f"Cellar CELEX request returned {status}, expected one 303")
        if len(locations) != 1 or _LOCATION_RE.fullmatch(locations[0]) is None:
            raise UpstreamNotFoundError("Cellar redirect is not a single Location on the Cellar zip path")
        target = locations[0].decode("ascii")
        return "https://" + target.split("://", 1)[1]

    async def _download(self, url: str) -> bytes:
        """Hop 2: stream the zip under the byte cap; no redirect is followed."""
        cap = self._settings.max_zip_bytes
        try:
            async with self._client() as client:
                async with client.stream("GET", url) as response:
                    if _raw_values(response, _WAF_HEADER):
                        raise UpstreamNotFoundError("Cellar answered with a WAF challenge; not retried")
                    if response.status_code != 200:
                        raise UpstreamNotFoundError(
                            f"Cellar zip request returned {response.status_code}, expected 200"
                        )
                    types = _raw_values(response, b"content-type")
                    media = types[0].split(b";", 1)[0].strip().lower() if len(types) == 1 else b""
                    if media != _ZIP_MEDIA_TYPE.encode():
                        raise SchemaDriftError("Cellar zip response does not have a single application/zip type")
                    lengths = _raw_values(response, b"content-length")
                    if len(lengths) == 1 and _DIGITS_RE.fullmatch(lengths[0]) and int(lengths[0]) > cap:
                        raise SchemaDriftError(f"Cellar zip too large (declared length over cap {cap})")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > cap:
                            raise SchemaDriftError(f"Cellar zip too large (over cap {cap} bytes)")
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise UpstreamNotFoundError(f"Cellar zip request failed ({type(exc).__name__})") from None
        return bytes(body)
