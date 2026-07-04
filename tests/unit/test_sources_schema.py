from __future__ import annotations
import pytest
from pydantic import ValidationError


def test_valid_eurlex_config_parses() -> None:
    from legal_corpus_ingester.sources.schema import SourceConfig
    cfg = SourceConfig.model_validate({
        "name": "eurlex",
        "jurisdiction": "EU",
        "base_url": "https://eur-lex.europa.eu",
        "license": {"spdx": "CC-BY-4.0", "url": "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32016R0679"},
        "refresh": {"cadence": "weekly"},
        "pipeline": {"fetcher": "fetchers.eurlex.EurLexFetcher"},
    })
    assert cfg.name == "eurlex"
    assert cfg.license.spdx == "CC-BY-4.0"


def test_unknown_jurisdiction_raises() -> None:
    from legal_corpus_ingester.sources.schema import SourceConfig
    with pytest.raises(ValidationError):
        SourceConfig.model_validate({
            "name": "fake",
            "jurisdiction": "XX-INVALID",
            "base_url": "https://example.com",
            "license": {"spdx": "MIT"},
            "refresh": {"cadence": "weekly"},
            "pipeline": {"fetcher": "fetchers.fake.FakeFetcher"},
        })


def test_missing_license_spdx_raises() -> None:
    from legal_corpus_ingester.sources.schema import SourceConfig
    with pytest.raises(ValidationError):
        SourceConfig.model_validate({
            "name": "fake",
            "jurisdiction": "EU",
            "base_url": "https://example.com",
            "license": {},  # no spdx
            "refresh": {"cadence": "weekly"},
            "pipeline": {"fetcher": "fetchers.fake.FakeFetcher"},
        })


def test_invalid_cadence_raises() -> None:
    from legal_corpus_ingester.sources.schema import SourceConfig
    with pytest.raises(ValidationError):
        SourceConfig.model_validate({
            "name": "fake",
            "jurisdiction": "EU",
            "base_url": "https://example.com",
            "license": {"spdx": "CC-BY-4.0"},
            "refresh": {"cadence": "daily"},  # not in allowed set
            "pipeline": {"fetcher": "fetchers.fake.FakeFetcher"},
        })


def test_unsafe_source_name_raises() -> None:
    from legal_corpus_ingester.sources.schema import SourceConfig
    with pytest.raises(ValidationError):
        SourceConfig.model_validate({
            "name": "../evil",
            "jurisdiction": "EU",
            "base_url": "https://example.com",
            "license": {"spdx": "CC-BY-4.0"},
            "refresh": {"cadence": "weekly"},
            "pipeline": {"fetcher": "fetchers.fake.FakeFetcher"},
        })


# ---------------------------------------------------------------------------
# LicenseConfig URL scheme validator tests (Security MEDIUM — Fix 8)
# ---------------------------------------------------------------------------

def test_license_config_https_url_accepted() -> None:
    from legal_corpus_ingester.sources.schema import LicenseConfig
    cfg = LicenseConfig(spdx="CC-BY-4.0", url="https://example.com/license")
    assert cfg.url == "https://example.com/license"


def test_license_config_empty_url_accepted() -> None:
    from legal_corpus_ingester.sources.schema import LicenseConfig
    cfg = LicenseConfig(spdx="CC-BY-4.0", url="")
    assert cfg.url == ""


def test_license_config_http_url_rejected() -> None:
    from legal_corpus_ingester.sources.schema import LicenseConfig
    with pytest.raises(ValidationError):
        LicenseConfig(spdx="CC-BY-4.0", url="http://169.254.169.254/latest/meta-data/")


def test_license_config_file_url_rejected() -> None:
    from legal_corpus_ingester.sources.schema import LicenseConfig
    with pytest.raises(ValidationError):
        LicenseConfig(spdx="CC-BY-4.0", url="file:///etc/passwd")
