from __future__ import annotations
from legal_corpus_ingester.chunkers.text_utils import chunk_text
from legal_corpus_ingester.types import Chunk, CleanedDocument

CHUNK_SIZE = 800
OVERLAP = 100


class PlainChunker:
    """Chunks plain (non-sectioned) documents using a sliding window."""

    def chunk(self, doc: CleanedDocument) -> list[Chunk]:
        chunks: list[Chunk] = []
        # chunk_text returns List[Tuple[int, str]] — (char_offset, chunk_text)
        for i, (char_offset, text) in enumerate(chunk_text(doc.text, chunk_size=CHUNK_SIZE, overlap=OVERLAP)):
            end = char_offset + len(text)
            chunks.append(Chunk(
                text=text,
                section="",
                # Merge doc.headers into metadata so downstream consumers
                # (publishers, LegalKnowledgeBase) can access jurisdiction etc.
                metadata={"chunk_index": i, **doc.headers},
                provenance=doc.provenance,
                offset_start=char_offset,
                offset_end=end,
            ))
        return chunks
