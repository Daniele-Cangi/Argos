"""Deterministic assessment helpers for the bounded technical campaign.

The live command remains an adapter.  This module only validates recorded
command reports and content identities, so the pass/fail decision is replayable
without a network, clock, subprocess, or ambient filesystem.
"""

from __future__ import annotations

import hashlib
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
    "EnduranceCheckpointV1",
    "EnduranceScenarioEvidenceV1",
    "FunctionalScenarioEvidenceV1",
    "StabilityResourceSampleV1",
    "StabilityScenarioEvidenceV1",
    "assess_endurance_scenario",
    "assess_functional_scenario",
    "assess_stability_scenario",
]

T2_EXPECTED_SAMPLE_INTERVAL_SECONDS = 60
T2_MAXIMUM_SAMPLE_GAP_SECONDS = 120
T2_MAXIMUM_RESIDENT_MEMORY_BYTES = 536_870_912
T2_MAXIMUM_ARTIFACT_BYTES = 2_147_483_648
T3_EXPECTED_CHECKPOINT_INTERVAL_SECONDS = 300
T3_MAXIMUM_CHECKPOINT_GAP_SECONDS = 600
T3_MAXIMUM_RESIDENT_MEMORY_BYTES = 536_870_912
T3_MAXIMUM_ARTIFACT_BYTES = 4_294_967_296
T3_MAXIMUM_DURATION_SECONDS = 21_600
T3_MAXIMUM_FRAME_COUNT = 300_000


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
    events_seen: int = Field(ge=0)
    decode_failures: int = Field(ge=0)
    not_applicable: int = Field(ge=0)
    unknown_event_type: int = Field(ge=0)
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
        if observed != sorted(observed):
            raise ValueError("resource sample times must not regress")
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
    if evidence.decode_failures:
        reasons.append("capture contains decode failures")
    if evidence.unknown_event_type:
        reasons.append("capture contains unknown event types")
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


class EnduranceCheckpointV1(VersionedModel):
    """One immutable, hash-linked T3 checkpoint."""

    schema_version: ClassVar[str] = "endurance_checkpoint.v1"

    ordinal: int = Field(ge=0)
    observed_at: datetime
    state: str
    resident_memory_bytes: int = Field(ge=0)
    artifact_bytes: int = Field(ge=0)
    previous_checkpoint_sha256: str | None = None

    @field_validator("observed_at")
    @classmethod
    def _utc_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("state")
    @classmethod
    def _state(cls, value: str) -> str:
        if value not in {"RUNNING", "COMPLETED"}:
            raise ValueError("checkpoint state must be RUNNING or COMPLETED")
        return value

    @field_validator("previous_checkpoint_sha256")
    @classmethod
    def _previous_hash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        lowered = value.lower()
        if len(lowered) != SHA256_LENGTH or any(c not in "0123456789abcdef" for c in lowered):
            raise ValueError("previous checkpoint identity must be a sha256")
        return lowered

    @property
    def checkpoint_sha256(self) -> str:
        import orjson

        raw = orjson.dumps(self.to_record(), option=orjson.OPT_SORT_KEYS)
        return hashlib.sha256(raw).hexdigest()


