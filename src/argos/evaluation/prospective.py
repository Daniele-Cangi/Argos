"""Persistent evidence required before a prospective M4 forecast.

ADR-0014 makes timestamps inside a contract insufficient evidence of when the
contract became available.  This module therefore keeps three different facts
separate:

* what the predeclared experiment protocol says;
* which exact target and compiled contract were selected under that protocol;
* when the canonical bytes of each record were durably written and read back.

The records here do not score forecasts.  They are the pre-forecast boundary a
future prospective evaluation bundle must bind.  Keeping that boundary small
also makes it possible to persist and publish it before observing the market.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import ClassVar

import orjson
from pydantic import Field, field_validator, model_validator

from argos.clock import ensure_utc
from argos.config.manifest import WorkingTreeStatus
from argos.domain.provenance import SHA256_LENGTH, SourceProvenanceV1, sha256_hex
from argos.domain.versioning import VersionedModel
from argos.errors import ImmutabilityViolationError, StorageError
from argos.evaluation.numeric import evaluation_context
from argos.resolution.gamma_resolution import ResolutionStatus
from argos.store.raw_archive import (
    archive_relative_location,
    read_raw_payload,
    write_raw_payload,
)

__all__ = [
    "AcrossTargetWeighting",
    "CutoffBasis",
    "EvidenceArtifactKind",
    "EvidencePersistenceReceiptV1",
    "LifecycleObservationV1",
    "ProspectiveExperimentProtocolV1",
    "ProspectiveExperimentProtocolV2",
    "ProspectiveTargetV1",
    "ResolutionCutoffEvidenceV1",
    "StandaloneLastTradePolicy",
    "WithinTargetAggregation",
    "build_cutoff_evidence_id",
    "build_lifecycle_observation_id",
    "build_persistence_receipt_id",
    "build_target_id",
    "load_persisted_record",
    "persist_evidence_record",
    "verify_receipt_for_record",
]


class CutoffBasis(StrEnum):
    """The two admissible cutoff bases accepted by ADR-0014."""

    SOURCE_TERMINAL_TIMESTAMP = "source_terminal_timestamp"
    FIRST_OBSERVED_FINAL_SETTLEMENT = "first_observed_final_settlement"


class StandaloneLastTradePolicy(StrEnum):
    """What an unmodeled standalone trade event does to a prospective run."""

    EXCLUDE_TARGET = "exclude_target"
    TERMINATE_EXPERIMENT = "terminate_experiment"


class WithinTargetAggregation(StrEnum):
    """Supported ways one dependent trajectory contributes to a study."""

    LAST_ADMISSIBLE_PRE_CUTOFF_PER_METHOD = "last_admissible_pre_cutoff_point_per_method"


class AcrossTargetWeighting(StrEnum):
    """Supported weighting units for independent resolved targets."""

    EQUAL_RESOLVED_TARGET = "equal_weight_per_independently_resolved_target"


class EvidenceArtifactKind(StrEnum):
    """Kinds whose durable availability has prospective meaning."""

    EXPERIMENT_PROTOCOL = "experiment_protocol"
    MARKET_DEFINITION = "market_definition"
    COMPILED_CONTRACT = "compiled_contract"
    TARGET_DECLARATION = "target_declaration"
    LIFECYCLE_OBSERVATION = "lifecycle_observation"
    RESOLUTION_CUTOFF = "resolution_cutoff"
    CAPTURE_REJECTION = "capture_rejection_evidence"
    TARGET_TERMINAL = "target_terminal_evidence"
    TARGET_EXCLUSION = "target_exclusion"
    EXPERIMENT_AGGREGATE = "experiment_aggregate"


class ProspectiveExperimentProtocolV1(VersionedModel):
    """A complete M4 experiment declaration persisted before observation."""

    schema_version: ClassVar[str] = "prospective_experiment_protocol.v1"

    experiment_id: str = Field(min_length=1)
    declared_at: datetime
    target_population: str = Field(min_length=1)
    inclusion_rules: tuple[str, ...]
    exclusion_rules: tuple[str, ...]
    market_selection_mechanism: str = Field(min_length=1)
    observation_window_start: datetime
    observation_window_end: datetime
    stopping_rule: str = Field(min_length=1)

    structural_minimum_independent_resolved_targets: int = Field(default=2, ge=2)
    minimum_intended_resolved_target_count: int = Field(ge=2)
    scientific_minimum_resolved_target_count: int = Field(ge=2)
    minimum_target_count_rationale: str = Field(min_length=1)
    within_target_aggregation: WithinTargetAggregation
    across_target_weighting: AcrossTargetWeighting

    metrics: tuple[str, ...]
    calibration_bin_edges: tuple[Decimal, ...]
    log_loss_epsilon: Decimal = Field(gt=0, lt=1)
    uncertainty_reporting: str = Field(min_length=1)
    missingness_treatment: str = Field(min_length=1)
    abstention_treatment: str = Field(min_length=1)
    disputed_unresolved_treatment: str = Field(min_length=1)
    category_dispersion_requirement: str = Field(min_length=1)
    outcome_dispersion_requirement: str = Field(min_length=1)
    minimum_category_count_for_calibration: int = Field(ge=1)
    minimum_yes_outcomes_for_calibration: int = Field(ge=1)
    minimum_no_outcomes_for_calibration: int = Field(ge=1)
    protocol_sufficiency_rule: str = Field(min_length=1)

    cutoff_basis: CutoffBasis
    standalone_last_trade_policy: StandaloneLastTradePolicy
    target_replacement_policy: str = Field(min_length=1)

    code_revision: str = Field(min_length=1)
    working_tree: WorkingTreeStatus
    config_fingerprint: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)

    @field_validator("declared_at", "observation_window_start", "observation_window_end")
    @classmethod
    def _anchor_times(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator(
        "inclusion_rules",
        "exclusion_rules",
        "metrics",
    )
    @classmethod
    def _non_empty_text_collection(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or not all(item.strip() for item in value):
            raise ValueError("protocol rule and metric collections must be non-empty")
        if len(set(value)) != len(value):
            raise ValueError("protocol rule and metric collections must not contain duplicates")
        return value

    @field_validator("calibration_bin_edges")
    @classmethod
    def _valid_bin_edges(cls, value: tuple[Decimal, ...]) -> tuple[Decimal, ...]:
        if len(value) < 3 or value[0] != 0 or value[-1] != 1:
            raise ValueError("calibration bins must contain at least 0, one interior edge, and 1")
        if any(left >= right for left, right in pairwise(value)):
            raise ValueError("calibration bin edges must be strictly increasing")
        with evaluation_context():
            width = Decimal(1) / (len(value) - 1)
            expected = tuple(Decimal(index) * width for index in range(len(value)))
        if value != expected:
            raise ValueError("the current evaluator supports only declared equal-width bins")
        return value

    @model_validator(mode="after")
    def _protocol_is_coherent(self) -> ProspectiveExperimentProtocolV1:
        if self.declared_at > self.observation_window_start:
            raise ValueError("the protocol must be declared before its observation window")
        if self.observation_window_start >= self.observation_window_end:
            raise ValueError("the observation window must have positive duration")
        if (
            self.minimum_intended_resolved_target_count
            < self.structural_minimum_independent_resolved_targets
        ):
            raise ValueError("scientific intent cannot be below the structural target floor")
        if self.scientific_minimum_resolved_target_count < max(
            self.minimum_intended_resolved_target_count,
            self.structural_minimum_independent_resolved_targets,
        ):
            raise ValueError("scientific sufficiency cannot be below the intended target count")
        required_metrics = {"brier_score", "log_loss", "absolute_error"}
        if not required_metrics.issubset(self.metrics):
            raise ValueError("the prospective protocol must retain all required scoring metrics")
        if self.working_tree is not WorkingTreeStatus.CLEAN:
            raise ValueError("a frozen prospective protocol must identify a clean revision")
        return self


class ProspectiveExperimentProtocolV2(ProspectiveExperimentProtocolV1):
    """Protocol whose operational stopping and capture bounds are evidence-bound."""

    schema_version: ClassVar[str] = "prospective_experiment_protocol.v2"

    lifecycle_deadline: datetime
    lifecycle_poll_interval_seconds: int = Field(gt=0)
    capture_max_seconds_per_target: int = Field(gt=0)
    capture_max_frames_per_target: int = Field(gt=0)
    capture_separate_database_per_target: bool
    capture_subscribe_both_tokens: bool
    capture_raw_archive: bool

    @field_validator("lifecycle_deadline")
    @classmethod
    def _anchor_lifecycle_deadline(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _operational_bounds_are_coherent(self) -> ProspectiveExperimentProtocolV2:
        if self.lifecycle_deadline <= self.observation_window_end:
            raise ValueError("lifecycle deadline must follow the observation window")
        lifecycle_seconds = int(
            (self.lifecycle_deadline - self.observation_window_end).total_seconds()
        )
        if self.lifecycle_poll_interval_seconds > lifecycle_seconds:
            raise ValueError("lifecycle cadence must fit before the frozen deadline")
        observation_seconds = int(
            (self.observation_window_end - self.observation_window_start).total_seconds()
        )
        if self.capture_max_seconds_per_target > observation_seconds:
            raise ValueError("per-target capture duration exceeds the observation window")
        if not self.capture_separate_database_per_target:
            raise ValueError("prospective captures require a separate database per target")
        if not self.capture_subscribe_both_tokens:
            raise ValueError("prospective captures must subscribe both target tokens")
        if not self.capture_raw_archive:
            raise ValueError("prospective captures require immutable raw archival")
        return self


class EvidencePersistenceReceiptV1(VersionedModel):
    """Proof that canonical artifact bytes were durably written and reloaded."""

    schema_version: ClassVar[str] = "evidence_persistence_receipt.v1"

    receipt_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    artifact_kind: EvidenceArtifactKind
    artifact_id: str = Field(min_length=1)
    artifact_schema_version: str = Field(min_length=1)
    artifact_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    artifact_byte_length: int = Field(gt=0)
    persisted_at: datetime
    storage_backend: str = "content_addressed_raw_archive.v1"
    storage_identity: str = Field(min_length=1)

    @field_validator("persisted_at")
    @classmethod
    def _anchor_persisted_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("artifact_sha256")
    @classmethod
    def _hex_digest(cls, value: str) -> str:
        lowered = value.lower()
        if not all(character in "0123456789abcdef" for character in lowered):
            raise ValueError("artifact_sha256 must be hexadecimal")
        return lowered

    @field_validator("storage_identity")
    @classmethod
    def _relative_storage_identity(cls, value: str) -> str:
        path = PurePosixPath(value.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("storage_identity must stay relative to its evidence archive")
        return path.as_posix()

    @model_validator(mode="after")
    def _identity_is_recomputable(self) -> EvidencePersistenceReceiptV1:
        expected = build_persistence_receipt_id(
            experiment_id=self.experiment_id,
            artifact_kind=self.artifact_kind,
            artifact_id=self.artifact_id,
            artifact_schema_version=self.artifact_schema_version,
            artifact_sha256=self.artifact_sha256,
            artifact_byte_length=self.artifact_byte_length,
            persisted_at=self.persisted_at,
            storage_backend=self.storage_backend,
            storage_identity=self.storage_identity,
        )
        if self.receipt_id != expected:
            raise ValueError("receipt_id disagrees with the persistence facts it identifies")
        return self


class ProspectiveTargetV1(VersionedModel):
    """One exact target selected under the frozen protocol before capture."""

    schema_version: ClassVar[str] = "prospective_target.v1"

    target_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    selection_rank: int = Field(gt=0)
    selected_at: datetime
    market_id: str = Field(min_length=1)
    condition_id: str = Field(min_length=1)
    yes_token_id: str = Field(min_length=1)
    no_token_id: str = Field(min_length=1)
    category: str | None = Field(default=None, min_length=1)
    cutoff_basis: CutoffBasis

    market_record_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    market_receipt_id: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    contract_record_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    contract_receipt_id: str = Field(min_length=1)

    @field_validator("selected_at")
    @classmethod
    def _anchor_selected_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _identity_and_tokens_are_coherent(self) -> ProspectiveTargetV1:
        if self.yes_token_id == self.no_token_id:
            raise ValueError("a binary target must name two distinct token ids")
        expected = build_target_id(
            experiment_id=self.experiment_id,
            market_id=self.market_id,
            condition_id=self.condition_id,
            yes_token_id=self.yes_token_id,
            no_token_id=self.no_token_id,
        )
        if self.target_id != expected:
            raise ValueError("target_id disagrees with the selected market and token mapping")
        return self


class LifecycleObservationV1(VersionedModel):
    """One immutable lifecycle poll, chained in retrieval order."""

    schema_version: ClassVar[str] = "lifecycle_observation.v1"

    lifecycle_observation_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    ordinal: int = Field(gt=0)
    previous_observation_id: str | None = Field(default=None, min_length=1)
    source: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    source_time: datetime | None = None
    retrieved_at: datetime
    raw_payload_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    byte_length: int = Field(gt=0)
    raw_payload_location: str = Field(min_length=1)
    finality: ResolutionStatus
    resolution_id: str | None = Field(default=None, min_length=1)
    resolution_record_sha256: str | None = Field(
        default=None, min_length=SHA256_LENGTH, max_length=SHA256_LENGTH
    )

    @field_validator("source_time", "retrieved_at")
    @classmethod
    def _anchor_observation_times(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @model_validator(mode="after")
    def _observation_is_recomputable(self) -> LifecycleObservationV1:
        if (self.resolution_id is None) != (self.resolution_record_sha256 is None):
            raise ValueError("resolution identity and digest must be present or absent together")
        if self.finality is ResolutionStatus.FINAL and self.resolution_id is None:
            raise ValueError("a final lifecycle observation must bind its resolution record")
        if self.ordinal == 1 and self.previous_observation_id is not None:
            raise ValueError("the first lifecycle observation cannot name a predecessor")
        if self.ordinal > 1 and self.previous_observation_id is None:
            raise ValueError("later lifecycle observations must name their predecessor")
        expected = build_lifecycle_observation_id(
            experiment_id=self.experiment_id,
            target_id=self.target_id,
            ordinal=self.ordinal,
            previous_observation_id=self.previous_observation_id,
            source=self.source,
            endpoint=self.endpoint,
            source_time=self.source_time,
            retrieved_at=self.retrieved_at,
            raw_payload_sha256=self.raw_payload_sha256,
            byte_length=self.byte_length,
            raw_payload_location=self.raw_payload_location,
            finality=self.finality,
            resolution_id=self.resolution_id,
            resolution_record_sha256=self.resolution_record_sha256,
        )
        if self.lifecycle_observation_id != expected:
            raise ValueError("lifecycle_observation_id disagrees with its evidence")
        return self


class ResolutionCutoffEvidenceV1(VersionedModel):
    """The exact source observation and rule that set one target's cutoff."""

    schema_version: ClassVar[str] = "resolution_cutoff_evidence.v1"

    cutoff_evidence_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    cutoff_basis: CutoffBasis
    lifecycle_observation_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    source_time: datetime | None = None
    retrieved_at: datetime
    selected_cutoff: datetime
    raw_payload_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    byte_length: int = Field(gt=0)
    raw_payload_location: str = Field(min_length=1)
    finality: ResolutionStatus
    resolution_id: str = Field(min_length=1)
    resolution_record_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)

    @field_validator("source_time", "retrieved_at", "selected_cutoff")
    @classmethod
    def _anchor_cutoff_times(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @model_validator(mode="after")
    def _cutoff_follows_adr_0014(self) -> ResolutionCutoffEvidenceV1:
        if self.finality is not ResolutionStatus.FINAL:
            raise ValueError("only a final lifecycle observation can establish a cutoff")
        if self.cutoff_basis is CutoffBasis.SOURCE_TERMINAL_TIMESTAMP:
            if self.source_time is None or self.selected_cutoff != self.source_time:
                raise ValueError("source-terminal cutoff must equal the source terminal time")
        elif self.selected_cutoff != self.retrieved_at:
            raise ValueError("first-observed-final cutoff must equal retrieval time")
        expected = build_cutoff_evidence_id(
            experiment_id=self.experiment_id,
            target_id=self.target_id,
            cutoff_basis=self.cutoff_basis,
            lifecycle_observation_id=self.lifecycle_observation_id,
            source=self.source,
            endpoint=self.endpoint,
            source_time=self.source_time,
            retrieved_at=self.retrieved_at,
            selected_cutoff=self.selected_cutoff,
            raw_payload_sha256=self.raw_payload_sha256,
            byte_length=self.byte_length,
            raw_payload_location=self.raw_payload_location,
            resolution_id=self.resolution_id,
            resolution_record_sha256=self.resolution_record_sha256,
        )
        if self.cutoff_evidence_id != expected:
            raise ValueError("cutoff_evidence_id disagrees with its source observation")
        return self


def build_target_id(
    *, experiment_id: str, market_id: str, condition_id: str, yes_token_id: str, no_token_id: str
) -> str:
    return _identity(
        "prospective_target_identity.v1",
        experiment_id,
        market_id,
        condition_id,
        yes_token_id,
        no_token_id,
        prefix="target",
    )


def build_persistence_receipt_id(
    *,
    experiment_id: str,
    artifact_kind: EvidenceArtifactKind,
    artifact_id: str,
    artifact_schema_version: str,
    artifact_sha256: str,
    artifact_byte_length: int,
    persisted_at: datetime,
    storage_backend: str,
    storage_identity: str,
) -> str:
    return _identity(
        "evidence_persistence_receipt_identity.v1",
        experiment_id,
        artifact_kind.value,
        artifact_id,
        artifact_schema_version,
        artifact_sha256,
        str(artifact_byte_length),
        ensure_utc(persisted_at).isoformat(),
        storage_backend,
        storage_identity,
        prefix="receipt",
    )


def build_lifecycle_observation_id(
    *,
    experiment_id: str,
    target_id: str,
    ordinal: int,
    previous_observation_id: str | None,
    source: str,
    endpoint: str,
    source_time: datetime | None,
    retrieved_at: datetime,
    raw_payload_sha256: str,
    byte_length: int,
    raw_payload_location: str,
    finality: ResolutionStatus,
    resolution_id: str | None,
    resolution_record_sha256: str | None,
) -> str:
    return _identity(
        "lifecycle_observation_identity.v1",
        experiment_id,
        target_id,
        str(ordinal),
        previous_observation_id or "",
        source,
        endpoint,
        ensure_utc(source_time).isoformat() if source_time else "",
        ensure_utc(retrieved_at).isoformat(),
        raw_payload_sha256,
        str(byte_length),
        raw_payload_location,
        finality.value,
        resolution_id or "",
        resolution_record_sha256 or "",
        prefix="lifecycle",
    )


def build_cutoff_evidence_id(
    *,
    experiment_id: str,
    target_id: str,
    cutoff_basis: CutoffBasis,
    lifecycle_observation_id: str,
    source: str,
    endpoint: str,
    source_time: datetime | None,
    retrieved_at: datetime,
    selected_cutoff: datetime,
    raw_payload_sha256: str,
    byte_length: int,
    raw_payload_location: str,
    resolution_id: str,
    resolution_record_sha256: str,
) -> str:
    return _identity(
        "resolution_cutoff_evidence_identity.v1",
        experiment_id,
        target_id,
        cutoff_basis.value,
        lifecycle_observation_id,
        source,
        endpoint,
        ensure_utc(source_time).isoformat() if source_time else "",
        ensure_utc(retrieved_at).isoformat(),
        ensure_utc(selected_cutoff).isoformat(),
        raw_payload_sha256,
        str(byte_length),
        raw_payload_location,
        resolution_id,
        resolution_record_sha256,
        prefix="cutoff",
    )


def persist_evidence_record(
    directory: Path,
    *,
    record: VersionedModel,
    experiment_id: str,
    artifact_kind: EvidenceArtifactKind,
    artifact_id: str,
    persisted_at: datetime,
) -> EvidencePersistenceReceiptV1:
    """Durably write canonical record bytes, read them back, and issue a receipt.

    The receipt uses the archive sidecar's retrieval time after the read-back.
    Therefore an idempotent second call preserves the first durable time rather
    than pretending identical bytes were first persisted again later.
    """
    raw = _record_bytes(record)
    digest = sha256_hex(raw)
    provenance = SourceProvenanceV1(
        source="argos_evidence",
        endpoint=f"argos-evidence://{record.schema_version}/{artifact_id}",
        retrieved_at=ensure_utc(persisted_at),
        raw_sha256=digest,
        byte_length=len(raw),
    )
    write_raw_payload(directory, raw=raw, provenance=provenance)
    reloaded, stored_provenance = read_raw_payload(directory, digest)
    if reloaded != raw or stored_provenance.reconstructed:
        raise ImmutabilityViolationError(
            "prospective evidence did not reload as the first-hand canonical bytes",
            artifact_id=artifact_id,
        )
    storage_identity = archive_relative_location(stored_provenance)
    persisted = stored_provenance.retrieved_at
    receipt_id = build_persistence_receipt_id(
        experiment_id=experiment_id,
        artifact_kind=artifact_kind,
        artifact_id=artifact_id,
        artifact_schema_version=record.schema_version,
        artifact_sha256=digest,
        artifact_byte_length=len(raw),
        persisted_at=persisted,
        storage_backend="content_addressed_raw_archive.v1",
        storage_identity=storage_identity,
    )
    return EvidencePersistenceReceiptV1(
        receipt_id=receipt_id,
        experiment_id=experiment_id,
        artifact_kind=artifact_kind,
        artifact_id=artifact_id,
        artifact_schema_version=record.schema_version,
        artifact_sha256=digest,
        artifact_byte_length=len(raw),
        persisted_at=persisted,
        storage_identity=storage_identity,
    )


def load_persisted_record[RecordT: VersionedModel](
    directory: Path, receipt: EvidencePersistenceReceiptV1, model: type[RecordT]
) -> RecordT:
    """Reload and validate the exact record named by ``receipt``."""
    if receipt.artifact_schema_version != model.schema_version:
        raise StorageError(
            "receipt schema does not match the requested evidence model",
            receipt_schema=receipt.artifact_schema_version,
            requested_schema=model.schema_version,
        )
    raw, provenance = read_raw_payload(directory, receipt.artifact_sha256)
    if archive_relative_location(provenance) != receipt.storage_identity:
        raise ImmutabilityViolationError(
            "receipt storage identity disagrees with the archived evidence",
            receipt_id=receipt.receipt_id,
        )
    if provenance.retrieved_at != receipt.persisted_at or not provenance.matches(raw):
        raise ImmutabilityViolationError(
            "receipt persistence facts disagree with the archived evidence",
            receipt_id=receipt.receipt_id,
        )
    try:
        decoded = orjson.loads(raw)
    except orjson.JSONDecodeError as error:  # pragma: no cover - archive verifies bytes first
        raise StorageError(
            "persisted evidence is not JSON", receipt_id=receipt.receipt_id
        ) from error
    if not isinstance(decoded, dict):
        raise StorageError(
            "persisted evidence record is not an object", receipt_id=receipt.receipt_id
        )
    restored = model.from_record(decoded)
    verify_receipt_for_record(receipt, restored)
    return restored


def verify_receipt_for_record(
    receipt: EvidencePersistenceReceiptV1, record: VersionedModel
) -> None:
    """Cross-check a receipt against canonical record bytes."""
    raw = _record_bytes(record)
    if (
        receipt.artifact_schema_version != record.schema_version
        or receipt.artifact_sha256 != sha256_hex(raw)
        or receipt.artifact_byte_length != len(raw)
    ):
        raise ValueError("persistence receipt disagrees with its canonical artifact record")


def _record_bytes(record: VersionedModel) -> bytes:
    return orjson.dumps(record.to_record(), option=orjson.OPT_SORT_KEYS)


def _identity(version: str, *parts: str, prefix: str) -> str:
    material = (version, *parts)
    encoded = "|".join(f"{len(part)}:{part}" for part in material)
    return f"{prefix}-{hashlib.sha256(encoded.encode()).hexdigest()[:32]}"
