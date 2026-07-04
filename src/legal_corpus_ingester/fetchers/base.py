from __future__ import annotations
import asyncio
import httpx
from legal_corpus_ingester.errors import UpstreamNotFoundError

# Maximum response body size accepted before buffering — prevents DoS via
# oversized upstream payloads.
MAX_RESPONSE_BYTES = 50 * 1024 * 1024  # 50 MB


class BaseFetcher:
    """Base HTTP fetcher with retry-on-429 and exponential backoff."""

    def __init__(
        self,
        rate_limit_rps: float = 1.0,
        max_retries: int = 3,
        backoff_base: float = 1.0,
    ) -> None:
        self._rate_limit_rps = rate_limit_rps
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    async def _fetch_with_retry(self, url: str, **kwargs: object) -> httpx.Response:
        """Fetch url with retry on 429 (exponential backoff). Raises UpstreamNotFoundError on exhaustion or 404."""
        async with httpx.AsyncClient() as client:
            for attempt in range(self._max_retries + 1):
                response = await client.get(url, **kwargs)  # type: ignore[arg-type]
                if response.status_code == 200:
                    # Check actual body size regardless of Content-Length header presence.
                    # Covers chunked TE responses that omit the header (the prior
                    # header-only check silently skipped those cases).
                    if len(response.content) > MAX_RESPONSE_BYTES:
                        raise UpstreamNotFoundError(
                            f"Response body too large ({len(response.content)} bytes, "
                            f"limit {MAX_RESPONSE_BYTES}): {url}"
                        )
                    return response
                if response.status_code == 404:
                    raise UpstreamNotFoundError(f"404 Not Found: {url}")
                if response.status_code == 429:
                    if attempt < self._max_retries:
                        delay = self._backoff_base * (2 ** attempt)
                        await asyncio.sleep(delay)
                        continue
                    raise UpstreamNotFoundError(
                        f"Rate limited after {self._max_retries} retries: {url}"
                    )
                # Other non-200 status
                raise UpstreamNotFoundError(
                    f"Unexpected status {response.status_code}: {url}"
                )
        raise UpstreamNotFoundError(f"Exhausted retries for {url}")
