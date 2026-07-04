from __future__ import annotations
import re
from legal_corpus_ingester.chunkers.text_utils import chunk_text
from legal_corpus_ingester.types import Chunk, CleanedDocument

CHUNK_SIZE = 1000
OVERLAP = 150
SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)


class SectionedChunker:
    """Chunks sectioned documents, keeping section context in each chunk."""

    def chunk(self, doc: CleanedDocument) -> list[Chunk]:
        sections = _split_sections(doc.text)
        chunks: list[Chunk] = []
        global_offset = 0
        chunk_index = 0
        for title, body in sections:
            section_text = f"{title}\n{body}".strip() if title else body
            # chunk_text returns List[Tuple[int, str]] — (char_offset_within_section, text)
            for _local_offset, text in chunk_text(section_text, chunk_size=CHUNK_SIZE, overlap=OVERLAP):
                end = global_offset + len(text)
                chunks.append(Chunk(
                    text=text,
                    section=title,
                    # Merge doc.headers into metadata so downstream consumers
                    # (publishers, LegalKnowledgeBase) can access jurisdiction etc.
                    metadata={"chunk_index": chunk_index, **doc.headers},
                    provenance=doc.provenance,
                    offset_start=global_offset,
                    offset_end=end,
                ))
                global_offset = max(0, end - OVERLAP)
                chunk_index += 1
        return chunks


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split text into (heading, body) pairs on ## markers."""
    parts: list[tuple[str, str]] = []
    matches = list(SECTION_RE.finditer(text))
    if not matches:
        return [("", text)]
    # Text before first section
    if matches[0].start() > 0:
        parts.append(("", text[:matches[0].start()].strip()))
    for i, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        parts.append((title, body))
    return parts
