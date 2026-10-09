from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(scope="module")
def vcr_cassette_dir() -> str:
    """Point pytest-recording at our shared eurlex cassette directory."""
    return "tests/fixtures/cassettes/eurlex"


# Response-header name fragments that must never be written to a cassette
# (terms-analysis#92 security F3). vcrpy's ``filter_headers`` scrubs REQUEST
# headers only, so live ``Set-Cookie`` / auth challenges from a re-record would
# otherwise land in tests/fixtures/cassettes/. Matched case-insensitively as
# substrings so variants (Set-Cookie2, X-Auth-Token, Proxy-Authenticate ...)
# are covered structurally rather than by a growing exact-name list.
_SENSITIVE_RESPONSE_HEADER_FRAGMENTS: tuple[str, ...] = (
    "cookie",
    "authorization",
    "authenticate",
    "auth-token",
    "api-key",
    "session",
)


def _scrub_response(response: dict[str, Any]) -> dict[str, Any]:
    """vcrpy ``before_record_response`` hook: drop sensitive response headers."""
    headers = response.get("headers")
    if isinstance(headers, dict):
        for name in list(headers):
            lowered = str(name).lower()
            if any(frag in lowered for frag in _SENSITIVE_RESPONSE_HEADER_FRAGMENTS):
                del headers[name]
    return response


@pytest.fixture(scope="module")
def vcr_config() -> dict[str, object]:
    # record_mode is deliberately NOT set here (issue #92): in pytest-recording a
    # vcr_config record_mode overrides the CLI, which stopped the drift canary's
    # --record-mode=rewrite from ever re-fetching. With no flag, pytest-recording's
    # record_mode fixture defaults to "none" (replay-only), so CI stays offline.
    # filter_headers applies to request headers only; response headers are
    # scrubbed by before_record_response (security F3).
    return {
        "filter_headers": ["authorization", "cookie", "x-api-key"],
        "before_record_response": _scrub_response,
    }


@pytest.mark.vcr
@pytest.mark.default_cassette("gdpr_fetch")
def test_eurlex_fetcher_returns_fetch_result() -> None:
    import asyncio
    from legal_corpus_ingester.fetchers.eurlex import EurLexFetcher
    from legal_corpus_ingester.types import FetchResult

    fetcher = EurLexFetcher()
    result = asyncio.run(fetcher.fetch("32016R0679"))
    assert isinstance(result, FetchResult)
    assert result.mime_type in {"application/xml", "text/html", "text/plain"}
    assert result.provenance.source_name == "eurlex"
    assert result.provenance.upstream_version == "32016R0679"
    assert result.provenance.license == "CC-BY-4.0"
    assert len(result.raw_bytes) > 0
