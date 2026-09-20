"""Deterministic assessment helpers for the bounded technical campaign.

The live command remains an adapter.  This module only validates recorded
command reports and content identities, so the pass/fail decision is replayable
without a network, clock, subprocess, or ambient filesystem.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from pydantic import Field, field_validator, model_validator

from argos.clock import ensure_utc
from argos.domain.provenance import SHA256_LENGTH
from argos.domain.versioning import VersionedModel
from argos.evaluation.technical_campaign import (
    TechnicalScenario,
    TechnicalScenarioResultV1,
    TechnicalScenarioStatus,
)

__all__ = ["FunctionalScenarioEvidenceV1", "assess_functional_scenario"]


class FunctionalScenarioEvidenceV1(VersionedModel):
    """Recorded facts needed to decide T1 without reopening its artifacts."""

    schema_version: ClassVar[str] = "functional_scenario_evidence.v1"

    campaign_id: str = Field(min_length=1)
    capture_run_id: str = Field(min_length=1)
    started_at: datetime
    ended_at: datetime
    capture_completed: bool
    capture_interrupted: bool
    loop_accepted: int = Field(ge=0)
    loop_duplicate: int = Field(ge=0)
    loop_rejected: int = Field(ge=0)
    store_accepted: int = Field(ge=0)
    store_duplicate: int = Field(ge=0)
    store_rejected: int = Field(ge=0)
    frames_consumed: int = Field(ge=0)
    raw_payload_count: int = Field(ge=0)
    database_sha256_before_replay: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    database_sha256_after_first_replay: str = Field(
        min_length=SHA256_LENGTH, max_length=SHA256_LENGTH
    )
    database_sha256_after_second_replay: str = Field(
        min_length=SHA256_LENGTH, max_length=SHA256_LENGTH
    )
    first_replay_state_hash: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    second_replay_state_hash: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    artifact_identities: tuple[str, ...]

    @field_validator("started_at", "ended_at")
    @classmethod
    def _utc_times(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator(
        "database_sha256_before_replay",
        "database_sha256_after_first_replay",
        "database_sha256_after_second_replay",
        "first_replay_state_hash",
        "second_replay_state_hash",
    )
    @classmethod
    def _sha256(cls, value: str) -> str:
        lowered = value.lower()
        if any(character not in "0123456789abcdef" for character in lowered):
            raise ValueError("content identities must be hexadecimal sha256 values")
        return lowered

    @field_validator("artifact_identities")
    @classmethod
    def _artifacts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item.strip() for item in value):
            raise ValueError("T1 evidence requires nonblank artifact identities")
        if len(set(value)) != len(value):
            raise ValueError("artifact identities must be unique")
        return value

    @model_validator(mode="after")
    def _timeline(self) -> FunctionalScenarioEvidenceV1:
        if self.ended_at < self.started_at:
            raise ValueError("T1 end cannot precede its start")
        return self


def assess_functional_scenario(evidence: FunctionalScenarioEvidenceV1) -> TechnicalScenarioResultV1:
    """Apply the frozen T1 pass rule and preserve every failing condition."""

    reasons: list[str] = []
    if not evidence.capture_completed:
        reasons.append("capture run is not completed")
    if evidence.capture_interrupted:
        reasons.append("capture reports operator interruption")
    if evidence.frames_consumed == 0:
        reasons.append("capture consumed no frames")
    if evidence.loop_rejected or evidence.store_rejected:
        reasons.append("capture contains rejected observations")
    loop_counts = (
        evidence.loop_accepted,
        evidence.loop_duplicate,
        evidence.loop_rejected,
    )
    store_counts = (
        evidence.store_accepted,
        evidence.store_duplicate,
        evidence.store_rejected,
    )
    if loop_counts != store_counts:
        reasons.append("capture loop and durable store counts disagree")
    if evidence.raw_payload_count == 0:
        reasons.append("raw archive is empty")
    database_hashes = {
        evidence.database_sha256_before_replay,
        evidence.database_sha256_after_first_replay,
        evidence.database_sha256_after_second_replay,
    }
    if len(database_hashes) != 1:
        reasons.append("replay changed the capture database")
    if evidence.first_replay_state_hash != evidence.second_replay_state_hash:
        reasons.append("repeated replay produced different state hashes")

    status = TechnicalScenarioStatus.FAILED if reasons else TechnicalScenarioStatus.PASSED
    return TechnicalScenarioResultV1(
        campaign_id=evidence.campaign_id,
        scenario=TechnicalScenario.FUNCTIONAL,
        status=status,
        started_at=evidence.started_at,
        last_checkpoint_at=evidence.ended_at,
        ended_at=evidence.ended_at,
        observed_frame_count=evidence.frames_consumed,
        artifact_identities=evidence.artifact_identities,
        observed_outcome=(
            "bounded capture completed; durable counts agree; raw evidence exists; "
            "two read-only replays produced an identical state hash"
            if not reasons
            else None
        ),
        reason="; ".join(reasons) if reasons else None,
        follow_up_action=(
            "inspect the named artifacts and correct the failed invariant before rerunning T1"
            if reasons
            else None
        ),
    )
