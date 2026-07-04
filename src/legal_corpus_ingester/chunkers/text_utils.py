from __future__ import annotations
# vendored from terms-analysis@57e1103fd7a5bfb5648d194affb61d5216bc071f src/backend/app/services/embedding.py
# Do not modify without updating the snapshot test and bumping __version__.
__version__ = "v1.0.0-vendored-from-terms-analysis@57e1103fd7a5bfb5648d194affb61d5216bc071f"

from typing import List, Tuple


def chunk_text(
    text: str,
    chunk_size: int = 800,
    overlap: int = 100,
) -> List[Tuple[int, str]]:
    """
    Split text into overlapping chunks preserving line boundaries.
    Returns list of (char_offset, chunk_text) pairs.
    """
    lines = text.splitlines(keepends=True)
    chunks: List[Tuple[int, str]] = []
    current: List[str] = []
    current_len = 0
    offset = 0
    chunk_start = 0

    for line in lines:
        if current_len + len(line) > chunk_size and current:
            chunks.append((chunk_start, "".join(current)))
            overlap_lines: List[str] = []
            overlap_len = 0
            for prev_line in reversed(current):
                if overlap_len + len(prev_line) > overlap:
                    break
                overlap_lines.insert(0, prev_line)
                overlap_len += len(prev_line)
            chunk_start = offset - overlap_len
            current = overlap_lines
            current_len = overlap_len

        current.append(line)
        current_len += len(line)
        offset += len(line)

    if current:
        chunks.append((chunk_start, "".join(current)))

    return chunks if chunks else [(0, text)]
