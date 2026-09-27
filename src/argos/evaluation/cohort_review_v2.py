"""Explicit human judgment for a specific archived Gamma entry and contract.

Constructing a record is not human authentication. The operator must collect the
review from a person and persist it before selection; the offline verifier checks
its exact receipt and source/contract linkage, never infers approval from text.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import ClassVar

import orjson
from pydantic import Field, field_validator, model_validator

from argos.clock import ensure_utc
from argos.domain.provenance import SHA256_LENGTH, sha256_hex
from argos.domain.versioning import VersionedModel


class SemanticReviewDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class HumanSemanticReviewV1(VersionedModel):
    """A reviewer attestation, bound to exact archived bytes and compiled rules."""

    schema_version: ClassVar[str] = "m4_human_semantic_review.v1"

    experiment_id: str = Field(min_length=1)
    reviewer_id: str = Field(min_length=1)
    reviewed_at: datetime
    source_payload_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    entry_index: int = Field(ge=0, strict=True)
    entry_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    market_id: str = Field(min_length=1)
    condition_id: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    compiler_version: str = Field(min_length=1)
    decision: SemanticReviewDecision
    notes: str = Field(min_length=1)
    yes_condition: str | None = None
    no_condition: str | None = None
    resolution_authority: str | None = None
    category: str | None = None
    event_group_id: str | None = None
    earliest_outcome_knowable_at: datetime | None = None
    rejection_reason: str | None = None

    @field_validator("reviewed_at", "earliest_outcome_knowable_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @model_validator(mode="after")
    def _decision_is_accountable(self) -> HumanSemanticReviewV1:
        if not self.reviewer_id.strip() or not self.notes.strip():
            raise ValueError("human review needs a reviewer and substantive notes")
        if self.decision is SemanticReviewDecision.APPROVED:
            fields = (
                self.yes_condition,
                self.no_condition,
                self.resolution_authority,
                self.category,
                self.event_group_id,
            )
            if any(value is None or not value.strip() for value in fields):
                raise ValueError(
                    "approved review needs both outcomes, authority, category and group"
                )
            if self.yes_condition == self.no_condition:
                raise ValueError("approved Yes and No conditions must differ")
            if (
                self.earliest_outcome_knowable_at is None
                or self.earliest_outcome_knowable_at <= self.reviewed_at
            ):
                raise ValueError("approved review needs a future outcome-knowable bound")
            if self.rejection_reason is not None:
                raise ValueError("approved review cannot carry a rejection reason")
        elif self.rejection_reason is None or not self.rejection_reason.strip():
            raise ValueError("rejected review needs a reason")
        return self

    @property
    def review_id(self) -> str:
        canonical = orjson.dumps(self.to_record(), option=orjson.OPT_SORT_KEYS)
        return f"review-{sha256_hex(canonical)}"
