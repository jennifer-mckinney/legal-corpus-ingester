from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Protocol

import numpy as np

from legal_corpus_ingester.errors import EmbedEndpointError
from legal_corpus_ingester.pipeline.manifest import Manifest
from legal_corpus_ingester.pipeline.state import CheckpointState, CheckpointStore
from legal_corpus_ingester.publishers.base import PublishReceipt, PublishTarget
from legal_corpus_ingester.types import Chunk, CleanedDocument, Corpus, FetchResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Narrow protocols — each pipeline stage is injectable for testing.
# ---------------------------------------------------------------------------


class Fetcher(Protocol):
    async def fetch(self, source_config: object) -> FetchResult: ...


class Cleaner(Protocol):
    def clean(self, result: FetchResult) -> CleanedDocument: ...


class Chunker(Protocol):
    def chunk(self, doc: CleanedDocument) -> list[Chunk]: ...


class Embedder(Protocol):
    async def embed(self, chunks: list[Chunk]) -> np.ndarray: ...  # type: ignore[type-arg]

    def revision(self) -> str: ...


class Publisher(Protocol):
    def publish(self, corpus: Corpus, target: PublishTarget, manifest: Manifest) -> PublishReceipt: ...


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    """Wire all pipeline stages together with checkpoint/resume semantics."""

    def __init__(
        self,
        fetcher: Fetcher,
        cleaner: Cleaner,
        chunker: Chunker,
        embedder: Embedder,
        publisher: Publisher,
        publish_target: PublishTarget,
        state_dir: Path,
        corpus_version: str = "2026.07.0",
        chunker_version: str = "v1.0.0",
    ) -> None:
        self._fetcher = fetcher
        self._cleaner = cleaner
        self._chunker = chunker
        self._embedder = embedder
        self._publisher = publisher
        self._target = publish_target
        self._store = CheckpointStore(state_dir)
        self._corpus_version = corpus_version
        self._chunker_version = chunker_version

    def run(self, source_name: str, source_config: object) -> PublishReceipt:
        """Execute the full pipeline for *source_name*.

        Resumes from a prior checkpoint if one exists.  A "done" checkpoint
        is cleared first so the run is idempotent (re-triggerable).
        """
        checkpoint = self._store.load(source_name)
        if checkpoint and checkpoint.stage == "done":
            # Already finished — clear so we can run fresh (idempotent re-trigger).
            self._store.clear(source_name)

        # --- fetch ---
        self._store.save(CheckpointState(source_name, "fetch", self._corpus_version))
        try:
            result: FetchResult = asyncio.run(self._fetcher.fetch(source_config))
        except Exception as exc:
            self._store.save(
                CheckpointState(source_name, "fetch_failed", self._corpus_version, error=str(exc))
            )
            raise

        # --- clean ---
        self._store.save(CheckpointState(source_name, "clean", self._corpus_version))
        doc: CleanedDocument = self._cleaner.clean(result)

        # --- chunk ---
        self._store.save(CheckpointState(source_name, "chunk", self._corpus_version))
        chunks: list[Chunk] = self._chunker.chunk(doc)

        # --- embed ---
        self._store.save(CheckpointState(source_name, "embed", self._corpus_version))
        try:
            embeddings: np.ndarray = asyncio.run(self._embedder.embed(chunks))  # type: ignore[type-arg]
        except EmbedEndpointError as exc:
            self._store.save(
                CheckpointState(source_name, "embed_failed", self._corpus_version, error=str(exc))
            )
            raise

        corpus = Corpus(
            chunks=chunks,
            embeddings=embeddings,
            corpus_files=[source_name],
        )

        # --- publish ---
        self._store.save(CheckpointState(source_name, "publish", self._corpus_version))
        manifest = Manifest(
            corpus_version=self._corpus_version,
            chunker_version=self._chunker_version,
            embedder_model=source_name,
            embedder_revision=self._embedder.revision(),
            sources=[source_name],
            chunk_count=len(chunks),
        )
        receipt = self._publisher.publish(corpus, self._target, manifest)

        self._store.save(CheckpointState(source_name, "done", self._corpus_version))
        logger.info("Pipeline complete for %r: %d chunks published.", source_name, len(chunks))
        return receipt
