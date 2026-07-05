from __future__ import annotations

import warnings
from datetime import date
from pathlib import Path

import yaml

from legal_corpus_ingester.errors import LegalReviewGateError

# Number of days before expiry at which a warning is emitted.
EXPIRY_WARNING_DAYS = 60

# Placeholder SHA256 used in unsigned APPROVAL.yaml templates; ingest is blocked until replaced.
_PLACEHOLDER_SHA = "0" * 64


def load_approval(approval_file: Path) -> dict:  # type: ignore[type-arg]
    """Load and return the raw APPROVAL.yaml dict.

    Raises:
        LegalReviewGateError: if the file is missing or the content is not a dict.
    """
    if not approval_file.exists():
        raise LegalReviewGateError(f"APPROVAL.yaml not found: {approval_file}")
    data = yaml.safe_load(approval_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise LegalReviewGateError(f"APPROVAL.yaml malformed: {approval_file}")
    return data


def check_approval(
    approval_file: Path,
    source_id: str,
    artifact_sha256: str,
    today: date | None = None,
) -> None:
    """Verify an APPROVAL.yaml is valid for the given source and artifact SHA256.

    Emits a UserWarning if the approval expires within EXPIRY_WARNING_DAYS days.

    Args:
        approval_file:    Path to the APPROVAL.yaml for this source.
        source_id:        Expected source_id value (must match file content).
        artifact_sha256:  Expected SHA256 of the signed artifact.
        today:            Override for "today" (defaults to date.today()).

    Raises:
        LegalReviewGateError: if the approval is missing, malformed, expired,
                              or mismatches source_id / SHA256.
    """
    if today is None:
        today = date.today()

    data = load_approval(approval_file)

    # --- source_id check ---
    if data.get("source_id") != source_id:
        raise LegalReviewGateError(
            f"source_id mismatch: expected {source_id!r}, got {data.get('source_id')!r}"
        )

    # --- SHA256 check ---
    stored_sha = data.get("signed_artifact_sha256", "")
    # Reject the all-zeros placeholder that appears in unsigned APPROVAL.yaml templates.
    if stored_sha == _PLACEHOLDER_SHA:
        raise LegalReviewGateError(
            f"Approval for {source_id!r} is not yet signed: replace the all-zeros "
            "placeholder SHA256 with the real artifact hash before ingest"
        )
    if stored_sha != artifact_sha256:
        raise LegalReviewGateError(
            f"SHA256 mismatch for {source_id}: approval is for a different artifact"
        )

    # --- expiry check ---
    # Missing expiry is a hard gate failure, not a raw ValueError.
    raw_expiry = data.get("expiry")
    if not raw_expiry:
        raise LegalReviewGateError(
            f"APPROVAL.yaml missing required 'expiry' field: {approval_file}"
        )
    try:
        expiry = date.fromisoformat(str(raw_expiry))
    except ValueError as exc:
        raise LegalReviewGateError(
            f"APPROVAL.yaml 'expiry' is not a valid ISO date in {approval_file}: {raw_expiry!r}"
        ) from exc
    if today >= expiry:
        raise LegalReviewGateError(f"Approval for {source_id} expired on {expiry}")

    days_remaining = (expiry - today).days
    if days_remaining <= EXPIRY_WARNING_DAYS:
        warnings.warn(
            f"Approval for {source_id} expires in {days_remaining} days ({expiry})",
            stacklevel=2,
        )
