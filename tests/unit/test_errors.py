from __future__ import annotations
import pytest
from legal_corpus_ingester.errors import (
    CorpusMismatchError,
    EmbedEndpointError,
    IndexMismatchError,
    IngesterError,
    LegalReviewGateError,
    LicenseDriftError,
    PublishError,
    RateLimitedWarning,
    RoundTripValidationError,
    SchemaDriftError,
    UpstreamNotFoundError,
)


def test_all_errors_inherit_from_ingester_error() -> None:
    subclasses = [
        UpstreamNotFoundError,
        SchemaDriftError,
        LicenseDriftError,
        EmbedEndpointError,
        IndexMismatchError,
        PublishError,
        RoundTripValidationError,
        LegalReviewGateError,
        CorpusMismatchError,
    ]
    for cls in subclasses:
        assert issubclass(cls, IngesterError), f"{cls.__name__} must inherit IngesterError"


def test_rate_limited_warning_inherits_from_warning() -> None:
    assert issubclass(RateLimitedWarning, Warning)


def test_errors_can_be_raised_and_caught() -> None:
    with pytest.raises(IngesterError):
        raise UpstreamNotFoundError("https://example.com/law.html not found")


def test_license_drift_carries_spdx_info() -> None:
    err = LicenseDriftError(
        source="eurlex",
        old_spdx="CC-BY-4.0",
        new_spdx="CC-BY-NC-4.0",
    )
    assert err.source == "eurlex"
    assert "CC-BY-NC-4.0" in str(err)
