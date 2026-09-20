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

__all__ = [
    "FunctionalScenarioEvidenceV1",
    "StabilityResourceSampleV1",
    "StabilityScenarioEvidenceV1",
    "assess_functional_scenario",
    "assess_stability_scenario",
]

T2_EXPECTED_SAMPLE_INTERVAL_SECONDS = 60
T2_MAXIMUM_SAMPLE_GAP_SECONDS = 120
T2_MAXIMUM_RESIDENT_MEMORY_BYTES = 536_870_912
T2_MAXIMUM_ARTIFACT_BYTES = 2_147_483_648


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


class StabilityResourceSampleV1(VersionedModel):
    """One ordered process-tree and artifact-size observation."""

    schema_version: ClassVar[str] = "stability_resource_sample.v1"

    ordinal: int = Field(ge=0)
    observed_at: datetime
    resident_memory_bytes: int = Field(ge=0)
    artifact_bytes: int = Field(ge=0)

    @field_validator("observed_at")
    @classmethod
    def _utc_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class StabilityScenarioEvidenceV1(VersionedModel):
    """Recorded T2 resource envelope and capture accounting."""

    schema_version: ClassVar[str] = "stability_scenario_evidence.v1"

    campaign_id: str = Field(min_length=1)
    capture_run_id: str = Field(min_length=1)
    started_at: datetime
    ended_at: datetime
    capture_completed: bool
    capture_interrupted: bool
    frames_consumed: int = Field(ge=0)
    loop_counts: tuple[int, int, int]
    store_counts: tuple[int, int, int]
    raw_payload_count: int = Field(ge=0)
    expected_sample_interval_seconds: int = Field(gt=0)
    maximum_sample_gap_seconds: int = Field(gt=0)
    maximum_resident_memory_bytes: int = Field(gt=0)
    maximum_artifact_bytes: int = Field(gt=0)
    sampling_errors: tuple[str, ...] = ()
    samples: tuple[StabilityResourceSampleV1, ...]
    artifact_identities: tuple[str, ...]

    @field_validator("started_at", "ended_at")
    @classmethod
    def _utc_times(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("loop_counts", "store_counts")
    @classmethod
    def _counts(cls, value: tuple[int, int, int]) -> tuple[int, int, int]:
        if any(item < 0 for item in value):
            raise ValueError("capture counts must not be negative")
        return value

    @field_validator("artifact_identities")
    @classmethod
    def _artifact_identities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item.strip() for item in value):
            raise ValueError("T2 evidence requires nonblank artifact identities")
        if len(value) != len(set(value)):
            raise ValueError("artifact identities must be unique")
        return value

    @field_validator("sampling_errors")
    @classmethod
    def _sampling_errors(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("sampling errors must not be blank")
        return value

    @model_validator(mode="after")
    def _ordered_timeline(self) -> StabilityScenarioEvidenceV1:
        frozen = (
            self.expected_sample_interval_seconds,
            self.maximum_sample_gap_seconds,
            self.maximum_resident_memory_bytes,
            self.maximum_artifact_bytes,
        )
        if frozen != (
            T2_EXPECTED_SAMPLE_INTERVAL_SECONDS,
            T2_MAXIMUM_SAMPLE_GAP_SECONDS,
            T2_MAXIMUM_RESIDENT_MEMORY_BYTES,
            T2_MAXIMUM_ARTIFACT_BYTES,
        ):
            raise ValueError("T2 cadence and resource bounds must match the frozen protocol")
        if self.ended_at < self.started_at:
            raise ValueError("T2 end cannot precede its start")
        if [item.ordinal for item in self.samples] != list(range(len(self.samples))):
            raise ValueError("resource sample ordinals must be contiguous from zero")
        observed = [item.observed_at for item in self.samples]
        if observed != sorted(observed) or len(observed) != len(set(observed)):
            raise ValueError("resource sample times must be strictly increasing")
        if any(item < self.started_at or item > self.ended_at for item in observed):
            raise ValueError("resource samples must lie inside the scenario timeline")
        return self


def assess_stability_scenario(evidence: StabilityScenarioEvidenceV1) -> TechnicalScenarioResultV1:
    """Apply the frozen T2 accounting, cadence and resource bounds."""

    reasons: list[str] = []
    if not evidence.capture_completed:
        reasons.append("capture run is not completed")
    if evidence.capture_interrupted:
        reasons.append("capture reports operator interruption")
    if evidence.frames_consumed == 0:
        reasons.append("capture consumed no frames")
    if evidence.loop_counts[2] or evidence.store_counts[2]:
        reasons.append("capture contains rejected observations")
    if evidence.loop_counts != evidence.store_counts:
        reasons.append("capture loop and durable store counts disagree")
    if evidence.raw_payload_count == 0:
        reasons.append("raw archive is empty")
    if len(evidence.samples) < 2:
        reasons.append("fewer than two resource samples were recorded")
    if evidence.sampling_errors:
        reasons.append("resource sampling reported errors: " + "; ".join(evidence.sampling_errors))
    gaps = (
        [
            (evidence.samples[0].observed_at - evidence.started_at).total_seconds(),
            (evidence.ended_at - evidence.samples[-1].observed_at).total_seconds(),
        ]
        if evidence.samples
        else []
    )
    gaps.extend(
        [
            (current.observed_at - previous.observed_at).total_seconds()
            for previous, current in zip(evidence.samples, evidence.samples[1:], strict=False)
        ]
    )
    if gaps and max(gaps) > evidence.maximum_sample_gap_seconds:
        reasons.append("resource sampling cadence exceeded its declared maximum gap")
    if any(
        item.resident_memory_bytes > evidence.maximum_resident_memory_bytes
        for item in evidence.samples
    ):
        reasons.append("resident memory exceeded its declared bound")
    if any(item.artifact_bytes > evidence.maximum_artifact_bytes for item in evidence.samples):
        reasons.append("artifact storage exceeded its declared bound")

    status = TechnicalScenarioStatus.FAILED if reasons else TechnicalScenarioStatus.PASSED
    return TechnicalScenarioResultV1(
        campaign_id=evidence.campaign_id,
        scenario=TechnicalScenario.STABILITY,
        status=status,
        started_at=evidence.started_at,
        last_checkpoint_at=evidence.samples[-1].observed_at
        if evidence.samples
        else evidence.started_at,
        ended_at=evidence.ended_at,
        observed_frame_count=evidence.frames_consumed,
        artifact_identities=evidence.artifact_identities,
        observed_outcome=(
            "bounded capture completed with complete accounting, sampled cadence and resource "
            "usage inside the frozen memory and storage limits"
            if not reasons
            else None
        ),
        reason="; ".join(reasons) if reasons else None,
        follow_up_action=(
            "inspect the capture and resource samples before rerunning T2" if reasons else None
        ),
    )
