from __future__ import annotations
import asyncio
import hashlib
import json
import numpy as np
from unittest.mock import MagicMock, patch
from legal_corpus_ingester.types import Chunk, ProvenanceRecord

_PROV = ProvenanceRecord(
    source_url="https://example.com",
    fetch_timestamp="2026-07-04T00:00:00Z",
    license="CC-BY-4.0",
    upstream_version=None,
    content_sha256="e" * 64,
    source_name="test",
    fetcher_class="test.Fetcher",
)

_CHUNKS = [
    Chunk(text="Article 1 text", section="s1", metadata={}, provenance=_PROV, offset_start=0, offset_end=14),
    Chunk(text="Article 2 text", section="s2", metadata={}, provenance=_PROV, offset_start=14, offset_end=28),
]


def _make_mock_response(n: int, dim: int = 1024) -> MagicMock:
    """Create a mock httpx response with n float32 L2-normalized embeddings."""
    raw = np.random.default_rng(0).random((n, dim)).astype(np.float32)
    norms = np.linalg.norm(raw, axis=1, keepdims=True)
    embeddings = (raw / norms).tolist()
    body = json.dumps({
        "data": [{"embedding": emb, "index": i} for i, emb in enumerate(embeddings)],
        "model": "apertus-8b-instruct",
    })
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = json.loads(body)
    return mock_resp


def test_embed_returns_float32_matrix() -> None:
    from legal_corpus_ingester.embedders.localai import LocalAIEmbedder
    embedder = LocalAIEmbedder(url="http://localhost:8080/v1", model="apertus-8b-instruct")
    mock_resp = _make_mock_response(len(_CHUNKS))

    async def mock_post(*args: object, **kwargs: object) -> MagicMock:
        return mock_resp

    with patch("httpx.AsyncClient.post", new=mock_post):
        matrix = asyncio.run(embedder.embed(_CHUNKS))

    assert matrix.dtype == np.float32
    assert matrix.shape == (len(_CHUNKS), 1024)


def test_embed_output_is_l2_normalized() -> None:
    from legal_corpus_ingester.embedders.localai import LocalAIEmbedder
    embedder = LocalAIEmbedder(url="http://localhost:8080/v1", model="apertus-8b-instruct")
    mock_resp = _make_mock_response(len(_CHUNKS))

    async def mock_post(*args: object, **kwargs: object) -> MagicMock:
        return mock_resp

    with patch("httpx.AsyncClient.post", new=mock_post):
        matrix = asyncio.run(embedder.embed(_CHUNKS))

    norms = np.linalg.norm(matrix, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)


def test_embedder_revision_is_sha256() -> None:
    from legal_corpus_ingester.embedders.localai import LocalAIEmbedder
    embedder = LocalAIEmbedder(url="http://localhost:8080/v1", model="apertus-8b-instruct")
    rev = embedder.revision()
    # revision is SHA256 hex of the model identifier
    expected = hashlib.sha256("apertus-8b-instruct".encode()).hexdigest()
    assert rev == expected


def test_health_check_true_on_200() -> None:
    from legal_corpus_ingester.embedders.localai import LocalAIEmbedder
    embedder = LocalAIEmbedder(url="http://localhost:8080/v1", model="apertus-8b-instruct")
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    async def mock_get(*args: object, **kwargs: object) -> MagicMock:
        return mock_resp

    with patch("httpx.AsyncClient.get", new=mock_get):
        result = asyncio.run(embedder.health_check())
    assert result is True


def test_health_check_false_on_connection_error() -> None:
    import httpx
    from legal_corpus_ingester.embedders.localai import LocalAIEmbedder
    embedder = LocalAIEmbedder(url="http://localhost:8080/v1", model="apertus-8b-instruct")

    async def mock_get(*args: object, **kwargs: object) -> MagicMock:
        raise httpx.ConnectError("Connection refused")

    with patch("httpx.AsyncClient.get", new=mock_get):
        result = asyncio.run(embedder.health_check())
    assert result is False
