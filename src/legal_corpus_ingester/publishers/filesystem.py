from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from legal_corpus_ingester.publishers.base import PublishReceipt, PublishTarget

if TYPE_CHECKING:
    from legal_corpus_ingester.pipeline.manifest import Manifest
    from legal_corpus_ingester.types import Corpus


def _sha256_file(path: Path) -> str:
    """Return the SHA256 hex digest of the bytes at *path*."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write_bytes(data: bytes, final_path: Path) -> None:
    """Write *data* to *final_path* atomically using a sibling tmp file + os.rename."""
    tmp_fd, tmp_name = tempfile.mkstemp(dir=final_path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "wb") as fh:
            fh.write(data)
        os.rename(tmp_name, str(final_path))
    except Exception:
        # Clean up tmp on failure to avoid leaving debris
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _atomic_save_npy(array: np.ndarray[Any, np.dtype[np.float32]], final_path: Path) -> None:
    """Save *array* as .npy to *final_path* atomically."""
    tmp_fd, tmp_name = tempfile.mkstemp(dir=final_path.parent, suffix=".npy")
    os.close(tmp_fd)  # np.save opens by path
    try:
        np.save(tmp_name, array)
        os.rename(tmp_name, str(final_path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class FilesystemPublisher:
    """Writes a Corpus to a structured directory layout."""

    def publish(
        self,
        corpus: Corpus,
        target: PublishTarget,
        manifest: Manifest,
    ) -> PublishReceipt:
        """
        Write corpus to target.path with the following layout::

            <target.path>/
              corpus/<source_name>/<chunk_index>.txt
              index/legal_kb.npy
              index/legal_kb_metadata.json
              MANIFEST.yaml
              checksums.txt
        """
        root = target.path
        root.mkdir(parents=True, exist_ok=True)

        # --- 1. Write per-chunk .txt files grouped by source_name ---
        _written_files: list[Path] = []
        # Track chunk index per source to generate stable filenames
        source_chunk_count: dict[str, int] = {}

        for chunk in corpus.chunks:
            source_name = chunk.provenance.source_name
            idx = source_chunk_count.get(source_name, 0)
            source_chunk_count[source_name] = idx + 1

            # Traversal guard: resolve and confirm path stays inside corpus dir.
            chunk_dir = (root / "corpus" / source_name).resolve()
            if not chunk_dir.is_relative_to((root / "corpus").resolve()):
                raise ValueError(
                    f"source_name {source_name!r} would escape corpus dir; path traversal blocked"
                )
            chunk_dir.mkdir(parents=True, exist_ok=True)

            txt_path = chunk_dir / f"{idx:06d}.txt"
            header = f"# source: {source_name}  section: {chunk.section}\n\n"
            txt_path.write_text(header + chunk.text, encoding="utf-8")
            _written_files.append(txt_path)

        # --- 2. Write index/legal_kb.npy atomically ---
        index_dir = root / "index"
        index_dir.mkdir(parents=True, exist_ok=True)

        npy_path = index_dir / "legal_kb.npy"
        _atomic_save_npy(corpus.embeddings, npy_path)
        _written_files.append(npy_path)

        # --- 3. Write index/legal_kb_metadata.json atomically ---
        # Include chunk.text so LegalKnowledgeBase can run BM25 scoring, and
        # spread chunk.metadata (which now carries jurisdiction from doc.headers)
        # so retrieve() can filter and return jurisdiction-tagged results.
        # chunk.metadata is spread first (lowest precedence) so that provenance
        # fields (source_name, license, etc.) always win on key collision.
        metadata: list[dict[str, Any]] = [
            {
                **chunk.metadata,           # lowest precedence — must come first
                "text": chunk.text,
                "section": chunk.section,
                "source_name": chunk.provenance.source_name,
                "offset_start": chunk.offset_start,
                "offset_end": chunk.offset_end,
                "license": chunk.provenance.license,
            }
            for chunk in corpus.chunks
        ]
        meta_bytes = json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")
        meta_path = index_dir / "legal_kb_metadata.json"
        _atomic_write_bytes(meta_bytes, meta_path)
        _written_files.append(meta_path)

        # --- 4. Write MANIFEST.yaml ---
        manifest.write(root)
        manifest_path = root / "MANIFEST.yaml"
        _written_files.append(manifest_path)

        # --- 5. Compute per-file SHA256 and write checksums.txt ---
        checksum_lines: list[str] = []
        for fpath in _written_files:
            sha = _sha256_file(fpath)
            rel = fpath.relative_to(root)
            checksum_lines.append(f"{sha}  {rel}")

        checksums_path = root / "checksums.txt"
        checksums_path.write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")

        # --- 6. Build and return receipt ---
        matrix_sha256 = _sha256_file(npy_path)
        metadata_sha256 = _sha256_file(meta_path)

        return PublishReceipt(
            chunk_count=len(corpus.chunks),
            matrix_sha256=matrix_sha256,
            metadata_sha256=metadata_sha256,
        )
