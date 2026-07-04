from __future__ import annotations
import pytest


@pytest.fixture(scope="module")
def vcr_cassette_dir() -> str:
    """Point pytest-recording at our shared eurlex cassette directory."""
    return "tests/fixtures/cassettes/eurlex"


@pytest.fixture(scope="module")
def vcr_config() -> dict[str, object]:
    return {
        "record_mode": "none",  # replay only; cassette must exist
        "filter_headers": ["authorization", "cookie", "set-cookie", "x-api-key"],
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
