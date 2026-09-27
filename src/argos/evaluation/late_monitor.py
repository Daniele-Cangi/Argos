"""Exclusive, resumable late-resolution polling and pending/final scoring.

The frozen forecast remains unchanged while this owner appends Gamma lifecycle
observations. Checkpoint recovery is evidence-led: already archived polls are
reconciled before another request can be issued. A late final cutoff is always
the retrieval time of the first persisted final observation, never Gamma's
historical ``updatedAt`` value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Protocol
from urllib.parse import urlparse

import orjson
from pydantic import Field, field_validator, model_validator

from argos.clock import Clock, ensure_utc
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.domain.versioning import VersionedModel, ensure_supported_version, resolve_schema
from argos.errors import ContractViolationError
from argos.evaluation.bundle import record_sha256
from argos.evaluation.late_resolution import (
    FrozenForecastSnapshotV1,
    LateFinalOutcomeV1,
    build_late_final_outcome_id,
)
from argos.evaluation.late_retrieval import (
    LifecyclePollRetrievalV1,
    build_lifecycle_poll_retrieval_id,
    load_lifecycle_poll_retrievals,
    verify_lifecycle_poll_retrieval,
)
from argos.evaluation.numeric import require_epsilon
from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    EvidencePersistenceReceiptV1,
    LifecycleObservationV1,
    ProspectiveExperimentProtocolV2,
    ProspectiveTargetV1,
    build_lifecycle_observation_id,
    build_persistence_receipt_id,
    persist_evidence_record,
    verify_receipt_for_record,
)
from argos.evaluation.scoring import (
    ForecastEvaluationV2,
    score_forecast_v2,
)
from argos.monitoring.resumable import (
    MonitorGapV1,
    PollCommit,
    ResumableMonitor,
    ResumableMonitorCheckpointV1,
    checkpoint_after_failure,
)
from argos.resolution import (
    ResolutionStatus,
    ResolutionV1,
    WinningOutcome,
    normalize_gamma_resolution,
)
from argos.sources.gamma import GammaResponse
from argos.store.raw_archive import (
    archive_relative_location,
    read_raw_payload,
    write_raw_payload,
)

__all__ = [
    "LateLifecycleMonitor",
    "LateMonitoringScheduleV1",
    "LateResolutionProgressV1",
    "LateScoreDisposition",
    "LateScoringResultV1",
    "LifecycleMonitorGapEvidenceV1",
    "LifecyclePollResult",
    "LifecycleReceiptChainLinkV1",
    "build_late_monitoring_schedule_id",
    "pending_late_resolution_score",
    "score_late_final_outcome",
]


class LifecycleMarketSource(Protocol):
    async def get_market(self, market_id: str) -> GammaResponse: ...


def _identity(prefix: str, fields: Mapping[str, Any]) -> str:
    material = {"identity_version": f"{prefix}.v1", **fields}
    return f"{prefix}-{record_sha256(material)[:32]}"


def _normalize_gamma_base_url(value: str) -> str:
    parsed = urlparse(value.strip().rstrip("/"))
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("late-monitor Gamma base URL must be an HTTPS origin")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("late-monitor Gamma base URL has an invalid port") from error
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    authority = host if port in (None, 443) else f"{host}:{port}"
    return f"https://{authority}"


def build_late_monitoring_schedule_id(
    *,
    experiment_id: str,
    target_id: str,
    protocol_receipt_id: str,
    effective_at: datetime,
    first_poll_at: datetime,
    poll_interval_seconds: int,
    maximum_gap_multiple: int,
    owner_code_revision: str,
    config_fingerprint: str,
    gamma_base_url: str,
) -> str:
    return _identity(
        "late-monitoring-schedule",
        {
            "experiment_id": experiment_id,
            "target_id": target_id,
            "protocol_receipt_id": protocol_receipt_id,
            "effective_at": ensure_utc(effective_at).isoformat(),
            "first_poll_at": ensure_utc(first_poll_at).isoformat(),
            "poll_interval_seconds": poll_interval_seconds,
            "maximum_gap_multiple": maximum_gap_multiple,
            "owner_code_revision": owner_code_revision.lower(),
            "config_fingerprint": config_fingerprint.lower(),
            "gamma_base_url": _normalize_gamma_base_url(gamma_base_url),
        },
    )


class LateMonitoringScheduleV1(VersionedModel):
    """Durable lower-frequency follow-up, frozen after the original deadline."""

    schema_version: ClassVar[str] = "late_monitoring_schedule.v1"

    schedule_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    protocol_receipt_id: str = Field(min_length=1)
    effective_at: datetime
    first_poll_at: datetime
    poll_interval_seconds: int = Field(gt=0)
    maximum_gap_multiple: int = Field(ge=2)
    owner_code_revision: str = Field(min_length=7, max_length=64)
    config_fingerprint: str = Field(min_length=64, max_length=64)
    gamma_base_url: str = Field(min_length=1)
    created_at: datetime

    @field_validator("effective_at", "first_poll_at", "created_at")
    @classmethod
    def _utc_times(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("config_fingerprint")
    @classmethod
    def _hex_fingerprint(cls, value: str) -> str:
        lowered = value.lower()
        if not all(character in "0123456789abcdef" for character in lowered):
            raise ValueError("late monitoring config fingerprint must be hexadecimal")
        return lowered

    @field_validator("owner_code_revision")
    @classmethod
    def _hex_owner_revision(cls, value: str) -> str:
        lowered = value.lower()
        if not all(character in "0123456789abcdef" for character in lowered):
            raise ValueError("late monitoring owner revision must be hexadecimal")
        return lowered

    @field_validator("gamma_base_url")
    @classmethod
    def _frozen_gamma_origin(cls, value: str) -> str:
        return _normalize_gamma_base_url(value)

    @model_validator(mode="after")
    def _schedule_is_frozen_and_identifiable(self) -> LateMonitoringScheduleV1:
        if self.created_at > self.effective_at:
            raise ValueError("late cadence must be persisted before it takes effect")
        if self.first_poll_at < self.effective_at:
            raise ValueError("first late poll cannot precede the schedule")
        expected = build_late_monitoring_schedule_id(
            experiment_id=self.experiment_id,
            target_id=self.target_id,
            protocol_receipt_id=self.protocol_receipt_id,
            effective_at=self.effective_at,
            first_poll_at=self.first_poll_at,
            poll_interval_seconds=self.poll_interval_seconds,
            maximum_gap_multiple=self.maximum_gap_multiple,
            owner_code_revision=self.owner_code_revision,
            config_fingerprint=self.config_fingerprint,
            gamma_base_url=self.gamma_base_url,
        )
        if self.schedule_id != expected:
            raise ValueError("late monitoring schedule identity disagrees with its fields")
        return self


def _gap_id(
    *,
    schedule_id: str,
    attempted_ordinal: int,
    started_at: datetime,
    ended_at: datetime,
    predecessor_observation_id: str | None,
    predecessor_receipt_id: str | None,
    reason: str,
) -> str:
    return _identity(
        "lifecycle-monitor-gap",
        {
            "schedule_id": schedule_id,
            "attempted_ordinal": attempted_ordinal,
            "started_at": ensure_utc(started_at).isoformat(),
            "ended_at": ensure_utc(ended_at).isoformat(),
            "predecessor_observation_id": predecessor_observation_id,
            "predecessor_receipt_id": predecessor_receipt_id,
            "reason": reason,
        },
    )


class LifecycleMonitorGapEvidenceV1(VersionedModel):
    """Append-only evidence of a failed or materially missed polling interval."""

    schema_version: ClassVar[str] = "lifecycle_monitor_gap.v1"

    gap_id: str = Field(min_length=1)
    schedule_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    attempted_ordinal: int = Field(gt=0)
    started_at: datetime
    ended_at: datetime
    predecessor_observation_id: str | None = Field(default=None, min_length=1)
    predecessor_receipt_id: str | None = Field(default=None, min_length=1)
    reason: str = Field(min_length=1, max_length=1024)

    @field_validator("started_at", "ended_at")
    @classmethod
    def _utc_times(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _gap_is_well_formed(self) -> LifecycleMonitorGapEvidenceV1:
        if self.ended_at < self.started_at:
            raise ValueError("monitor gap end cannot precede its start")
        expected = _gap_id(
            schedule_id=self.schedule_id,
            attempted_ordinal=self.attempted_ordinal,
            started_at=self.started_at,
            ended_at=self.ended_at,
            predecessor_observation_id=self.predecessor_observation_id,
            predecessor_receipt_id=self.predecessor_receipt_id,
            reason=self.reason,
        )
        if self.gap_id != expected:
            raise ValueError("monitor gap identity disagrees with its evidence")
        return self


def _receipt_link_id(
    *,
    schedule_id: str,
    observation: LifecycleObservationV1,
    observation_receipt: EvidencePersistenceReceiptV1,
    previous_receipt_id: str | None,
    linked_at: datetime,
) -> str:
    return _identity(
        "lifecycle-receipt-link",
        {
            "schedule_id": schedule_id,
            "observation_id": observation.lifecycle_observation_id,
            "observation_receipt_id": observation_receipt.receipt_id,
            "ordinal": observation.ordinal,
            "previous_receipt_id": previous_receipt_id,
            "linked_at": ensure_utc(linked_at).isoformat(),
        },
    )


class LifecycleReceiptChainLinkV1(VersionedModel):
    """A durable predecessor link for one lifecycle observation receipt."""

    schema_version: ClassVar[str] = "lifecycle_receipt_chain_link.v1"

    link_id: str = Field(min_length=1)
    schedule_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    ordinal: int = Field(gt=0)
    observation: LifecycleObservationV1
    observation_receipt: EvidencePersistenceReceiptV1
    previous_receipt_id: str | None = Field(default=None, min_length=1)
    linked_at: datetime

    @field_validator("linked_at")
    @classmethod
    def _utc_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _receipt_link_is_coherent(self) -> LifecycleReceiptChainLinkV1:
        observation = self.observation
        receipt = self.observation_receipt
        verify_receipt_for_record(receipt, observation)
        if (
            observation.experiment_id != self.experiment_id
            or observation.target_id != self.target_id
            or observation.ordinal != self.ordinal
            or receipt.experiment_id != self.experiment_id
            or receipt.artifact_kind is not EvidenceArtifactKind.LIFECYCLE_OBSERVATION
            or receipt.artifact_id != observation.lifecycle_observation_id
        ):
            raise ValueError("receipt link names different lifecycle evidence")
        if self.ordinal == 1 and self.previous_receipt_id is not None:
            raise ValueError("first lifecycle receipt cannot have a predecessor")
        if self.ordinal > 1 and self.previous_receipt_id is None:
            raise ValueError("later lifecycle receipts require a predecessor")
        if self.linked_at < receipt.persisted_at:
            raise ValueError("receipt link predates the lifecycle receipt")
        expected = _receipt_link_id(
            schedule_id=self.schedule_id,
            observation=observation,
            observation_receipt=receipt,
            previous_receipt_id=self.previous_receipt_id,
            linked_at=self.linked_at,
        )
        if self.link_id != expected:
            raise ValueError("receipt link identity disagrees with its fields")
        return self

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        record["observation"] = self.observation.to_record()
        record["observation_receipt"] = self.observation_receipt.to_record()
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> LifecycleReceiptChainLinkV1:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["observation"] = LifecycleObservationV1.from_record(dict(payload["observation"]))
        payload["observation_receipt"] = EvidencePersistenceReceiptV1.from_record(
            dict(payload["observation_receipt"])
        )
        return cls.model_validate(payload)


class LateResolutionStatus(StrEnum):
    PENDING_RESOLUTION = "pending_resolution"
    FINAL = "final"


class LateResolutionProgressV1(VersionedModel):
    """Derived progress view; unresolved targets stay explicitly pending."""

    schema_version: ClassVar[str] = "late_resolution_progress.v1"

    experiment_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    status: LateResolutionStatus
    last_observation_id: str | None = Field(default=None, min_length=1)
    last_observed_finality: ResolutionStatus
    last_observed_at: datetime | None = None
    late_outcome_id: str | None = Field(default=None, min_length=1)
    selected_cutoff: datetime | None = None
    assessed_at: datetime

    @field_validator("last_observed_at", "selected_cutoff", "assessed_at")
    @classmethod
    def _utc_times(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @model_validator(mode="after")
    def _progress_matches_disposition(self) -> LateResolutionProgressV1:
        if self.status is LateResolutionStatus.PENDING_RESOLUTION:
            if self.last_observed_finality is ResolutionStatus.FINAL:
                raise ValueError("a final observation cannot be reported as pending")
            if self.late_outcome_id is not None or self.selected_cutoff is not None:
                raise ValueError("pending target cannot carry a final outcome or cutoff")
        else:
            if self.last_observation_id is None:
                raise ValueError("final progress requires a bound observation ID")
            if self.late_outcome_id is None:
                raise ValueError("final progress requires a bound outcome and actual cutoff")
            if (
                self.last_observed_finality is not ResolutionStatus.FINAL
                or self.selected_cutoff is None
                or self.last_observed_at != self.selected_cutoff
            ):
                raise ValueError("final progress requires a final observation and actual cutoff")
        return self


class LateScoreDisposition(StrEnum):
    PENDING_RESOLUTION = "pending_resolution"
    SCORED = "scored"
    FINAL_UNSCORABLE = "final_unscorable"


class LateScoringResultV1(VersionedModel):
    """Per-target score bound to the immutable forecast snapshot."""

    schema_version: ClassVar[str] = "late_scoring_result.v1"

    result_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    snapshot: FrozenForecastSnapshotV1
    late_outcome_id: str | None = Field(default=None, min_length=1)
    late_outcome: LateFinalOutcomeV1 | None = None
    disposition: LateScoreDisposition
    latest_finality: ResolutionStatus
    planned_forecast_count: int = Field(gt=0)
    evaluations: tuple[ForecastEvaluationV2, ...] = ()
    abstained_forecast_ids: tuple[str, ...] = ()
    created_at: datetime
    log_loss_epsilon: Decimal | None = None

    @field_validator("created_at")
    @classmethod
    def _utc_created_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("log_loss_epsilon")
    @classmethod
    def _valid_log_loss_epsilon(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        try:
            return require_epsilon(value)
        except ContractViolationError as error:
            raise ValueError(error.message) from error

    @model_validator(mode="after")
    def _scoring_state_is_truthful(self) -> LateScoringResultV1:
        if (
            self.snapshot.snapshot_id != self.snapshot_id
            or self.snapshot.target.experiment_id != self.experiment_id
            or self.snapshot.target.target_id != self.target_id
        ):
            raise ValueError("late score is not bound to its frozen forecast snapshot")
        forecasts = {forecast.forecast_id: forecast for forecast in self.snapshot.forecasts}
        if self.planned_forecast_count != len(forecasts):
            raise ValueError("late score denominator disagrees with the frozen snapshot")
        if self.disposition is LateScoreDisposition.PENDING_RESOLUTION:
            if (
                self.late_outcome_id is not None
                or self.late_outcome is not None
                or self.latest_finality is ResolutionStatus.FINAL
                or self.evaluations
                or self.abstained_forecast_ids
            ):
                raise ValueError("pending target cannot be scored or labeled as final")
            if self.log_loss_epsilon is not None:
                raise ValueError("pending target cannot declare a log-loss epsilon before finality")
        else:
            outcome = self.late_outcome
            if (
                self.late_outcome_id is None
                or outcome is None
                or self.latest_finality is not ResolutionStatus.FINAL
            ):
                raise ValueError("final scoring requires a bound final outcome")
            if (
                outcome.late_outcome_id != self.late_outcome_id
                or outcome.snapshot != self.snapshot
                or outcome.protocol.experiment_id != self.experiment_id
            ):
                raise ValueError("late score is not bound to its durable late outcome")
            if self.log_loss_epsilon is None:
                raise ValueError("final scoring requires the frozen protocol log-loss epsilon")
            if self.log_loss_epsilon != outcome.protocol.log_loss_epsilon:
                raise ValueError("late score epsilon disagrees with the frozen protocol")
            if self.created_at < outcome.selected_cutoff:
                raise ValueError("late score cannot predate its bound final outcome cutoff")
            if len(self.evaluations) + len(self.abstained_forecast_ids) != (
                self.planned_forecast_count
            ):
                raise ValueError("final score must account for every planned baseline")
            if self.disposition is LateScoreDisposition.SCORED and not self.evaluations:
                raise ValueError("scored disposition requires at least one evaluation")
            if self.disposition is LateScoreDisposition.FINAL_UNSCORABLE and self.evaluations:
                raise ValueError("unscorable final target cannot carry evaluations")
            forecast_ids = [item.forecast_id for item in self.evaluations]
            if len(set(forecast_ids)) != len(forecast_ids):
                raise ValueError("late evaluations must name unique frozen forecasts")
            if set(forecast_ids).intersection(self.abstained_forecast_ids):
                raise ValueError("a frozen forecast cannot be both scored and abstained")
            if any(item.log_loss_epsilon != self.log_loss_epsilon for item in self.evaluations):
                raise ValueError("late evaluations disagree with the declared clipping epsilon")
            accounted_ids = set(forecast_ids).union(self.abstained_forecast_ids)
            if accounted_ids != set(forecasts):
                raise ValueError("late score accounting does not match the frozen forecasts")
            for evaluation in self.evaluations:
                forecast = forecasts.get(evaluation.forecast_id)
                if forecast is None:
                    raise ValueError("late evaluation names no frozen forecast")
                if (
                    forecast.abstained
                    or forecast.raw_score is None
                    or evaluation.evaluation_run_id != forecast.evaluation_run_id
                    or evaluation.contract_id
                    != (forecast.contract_id or self.snapshot.target.contract_id)
                    or evaluation.forecast_method != forecast.method.value
                    or evaluation.condition_id != forecast.condition_id
                    or evaluation.token_id != forecast.token_id
                    or evaluation.score != forecast.raw_score
                    or evaluation.calibration_status != forecast.calibration_status.value
                ):
                    raise ValueError("late evaluation disagrees with its frozen forecast")
            abstained_forecasts = {
                forecast_id
                for forecast_id, forecast in forecasts.items()
                if forecast.abstained or forecast.raw_score is None
            }
            if set(self.abstained_forecast_ids) != abstained_forecasts:
                raise ValueError("late score abstentions disagree with the frozen snapshot")
            expected_outcome_yes = int(
                outcome.resolution.winning_token_id == self.snapshot.target.yes_token_id
            )
            if any(
                item.resolution_id != outcome.resolution.resolution_id
                or item.outcome_yes != expected_outcome_yes
                for item in self.evaluations
            ):
                raise ValueError("late evaluations disagree with their bound late outcome")
        if len(set(self.abstained_forecast_ids)) != len(self.abstained_forecast_ids):
            raise ValueError("abstained forecast ids must be unique")
        expected_id = _late_score_identity(
            {
                "experiment_id": self.experiment_id,
                "target_id": self.target_id,
                "snapshot_id": self.snapshot_id,
                "late_outcome_id": self.late_outcome_id,
                "late_outcome": self.late_outcome,
                "disposition": self.disposition,
                "latest_finality": self.latest_finality,
                "planned_forecast_count": self.planned_forecast_count,
                "evaluations": self.evaluations,
                "abstained_forecast_ids": self.abstained_forecast_ids,
                "created_at": self.created_at,
                "log_loss_epsilon": self.log_loss_epsilon,
            }
        )
        if self.result_id != expected_id:
            raise ValueError("late scoring result identity disagrees with its fields")
        return self

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        if self.log_loss_epsilon is None:
            record.pop("log_loss_epsilon", None)
        record["snapshot"] = self.snapshot.to_record()
        record["late_outcome"] = (
            self.late_outcome.to_record() if self.late_outcome is not None else None
        )
        record["evaluations"] = [item.to_record() for item in self.evaluations]
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> LateScoringResultV1:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["snapshot"] = FrozenForecastSnapshotV1.from_record(dict(payload["snapshot"]))
        if payload.get("late_outcome") is not None:
            payload["late_outcome"] = LateFinalOutcomeV1.from_record(dict(payload["late_outcome"]))
        payload["evaluations"] = tuple(
            ForecastEvaluationV2.from_record(dict(item)) for item in payload.get("evaluations", ())
        )
        payload["abstained_forecast_ids"] = tuple(payload.get("abstained_forecast_ids", ()))
        return cls.model_validate(payload)


def pending_late_resolution_score(
    snapshot: FrozenForecastSnapshotV1,
    *,
    latest_observation: LifecycleObservationV1 | None,
    created_at: datetime,
) -> LateScoringResultV1:
    """Represent a target in the denominator without turning pending into NO."""
    finality = latest_observation.finality if latest_observation else ResolutionStatus.UNKNOWN
    if latest_observation is not None and (
        latest_observation.experiment_id != snapshot.target.experiment_id
        or latest_observation.target_id != snapshot.target.target_id
    ):
        raise ValueError("pending progress observation names a different target")
    if finality is ResolutionStatus.FINAL:
        raise ValueError("final observations must be scored through a late outcome")
    fields: dict[str, Any] = {
        "experiment_id": snapshot.target.experiment_id,
        "target_id": snapshot.target.target_id,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot": snapshot,
        "late_outcome_id": None,
        "late_outcome": None,
        "disposition": LateScoreDisposition.PENDING_RESOLUTION,
        "latest_finality": finality,
        "planned_forecast_count": len(snapshot.forecasts),
        "evaluations": (),
        "abstained_forecast_ids": (),
        "created_at": ensure_utc(created_at),
        "log_loss_epsilon": None,
    }
    return LateScoringResultV1(result_id=_late_score_identity(fields), **fields)


def score_late_final_outcome(
    outcome: LateFinalOutcomeV1,
    *,
    created_at: datetime,
    epsilon: Decimal | None = None,
) -> LateScoringResultV1:
    """Score only the original frozen baselines after durable observed finality."""
    created = ensure_utc(created_at)
    frozen_epsilon = outcome.protocol.log_loss_epsilon
    selected_epsilon = frozen_epsilon if epsilon is None else epsilon
    require_epsilon(selected_epsilon)
    if selected_epsilon != frozen_epsilon:
        raise ValueError("late score epsilon must match the frozen protocol")
    if created < outcome.selected_cutoff:
        raise ValueError("late score cannot predate the observed final cutoff")
    winning = (
        WinningOutcome.YES
        if outcome.resolution.winning_token_id == outcome.snapshot.target.yes_token_id
        else WinningOutcome.NO
    )
    evaluations: list[ForecastEvaluationV2] = []
    abstained: list[str] = []
    for forecast in outcome.snapshot.forecasts:
        if forecast.abstained or forecast.raw_score is None:
            abstained.append(forecast.forecast_id)
            continue
        evaluations.append(
            score_forecast_v2(
                forecast_id=forecast.forecast_id,
                evaluation_run_id=forecast.evaluation_run_id,
                contract_id=forecast.contract_id or outcome.snapshot.target.contract_id,
                forecast_method=forecast.method.value,
                condition_id=forecast.condition_id,
                token_id=forecast.token_id,
                score=forecast.raw_score,
                resolution_id=outcome.resolution.resolution_id,
                winning_outcome=winning,
                calibration_status=forecast.calibration_status.value,
                created_at=created,
                epsilon=selected_epsilon,
            )
        )
    disposition = (
        LateScoreDisposition.SCORED if evaluations else LateScoreDisposition.FINAL_UNSCORABLE
    )
    fields: dict[str, Any] = {
        "experiment_id": outcome.protocol.experiment_id,
        "target_id": outcome.snapshot.target.target_id,
        "snapshot_id": outcome.snapshot.snapshot_id,
        "snapshot": outcome.snapshot,
        "late_outcome_id": outcome.late_outcome_id,
        "late_outcome": outcome,
        "disposition": disposition,
        "latest_finality": ResolutionStatus.FINAL,
        "planned_forecast_count": len(outcome.snapshot.forecasts),
        "evaluations": tuple(evaluations),
        "abstained_forecast_ids": tuple(abstained),
        "created_at": created,
        "log_loss_epsilon": selected_epsilon,
    }
    return LateScoringResultV1(result_id=_late_score_identity(fields), **fields)


def _late_score_identity(fields: Mapping[str, Any]) -> str:
    return _identity(
        "late-scoring-result",
        {
            "experiment_id": fields["experiment_id"],
            "target_id": fields["target_id"],
            "snapshot_id": fields["snapshot_id"],
            "late_outcome_id": fields["late_outcome_id"],
            "late_outcome": (
                fields["late_outcome"].to_record()
                if fields.get("late_outcome") is not None
                else None
            ),
            "disposition": LateScoreDisposition(fields["disposition"]).value,
            "latest_finality": ResolutionStatus(fields["latest_finality"]).value,
            "planned_forecast_count": fields["planned_forecast_count"],
            "evaluations": [item.to_record() for item in fields["evaluations"]],
            "abstained_forecast_ids": fields["abstained_forecast_ids"],
            "created_at": ensure_utc(fields["created_at"]).isoformat(),
            "log_loss_epsilon": (
                str(fields["log_loss_epsilon"]) if fields["log_loss_epsilon"] is not None else None
            ),
        },
    )


class _PollNotDue(RuntimeError):
    def __init__(self, next_poll_at: datetime) -> None:
        super().__init__("late lifecycle poll is not due")
        self.next_poll_at = next_poll_at


class _AlreadyFinal(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LifecyclePollResult:
    checkpoint: ResumableMonitorCheckpointV1
    observation: LifecycleObservationV1 | None
    receipt: EvidencePersistenceReceiptV1 | None
    progress: LateResolutionProgressV1
    outcome: LateFinalOutcomeV1 | None
    outcome_receipt: EvidencePersistenceReceiptV1 | None
    poll_performed: bool
    next_poll_at: datetime | None


class LateLifecycleMonitor:
    """One exclusive owner for a target's post-deadline lifecycle chain."""

    def __init__(
        self,
        *,
        protocol: ProspectiveExperimentProtocolV2,
        protocol_receipt: EvidencePersistenceReceiptV1,
        target: ProspectiveTargetV1,
        target_receipt: EvidencePersistenceReceiptV1,
        snapshot: FrozenForecastSnapshotV1,
        snapshot_receipt: EvidencePersistenceReceiptV1,
        schedule: LateMonitoringScheduleV1,
        schedule_receipt: EvidencePersistenceReceiptV1,
        source: LifecycleMarketSource,
        clock: Clock,
        source_archive: Path,
        evidence_archive: Path,
        checkpoint_path: Path,
        lock_path: Path,
    ) -> None:
        self.protocol = protocol
        self.protocol_receipt = protocol_receipt
        self.target = target
        self.target_receipt = target_receipt
        self.snapshot = snapshot
        self.snapshot_receipt = snapshot_receipt
        self.schedule = schedule
        self.schedule_receipt = schedule_receipt
        self.source = source
        self.clock = clock
        self.source_archive = Path(source_archive)
        self.evidence_archive = Path(evidence_archive)
        self._chain_cache: (
            list[tuple[LifecycleObservationV1, EvidencePersistenceReceiptV1]] | None
        ) = None
        self._gap_cache: (
            list[tuple[LifecycleMonitorGapEvidenceV1, EvidencePersistenceReceiptV1]] | None
        ) = None
        self._cache_archive_names: frozenset[str] | None = None
        self._minimum_retrieved_at: datetime | None = None
        self._campaign_id = f"{protocol.experiment_id}:{target.target_id}"
        self._configuration_sha256 = record_sha256(
            {
                "protocol_receipt_id": protocol_receipt.receipt_id,
                "target_receipt_id": target_receipt.receipt_id,
                "snapshot_receipt_id": snapshot_receipt.receipt_id,
                "schedule_receipt_id": schedule_receipt.receipt_id,
                "schedule": schedule.to_record(),
            }
        )
        self.monitor = ResumableMonitor(Path(checkpoint_path), Path(lock_path))
        self._validate_bindings()

    def _validate_bindings(self) -> None:
        verify_receipt_for_record(self.protocol_receipt, self.protocol)
        verify_receipt_for_record(self.target_receipt, self.target)
        verify_receipt_for_record(self.snapshot_receipt, self.snapshot)
        verify_receipt_for_record(self.schedule_receipt, self.schedule)
        if (
            self.protocol_receipt.artifact_kind is not EvidenceArtifactKind.EXPERIMENT_PROTOCOL
            or self.protocol_receipt.artifact_id != self.protocol.experiment_id
            or self.target_receipt.artifact_kind is not EvidenceArtifactKind.TARGET_DECLARATION
            or self.target_receipt.artifact_id != self.target.target_id
            or self.snapshot_receipt.artifact_kind
            is not EvidenceArtifactKind.FROZEN_FORECAST_SNAPSHOT
            or self.snapshot_receipt.artifact_id != self.snapshot.snapshot_id
            or self.schedule_receipt.artifact_kind
            is not EvidenceArtifactKind.LATE_MONITORING_SCHEDULE
            or self.schedule_receipt.artifact_id != self.schedule.schedule_id
            or self.protocol_receipt.experiment_id != self.protocol.experiment_id
            or self.target_receipt.experiment_id != self.protocol.experiment_id
            or self.snapshot_receipt.experiment_id != self.protocol.experiment_id
            or self.schedule_receipt.experiment_id != self.protocol.experiment_id
        ):
            raise ValueError("late monitor receipts do not match their evidence records")
        for record, receipt, artifact_kind in (
            (
                self.protocol,
                self.protocol_receipt,
                EvidenceArtifactKind.EXPERIMENT_PROTOCOL,
            ),
            (self.target, self.target_receipt, EvidenceArtifactKind.TARGET_DECLARATION),
            (
                self.snapshot,
                self.snapshot_receipt,
                EvidenceArtifactKind.FROZEN_FORECAST_SNAPSHOT,
            ),
            (
                self.schedule,
                self.schedule_receipt,
                EvidenceArtifactKind.LATE_MONITORING_SCHEDULE,
            ),
        ):
            self._verify_archived_receipt(record, receipt, artifact_kind)
        if (
            self.protocol.experiment_id != self.target.experiment_id
            or self.snapshot.target != self.target
            or self.snapshot.target_receipt != self.target_receipt
            or self.schedule.experiment_id != self.protocol.experiment_id
            or self.schedule.target_id != self.target.target_id
            or self.schedule.protocol_receipt_id != self.protocol_receipt.receipt_id
        ):
            raise ValueError("late monitor inputs name different experiment or target evidence")
        if self.target.cutoff_basis is not self.protocol.cutoff_basis:
            raise ValueError("late monitor target cutoff rule disagrees with protocol")
        if self.schedule.effective_at <= self.protocol.lifecycle_deadline:
            raise ValueError("late cadence must begin after the original lifecycle deadline")
        if self.schedule_receipt.persisted_at > self.schedule.effective_at:
            raise ValueError("late schedule was not durable before it became effective")
        self._assert_schedule_isolation()

    def _assert_schedule_isolation(self) -> None:
        evidence_root = self.evidence_archive / "argos_evidence"
        found_current_schedule = False
        for path in sorted(evidence_root.glob("*.raw.json")):
            raw, provenance = read_raw_payload(self.evidence_archive, provenance_digest(path))
            decoded, model = self._decode_evidence_record(raw)
            if model is not LateMonitoringScheduleV1:
                continue
            archived_schedule = LateMonitoringScheduleV1.from_record(decoded)
            if (
                archived_schedule.experiment_id != self.protocol.experiment_id
                or archived_schedule.target_id != self.target.target_id
            ):
                continue
            self._verify_evidence_provenance(
                record=archived_schedule,
                provenance=provenance,
                raw=raw,
                artifact_id=archived_schedule.schedule_id,
            )
            if archived_schedule.schedule_id != self.schedule.schedule_id:
                raise ValueError(
                    "late-monitor archive already contains a different frozen schedule"
                )
            found_current_schedule = True
        if not found_current_schedule:
            raise ValueError("frozen late-monitor schedule is not durable in its evidence archive")

    def _initial_checkpoint(self) -> ResumableMonitorCheckpointV1:
        return ResumableMonitorCheckpointV1(
            campaign_id=self._campaign_id,
            configuration_sha256=self._configuration_sha256,
            next_ordinal=0,
            updated_at=max(
                self.target_receipt.persisted_at,
                self.snapshot_receipt.persisted_at,
                self.schedule_receipt.persisted_at,
            ),
        )

    async def poll_once(self) -> LifecyclePollResult:
        """Poll once when due, or return the current final/pending state."""
        committed: list[tuple[LifecycleObservationV1, EvidencePersistenceReceiptV1]] = []
        final_outcome: list[tuple[LateFinalOutcomeV1, EvidencePersistenceReceiptV1] | None] = []

        def reconcile(
            checkpoint: ResumableMonitorCheckpointV1,
        ) -> ResumableMonitorCheckpointV1:
            return self._reconcile(checkpoint)

        async def poll(checkpoint: ResumableMonitorCheckpointV1) -> PollCommit:
            chain = self._load_chain()
            if chain and chain[-1][0].finality is ResolutionStatus.FINAL:
                raise _AlreadyFinal("target already has a persisted final observation")
            attempted_at = ensure_utc(self.clock.now())
            response = await self.source.get_market(self.target.market_id)
            observation, receipt, resolution = self._persist_poll(
                checkpoint, chain, response, attempted_at=attempted_at
            )
            committed.append((observation, receipt))
            if resolution is not None and resolution.resolution_status is ResolutionStatus.FINAL:
                outcome = self._build_late_outcome((*chain, (observation, receipt)), resolution)
                outcome_receipt = persist_evidence_record(
                    self.evidence_archive,
                    record=outcome,
                    experiment_id=self.protocol.experiment_id,
                    artifact_kind=EvidenceArtifactKind.LATE_FINAL_OUTCOME,
                    artifact_id=outcome.late_outcome_id,
                    persisted_at=ensure_utc(self.clock.now()),
                )
                final_outcome.append((outcome, outcome_receipt))
            else:
                final_outcome.append(None)
            # This snapshot is taken while the lease is held. A different owner
            # appending after release will then invalidate our cached chain.
            self._mark_archive_cache_current()
            return PollCommit(
                ordinal=checkpoint.next_ordinal,
                receipt_id=receipt.receipt_id,
                record_sha256=receipt.artifact_sha256,
                persisted_at=receipt.persisted_at,
                previous_receipt_id=checkpoint.last_receipt_id,
            )

        try:
            checkpoint = await self.monitor.run_once_async(
                poll=poll,
                failure_time=self.clock.now,
                initial_checkpoint=self._initial_checkpoint(),
                reconcile=reconcile,
                record_failure=self._record_failed_poll,
            )
        except _PollNotDue as not_due:
            checkpoint = self.monitor.load()
            return self._result_from_checkpoint(
                checkpoint, poll_performed=False, next_poll_at=not_due.next_poll_at
            )
        except _AlreadyFinal:
            checkpoint = self.monitor.load()
            return self._result_from_checkpoint(checkpoint, poll_performed=False, next_poll_at=None)
        except BaseException:
            # A poll may have durably appended evidence before a later step or
            # checkpoint write failed. Force the next attempt to reconcile the
            # archive rather than trusting an in-memory head that may be stale.
            self._invalidate_archive_cache()
            raise
        if len(committed) != 1 or len(final_outcome) != 1:
            self._invalidate_archive_cache()
            raise RuntimeError("late monitor returned without exactly one durable poll")
        observation, receipt = committed[0]
        chain = self._load_chain()
        if len(chain) + 1 != observation.ordinal:
            self._invalidate_archive_cache()
            raise RuntimeError("committed lifecycle ordinal disagrees with the cached chain head")
        chain.append((observation, receipt))
        outcome_tuple = final_outcome[0]
        outcome = outcome_tuple[0] if outcome_tuple is not None else None
        outcome_receipt = outcome_tuple[1] if outcome_tuple is not None else None
        progress = self._progress(observation, outcome)
        return LifecyclePollResult(
            checkpoint=checkpoint,
            observation=observation,
            receipt=receipt,
            progress=progress,
            outcome=outcome,
            outcome_receipt=outcome_receipt,
            poll_performed=True,
            next_poll_at=(
                receipt.persisted_at + timedelta(seconds=self.schedule.poll_interval_seconds)
                if outcome is None
                else None
            ),
        )

    def _reconcile(self, checkpoint: ResumableMonitorCheckpointV1) -> ResumableMonitorCheckpointV1:
        if (
            checkpoint.campaign_id != self._campaign_id
            or checkpoint.configuration_sha256 != self._configuration_sha256
        ):
            raise ValueError("late monitor checkpoint belongs to another frozen schedule")
        self._refresh_archive_caches()
        chain = self._load_chain()
        if checkpoint.next_ordinal > len(chain):
            raise ValueError("checkpoint is ahead of the durable lifecycle evidence")
        reconciled = checkpoint
        if checkpoint.next_ordinal:
            checkpoint_observation, checkpoint_receipt = chain[checkpoint.next_ordinal - 1]
            if (
                checkpoint_observation.ordinal != checkpoint.next_ordinal
                or checkpoint.last_receipt_id != checkpoint_receipt.receipt_id
                or checkpoint.last_record_sha256 != checkpoint_receipt.artifact_sha256
                or checkpoint.last_success_at != checkpoint_receipt.persisted_at
            ):
                raise ValueError("checkpoint head disagrees with its durable lifecycle prefix")
        elif any(
            value is not None
            for value in (
                checkpoint.last_receipt_id,
                checkpoint.last_record_sha256,
                checkpoint.last_success_at,
            )
        ):
            raise ValueError("empty durable lifecycle chain has a nonempty checkpoint head")
        if checkpoint.next_ordinal < len(chain):
            tail, receipt = chain[-1]
            if tail.ordinal <= checkpoint.next_ordinal:
                raise ValueError("lifecycle evidence does not extend the stale checkpoint")
            reconciled = checkpoint.model_copy(
                update={
                    "next_ordinal": tail.ordinal,
                    "last_receipt_id": receipt.receipt_id,
                    "last_record_sha256": receipt.artifact_sha256,
                    "last_success_at": receipt.persisted_at,
                    "updated_at": max(checkpoint.updated_at, receipt.persisted_at),
                }
            )
        reconciled = self._reconcile_archived_gaps(checkpoint, reconciled, chain)
        # Loading a stale chain may itself repair a missing receipt link.
        self._mark_archive_cache_current()

        if chain and chain[-1][0].finality is ResolutionStatus.FINAL:
            # The durable observation is authoritative if a crash happened
            # before the checkpoint rename. Reconcile it before going terminal.
            if reconciled != checkpoint:
                self.monitor.save(reconciled)
            raise _AlreadyFinal("target already has a persisted final observation")

        now = ensure_utc(self.clock.now())
        cadence_anchor = reconciled.updated_at
        if reconciled.next_ordinal == 0 and not reconciled.gaps:
            next_poll_at = self.schedule.first_poll_at
        else:
            next_poll_at = max(
                self.schedule.first_poll_at,
                cadence_anchor + timedelta(seconds=self.schedule.poll_interval_seconds),
            )
        self._minimum_retrieved_at = next_poll_at
        if now < next_poll_at:
            if reconciled != checkpoint:
                self.monitor.save(reconciled)
            raise _PollNotDue(next_poll_at)
        maximum_gap = timedelta(
            seconds=self.schedule.poll_interval_seconds * self.schedule.maximum_gap_multiple
        )
        if reconciled.next_ordinal == 0 and not reconciled.gaps:
            cadence_anchor = self.schedule.first_poll_at
        if now - cadence_anchor > maximum_gap:
            reason = (
                "owner resumed after the frozen maximum interval; the intervening source state "
                "is unobserved and is not backfilled"
            )
            started_at = cadence_anchor
            gap = checkpoint_after_failure(
                reconciled,
                started_at=started_at,
                ended_at=now,
                reason=reason,
            )
            self._persist_gap(
                checkpoint=reconciled,
                started_at=started_at,
                ended_at=now,
                reason=reason,
            )
            reconciled = gap
            self._minimum_retrieved_at = now
            self._mark_archive_cache_current()
        return reconciled

    def _archived_evidence_names(self) -> frozenset[str]:
        return frozenset(
            path.name for path in (self.evidence_archive / "argos_evidence").glob("*.raw.json")
        )

    def _refresh_archive_caches(self) -> None:
        names = self._archived_evidence_names()
        if names != self._cache_archive_names:
            self._chain_cache = None
            self._gap_cache = None
        self._cache_archive_names = names

    def _mark_archive_cache_current(self) -> None:
        self._cache_archive_names = self._archived_evidence_names()

    def _invalidate_archive_cache(self) -> None:
        self._chain_cache = None
        self._gap_cache = None
        self._cache_archive_names = None

    def _reconcile_archived_gaps(
        self,
        checkpoint: ResumableMonitorCheckpointV1,
        reconciled: ResumableMonitorCheckpointV1,
        chain: Sequence[tuple[LifecycleObservationV1, EvidencePersistenceReceiptV1]],
    ) -> ResumableMonitorCheckpointV1:
        known = {
            (gap.attempted_ordinal, gap.started_at, gap.ended_at, gap.reason)
            for gap in reconciled.gaps
        }
        merged = list(reconciled.gaps)
        updated_at = reconciled.updated_at
        for evidence, _ in self._load_gap_evidence():
            if evidence.attempted_ordinal > reconciled.next_ordinal + 1:
                raise ValueError("durable lifecycle gap refers to a future poll ordinal")
            self._verify_gap_predecessor(evidence, chain)
            key = (
                evidence.attempted_ordinal - 1,
                evidence.started_at,
                evidence.ended_at,
                evidence.reason,
            )
            if key in known:
                continue
            if evidence.started_at < checkpoint.updated_at:
                raise ValueError("durable lifecycle gap regresses behind the checkpoint")
            merged.append(
                MonitorGapV1(
                    attempted_ordinal=evidence.attempted_ordinal - 1,
                    started_at=evidence.started_at,
                    ended_at=evidence.ended_at,
                    reason=evidence.reason,
                )
            )
            known.add(key)
            updated_at = max(updated_at, evidence.ended_at)
        if tuple(merged) == reconciled.gaps and updated_at == reconciled.updated_at:
            return reconciled
        return reconciled.model_copy(update={"gaps": tuple(merged), "updated_at": updated_at})

    @staticmethod
    def _verify_gap_predecessor(
        evidence: LifecycleMonitorGapEvidenceV1,
        chain: Sequence[tuple[LifecycleObservationV1, EvidencePersistenceReceiptV1]],
    ) -> None:
        # A gap normally points at the last completed poll (attempted - 1).
        # If persistence failed after the current poll's observation was
        # already archived, it may instead point at that durable observation.
        for ordinal in (evidence.attempted_ordinal - 1, evidence.attempted_ordinal):
            if ordinal == 0:
                matches = (
                    evidence.predecessor_observation_id is None
                    and evidence.predecessor_receipt_id is None
                )
            elif ordinal <= len(chain):
                observation, receipt = chain[ordinal - 1]
                matches = (
                    evidence.predecessor_observation_id == observation.lifecycle_observation_id
                    and evidence.predecessor_receipt_id == receipt.receipt_id
                )
            else:
                matches = False
            if matches:
                return
        raise ValueError(
            "durable lifecycle gap predecessor disagrees with the verified observation chain"
        )

    def _load_chain(
        self,
    ) -> list[tuple[LifecycleObservationV1, EvidencePersistenceReceiptV1]]:
        if self._chain_cache is not None:
            return self._chain_cache
        # A new owner or recovery pass fully verifies the archive once. Normal
        # polls then append to this indexed head instead of rereading history.
        observation_records: dict[
            int, tuple[LifecycleObservationV1, EvidencePersistenceReceiptV1]
        ] = {}
        retrievals = load_lifecycle_poll_retrievals(
            self.evidence_archive,
            experiment_id=self.protocol.experiment_id,
            target_id=self.target.target_id,
        )
        for path in sorted((self.evidence_archive / "argos_evidence").glob("*.raw.json")):
            raw, provenance = read_raw_payload(self.evidence_archive, provenance_digest(path))
            decoded, model = self._decode_evidence_record(raw)
            if model is not LifecycleObservationV1:
                continue
            observation = LifecycleObservationV1.from_record(decoded)
            if (
                observation.experiment_id != self.protocol.experiment_id
                or observation.target_id != self.target.target_id
            ):
                continue
            self._verify_evidence_provenance(
                record=observation,
                provenance=provenance,
                raw=raw,
                artifact_id=observation.lifecycle_observation_id,
            )
            receipt = self._reconstruct_receipt(
                observation,
                provenance,
                EvidenceArtifactKind.LIFECYCLE_OBSERVATION,
            )
            verify_receipt_for_record(receipt, observation)
            raw_source = self._verify_source_for_observation(observation)
            retrieval = retrievals.get(observation.lifecycle_observation_id)
            if retrieval is not None and retrieval[0].schedule_id != self.schedule.schedule_id:
                raise ValueError("per-poll retrieval evidence belongs to another frozen schedule")
            verify_lifecycle_poll_retrieval(observation, receipt, raw_source, retrievals)
            if observation.ordinal in observation_records:
                raise ValueError("duplicate durable lifecycle ordinal for selected target")
            observation_records[observation.ordinal] = (observation, receipt)

        chain = tuple(observation_records[index] for index in sorted(observation_records))
        previous_observation_id: str | None = None
        previous_receipt_id: str | None = None
        previous_retrieved_at: datetime | None = None
        expected_links: dict[int, LifecycleReceiptChainLinkV1] = {}
        existing_links = self._read_receipt_links()
        for ordinal, (observation, receipt) in enumerate(chain, start=1):
            if (
                observation.ordinal != ordinal
                or observation.previous_observation_id != previous_observation_id
                or receipt.persisted_at < observation.retrieved_at
                or (previous_retrieved_at and observation.retrieved_at < previous_retrieved_at)
            ):
                raise ValueError("durable lifecycle ordinal or receipt chain is broken")
            link = self._build_receipt_link(
                observation,
                receipt,
                previous_receipt_id=previous_receipt_id,
                linked_at=(
                    existing_links[ordinal].linked_at
                    if ordinal in existing_links
                    else max(receipt.persisted_at, ensure_utc(self.clock.now()))
                ),
            )
            expected_links[ordinal] = link
            previous_observation_id = observation.lifecycle_observation_id
            previous_receipt_id = receipt.receipt_id
            previous_retrieved_at = observation.retrieved_at

        self._verify_or_append_receipt_links(expected_links, existing_links)
        if chain:
            finals = [item.finality is ResolutionStatus.FINAL for item, _ in chain]
            if any(finals[:-1]) or (any(finals) and not finals[-1]):
                raise ValueError("durable lifecycle chain continues after an earlier final")
            if finals[-1] and chain[-1][0].retrieved_at <= self.protocol.lifecycle_deadline:
                raise ValueError("late lifecycle finality was observed before the frozen deadline")
        self._chain_cache = list(chain)
        return self._chain_cache

    def _verify_or_append_receipt_links(
        self,
        expected: Mapping[int, LifecycleReceiptChainLinkV1],
        found: Mapping[int, LifecycleReceiptChainLinkV1],
    ) -> None:
        if set(found) - set(expected):
            raise ValueError("receipt archive contains a link without its lifecycle observation")
        for ordinal, link in expected.items():
            existing = found.get(ordinal)
            if existing is not None:
                if (
                    existing.link_id != link.link_id
                    or existing.observation_receipt != link.observation_receipt
                    or existing.previous_receipt_id != link.previous_receipt_id
                ):
                    raise ValueError("archived lifecycle receipt link disagrees with chain")
                continue
            persist_evidence_record(
                self.evidence_archive,
                record=link,
                experiment_id=self.protocol.experiment_id,
                artifact_kind=EvidenceArtifactKind.LIFECYCLE_RECEIPT_CHAIN_LINK,
                artifact_id=link.link_id,
                persisted_at=ensure_utc(self.clock.now()),
            )

    def _read_receipt_links(self) -> dict[int, LifecycleReceiptChainLinkV1]:
        found: dict[int, LifecycleReceiptChainLinkV1] = {}
        for path in sorted((self.evidence_archive / "argos_evidence").glob("*.raw.json")):
            raw, provenance = read_raw_payload(self.evidence_archive, provenance_digest(path))
            decoded, model = self._decode_evidence_record(raw)
            if model is not LifecycleReceiptChainLinkV1:
                continue
            link = LifecycleReceiptChainLinkV1.from_record(decoded)
            if link.target_id != self.target.target_id or link.experiment_id != (
                self.protocol.experiment_id
            ):
                continue
            if link.schedule_id != self.schedule.schedule_id:
                raise ValueError("receipt archive contains a different frozen target schedule")
            if link.ordinal in found:
                raise ValueError("duplicate lifecycle receipt-chain link ordinal")
            self._verify_evidence_provenance(
                record=link,
                provenance=provenance,
                raw=raw,
                artifact_id=link.link_id,
            )
            reconstructed = self._reconstruct_receipt(
                link,
                provenance,
                EvidenceArtifactKind.LIFECYCLE_RECEIPT_CHAIN_LINK,
            )
            verify_receipt_for_record(reconstructed, link)
            found[link.ordinal] = link
        return found

    def _load_gap_evidence(
        self,
    ) -> list[tuple[LifecycleMonitorGapEvidenceV1, EvidencePersistenceReceiptV1]]:
        if self._gap_cache is not None:
            return self._gap_cache
        found: dict[str, tuple[LifecycleMonitorGapEvidenceV1, EvidencePersistenceReceiptV1]] = {}
        for path in sorted((self.evidence_archive / "argos_evidence").glob("*.raw.json")):
            raw, provenance = read_raw_payload(self.evidence_archive, provenance_digest(path))
            decoded, model = self._decode_evidence_record(raw)
            if model is not LifecycleMonitorGapEvidenceV1:
                continue
            gap = LifecycleMonitorGapEvidenceV1.from_record(decoded)
            if gap.experiment_id != self.protocol.experiment_id or gap.target_id != (
                self.target.target_id
            ):
                continue
            if gap.schedule_id != self.schedule.schedule_id:
                raise ValueError("gap archive contains a different frozen target schedule")
            self._verify_evidence_provenance(
                record=gap,
                provenance=provenance,
                raw=raw,
                artifact_id=gap.gap_id,
            )
            receipt = self._reconstruct_receipt(
                gap,
                provenance,
                EvidenceArtifactKind.LIFECYCLE_MONITOR_GAP,
            )
            verify_receipt_for_record(receipt, gap)
            if gap.gap_id in found:
                raise ValueError("duplicate durable lifecycle gap identity")
            found[gap.gap_id] = (gap, receipt)
        self._gap_cache = list(found.values())
        return self._gap_cache

    @staticmethod
    def _decode_evidence_record(raw: bytes) -> tuple[dict[str, Any], type[VersionedModel]]:
        try:
            decoded = orjson.loads(raw)
        except orjson.JSONDecodeError as error:
            raise ValueError("evidence archive contains malformed JSON") from error
        if not isinstance(decoded, dict):
            raise ValueError("evidence archive record is not a JSON object")
        schema_version = decoded.get("schema_version")
        if not isinstance(schema_version, str) or not schema_version:
            raise ValueError("evidence archive record has no schema version")
        return decoded, resolve_schema(schema_version)

    def _verify_evidence_provenance(
        self,
        *,
        record: VersionedModel,
        provenance: SourceProvenanceV1,
        raw: bytes,
        artifact_id: str,
    ) -> None:
        expected_raw = orjson.dumps(record.to_record(), option=orjson.OPT_SORT_KEYS)
        if (
            raw != expected_raw
            or provenance.reconstructed
            or provenance.source != "argos_evidence"
            or provenance.endpoint != f"argos-evidence://{record.schema_version}/{artifact_id}"
            or provenance.byte_length != len(raw)
        ):
            raise ValueError("archived evidence record or provenance is not first-hand canonical")

    def _verify_source_for_observation(self, observation: LifecycleObservationV1) -> bytes:
        if observation.source != "gamma":
            raise ValueError("lifecycle observation is not bound to first-hand Gamma source bytes")
        raw, provenance = read_raw_payload(self.source_archive, observation.raw_payload_sha256)
        self._verify_gamma_endpoint(observation.endpoint)
        if (
            provenance.reconstructed
            or provenance.source != "gamma"
            or provenance.endpoint != observation.endpoint
            or provenance.retrieved_at > observation.retrieved_at
            or provenance.byte_length != observation.byte_length
            or archive_relative_location(provenance) != observation.raw_payload_location
        ):
            raise ValueError("source archive provenance disagrees with lifecycle observation")
        try:
            payload = orjson.loads(raw)
        except orjson.JSONDecodeError as error:
            raise ValueError("archived Gamma lifecycle source is not JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("archived Gamma lifecycle source is not a market object")
        self._verify_target_payload(payload)
        normalized = normalize_gamma_resolution(
            payload,
            source_payload_sha256=observation.raw_payload_sha256,
            normalized_at=observation.retrieved_at,
        )
        resolution = normalized if isinstance(normalized, ResolutionV1) else None
        finality = resolution.resolution_status if resolution else _nonfinal_status(payload)
        if (
            observation.finality is not finality
            or observation.resolution_id != (resolution.resolution_id if resolution else None)
            or observation.resolution_record_sha256
            != (record_sha256(resolution.to_record()) if resolution else None)
            or observation.source_time != _source_time(payload)
        ):
            raise ValueError("lifecycle observation disagrees with archived Gamma bytes")
        return raw

    def _persist_poll(
        self,
        checkpoint: ResumableMonitorCheckpointV1,
        chain: Sequence[tuple[LifecycleObservationV1, EvidencePersistenceReceiptV1]],
        response: GammaResponse,
        *,
        attempted_at: datetime,
    ) -> tuple[LifecycleObservationV1, EvidencePersistenceReceiptV1, ResolutionV1 | None]:
        provenance = response.provenance
        attempted_at = ensure_utc(attempted_at)
        if provenance.source != "gamma" or provenance.reconstructed:
            raise ValueError("late lifecycle polls require first-hand Gamma source bytes")
        self._verify_gamma_endpoint(provenance.endpoint)
        if provenance.retrieved_at < checkpoint.updated_at:
            raise ValueError("Gamma retrieval time regresses behind durable monitor state")
        if provenance.retrieved_at < self.schedule.effective_at:
            raise ValueError("late lifecycle poll was retrieved before its frozen cadence")
        minimum_retrieved_at = self._minimum_retrieved_at
        if minimum_retrieved_at is None:
            if checkpoint.next_ordinal == 0 and not checkpoint.gaps:
                minimum_retrieved_at = self.schedule.first_poll_at
            else:
                minimum_retrieved_at = max(
                    self.schedule.first_poll_at,
                    checkpoint.updated_at + timedelta(seconds=self.schedule.poll_interval_seconds),
                )
        if provenance.retrieved_at < minimum_retrieved_at:
            raise ValueError("Gamma retrieval time is before its frozen cadence")
        if provenance.retrieved_at < attempted_at:
            raise ValueError("Gamma retrieval time is before the poll attempt")
        persisted_at = ensure_utc(self.clock.now())
        if provenance.retrieved_at > persisted_at:
            raise ValueError("Gamma retrieval time is ahead of the monitor clock")
        write_raw_payload(self.source_archive, raw=response.raw, provenance=provenance)
        archived_raw, archived_provenance = read_raw_payload(
            self.source_archive, provenance.raw_sha256
        )
        if (
            archived_raw != response.raw
            or archived_provenance.reconstructed
            or archived_provenance.source != provenance.source
            or archived_provenance.endpoint != provenance.endpoint
            or archived_provenance.retrieved_at > provenance.retrieved_at
            or archived_provenance.raw_sha256 != provenance.raw_sha256
            or archived_provenance.byte_length != provenance.byte_length
        ):
            raise ValueError("archived Gamma poll does not preserve first-hand source provenance")
        try:
            payload = orjson.loads(response.raw)
        except orjson.JSONDecodeError as error:
            raise ValueError("Gamma lifecycle response is not JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("Gamma lifecycle response is not a market object")
        self._verify_target_payload(payload)
        normalized = normalize_gamma_resolution(
            payload,
            source_payload_sha256=provenance.raw_sha256,
            normalized_at=provenance.retrieved_at,
        )
        resolution = normalized if isinstance(normalized, ResolutionV1) else None
        finality = resolution.resolution_status if resolution else _nonfinal_status(payload)
        source_time = _source_time(payload)
        observation_fields: dict[str, Any] = {
            "experiment_id": self.protocol.experiment_id,
            "target_id": self.target.target_id,
            "ordinal": checkpoint.next_ordinal + 1,
            "previous_observation_id": chain[-1][0].lifecycle_observation_id if chain else None,
            "source": provenance.source,
            "endpoint": provenance.endpoint,
            "source_time": source_time,
            "retrieved_at": provenance.retrieved_at,
            "raw_payload_sha256": provenance.raw_sha256,
            "byte_length": provenance.byte_length,
            "raw_payload_location": archive_relative_location(provenance),
            "finality": finality,
            "resolution_id": resolution.resolution_id if resolution else None,
            "resolution_record_sha256": (
                record_sha256(resolution.to_record()) if resolution else None
            ),
        }
        observation = LifecycleObservationV1(
            lifecycle_observation_id=build_lifecycle_observation_id(**observation_fields),
            **observation_fields,
        )
        if resolution is not None and resolution.resolution_status is ResolutionStatus.FINAL:
            # Validate the terminal outcome contract before making the final
            # observation durable. Otherwise an ineligible final observation
            # would permanently stop polling while every recovery retries the
            # same outcome construction failure.
            observation_raw = orjson.dumps(observation.to_record(), option=orjson.OPT_SORT_KEYS)
            planned_provenance = SourceProvenanceV1(
                source="argos_evidence",
                endpoint=(
                    f"argos-evidence://{observation.schema_version}/"
                    f"{observation.lifecycle_observation_id}"
                ),
                retrieved_at=persisted_at,
                raw_sha256=sha256_hex(observation_raw),
                byte_length=len(observation_raw),
            )
            planned_receipt = self._reconstruct_receipt(
                observation,
                planned_provenance,
                EvidenceArtifactKind.LIFECYCLE_OBSERVATION,
            )
            self._build_late_outcome((*chain, (observation, planned_receipt)), resolution)
        retrieval_fields: dict[str, Any] = {
            "schedule_id": self.schedule.schedule_id,
            "experiment_id": self.protocol.experiment_id,
            "target_id": self.target.target_id,
            "ordinal": observation.ordinal,
            "observation_id": observation.lifecycle_observation_id,
            "attempted_at": attempted_at,
            "provenance": provenance,
        }
        retrieval = LifecyclePollRetrievalV1(
            retrieval_id=build_lifecycle_poll_retrieval_id(**retrieval_fields),
            **retrieval_fields,
        )
        retrieval_receipt = persist_evidence_record(
            self.evidence_archive,
            record=retrieval,
            experiment_id=self.protocol.experiment_id,
            artifact_kind=EvidenceArtifactKind.LIFECYCLE_POLL_RETRIEVAL,
            artifact_id=retrieval.retrieval_id,
            persisted_at=persisted_at,
        )
        if retrieval_receipt.persisted_at < provenance.retrieved_at:
            raise ValueError("late retrieval evidence predates its source response")
        receipt = persist_evidence_record(
            self.evidence_archive,
            record=observation,
            experiment_id=self.protocol.experiment_id,
            artifact_kind=EvidenceArtifactKind.LIFECYCLE_OBSERVATION,
            artifact_id=observation.lifecycle_observation_id,
            persisted_at=persisted_at,
        )
        link = self._build_receipt_link(
            observation,
            receipt,
            previous_receipt_id=checkpoint.last_receipt_id,
            linked_at=max(receipt.persisted_at, ensure_utc(self.clock.now())),
        )
        persist_evidence_record(
            self.evidence_archive,
            record=link,
            experiment_id=self.protocol.experiment_id,
            artifact_kind=EvidenceArtifactKind.LIFECYCLE_RECEIPT_CHAIN_LINK,
            artifact_id=link.link_id,
            persisted_at=max(link.linked_at, ensure_utc(self.clock.now())),
        )
        return observation, receipt, resolution

    def _build_receipt_link(
        self,
        observation: LifecycleObservationV1,
        receipt: EvidencePersistenceReceiptV1,
        *,
        previous_receipt_id: str | None,
        linked_at: datetime,
    ) -> LifecycleReceiptChainLinkV1:
        fields: dict[str, Any] = {
            "schedule_id": self.schedule.schedule_id,
            "experiment_id": self.protocol.experiment_id,
            "target_id": self.target.target_id,
            "ordinal": observation.ordinal,
            "observation": observation,
            "observation_receipt": receipt,
            "previous_receipt_id": previous_receipt_id,
            "linked_at": ensure_utc(linked_at),
        }
        return LifecycleReceiptChainLinkV1(
            link_id=_receipt_link_id(
                schedule_id=self.schedule.schedule_id,
                observation=observation,
                observation_receipt=receipt,
                previous_receipt_id=previous_receipt_id,
                linked_at=fields["linked_at"],
            ),
            **fields,
        )

    def _reconstruct_receipt(
        self,
        record: VersionedModel,
        provenance: SourceProvenanceV1,
        artifact_kind: EvidenceArtifactKind,
    ) -> EvidencePersistenceReceiptV1:
        if isinstance(record, LifecycleObservationV1):
            artifact_id = record.lifecycle_observation_id
        elif isinstance(record, LifecycleReceiptChainLinkV1):
            artifact_id = record.link_id
        elif isinstance(record, LifecycleMonitorGapEvidenceV1):
            artifact_id = record.gap_id
        elif isinstance(record, ProspectiveExperimentProtocolV2):
            artifact_id = record.experiment_id
        elif isinstance(record, ProspectiveTargetV1):
            artifact_id = record.target_id
        elif isinstance(record, FrozenForecastSnapshotV1):
            artifact_id = record.snapshot_id
        elif isinstance(record, LateMonitoringScheduleV1):
            artifact_id = record.schedule_id
        else:
            raise TypeError("unsupported lifecycle archive artifact")
        storage_identity = archive_relative_location(provenance)
        receipt_fields: dict[str, Any] = {
            "experiment_id": self.protocol.experiment_id,
            "artifact_kind": artifact_kind,
            "artifact_id": artifact_id,
            "artifact_schema_version": record.schema_version,
            "artifact_sha256": provenance.raw_sha256,
            "artifact_byte_length": provenance.byte_length,
            "persisted_at": provenance.retrieved_at,
            "storage_backend": "content_addressed_raw_archive.v1",
            "storage_identity": storage_identity,
        }
        return EvidencePersistenceReceiptV1(
            receipt_id=build_persistence_receipt_id(**receipt_fields), **receipt_fields
        )

    def _verify_archived_receipt(
        self,
        record: VersionedModel,
        receipt: EvidencePersistenceReceiptV1,
        artifact_kind: EvidenceArtifactKind,
    ) -> None:
        raw, provenance = read_raw_payload(self.evidence_archive, receipt.artifact_sha256)
        self._verify_evidence_provenance(
            record=record,
            provenance=provenance,
            raw=raw,
            artifact_id=receipt.artifact_id,
        )
        expected = self._reconstruct_receipt(record, provenance, artifact_kind)
        if receipt != expected:
            raise ValueError("late monitor receipt disagrees with archived provenance")

    def _record_failed_poll(
        self,
        checkpoint: ResumableMonitorCheckpointV1,
        started_at: datetime,
        ended_at: datetime,
        error: BaseException,
    ) -> None:
        # The failed poll may already have persisted its observation before a
        # later receipt/outcome write failed; rebuild before choosing a gap head.
        self._chain_cache = None
        chain = self._load_chain()
        reason = f"{type(error).__name__}: {error}"[:1024]
        self._persist_gap(
            checkpoint=checkpoint,
            started_at=started_at,
            ended_at=max(ensure_utc(started_at), ensure_utc(ended_at)),
            reason=reason,
            chain=chain,
        )

    def _persist_gap(
        self,
        *,
        checkpoint: ResumableMonitorCheckpointV1,
        started_at: datetime,
        ended_at: datetime,
        reason: str,
        chain: Sequence[tuple[LifecycleObservationV1, EvidencePersistenceReceiptV1]] | None = None,
    ) -> LifecycleMonitorGapEvidenceV1:
        if chain is None:
            chain = self._load_chain()
        predecessor_observation_id = chain[-1][0].lifecycle_observation_id if chain else None
        predecessor_receipt_id = chain[-1][1].receipt_id if chain else None
        attempted_ordinal = checkpoint.next_ordinal + 1
        fields: dict[str, Any] = {
            "schedule_id": self.schedule.schedule_id,
            "experiment_id": self.protocol.experiment_id,
            "target_id": self.target.target_id,
            "attempted_ordinal": attempted_ordinal,
            "started_at": ensure_utc(started_at),
            "ended_at": ensure_utc(ended_at),
            "predecessor_observation_id": predecessor_observation_id,
            "predecessor_receipt_id": predecessor_receipt_id,
            "reason": reason or "poll attempt failed without a reported exception",
        }
        identity_fields = {
            key: value for key, value in fields.items() if key not in {"experiment_id", "target_id"}
        }
        gap = LifecycleMonitorGapEvidenceV1(gap_id=_gap_id(**identity_fields), **fields)
        gap_chain = self._load_gap_evidence()
        for existing, _ in gap_chain:
            if existing.gap_id == gap.gap_id:
                if existing != gap:
                    raise ValueError("durable lifecycle gap identity has different fields")
                return existing
        try:
            receipt = persist_evidence_record(
                self.evidence_archive,
                record=gap,
                experiment_id=self.protocol.experiment_id,
                artifact_kind=EvidenceArtifactKind.LIFECYCLE_MONITOR_GAP,
                artifact_id=gap.gap_id,
                persisted_at=max(gap.ended_at, ensure_utc(self.clock.now())),
            )
        except Exception:
            self._gap_cache = None
            raise
        gap_chain.append((gap, receipt))
        return gap

    def _build_late_outcome(
        self,
        chain: Sequence[tuple[LifecycleObservationV1, EvidencePersistenceReceiptV1]],
        resolution: ResolutionV1,
    ) -> LateFinalOutcomeV1:
        observations = tuple(item for item, _ in chain)
        receipts = tuple(receipt for _, receipt in chain)
        cutoff = observations[-1].retrieved_at
        fields: dict[str, Any] = {
            "protocol": self.protocol,
            "protocol_receipt": self.protocol_receipt,
            "snapshot": self.snapshot,
            "snapshot_receipt": self.snapshot_receipt,
            "lifecycle_observations": observations,
            "lifecycle_receipts": receipts,
            "resolution": resolution,
            "selected_cutoff": cutoff,
        }
        return LateFinalOutcomeV1(
            late_outcome_id=build_late_final_outcome_id(
                protocol_receipt=self.protocol_receipt,
                snapshot_receipt=self.snapshot_receipt,
                lifecycle_receipts=receipts,
                resolution=resolution,
                selected_cutoff=cutoff,
            ),
            **fields,
        )

    def _result_from_checkpoint(
        self,
        checkpoint: ResumableMonitorCheckpointV1,
        *,
        poll_performed: bool,
        next_poll_at: datetime | None,
    ) -> LifecyclePollResult:
        chain = self._load_chain()
        if not chain:
            return LifecyclePollResult(
                checkpoint=checkpoint,
                observation=None,
                receipt=None,
                progress=self._progress(None, None),
                outcome=None,
                outcome_receipt=None,
                poll_performed=poll_performed,
                next_poll_at=next_poll_at,
            )
        observation, receipt = chain[-1]
        outcome: LateFinalOutcomeV1 | None = None
        outcome_receipt: EvidencePersistenceReceiptV1 | None = None
        if (
            observation.finality is ResolutionStatus.FINAL
            and observation.retrieved_at > self.protocol.lifecycle_deadline
        ):
            raw, _ = read_raw_payload(self.source_archive, observation.raw_payload_sha256)
            payload = orjson.loads(raw)
            normalized = normalize_gamma_resolution(
                payload,
                source_payload_sha256=observation.raw_payload_sha256,
                normalized_at=observation.retrieved_at,
            )
            if not isinstance(normalized, ResolutionV1):
                raise ValueError("final lifecycle observation no longer normalizes to a resolution")
            outcome = self._build_late_outcome(chain, normalized)
            outcome_receipt = persist_evidence_record(
                self.evidence_archive,
                record=outcome,
                experiment_id=self.protocol.experiment_id,
                artifact_kind=EvidenceArtifactKind.LATE_FINAL_OUTCOME,
                artifact_id=outcome.late_outcome_id,
                persisted_at=ensure_utc(self.clock.now()),
            )
        return LifecyclePollResult(
            checkpoint=checkpoint,
            observation=observation,
            receipt=receipt,
            progress=self._progress(observation, outcome),
            outcome=outcome,
            outcome_receipt=outcome_receipt,
            poll_performed=poll_performed,
            next_poll_at=(None if outcome else next_poll_at),
        )

    def _progress(
        self,
        observation: LifecycleObservationV1 | None,
        outcome: LateFinalOutcomeV1 | None,
    ) -> LateResolutionProgressV1:
        return LateResolutionProgressV1(
            experiment_id=self.protocol.experiment_id,
            target_id=self.target.target_id,
            snapshot_id=self.snapshot.snapshot_id,
            status=(
                LateResolutionStatus.FINAL
                if outcome or (observation and observation.finality is ResolutionStatus.FINAL)
                else LateResolutionStatus.PENDING_RESOLUTION
            ),
            last_observation_id=(observation.lifecycle_observation_id if observation else None),
            last_observed_finality=(
                observation.finality if observation else ResolutionStatus.UNKNOWN
            ),
            last_observed_at=(observation.retrieved_at if observation else None),
            late_outcome_id=outcome.late_outcome_id if outcome else None,
            selected_cutoff=(
                outcome.selected_cutoff
                if outcome
                else observation.retrieved_at
                if observation and observation.finality is ResolutionStatus.FINAL
                else None
            ),
            assessed_at=ensure_utc(self.clock.now()),
        )

    def _verify_target_payload(self, payload: Mapping[str, Any]) -> None:
        if (
            str(payload.get("id")) != self.target.market_id
            or str(payload.get("conditionId")) != self.target.condition_id
        ):
            raise ValueError("Gamma payload does not identify the frozen target")
        tokens = payload.get("clobTokenIds")
        if isinstance(tokens, str):
            try:
                tokens = orjson.loads(tokens)
            except orjson.JSONDecodeError as error:
                raise ValueError("Gamma token mapping is malformed") from error
        ordered_tokens = list(tokens) if isinstance(tokens, (list, tuple)) else None
        if ordered_tokens != [
            self.target.yes_token_id,
            self.target.no_token_id,
        ]:
            raise ValueError("Gamma token mapping disagrees with the frozen target")

    def _verify_gamma_endpoint(self, endpoint: str) -> None:
        expected = urlparse(self.schedule.gamma_base_url)
        actual = urlparse(endpoint)
        if (
            expected.scheme.lower() != "https"
            or not expected.hostname
            or actual.scheme.lower() != "https"
            or not actual.hostname
            or actual.username is not None
            or actual.password is not None
            or actual.params
            or actual.query
            or actual.fragment
        ):
            raise ValueError("late lifecycle endpoint must be a credential-free HTTPS URL")
        try:
            expected_port = expected.port
            actual_port = actual.port
        except ValueError as error:
            raise ValueError("late lifecycle endpoint has an invalid port") from error
        if expected_port in (None, 443):
            expected_port = None
        if actual_port in (None, 443):
            actual_port = None
        if (
            actual.scheme.lower() != expected.scheme.lower()
            or actual.hostname.lower() != expected.hostname.lower()
            or actual_port != expected_port
            or actual.path.rstrip("/") != f"/markets/{self.target.market_id}"
        ):
            raise ValueError("Gamma response endpoint disagrees with the frozen Gamma origin")


def provenance_digest(path: Path) -> str:
    name = path.name
    suffix = ".raw.json"
    if not name.endswith(suffix):
        raise ValueError("archive path is not a raw payload")
    return name[: -len(suffix)]


def _source_time(payload: Mapping[str, Any]) -> datetime | None:
    value = payload.get("updatedAt")
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("Gamma updatedAt must be an ISO timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Gamma updatedAt is not a valid ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Gamma updatedAt must include an explicit UTC offset")
    return ensure_utc(parsed)


def _nonfinal_status(payload: Mapping[str, Any]) -> ResolutionStatus:
    raw = payload.get("umaResolutionStatuses")
    try:
        statuses = orjson.loads(raw) if isinstance(raw, str) else raw
    except orjson.JSONDecodeError:
        return ResolutionStatus.UNKNOWN
    if not isinstance(statuses, list) or not statuses:
        return ResolutionStatus.UNKNOWN
    latest = str(statuses[-1]).strip().lower()
    return {
        "proposed": ResolutionStatus.PROPOSED,
        "disputed": ResolutionStatus.DISPUTED,
    }.get(latest, ResolutionStatus.UNKNOWN)


def _restore_lifecycle_observation(record: Mapping[str, Any]) -> LifecycleObservationV1:
    return LifecycleObservationV1.from_record(dict(record))
