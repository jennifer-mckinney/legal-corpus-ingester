from __future__ import annotations
import hashlib
import numpy as np
import httpx
from legal_corpus_ingester.types import Chunk


class LocalAIEmbedder:
    """Embeds chunks via LocalAI's OpenAI-compatible embeddings endpoint."""

    DIM = 1024  # Apertus-8B embedding dimension

    def __init__(self, url: str, model: str, batch_size: int = 32) -> None:
        self._url = url.rstrip("/")
        self._model = model
        self._batch_size = batch_size

    def revision(self) -> str:
        """SHA256 of the model identifier — pinned per ADR-004 (L6)."""
        return hashlib.sha256(self._model.encode()).hexdigest()

    async def embed(self, chunks: list[Chunk]) -> np.ndarray:  # type: ignore[type-arg]
        """Embed chunks in batches. Returns float32 matrix shape (n, DIM), L2-normalized."""
        if not chunks:
            return np.empty((0, self.DIM), dtype=np.float32)
        texts = [c.text for c in chunks]
        all_embeddings: list[list[float]] = []
        from legal_corpus_ingester.errors import EmbedEndpointError
        async with httpx.AsyncClient(timeout=60.0) as client:
            for i in range(0, len(texts), self._batch_size):
                batch = texts[i : i + self._batch_size]
                try:
                    response = await client.post(
                        f"{self._url}/embeddings",
                        json={"model": self._model, "input": batch},
                    )
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise EmbedEndpointError(
                        f"LocalAI returned {exc.response.status_code}: "
                        f"{exc.response.text[:200]}"
                    ) from exc
                except httpx.HTTPError as exc:
                    raise EmbedEndpointError(
                        f"LocalAI request failed: {exc}"
                    ) from exc
                try:
                    data = response.json()
                    sorted_data = sorted(data["data"], key=lambda x: x["index"])
                    all_embeddings.extend(item["embedding"] for item in sorted_data)
                except (KeyError, TypeError, ValueError) as exc:
                    raise EmbedEndpointError(
                        f"LocalAI response malformed: {exc} — body: {response.text[:200]}"
                    ) from exc
        matrix = np.array(all_embeddings, dtype=np.float32)
        # L2-normalize each row; guard against zero-norm vectors
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        return (matrix / norms).astype(np.float32)  # type: ignore[no-any-return]

    async def health_check(self) -> bool:
        """Return True if LocalAI endpoint is reachable and responding."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self._url}/models")
                return response.status_code == 200
        except httpx.HTTPError:
            return False
