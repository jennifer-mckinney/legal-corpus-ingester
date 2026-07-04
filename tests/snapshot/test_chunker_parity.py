from __future__ import annotations
from pathlib import Path
from legal_corpus_ingester.chunkers.text_utils import chunk_text


def test_chunker_snapshot_matches_terms_analysis(snapshot: object) -> None:
    text = Path("tests/fixtures/corpus/gdpr_placeholder.txt").read_text()
    chunks = list(chunk_text(text))
    assert chunks == snapshot
