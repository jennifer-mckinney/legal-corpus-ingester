from __future__ import annotations
from pathlib import Path
from legal_corpus_ingester.types import ProvenanceRecord

_PROV = ProvenanceRecord(
    source_url="https://eur-lex.europa.eu/eli/reg/2016/679/oj",
    fetch_timestamp="2026-07-04T00:00:00Z",
    license="CC-BY-4.0",
    upstream_version="32016R0679",
    content_sha256="c" * 64,
    source_name="eurlex",
    fetcher_class="fetchers.eurlex.EurLexFetcher",
)

AKN_FIXTURE = Path("tests/fixtures/corpus/gdpr_akn_sample.xml").read_bytes()


def test_cleaner_produces_cleaned_document() -> None:
    from legal_corpus_ingester.cleaners.xml_akn import AKNXMLCleaner
    from legal_corpus_ingester.types import CleanedDocument
    cleaner = AKNXMLCleaner()
    doc = cleaner.clean(AKN_FIXTURE, provenance=_PROV)
    assert isinstance(doc, CleanedDocument)
    assert len(doc.text) > 0


def test_cleaner_text_contains_article_markers() -> None:
    from legal_corpus_ingester.cleaners.xml_akn import AKNXMLCleaner
    cleaner = AKNXMLCleaner()
    doc = cleaner.clean(AKN_FIXTURE, provenance=_PROV)
    # Section headings become ## markers
    assert "Article 1" in doc.text or "Subject-matter" in doc.text


def test_cleaner_has_sections_true() -> None:
    from legal_corpus_ingester.cleaners.xml_akn import AKNXMLCleaner
    cleaner = AKNXMLCleaner()
    doc = cleaner.clean(AKN_FIXTURE, provenance=_PROV)
    assert doc.has_sections is True


def test_cleaner_headers_contain_jurisdiction() -> None:
    from legal_corpus_ingester.cleaners.xml_akn import AKNXMLCleaner
    cleaner = AKNXMLCleaner()
    doc = cleaner.clean(AKN_FIXTURE, provenance=_PROV)
    assert "jurisdiction" in doc.headers
    assert doc.headers["jurisdiction"] == "GDPR"


def test_cleaner_provenance_preserved() -> None:
    from legal_corpus_ingester.cleaners.xml_akn import AKNXMLCleaner
    cleaner = AKNXMLCleaner()
    doc = cleaner.clean(AKN_FIXTURE, provenance=_PROV)
    assert doc.provenance == _PROV
