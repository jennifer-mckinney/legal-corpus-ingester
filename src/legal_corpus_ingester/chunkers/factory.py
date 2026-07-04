from __future__ import annotations
from legal_corpus_ingester.chunkers.plain import PlainChunker
from legal_corpus_ingester.chunkers.sectioned import SectionedChunker
from legal_corpus_ingester.types import Chunk, CleanedDocument


class ChunkerFactory:
    """Dispatches to SectionedChunker or PlainChunker based on has_sections."""

    def __init__(self) -> None:
        self._sectioned = SectionedChunker()
        self._plain = PlainChunker()

    def chunk(self, doc: CleanedDocument) -> list[Chunk]:
        if doc.has_sections:
            return self._sectioned.chunk(doc)
        return self._plain.chunk(doc)
