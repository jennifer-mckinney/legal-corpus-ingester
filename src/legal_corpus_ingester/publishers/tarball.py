from __future__ import annotations

import hashlib
import tarfile
from typing import TYPE_CHECKING

from legal_corpus_ingester.publishers.base import PublishReceipt, PublishTarget
from legal_corpus_ingester.publishers.filesystem import FilesystemPublisher

if TYPE_CHECKING:
    from legal_corpus_ingester.pipeline.manifest import Manifest
    from legal_corpus_ingester.types import Corpus


class TarballPublisher:
    """Publishes a Corpus as a versioned bundle directory plus a .tar.gz archive.

    Uses stdlib ``tarfile`` + gzip only (HR2: no Meta-origin zstandard).

    Layout produced under *target.path* (the ``out/`` dir)::

        out/
          <corpus_version>/
            index/legal_kb.npy
            index/legal_kb_metadata.json
            MANIFEST.yaml
            checksums.txt
            corpus/<source_name>/*.txt
            legal-corpus-<corpus_version>.tar.gz   ← added by this publisher
    """

    def publish(
        self,
        corpus: Corpus,
        target: PublishTarget,
        manifest: Manifest,
    ) -> PublishReceipt:
        """Write bundle files + a gzip tarball of the entire bundle.

        Args:
            corpus: The Corpus to publish.
            target: PublishTarget whose ``path`` is the ``out/`` root directory.
            manifest: Manifest describing the bundle.

        Returns:
            PublishReceipt with ``tarball_sha256`` populated.
        """
        out_dir = target.path
        bundle_name = manifest.corpus_version  # e.g. "2026.07.0"
        bundle_dir = out_dir / bundle_name

        # --- 1. Write full bundle via FilesystemPublisher ---
        bundle_target = PublishTarget(kind="filesystem", path=bundle_dir)
        fs_receipt = FilesystemPublisher().publish(corpus, bundle_target, manifest)

        # --- 2. Pack every file in bundle_dir into a .tar.gz (exclude the tarball itself) ---
        tarball_name = f"legal-corpus-{bundle_name}.tar.gz"
        out_tar = bundle_dir / tarball_name

        with tarfile.open(str(out_tar), "w:gz") as tf:
            for file_path in sorted(bundle_dir.rglob("*")):
                if not file_path.is_file():
                    continue
                # Exclude the tarball itself to avoid recursive self-inclusion
                if file_path == out_tar:
                    continue
                # Use the path relative to bundle_dir as the archive name
                arcname = file_path.relative_to(bundle_dir)
                tf.add(str(file_path), arcname=str(arcname))

        # --- 3. Compute SHA256 of the tarball bytes ---
        tarball_sha256 = hashlib.sha256(out_tar.read_bytes()).hexdigest()

        # --- 4. Return receipt with tarball_sha256 set ---
        return PublishReceipt(
            chunk_count=fs_receipt.chunk_count,
            matrix_sha256=fs_receipt.matrix_sha256,
            metadata_sha256=fs_receipt.metadata_sha256,
            tarball_sha256=tarball_sha256,
        )
