from __future__ import annotations
from lxml import etree
from legal_corpus_ingester.types import CleanedDocument, ProvenanceRecord

AKN_NS = "http://docs.oasis-open.org/legaldocml/ns/akn/3.0"


class AKNXMLCleaner:
    """Cleans Akoma Ntoso XML from EUR-Lex into a CleanedDocument."""

    def clean(self, raw_bytes: bytes, provenance: ProvenanceRecord) -> CleanedDocument:
        root = etree.fromstring(raw_bytes)
        ns = {"akn": AKN_NS}

        sections = root.findall(".//akn:section", ns)
        has_sections = len(sections) > 0

        parts: list[str] = []
        for section in sections:
            heading_el = section.find("akn:heading", ns)
            heading = heading_el.text.strip() if heading_el is not None and heading_el.text else ""
            content_texts: list[str] = []
            for p in section.findall(".//akn:p", ns):
                if p.text and p.text.strip():
                    content_texts.append(p.text.strip())
            if heading:
                parts.append(f"## {heading}")
            parts.extend(content_texts)

        text = "\n\n".join(parts)

        # Infer jurisdiction from provenance: upstream_version is CELEX ID
        jurisdiction = _celex_to_jurisdiction(provenance.upstream_version or "")

        headers: dict[str, str] = {
            "jurisdiction": jurisdiction,
            "source": provenance.source_name,
            "upstream_version": provenance.upstream_version or "",
        }

        return CleanedDocument(
            text=text,
            has_sections=has_sections,
            headers=headers,
            provenance=provenance,
        )


def _celex_to_jurisdiction(celex_id: str) -> str:
    """Infer jurisdiction label from CELEX prefix. Defaults to 'EU'."""
    # GDPR: 32016R0679
    # EU-AI-Act: 32024R1689
    # For now: all EUR-Lex documents are GDPR/EU jurisdiction
    _CELEX_MAP: dict[str, str] = {
        "32016R0679": "GDPR",
        "32024R1689": "EU-AI-ACT",
    }
    return _CELEX_MAP.get(celex_id, "EU")
