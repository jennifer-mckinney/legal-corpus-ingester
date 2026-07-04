from __future__ import annotations


class IngesterError(Exception):
    """Base class for all ingester errors."""


class UpstreamNotFoundError(IngesterError):
    """Source URL returned 404 or equivalent."""


class SchemaDriftError(IngesterError):
    """Source schema changed in a breaking way."""


class LicenseDriftError(IngesterError):
    """License SPDX identifier changed -- publish blocked until approved."""

    def __init__(self, source: str, old_spdx: str, new_spdx: str) -> None:
        self.source = source
        self.old_spdx = old_spdx
        self.new_spdx = new_spdx
        super().__init__(
            f"license SPDX changed from {old_spdx} to {new_spdx} for source {source!r}"
            " -- publish blocked until APPROVAL.yaml updated"
        )


class EmbedEndpointError(IngesterError):
    """LocalAI embedding endpoint unreachable or returned an error."""


class IndexMismatchError(IngesterError):
    """Corpus index does not match chunk list (count or SHA mismatch)."""


class PublishError(IngesterError):
    """Atomic publish failed (symlink flip or SIGHUP error)."""


class RoundTripValidationError(IngesterError):
    """Round-trip validation failed: terms-analysis could not retrieve expected result."""


class LegalReviewGateError(IngesterError):
    """Publish blocked because INGESTER_LEGAL_REVIEW_APPROVED_<source> not set."""


class CorpusMismatchError(IngesterError):
    """Corpus bundle MANIFEST does not match expected state."""


class RateLimitedWarning(Warning):
    """Upstream source rate-limited the request; retry scheduled."""