class EnduranceScenarioEvidenceV1(VersionedModel):
    """Recorded T3 checkpoint chain, resource envelope and final accounting."""

    schema_version: ClassVar[str] = "endurance_scenario_evidence.v1"

    campaign_id: str = Field(min_length=1)
    capture_run_id: str = Field(min_length=1)
    started_at: datetime
    ended_at: datetime
    capture_completed: bool
    capture_interrupted: bool
    frames_consumed: int = Field(ge=0)
    decode_failures: int = Field(ge=0)
    unknown_event_type: int = Field(ge=0)
    loop_counts: tuple[int, int, int]
    store_counts: tuple[int, int, int]
    raw_payload_count: int = Field(ge=0)
    maximum_duration_seconds: int = Field(gt=0)
    maximum_frame_count: int = Field(gt=0)
    expected_checkpoint_interval_seconds: int = Field(gt=0)
    maximum_checkpoint_gap_seconds: int = Field(gt=0)
    maximum_resident_memory_bytes: int = Field(gt=0)
    maximum_artifact_bytes: int = Field(gt=0)
    checkpoint_errors: tuple[str, ...] = ()
    checkpoints: tuple[EnduranceCheckpointV1, ...]
    persisted_checkpoint_chain_reloaded: bool
    terminal_resume_verified: bool
    artifact_identities: tuple[str, ...]

    @field_validator("started_at", "ended_at")
    @classmethod
    def _utc_times(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _chain_and_protocol(self) -> EnduranceScenarioEvidenceV1:
        frozen = (
            self.maximum_duration_seconds,
            self.maximum_frame_count,
            self.expected_checkpoint_interval_seconds,
            self.maximum_checkpoint_gap_seconds,
            self.maximum_resident_memory_bytes,
            self.maximum_artifact_bytes,
        )
        if frozen != (
            T3_MAXIMUM_DURATION_SECONDS,
            T3_MAXIMUM_FRAME_COUNT,
            T3_EXPECTED_CHECKPOINT_INTERVAL_SECONDS,
            T3_MAXIMUM_CHECKPOINT_GAP_SECONDS,
            T3_MAXIMUM_RESIDENT_MEMORY_BYTES,
            T3_MAXIMUM_ARTIFACT_BYTES,
        ):
            raise ValueError("T3 cadence and resource bounds must match the frozen protocol")
        if self.ended_at < self.started_at:
            raise ValueError("T3 end cannot precede its start")
        if [item.ordinal for item in self.checkpoints] != list(range(len(self.checkpoints))):
            raise ValueError("checkpoint ordinals must be contiguous from zero")
        for index, checkpoint in enumerate(self.checkpoints):
            expected = None if index == 0 else self.checkpoints[index - 1].checkpoint_sha256
            if checkpoint.previous_checkpoint_sha256 != expected:
                raise ValueError("checkpoint hash chain is broken")
            if checkpoint.observed_at < self.started_at or checkpoint.observed_at > self.ended_at:
                raise ValueError("checkpoints must lie inside the scenario timeline")
            if index and checkpoint.observed_at < self.checkpoints[index - 1].observed_at:
                raise ValueError("checkpoint times must not regress")
        if self.checkpoints and any(item.state != "RUNNING" for item in self.checkpoints[:-1]):
            raise ValueError("only the final checkpoint may be terminal")
        if any(item < 0 for item in (*self.loop_counts, *self.store_counts)):
            raise ValueError("capture counts must not be negative")
        if any(not item.strip() for item in (*self.checkpoint_errors, *self.artifact_identities)):
            raise ValueError("errors and artifact identities must not be blank")
        if not self.artifact_identities or len(set(self.artifact_identities)) != len(
            self.artifact_identities
        ):
            raise ValueError("T3 requires unique artifact identities")
        return self


def assess_endurance_scenario(evidence: EnduranceScenarioEvidenceV1) -> TechnicalScenarioResultV1:
    """Apply the frozen T3 accounting, checkpoint and resource rules."""

    reasons: list[str] = []
    if not evidence.capture_completed:
        reasons.append("capture run is not completed")
    if evidence.capture_interrupted:
        reasons.append("capture reports operator interruption")
    if evidence.frames_consumed == 0:
        reasons.append("capture consumed no frames")
    if evidence.frames_consumed > evidence.maximum_frame_count:
        reasons.append("capture exceeded its declared frame-count bound")
    if (
        evidence.ended_at - evidence.started_at
    ).total_seconds() > evidence.maximum_duration_seconds:
        reasons.append("capture exceeded its declared duration bound")
    if evidence.loop_counts[2] or evidence.store_counts[2]:
        reasons.append("capture contains rejected observations")
    if evidence.decode_failures:
        reasons.append("capture contains decode failures")
    if evidence.unknown_event_type:
        reasons.append("capture contains unknown event types")
    if evidence.loop_counts != evidence.store_counts:
        reasons.append("capture loop and durable store counts disagree")
    if evidence.raw_payload_count == 0:
        reasons.append("raw archive is empty")
    if len(evidence.checkpoints) < 2:
        reasons.append("fewer than two checkpoints were recorded")
    elif evidence.checkpoints[-1].state != "COMPLETED":
        reasons.append("checkpoint chain has no terminal checkpoint")
    if not evidence.persisted_checkpoint_chain_reloaded:
        reasons.append("persisted checkpoint chain was not reloadable")
    if not evidence.terminal_resume_verified:
        reasons.append("terminal state did not pass the deterministic resume probe")
    if evidence.checkpoint_errors:
        reasons.append("checkpointing reported errors: " + "; ".join(evidence.checkpoint_errors))
    gaps = []
    if evidence.checkpoints:
        gaps = [
            (evidence.checkpoints[0].observed_at - evidence.started_at).total_seconds(),
            (evidence.ended_at - evidence.checkpoints[-1].observed_at).total_seconds(),
        ]
        gaps.extend(
            (current.observed_at - previous.observed_at).total_seconds()
            for previous, current in zip(
                evidence.checkpoints, evidence.checkpoints[1:], strict=False
            )
        )
    if gaps and max(gaps) > evidence.maximum_checkpoint_gap_seconds:
        reasons.append("checkpoint cadence exceeded its declared maximum gap")
    if any(
        item.resident_memory_bytes > evidence.maximum_resident_memory_bytes
        for item in evidence.checkpoints
    ):
        reasons.append("resident memory exceeded its declared bound")
    if any(item.artifact_bytes > evidence.maximum_artifact_bytes for item in evidence.checkpoints):
        reasons.append("artifact storage exceeded its declared bound")

    status = TechnicalScenarioStatus.FAILED if reasons else TechnicalScenarioStatus.PASSED
    return TechnicalScenarioResultV1(
        campaign_id=evidence.campaign_id,
        scenario=TechnicalScenario.ENDURANCE,
        status=status,
        started_at=evidence.started_at,
        last_checkpoint_at=(
            evidence.checkpoints[-1].observed_at if evidence.checkpoints else evidence.started_at
        ),
        ended_at=evidence.ended_at,
        observed_frame_count=evidence.frames_consumed,
        artifact_identities=evidence.artifact_identities,
        observed_outcome=(
            "bounded endurance capture completed with an intact durable checkpoint chain and "
            "a reloadable terminal state"
            if not reasons
            else None
        ),
        reason="; ".join(reasons) if reasons else None,
        follow_up_action=(
            "inspect the capture and checkpoint chain before rerunning T3" if reasons else None
        ),
    )
