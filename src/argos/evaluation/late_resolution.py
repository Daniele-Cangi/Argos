"""Append-only outcome evidence for a forecast frozen before late finality.

ADR-0019 does not let a later settlement rewrite a forecast or invent a
historical first-final cutoff. These two versioned records bind the original
forecast bytes and the complete observed lifecycle chain. The final cutoff is
the *actual retrieval time* of the first recorded final observation, including
when that time is after the earlier operational lifecycle deadline.

This module only authenticates the temporal boundary; scoring is a separate
operation and must independently check the capture and replay evidence.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, ClassVar

import orjson
from pydantic import Field, field_validator, model_validator

from argos.baselines import BaselineMethod, MarketBaselineForecastV2
from argos.clock import ensure_utc
from argos.domain.provenance import SHA256_LENGTH
from argos.domain.versioning import VersionedModel, ensure_supported_version
from argos.evaluation.prospective import (
    CutoffBasis,
    EvidenceArtifactKind,
    EvidencePersistenceReceiptV1,
    LifecycleObservationV1,
    ProspectiveExperimentProtocolV2,
    ProspectiveTargetV1,
    verify_receipt_for_record,
)
from argos.resolution import ResolutionStatus, ResolutionV1

__all__ = [
    "FrozenForecastSnapshotV1",
    "LateFinalOutcomeV1",
    "build_frozen_forecast_snapshot_id",
    "build_late_final_outcome_id",
]


def _digest(value: object) -> str:
    return hashlib.sha256(orjson.dumps(value, option=orjson.OPT_SORT_KEYS)).hexdigest()


def _identity(prefix: str, *parts: object) -> str:
    return f"{prefix}-{_digest(parts)[:32]}"


def build_frozen_forecast_snapshot_id(
    *,
    target: ProspectiveTargetV1,
    target_receipt: EvidencePersistenceReceiptV1,
    capture_run_id: str,
    capture_manifest_sha256: str,
    code_revision: str,
    config_fingerprint: str,
    forecasts: tuple[MarketBaselineForecastV2, ...],
    frozen_at: datetime,
) -> str:
    return _identity(
        "frozen-forecast",
        "frozen_forecast_snapshot_identity.v1",
        target.target_id,
        target_receipt.receipt_id,
        capture_run_id,
        capture_manifest_sha256,
        code_revision,
        config_fingerprint,
        [forecast.to_record() for forecast in forecasts],
        ensure_utc(frozen_at).isoformat(),
    )


class FrozenForecastSnapshotV1(VersionedModel):
    """One durable, pre-final set of last observed baselines for one target."""

    schema_version: ClassVar[str] = "frozen_forecast_snapshot.v1"

    snapshot_id: str = Field(min_length=1)
    target: ProspectiveTargetV1
    target_receipt: EvidencePersistenceReceiptV1
    capture_run_id: str = Field(min_length=1)
    capture_manifest_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    code_revision: str = Field(min_length=7, max_length=64)
    config_fingerprint: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    forecasts: tuple[MarketBaselineForecastV2, ...]
    frozen_at: datetime

    @field_validator("frozen_at")
    @classmethod
    def _utc_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("capture_manifest_sha256", "config_fingerprint")
    @classmethod
    def _hex_digest(cls, value: str) -> str:
        lowered = value.lower()
        if not all(character in "0123456789abcdef" for character in lowered):
            raise ValueError("snapshot hashes must be hexadecimal")
        return lowered

    @model_validator(mode="after")
    def _snapshot_is_one_frozen_information_state(self) -> FrozenForecastSnapshotV1:
        verify_receipt_for_record(self.target_receipt, self.target)
        if (
            self.target_receipt.experiment_id != self.target.experiment_id
            or self.target_receipt.artifact_kind is not EvidenceArtifactKind.TARGET_DECLARATION
            or self.target_receipt.artifact_id != self.target.target_id
        ):
            raise ValueError("snapshot target receipt names different evidence")
        if self.frozen_at < max(self.target.selected_at, self.target_receipt.persisted_at):
            raise ValueError("forecast snapshot cannot predate target persistence")
        if len(self.forecasts) != len(BaselineMethod) or {
            forecast.method for forecast in self.forecasts
        } != set(BaselineMethod):
            raise ValueError("forecast snapshot requires one record per declared baseline")
        state_keys = {
            (
                forecast.evaluation_run_id,
                forecast.as_of_ingest_sequence,
                forecast.information_state_hash,
                forecast.source_observation_id,
            )
            for forecast in self.forecasts
        }
        if len(state_keys) != 1:
            raise ValueError("snapshot baselines come from different information states")
        for forecast in self.forecasts:
            if (
                forecast.source_capture_run_id != self.capture_run_id
                or forecast.market_id != self.target.market_id
                or forecast.condition_id != self.target.condition_id
                or forecast.token_id != self.target.yes_token_id
                or forecast.contract_id != self.target.contract_id
            ):
                raise ValueError("snapshot forecast names a different target or capture")
            if not (
                self.target_receipt.persisted_at <= forecast.as_of_received_time <= self.frozen_at
            ):
                raise ValueError(
                    "forecast was not available after target receipt and before freeze"
                )
        expected = build_frozen_forecast_snapshot_id(
            target=self.target,
            target_receipt=self.target_receipt,
            capture_run_id=self.capture_run_id,
            capture_manifest_sha256=self.capture_manifest_sha256,
            code_revision=self.code_revision,
            config_fingerprint=self.config_fingerprint,
            forecasts=self.forecasts,
            frozen_at=self.frozen_at,
        )
        if self.snapshot_id != expected:
            raise ValueError("snapshot identity disagrees with frozen forecast evidence")
        return self

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        record["target"] = self.target.to_record()
        record["target_receipt"] = self.target_receipt.to_record()
        record["forecasts"] = [forecast.to_record() for forecast in self.forecasts]
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> FrozenForecastSnapshotV1:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["target"] = ProspectiveTargetV1.from_record(dict(payload["target"]))
        payload["target_receipt"] = EvidencePersistenceReceiptV1.from_record(
            dict(payload["target_receipt"])
        )
        payload["forecasts"] = tuple(
            MarketBaselineForecastV2.from_record(dict(item)) for item in payload["forecasts"]
        )
        return cls.model_validate(payload)


def build_late_final_outcome_id(
    *,
    protocol_receipt: EvidencePersistenceReceiptV1,
    snapshot_receipt: EvidencePersistenceReceiptV1,
    lifecycle_receipts: tuple[EvidencePersistenceReceiptV1, ...],
    resolution: ResolutionV1,
    selected_cutoff: datetime,
) -> str:
    return _identity(
        "late-final",
        "late_final_outcome_identity.v1",
        protocol_receipt.receipt_id,
        snapshot_receipt.receipt_id,
        [receipt.receipt_id for receipt in lifecycle_receipts],
        resolution.to_record(),
        ensure_utc(selected_cutoff).isoformat(),
    )


class LateFinalOutcomeV1(VersionedModel):
    """First observed finality after the old deadline, never backdated."""

    schema_version: ClassVar[str] = "late_final_outcome.v1"

    late_outcome_id: str = Field(min_length=1)
    protocol: ProspectiveExperimentProtocolV2
    protocol_receipt: EvidencePersistenceReceiptV1
    snapshot: FrozenForecastSnapshotV1
    snapshot_receipt: EvidencePersistenceReceiptV1
    lifecycle_observations: tuple[LifecycleObservationV1, ...]
    lifecycle_receipts: tuple[EvidencePersistenceReceiptV1, ...]
    resolution: ResolutionV1
    selected_cutoff: datetime

    @field_validator("selected_cutoff")
    @classmethod
    def _utc_cutoff(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _late_finality_is_bound_to_frozen_evidence(self) -> LateFinalOutcomeV1:
        verify_receipt_for_record(self.protocol_receipt, self.protocol)
        if (
            self.protocol_receipt.experiment_id != self.protocol.experiment_id
            or self.protocol_receipt.artifact_kind is not EvidenceArtifactKind.EXPERIMENT_PROTOCOL
            or self.protocol_receipt.artifact_id != self.protocol.experiment_id
        ):
            raise ValueError("late outcome protocol receipt names different evidence")
        verify_receipt_for_record(self.snapshot_receipt, self.snapshot)
        if (
            self.snapshot_receipt.experiment_id != self.protocol.experiment_id
            or self.snapshot_receipt.artifact_kind
            is not EvidenceArtifactKind.FROZEN_FORECAST_SNAPSHOT
            or self.snapshot_receipt.artifact_id != self.snapshot.snapshot_id
        ):
            raise ValueError("late outcome snapshot receipt names different evidence")
        if self.snapshot.target.experiment_id != self.protocol.experiment_id:
            raise ValueError("late outcome snapshot belongs to a different experiment")
        if self.protocol_receipt.persisted_at > self.snapshot.target.selected_at:
            raise ValueError("protocol was not durable before target selection")
        if self.snapshot.target.cutoff_basis is not self.protocol.cutoff_basis:
            raise ValueError("target cutoff basis disagrees with frozen protocol")
        if (
            self.snapshot.code_revision != self.protocol.code_revision
            or self.snapshot.config_fingerprint != self.protocol.config_fingerprint
        ):
            raise ValueError("late outcome snapshot disagrees with frozen protocol revision")
        if self.protocol.cutoff_basis is not CutoffBasis.FIRST_OBSERVED_FINAL_SETTLEMENT:
            raise ValueError("late finality requires first-observed-final cutoff policy")
        if self.snapshot.frozen_at > self.protocol.lifecycle_deadline:
            raise ValueError("forecast was not frozen before the old lifecycle deadline")
        if any(
            forecast.as_of_received_time > self.protocol.observation_window_end
            for forecast in self.snapshot.forecasts
        ):
            raise ValueError("snapshot includes a post-window forecast")
        if self.snapshot_receipt.persisted_at < self.snapshot.frozen_at:
            raise ValueError("snapshot receipt predates the forecast freeze")
        if not self.lifecycle_observations or len(self.lifecycle_observations) != len(
            self.lifecycle_receipts
        ):
            raise ValueError("late outcome needs a complete lifecycle receipt partition")
        previous: LifecycleObservationV1 | None = None
        for observation, receipt in zip(
            self.lifecycle_observations, self.lifecycle_receipts, strict=True
        ):
            verify_receipt_for_record(receipt, observation)
            if (
                observation.experiment_id != self.protocol.experiment_id
                or observation.target_id != self.snapshot.target.target_id
                or receipt.experiment_id != self.protocol.experiment_id
                or receipt.artifact_kind is not EvidenceArtifactKind.LIFECYCLE_OBSERVATION
                or receipt.artifact_id != observation.lifecycle_observation_id
            ):
                raise ValueError("late lifecycle chain names a different target or artifact")
            if observation.ordinal != (previous.ordinal + 1 if previous else 1):
                raise ValueError("late lifecycle ordinal chain has a gap")
            if observation.previous_observation_id != (
                previous.lifecycle_observation_id if previous else None
            ):
                raise ValueError("late lifecycle predecessor chain is broken")
            if previous and observation.retrieved_at < previous.retrieved_at:
                raise ValueError("late lifecycle retrieval time regressed")
            if previous and self.lifecycle_receipts[previous.ordinal - 1].persisted_at > (
                observation.retrieved_at
            ):
                raise ValueError("prior lifecycle observation was not durable before next poll")
            if receipt.persisted_at < observation.retrieved_at:
                raise ValueError("lifecycle receipt predates source retrieval")
            if previous and previous.finality is ResolutionStatus.FINAL:
                raise ValueError("a final observation already existed earlier in the chain")
            previous = observation
        assert previous is not None
        if previous.finality is not ResolutionStatus.FINAL:
            raise ValueError("late outcome has no final lifecycle observation")
        if self.selected_cutoff != previous.retrieved_at:
            raise ValueError("late first-observed cutoff cannot be backdated")
        if self.selected_cutoff <= self.protocol.lifecycle_deadline:
            raise ValueError("late outcome must follow the old lifecycle deadline")
        if self.snapshot_receipt.persisted_at >= self.selected_cutoff:
            raise ValueError("forecast snapshot was not durable before finality")
        if self.resolution.resolution_status is not ResolutionStatus.FINAL:
            raise ValueError("only final resolution may become a late outcome")
        if (
            previous.resolution_id != self.resolution.resolution_id
            or previous.resolution_record_sha256 != _digest(self.resolution.to_record())
            or previous.raw_payload_sha256 != self.resolution.source_payload_sha256
            or previous.source_time != self.resolution.resolved_at
            or self.resolution.market_id != self.snapshot.target.market_id
            or self.resolution.condition_id != self.snapshot.target.condition_id
            or self.resolution.winning_token_id
            not in (self.snapshot.target.yes_token_id, self.snapshot.target.no_token_id)
        ):
            raise ValueError("late resolution disagrees with source or target mapping")
        if self.resolution.normalized_at < previous.retrieved_at:
            raise ValueError("late resolution normalization predates source retrieval")
        if self.resolution.normalized_at > self.lifecycle_receipts[-1].persisted_at:
            raise ValueError("late resolution normalization postdates source receipt")
        if (
            self.resolution.resolved_at is not None
            and self.resolution.resolved_at < self.snapshot.frozen_at
        ):
            raise ValueError("forecast was frozen after the source terminal timestamp")
        expected = build_late_final_outcome_id(
            protocol_receipt=self.protocol_receipt,
            snapshot_receipt=self.snapshot_receipt,
            lifecycle_receipts=self.lifecycle_receipts,
            resolution=self.resolution,
            selected_cutoff=self.selected_cutoff,
        )
        if self.late_outcome_id != expected:
            raise ValueError("late outcome identity disagrees with immutable evidence")
        return self

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        record["protocol"] = self.protocol.to_record()
        record["protocol_receipt"] = self.protocol_receipt.to_record()
        record["snapshot"] = self.snapshot.to_record()
        record["snapshot_receipt"] = self.snapshot_receipt.to_record()
        record["lifecycle_observations"] = [
            observation.to_record() for observation in self.lifecycle_observations
        ]
        record["lifecycle_receipts"] = [receipt.to_record() for receipt in self.lifecycle_receipts]
        record["resolution"] = self.resolution.to_record()
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> LateFinalOutcomeV1:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["protocol"] = ProspectiveExperimentProtocolV2.from_record(dict(payload["protocol"]))
        payload["protocol_receipt"] = EvidencePersistenceReceiptV1.from_record(
            dict(payload["protocol_receipt"])
        )
        payload["snapshot"] = FrozenForecastSnapshotV1.from_record(dict(payload["snapshot"]))
        payload["snapshot_receipt"] = EvidencePersistenceReceiptV1.from_record(
            dict(payload["snapshot_receipt"])
        )
        payload["lifecycle_observations"] = tuple(
            LifecycleObservationV1.from_record(dict(item))
            for item in payload["lifecycle_observations"]
        )
        payload["lifecycle_receipts"] = tuple(
            EvidencePersistenceReceiptV1.from_record(dict(item))
            for item in payload["lifecycle_receipts"]
        )
        payload["resolution"] = ResolutionV1.from_record(dict(payload["resolution"]))
        return cls.model_validate(payload)
