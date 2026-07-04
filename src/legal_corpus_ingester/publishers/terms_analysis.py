from __future__ import annotations

import os
import shutil
import signal
from typing import TYPE_CHECKING

from legal_corpus_ingester.publishers.base import PublishReceipt, PublishTarget
from legal_corpus_ingester.publishers.filesystem import FilesystemPublisher
from legal_corpus_ingester.publishers.latest_symlink import atomic_symlink

if TYPE_CHECKING:
    from legal_corpus_ingester.pipeline.manifest import Manifest
    from legal_corpus_ingester.types import Corpus


def reload_signal(pid: int) -> None:
    """Send SIGHUP to *pid* to trigger a live reload of the embedding index."""
    os.kill(pid, signal.SIGHUP)


class TermsAnalysisPublisher:
    """Publishes a Corpus into a calver-versioned bundle directory.

    Layout produced under *target.path* (the ``out/`` dir)::

        out/
          <corpus_version>/          # versioned bundle (FilesystemPublisher layout)
            index/legal_kb.npy
            index/legal_kb_metadata.json
            MANIFEST.yaml
            checksums.txt
            corpus/<source_name>/*.txt
          current -> <corpus_version>  # atomic symlink updated each publish
          legal_kb.npy                 # hot-reload safety copy
          legal_kb_metadata.json       # hot-reload safety copy
    """

    def publish(
        self,
        corpus: Corpus,
        target: PublishTarget,
        manifest: Manifest,
        pid: int = 0,
    ) -> PublishReceipt:
        """Write *corpus* to a versioned bundle and update the 'current' symlink.

        Args:
            corpus: The Corpus to publish.
            target: PublishTarget whose ``path`` is the ``out/`` root directory.
            manifest: Manifest describing the bundle.
            pid: If > 0, ``SIGHUP`` is sent to this PID after the publish completes
                 so a live FastAPI process reloads the new index.  In tests,
                 patch ``reload_signal`` rather than using a real PID.
        """
        out_dir = target.path
        out_dir.mkdir(parents=True, exist_ok=True)

        bundle_name = manifest.corpus_version  # e.g. "2026.07.0"
        bundle_dir = out_dir / bundle_name

        # --- 1. Write full bundle via FilesystemPublisher ---
        bundle_target = PublishTarget(kind="filesystem", path=bundle_dir)
        receipt = FilesystemPublisher().publish(corpus, bundle_target, manifest)

        # --- 2. Atomically repoint the 'current' symlink ---
        atomic_symlink(bundle_name, out_dir / "current")

        # --- 3. Copy hot-reload safety files into out_dir root ---
        shutil.copy2(
            bundle_dir / "index" / "legal_kb.npy",
            out_dir / "legal_kb.npy",
        )
        shutil.copy2(
            bundle_dir / "index" / "legal_kb_metadata.json",
            out_dir / "legal_kb_metadata.json",
        )

        # --- 4. Optionally signal the live server to reload ---
        if pid > 0:
            reload_signal(pid)

        return receipt
