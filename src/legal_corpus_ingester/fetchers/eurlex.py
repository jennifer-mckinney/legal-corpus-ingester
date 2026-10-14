"""EU law from the Publications Office Cellar as Formex 4 zip packages (terms-analysis#96).

EUR-Lex HTML/XML sits behind a WAF, so the fetcher uses Cellar REST instead:

1. ``GET https://publications.europa.eu/resource/celex/<CELEX>`` asking for
   ``application/zip;mtype=fmx4`` answers ``303`` with a ``Location`` on
   ``/resource/cellar/<uuid>[.NNNN.NN]/zip``.
2. That one Location (rewritten to https) is fetched and
   must return ``200 application/zip``; the body is streamed under a byte cap and
   then validated in memory by ``cellar.read_formex_zip``.

Exactly one redirect is followed, and only to the fixed Cellar path. Any other status,
a WAF header, a second Location or a body that is not the requested act is refused;
the fetcher never retries a WAF challenge and never falls back to another host.

Every Cellar request (both hops, every 429 retry, every ``fetch`` call and every fetcher
instance) waits on one process-wide limiter, so ``refresh`` fetching several EU sources
back to back still keeps to ``rate_limit_rps``. The timeout and the rate come from
``config/fetchers/cellar.yaml``.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import re
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import httpx

from legal_corpus_ingester.errors import SchemaDriftError, UpstreamNotFoundError
from legal_corpus_ingester.fetchers.base import BaseFetcher
from legal_corpus_ingester.fetchers.cellar import CellarSettings, load_cellar_settings, parse_celex, read_formex_zip
from legal_corpus_ingester.types import FetchResult, ProvenanceRecord

# Protocol constants of the Cellar REST interface (fixed by the upstream, not tunable).
# CELEX_BASE_URL and _LOCATION_RE are F3 SSRF invariants: together they are the only
# hosts and paths the fetcher may contact. They are deliberately code, not config, so an
# operator-editable file can never widen them (the loader refuses such keys).
CELEX_BASE_URL = "https://publications.europa.eu/resource/celex/"
EURLEX_LICENSE = "CC-BY-4.0"
_ACCEPT = "application/zip;mtype=fmx4"
_ACCEPT_LANGUAGE = "eng"
_ZIP_MEDIA_TYPE = "application/zip"
_WAF_HEADER = b"x-amzn-waf-action"
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


class _RateLimiter:
    """Spaces requests at least ``interval`` seconds apart, process-wide.

    Each caller reserves the next free slot under a ``threading.Lock`` and then sleeps
    until it, so no two callers can read the same "last request" time: not coroutines in
    one event loop, not separate ``asyncio.run`` loops, not threads. The lock is held only
    for the arithmetic, never across an ``await``; an ``asyncio.Lock`` would bind to the
    first event loop that contends for it, and the orchestrator runs one loop per source.
    ``clock`` and ``sleep`` are injectable and default to ``time.monotonic`` and
    ``asyncio.sleep`` looked up at call time.
    """

    def __init__(
        self,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._clock: Callable[[], float] = clock if clock is not None else (lambda: time.monotonic())
        self._sleep: Callable[[float], Awaitable[None]] = (
            sleep if sleep is not None else (lambda delay: asyncio.sleep(delay))
        )
        self._guard = threading.Lock()
        self._last_slot: float | None = None

    def reset(self) -> None:
        """Forget the last request (a fresh process state, for test isolation)."""
        with self._guard:
            self._last_slot = None

    async def wait(self, interval: float) -> None:
        """Return once this request may start: ``interval`` after the previous slot, never
        before it."""
        with self._guard:
            now = self._clock()
            slot = now if self._last_slot is None else max(now, self._last_slot + interval)
            self._last_slot = slot
        # The reservation stays inside the lock, the sleep outside it. A sleep can wake
        # early (asyncio rounds to its clock resolution), so re-read the clock and wait
        # again until the slot has really arrived.
        remaining = slot - now
        while remaining > 0:
            await self._sleep(remaining)
            remaining = slot - self._clock()


# The one limiter for every Cellar request in this process.
_CELLAR_LIMITER = _RateLimiter()


def reset_cellar_rate_limiter() -> None:
    """Reset hook for the process-wide Cellar limiter (call from a test fixture)."""
    _CELLAR_LIMITER.reset()


def _raw_values(response: httpx.Response, name: bytes) -> list[bytes]:
    return [value for key, value in response.headers.raw if key.lower() == name]


class EurLexFetcher(BaseFetcher):
    """Fetches EU acts by CELEX id from Cellar as validated Formex 4 zips."""

    def __init__(self, settings: CellarSettings | None = None) -> None:
        resolved = settings if settings is not None else load_cellar_settings()
        super().__init__(rate_limit_rps=resolved.rate_limit_rps, max_retries=3, backoff_base=2.0)
        self._settings = resolved

    async def _wait_turn(self) -> None:
        """Every Cellar request calls this right before it is sent."""
        await _CELLAR_LIMITER.wait(1 / self._rate_limit_rps)

    def _client(self) -> httpx.AsyncClient:
        # A fresh client per hop: no cookie or connection state carries across hops,
        # proxy environment variables are ignored, and every timeout is set.
        return httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            # One value sets connect, read, write and pool.
            timeout=httpx.Timeout(self._settings.timeout_seconds),
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
                    # Retries included: a retry is a Cellar request too.
                    await self._wait_turn()
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
                await self._wait_turn()
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
