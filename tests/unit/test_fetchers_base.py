from __future__ import annotations
import asyncio
from unittest.mock import MagicMock, patch


def test_base_fetcher_retries_on_429() -> None:
    """BaseFetcher retries up to max_retries on 429, then raises UpstreamNotFoundError."""
    import httpx
    from legal_corpus_ingester.fetchers.base import BaseFetcher
    from legal_corpus_ingester.errors import UpstreamNotFoundError

    # Mock: first two calls return 429, third raises to simulate exhaustion
    mock_response_429 = MagicMock(spec=httpx.Response)
    mock_response_429.status_code = 429
    mock_response_429.headers = {"Retry-After": "0"}

    call_count = 0

    async def mock_get(*args: object, **kwargs: object) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return mock_response_429

    fetcher = BaseFetcher(rate_limit_rps=100.0, max_retries=2, backoff_base=0.01)

    with patch("httpx.AsyncClient.get", new=mock_get):
        try:
            asyncio.run(fetcher._fetch_with_retry("https://example.com/law.html"))
            raise AssertionError("Expected UpstreamNotFoundError")
        except UpstreamNotFoundError:
            pass

    assert call_count == 3  # initial + 2 retries


def test_base_fetcher_returns_on_200() -> None:
    """BaseFetcher returns raw bytes on 200."""
    import httpx
    from legal_corpus_ingester.fetchers.base import BaseFetcher

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.content = b"<html>legal text</html>"
    mock_response.headers = {"Content-Type": "text/html"}

    async def mock_get(*args: object, **kwargs: object) -> httpx.Response:
        return mock_response

    fetcher = BaseFetcher(rate_limit_rps=100.0, max_retries=3, backoff_base=0.01)

    with patch("httpx.AsyncClient.get", new=mock_get):
        result = asyncio.run(fetcher._fetch_with_retry("https://example.com/law.html"))

    assert result.content == b"<html>legal text</html>"
    assert result.status_code == 200


def test_base_fetcher_raises_on_404() -> None:
    """BaseFetcher raises UpstreamNotFoundError immediately on 404 (no retry)."""
    import httpx
    from legal_corpus_ingester.fetchers.base import BaseFetcher
    from legal_corpus_ingester.errors import UpstreamNotFoundError

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 404

    call_count = 0

    async def mock_get(*args: object, **kwargs: object) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return mock_response

    fetcher = BaseFetcher(rate_limit_rps=100.0, max_retries=3, backoff_base=0.01)

    with patch("httpx.AsyncClient.get", new=mock_get):
        try:
            asyncio.run(fetcher._fetch_with_retry("https://example.com/missing.html"))
            raise AssertionError("Expected UpstreamNotFoundError")
        except UpstreamNotFoundError:
            pass

    assert call_count == 1  # no retry on 404
