"""Proof-bearing closure for prospective targets without observed cutoffs.

The historical exclusion boundary proves a capture rejection.  A target that
completed capture cleanly but never produced an observed admissible settlement
needs a different record: its exact lifecycle payloads, receipts and frozen
cadence must remain visible without implying anything about an unobserved
real-world transition.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from itertools import pairwise
from typing import Any, ClassVar, cast

import orjson
from pydantic import Field, field_serializer, field_validator, model_validator

from argos.clock import ensure_utc
from argos.config.manifest import RunManifest, RunMode, WorkingTreeStatus
from argos.domain.provenance import SHA256_LENGTH, SourceProvenanceV1, sha256_hex
from argos.domain.versioning import VersionedModel, ensure_supported_version, freeze, thaw
from argos.evaluation.bundle import record_sha256
from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    EvidencePersistenceReceiptV1,
    LifecycleObservationV1,
    ProspectiveExperimentProtocolV2,
    ProspectiveTargetV1,
    verify_receipt_for_record,
)
from argos.evaluation.prospective_aggregation import (
    CalibrationVerdict,
    MeasurementLayerVerdict,
)
from argos.resolution import ResolutionStatus, ResolutionV1, normalize_gamma_resolution
from argos.store import CompletionStatus
from argos.store.raw_archive import archive_relative_location

__all__ = [
    "CaptureRunSummaryV1",
    "LifecycleContinuityStatus",
    "LifecyclePollEvidenceV1",
    "ProspectiveExperimentBundleV4",
    "ProspectiveTargetTerminalEvidenceV1",
    "ProspectiveTerminalReportV1",
    "ResolutionAdmissibilityStatus",
    "TargetAccountingStatus",
    "TargetTerminalDisposition",
    "aggregate_prospective_terminal_experiment",
    "build_capture_run_summary",
    "build_lifecycle_poll_evidence",
    "build_target_terminal_evidence",
    "prospective_terminal_digest_v1",
]


class TargetAccountingStatus(StrEnum):
    """Whether every preregistered target has exactly one terminal record."""

    COMPLETE = "TARGET_ACCOUNTING_COMPLETE"
    INCOMPLETE = "TARGET_ACCOUNTING_INCOMPLETE"


class LifecycleContinuityStatus(StrEnum):
    """Whether the recorded lifecycle stayed inside the frozen gap allowance."""

    COMPLETE = "LIFECYCLE_CONTINUITY_COMPLETE"
    INCOMPLETE = "LIFECYCLE_CONTINUITY_INCOMPLETE"


class ResolutionAdmissibilityStatus(StrEnum):
    """Whether any observed lifecycle record established an admissible cutoff."""

    OBSERVED_ADMISSIBLE_CUTOFF = "OBSERVED_ADMISSIBLE_CUTOFF"
    NO_ADMISSIBLE_CUTOFF = "NO_ADMISSIBLE_CUTOFF_OBSERVED"


class TargetTerminalDisposition(StrEnum):
    """The only terminal disposition represented by this closure schema."""

    NOT_PROVEN_FINAL_BY_DEADLINE = "TARGET_NOT_PROVEN_FINAL_BY_DEADLINE"


class CaptureRunSummaryV1(VersionedModel):
    """Digest-bound facts derived from one closed capture ledger."""

    schema_version: ClassVar[str] = "capture_run_summary.v1"

    capture_summary_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    manifest: RunManifest
    started_at: datetime
    ended_at: datetime
    completion_status: CompletionStatus
    frame_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    decode_failure_count: int = Field(ge=0)
    unknown_event_count: int = Field(ge=0)
    observation_schema_counts: Mapping[str, int]
    rejection_reason_counts: Mapping[str, int]
    ledger_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    ledger_byte_length: int = Field(gt=0)
    delivery_collection_digest: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    rejection_collection_digest: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)

    @field_validator("started_at", "ended_at")
    @classmethod
    def _anchor_times(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator(
        "ledger_sha256",
        "delivery_collection_digest",
        "rejection_collection_digest",
    )
    @classmethod
    def _hex_digests(cls, value: str) -> str:
        lowered = value.lower()
        if not all(character in "0123456789abcdef" for character in lowered):
            raise ValueError("capture summary digests must be hexadecimal")
        return lowered

    @field_validator("observation_schema_counts", "rejection_reason_counts")
    @classmethod
    def _freeze_counts(cls, value: Mapping[str, int]) -> Mapping[str, int]:
        if any(not key or count < 0 for key, count in value.items()):
            raise ValueError("capture summary counts must be named and non-negative")
        return cast(Mapping[str, int], freeze(value))

    @field_serializer("observation_schema_counts", "rejection_reason_counts")
    def _thaw_counts(self, value: Mapping[str, int]) -> dict[str, int]:
        return cast(dict[str, int], thaw(value))

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        record["manifest"] = self.manifest.to_record()
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> CaptureRunSummaryV1:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["manifest"] = RunManifest.from_record(dict(payload["manifest"]))
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def _summary_is_recomputable(self) -> CaptureRunSummaryV1:
        if self.completion_status is not CompletionStatus.COMPLETED:
            raise ValueError("terminal capture summary requires a completed run")
        if self.ended_at < self.started_at:
            raise ValueError("capture completion predates capture start")
        if (
            self.manifest.mode is not RunMode.CAPTURE
            or self.manifest.capture_run_id is None
            or self.manifest.run_id != self.manifest.capture_run_id
        ):
            raise ValueError("capture summary manifest does not name one capture run")
        if sum(self.observation_schema_counts.values()) != (
            self.accepted_count + self.duplicate_count
        ):
            raise ValueError("capture schema counts disagree with delivery counts")
        if sum(self.rejection_reason_counts.values()) != self.rejected_count:
            raise ValueError("capture rejection reasons disagree with rejection count")
        if self.decode_failure_count + self.unknown_event_count > self.rejected_count:
            raise ValueError("capture rejection subsets exceed total rejections")
        expected = _capture_summary_id(
            experiment_id=self.experiment_id,
            target_id=self.target_id,
            manifest=self.manifest,
            started_at=self.started_at,
            ended_at=self.ended_at,
            completion_status=self.completion_status,
            frame_count=self.frame_count,
            accepted_count=self.accepted_count,
            duplicate_count=self.duplicate_count,
            rejected_count=self.rejected_count,
            decode_failure_count=self.decode_failure_count,
            unknown_event_count=self.unknown_event_count,
            observation_schema_counts=self.observation_schema_counts,
            rejection_reason_counts=self.rejection_reason_counts,
            ledger_sha256=self.ledger_sha256,
            ledger_byte_length=self.ledger_byte_length,
            delivery_collection_digest=self.delivery_collection_digest,
            rejection_collection_digest=self.rejection_collection_digest,
        )
        if self.capture_summary_id != expected:
            raise ValueError("capture_summary_id disagrees with its ledger summary")
        return self


class LifecyclePollEvidenceV1(VersionedModel):
    """One lifecycle record plus the exact source bytes and persistence receipt."""

    schema_version: ClassVar[str] = "lifecycle_poll_evidence.v1"

    poll_evidence_id: str = Field(min_length=1)
    observation: LifecycleObservationV1
    receipt: EvidencePersistenceReceiptV1
    raw_payload_utf8: str = Field(min_length=1)

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        record["observation"] = self.observation.to_record()
        record["receipt"] = self.receipt.to_record()
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> LifecyclePollEvidenceV1:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["observation"] = LifecycleObservationV1.from_record(dict(payload["observation"]))
        payload["receipt"] = EvidencePersistenceReceiptV1.from_record(dict(payload["receipt"]))
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def _poll_is_semantically_bound(self) -> LifecyclePollEvidenceV1:
        observation = self.observation
        receipt = self.receipt
        if (
            receipt.experiment_id != observation.experiment_id
            or receipt.artifact_kind is not EvidenceArtifactKind.LIFECYCLE_OBSERVATION
            or receipt.artifact_id != observation.lifecycle_observation_id
        ):
            raise ValueError("lifecycle receipt names a different observation")
        verify_receipt_for_record(receipt, observation)
        if receipt.persisted_at < observation.retrieved_at:
            raise ValueError("lifecycle receipt predates source retrieval")

        raw = self.raw_payload_utf8.encode("utf-8")
        if len(raw) != observation.byte_length or sha256_hex(raw) != (
            observation.raw_payload_sha256
        ):
            raise ValueError("lifecycle source bytes disagree with the observation")
        provenance = SourceProvenanceV1(
            source=observation.source,
            endpoint=observation.endpoint,
            retrieved_at=observation.retrieved_at,
            raw_sha256=observation.raw_payload_sha256,
            byte_length=observation.byte_length,
        )
        if archive_relative_location(provenance) != observation.raw_payload_location:
            raise ValueError("lifecycle source location disagrees with the observation")
        _verify_observation_against_raw(observation, raw)

        expected = _poll_evidence_id(observation, receipt)
        if self.poll_evidence_id != expected:
            raise ValueError("poll_evidence_id disagrees with its lifecycle evidence")
        return self


class ProspectiveTargetTerminalEvidenceV1(VersionedModel):
    """A selected target not proven final by the frozen deadline."""

    schema_version: ClassVar[str] = "prospective_target_terminal_evidence.v1"

    terminal_evidence_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    protocol_receipt_id: str = Field(min_length=1)
    protocol_record_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    target: ProspectiveTargetV1
    target_receipt: EvidencePersistenceReceiptV1
    capture_summary: CaptureRunSummaryV1
    lifecycle_polls: tuple[LifecyclePollEvidenceV1, ...]
    closed_at: datetime
    last_admissible_observation_id: str = Field(min_length=1)
    last_observed_finality: ResolutionStatus
    final_polling_gap_microseconds: int = Field(ge=0)
    maximum_polling_gap_microseconds: int = Field(ge=0)
    continuity_status: LifecycleContinuityStatus
    admissible_cutoff_count: int = Field(ge=0)
    disposition: TargetTerminalDisposition
    claims_observed_through_deadline: bool
    replacement_target_id: str | None = Field(default=None, min_length=1)
    rescue_applied: bool

    @field_validator("closed_at")
    @classmethod
    def _anchor_closed_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("protocol_record_sha256")
    @classmethod
    def _protocol_digest_is_hex(cls, value: str) -> str:
        lowered = value.lower()
        if not all(character in "0123456789abcdef" for character in lowered):
            raise ValueError("protocol record digest must be hexadecimal")
        return lowered

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        record.update(
            {
                "target": self.target.to_record(),
                "target_receipt": self.target_receipt.to_record(),
                "capture_summary": self.capture_summary.to_record(),
                "lifecycle_polls": [item.to_record() for item in self.lifecycle_polls],
            }
        )
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> ProspectiveTargetTerminalEvidenceV1:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["target"] = ProspectiveTargetV1.from_record(dict(payload["target"]))
        payload["target_receipt"] = EvidencePersistenceReceiptV1.from_record(
            dict(payload["target_receipt"])
        )
        payload["capture_summary"] = CaptureRunSummaryV1.from_record(
            dict(payload["capture_summary"])
        )
        payload["lifecycle_polls"] = tuple(
            LifecyclePollEvidenceV1.from_record(dict(item)) for item in payload["lifecycle_polls"]
        )
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def _terminal_chain_is_recomputable(self) -> ProspectiveTargetTerminalEvidenceV1:
        target = self.target
        receipt = self.target_receipt
        if (
            self.experiment_id != target.experiment_id
            or receipt.experiment_id != self.experiment_id
            or receipt.artifact_kind is not EvidenceArtifactKind.TARGET_DECLARATION
            or receipt.artifact_id != target.target_id
        ):
            raise ValueError("terminal evidence names a different selected target")
        verify_receipt_for_record(receipt, target)
        if (
            self.capture_summary.experiment_id != self.experiment_id
            or self.capture_summary.target_id != target.target_id
        ):
            raise ValueError("capture summary names a different selected target")
        if not self.lifecycle_polls:
            raise ValueError("terminal evidence requires a lifecycle chain")
        previous: str | None = None
        for ordinal, poll in enumerate(self.lifecycle_polls, start=1):
            observation = poll.observation
            if (
                observation.experiment_id != self.experiment_id
                or observation.target_id != target.target_id
            ):
                raise ValueError("lifecycle chain names a different selected target")
            if observation.ordinal != ordinal or observation.previous_observation_id != previous:
                raise ValueError("lifecycle observation chain is incomplete or reordered")
            previous = observation.lifecycle_observation_id
        last = self.lifecycle_polls[-1].observation
        if (
            self.last_admissible_observation_id != last.lifecycle_observation_id
            or self.last_observed_finality is not last.finality
        ):
            raise ValueError("terminal summary disagrees with its last observed lifecycle state")
        if any(
            poll.observation.finality is ResolutionStatus.FINAL for poll in self.lifecycle_polls
        ):
            raise ValueError("a final lifecycle observation requires cutoff evidence")
        if self.admissible_cutoff_count != 0:
            raise ValueError("this terminal disposition cannot claim an admissible cutoff")
        if self.disposition is not TargetTerminalDisposition.NOT_PROVEN_FINAL_BY_DEADLINE:
            raise ValueError("terminal evidence uses the wrong target disposition")
        if self.claims_observed_through_deadline:
            raise ValueError("terminal evidence cannot claim coverage through an unobserved tail")
        if self.replacement_target_id is not None or self.rescue_applied:
            raise ValueError("the frozen target cannot be replaced or rescued")
        expected = _target_terminal_id(self)
        if self.terminal_evidence_id != expected:
            raise ValueError("terminal_evidence_id disagrees with its proof")
        return self

    def validate_against_protocol(self, protocol: ProspectiveExperimentProtocolV2) -> None:
        """Cross-check cadence, closure and capture bounds against the sibling protocol."""

        if (
            self.experiment_id != protocol.experiment_id
            or self.protocol_receipt_id == ""
            or self.protocol_record_sha256 != record_sha256(protocol.to_record())
            or self.target.cutoff_basis is not protocol.cutoff_basis
        ):
            raise ValueError("terminal target disagrees with the frozen protocol")
        if self.closed_at < protocol.lifecycle_deadline:
            raise ValueError("terminal evidence closes before the frozen deadline")
        if any(
            poll.observation.retrieved_at > protocol.lifecycle_deadline
            for poll in self.lifecycle_polls
        ):
            raise ValueError("terminal lifecycle contains a post-deadline observation")

        summary = self.capture_summary
        manifest = summary.manifest
        parameters = manifest.run_parameters
        subscribed = parameters.get("subscribed_token_ids")
        if (
            manifest.code_revision != protocol.code_revision
            or manifest.working_tree is not WorkingTreeStatus.CLEAN
            or manifest.config_fingerprint != protocol.config_fingerprint
        ):
            raise ValueError("capture summary disagrees with the frozen protocol runtime")
        if (
            not isinstance(subscribed, Sequence)
            or isinstance(subscribed, (str, bytes))
            or set(str(item) for item in subscribed)
            != {self.target.yes_token_id, self.target.no_token_id}
            or len(subscribed) != 2
        ):
            raise ValueError("capture summary subscriptions disagree with the target")
        try:
            max_seconds = Decimal(str(parameters.get("max_seconds")))
        except InvalidOperation as error:
            raise ValueError("capture duration is not a numeric frozen bound") from error
        if (
            max_seconds != Decimal(protocol.capture_max_seconds_per_target)
            or parameters.get("max_frames") != protocol.capture_max_frames_per_target
            or parameters.get("raw_archive") is not protocol.capture_raw_archive
        ):
            raise ValueError("capture summary disagrees with frozen capture bounds")
        if not (
            protocol.observation_window_start
            <= summary.started_at
            <= summary.ended_at
            <= protocol.observation_window_end
        ):
            raise ValueError("capture summary falls outside the observation window")
        if (
            summary.rejected_count != 0
            or summary.decode_failure_count != 0
            or summary.unknown_event_count != 0
        ):
            raise ValueError("clean terminal capture contains rejected or unknown input")

        final_gap, maximum_gap, continuity = _lifecycle_gaps(
            protocol,
            tuple(item.observation for item in self.lifecycle_polls),
        )
        if (
            self.final_polling_gap_microseconds != final_gap
            or self.maximum_polling_gap_microseconds != maximum_gap
        ):
            raise ValueError("terminal polling gap disagrees with the lifecycle chain")
        if self.continuity_status is not continuity:
            raise ValueError("lifecycle continuity status disagrees with the frozen cadence")


class ProspectiveTerminalReportV1(VersionedModel):
    """Three separate terminal claims and mechanically derived verdicts."""

    schema_version: ClassVar[str] = "prospective_terminal_report.v1"

    experiment_id: str = Field(min_length=1)
    created_at: datetime
    experiment_closed_by_frozen_deadline: bool
    target_accounting_status: TargetAccountingStatus
    lifecycle_continuity_status: LifecycleContinuityStatus
    resolution_admissibility_status: ResolutionAdmissibilityStatus
    selected_target_count: int = Field(ge=0)
    terminal_target_count: int = Field(ge=0)
    admissible_cutoff_count: int = Field(ge=0)
    measurement_layer_verdict: MeasurementLayerVerdict
    calibration_verdict: CalibrationVerdict
    limitations: tuple[str, ...]

    @field_validator("created_at")
    @classmethod
    def _anchor_created_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("limitations")
    @classmethod
    def _limitations_are_explicit(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or not all(item.strip() for item in value):
            raise ValueError("terminal report must state its limitations")
        return value


class ProspectiveExperimentBundleV4(VersionedModel):
    """Portable aggregate for terminal targets without observed cutoff evidence."""

    schema_version: ClassVar[str] = "prospective_experiment_bundle.v4"

    protocol: ProspectiveExperimentProtocolV2
    protocol_receipt: EvidencePersistenceReceiptV1
    terminal_targets: tuple[ProspectiveTargetTerminalEvidenceV1, ...]
    terminal_target_receipts: tuple[EvidencePersistenceReceiptV1, ...]
    report: ProspectiveTerminalReportV1
    evidence_digest: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        record.update(
            {
                "protocol": self.protocol.to_record(),
                "protocol_receipt": self.protocol_receipt.to_record(),
                "terminal_targets": [item.to_record() for item in self.terminal_targets],
                "terminal_target_receipts": [
                    item.to_record() for item in self.terminal_target_receipts
                ],
                "report": self.report.to_record(),
            }
        )
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> ProspectiveExperimentBundleV4:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["protocol"] = ProspectiveExperimentProtocolV2.from_record(dict(payload["protocol"]))
        payload["protocol_receipt"] = EvidencePersistenceReceiptV1.from_record(
            dict(payload["protocol_receipt"])
        )
        payload["terminal_targets"] = tuple(
            ProspectiveTargetTerminalEvidenceV1.from_record(dict(item))
            for item in payload["terminal_targets"]
        )
        payload["terminal_target_receipts"] = tuple(
            EvidencePersistenceReceiptV1.from_record(dict(item))
            for item in payload["terminal_target_receipts"]
        )
        payload["report"] = ProspectiveTerminalReportV1.from_record(dict(payload["report"]))
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def _aggregate_is_recomputable(self) -> ProspectiveExperimentBundleV4:
        verify_receipt_for_record(self.protocol_receipt, self.protocol)
        if (
            self.protocol_receipt.experiment_id != self.protocol.experiment_id
            or self.protocol_receipt.artifact_kind is not EvidenceArtifactKind.EXPERIMENT_PROTOCOL
        ):
            raise ValueError("terminal aggregate protocol receipt names different evidence")
        if len(self.terminal_targets) != len(self.terminal_target_receipts):
            raise ValueError("terminal target evidence and receipt counts disagree")
        target_ids: set[str] = set()
        capture_run_ids: set[str] = set()
        for terminal, receipt in zip(
            self.terminal_targets,
            self.terminal_target_receipts,
            strict=True,
        ):
            terminal.validate_against_protocol(self.protocol)
            if terminal.protocol_receipt_id != self.protocol_receipt.receipt_id:
                raise ValueError("terminal target names a different protocol receipt")
            if (
                receipt.experiment_id != self.protocol.experiment_id
                or receipt.artifact_kind is not EvidenceArtifactKind.TARGET_TERMINAL
                or receipt.artifact_id != terminal.terminal_evidence_id
            ):
                raise ValueError("terminal receipt names different target evidence")
            verify_receipt_for_record(receipt, terminal)
            target_id = terminal.target.target_id
            run_id = terminal.capture_summary.manifest.capture_run_id
            assert run_id is not None
            if target_id in target_ids or run_id in capture_run_ids:
                raise ValueError("terminal targets must have unique target and capture runs")
            target_ids.add(target_id)
            capture_run_ids.add(run_id)
        ranks = [item.target.selection_rank for item in self.terminal_targets]
        if ranks != sorted(ranks):
            raise ValueError("terminal targets must be ordered by frozen selection rank")
        expected_report = _terminal_report(
            protocol=self.protocol,
            terminal_targets=self.terminal_targets,
            created_at=self.report.created_at,
        )
        if self.report != expected_report:
            raise ValueError("terminal report disagrees with its target evidence")
        expected_digest = prospective_terminal_digest_v1(
            protocol=self.protocol,
            protocol_receipt=self.protocol_receipt,
            terminal_targets=self.terminal_targets,
            terminal_target_receipts=self.terminal_target_receipts,
            report=self.report,
        )
        if self.evidence_digest != expected_digest:
            raise ValueError("evidence_digest disagrees with the terminal experiment")
        return self


def build_capture_run_summary(
    *,
    experiment_id: str,
    target_id: str,
    manifest: RunManifest,
    started_at: datetime,
    ended_at: datetime,
    completion_status: CompletionStatus,
    frame_count: int,
    accepted_count: int,
    duplicate_count: int,
    rejected_count: int,
    decode_failure_count: int,
    unknown_event_count: int,
    observation_schema_counts: Mapping[str, int],
    rejection_reason_counts: Mapping[str, int],
    ledger_sha256: str,
    ledger_byte_length: int,
    delivery_collection_digest: str,
    rejection_collection_digest: str,
) -> CaptureRunSummaryV1:
    """Build the recomputable summary for one already-closed capture ledger."""

    fields: dict[str, Any] = {
        "experiment_id": experiment_id,
        "target_id": target_id,
        "manifest": manifest,
        "started_at": ensure_utc(started_at),
        "ended_at": ensure_utc(ended_at),
        "completion_status": completion_status,
        "frame_count": frame_count,
        "accepted_count": accepted_count,
        "duplicate_count": duplicate_count,
        "rejected_count": rejected_count,
        "decode_failure_count": decode_failure_count,
        "unknown_event_count": unknown_event_count,
        "observation_schema_counts": observation_schema_counts,
        "rejection_reason_counts": rejection_reason_counts,
        "ledger_sha256": ledger_sha256,
        "ledger_byte_length": ledger_byte_length,
        "delivery_collection_digest": delivery_collection_digest,
        "rejection_collection_digest": rejection_collection_digest,
    }
    identity = _capture_summary_id(**fields)
    return CaptureRunSummaryV1.model_validate({"capture_summary_id": identity, **fields})


def build_lifecycle_poll_evidence(
    *,
    observation: LifecycleObservationV1,
    receipt: EvidencePersistenceReceiptV1,
    raw_payload: bytes,
) -> LifecyclePollEvidenceV1:
    """Bind one lifecycle observation to its receipt and exact Gamma bytes."""

    try:
        raw_text = raw_payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("lifecycle source payload is not UTF-8") from error
    identity = _poll_evidence_id(observation, receipt)
    return LifecyclePollEvidenceV1(
        poll_evidence_id=identity,
        observation=observation,
        receipt=receipt,
        raw_payload_utf8=raw_text,
    )


def build_target_terminal_evidence(
    *,
    protocol: ProspectiveExperimentProtocolV2,
    protocol_receipt: EvidencePersistenceReceiptV1,
    target: ProspectiveTargetV1,
    target_receipt: EvidencePersistenceReceiptV1,
    capture_summary: CaptureRunSummaryV1,
    lifecycle_polls: Sequence[LifecyclePollEvidenceV1],
    closed_at: datetime,
) -> ProspectiveTargetTerminalEvidenceV1:
    """Build one negative terminal record without filling the unobserved tail."""

    polls = tuple(lifecycle_polls)
    if not polls:
        raise ValueError("terminal evidence requires at least one lifecycle poll")
    final_gap, maximum_gap, continuity = _lifecycle_gaps(
        protocol,
        tuple(item.observation for item in polls),
    )
    last = polls[-1].observation
    fields: dict[str, Any] = {
        "experiment_id": protocol.experiment_id,
        "protocol_receipt_id": protocol_receipt.receipt_id,
        "protocol_record_sha256": record_sha256(protocol.to_record()),
        "target": target,
        "target_receipt": target_receipt,
        "capture_summary": capture_summary,
        "lifecycle_polls": polls,
        "closed_at": ensure_utc(closed_at),
        "last_admissible_observation_id": last.lifecycle_observation_id,
        "last_observed_finality": last.finality,
        "final_polling_gap_microseconds": final_gap,
        "maximum_polling_gap_microseconds": maximum_gap,
        "continuity_status": continuity,
        "admissible_cutoff_count": 0,
        "disposition": TargetTerminalDisposition.NOT_PROVEN_FINAL_BY_DEADLINE,
        "claims_observed_through_deadline": False,
        "replacement_target_id": None,
        "rescue_applied": False,
    }
    draft = ProspectiveTargetTerminalEvidenceV1.model_construct(
        terminal_evidence_id="pending",
        **fields,
    )
    identity = _target_terminal_id(draft)
    terminal = ProspectiveTargetTerminalEvidenceV1.model_validate(
        {"terminal_evidence_id": identity, **fields}
    )
    terminal.validate_against_protocol(protocol)
    return terminal


def aggregate_prospective_terminal_experiment(
    *,
    protocol: ProspectiveExperimentProtocolV2,
    protocol_receipt: EvidencePersistenceReceiptV1,
    terminal_targets: Sequence[ProspectiveTargetTerminalEvidenceV1],
    terminal_target_receipts: Sequence[EvidencePersistenceReceiptV1],
    created_at: datetime,
) -> ProspectiveExperimentBundleV4:
    """Aggregate all frozen targets and derive conservative terminal verdicts."""

    targets = tuple(sorted(terminal_targets, key=lambda item: item.target.selection_rank))
    receipt_by_artifact: dict[str, EvidencePersistenceReceiptV1] = {}
    for receipt in terminal_target_receipts:
        if receipt.artifact_id in receipt_by_artifact:
            raise ValueError("duplicate terminal target receipt")
        receipt_by_artifact[receipt.artifact_id] = receipt
    try:
        receipts = tuple(receipt_by_artifact[target.terminal_evidence_id] for target in targets)
    except KeyError as error:
        raise ValueError("terminal target is missing its persistence receipt") from error
    if len(receipt_by_artifact) != len(targets):
        raise ValueError("terminal receipt set contains an unrelated artifact")
    report = _terminal_report(
        protocol=protocol,
        terminal_targets=targets,
        created_at=ensure_utc(created_at),
    )
    digest = prospective_terminal_digest_v1(
        protocol=protocol,
        protocol_receipt=protocol_receipt,
        terminal_targets=targets,
        terminal_target_receipts=receipts,
        report=report,
    )
    return ProspectiveExperimentBundleV4(
        protocol=protocol,
        protocol_receipt=protocol_receipt,
        terminal_targets=targets,
        terminal_target_receipts=receipts,
        report=report,
        evidence_digest=digest,
    )


def prospective_terminal_digest_v1(
    *,
    protocol: ProspectiveExperimentProtocolV2,
    protocol_receipt: EvidencePersistenceReceiptV1,
    terminal_targets: Sequence[ProspectiveTargetTerminalEvidenceV1],
    terminal_target_receipts: Sequence[EvidencePersistenceReceiptV1],
    report: ProspectiveTerminalReportV1,
) -> str:
    """Digest the complete portable terminal claim."""

    return record_sha256(
        {
            "version": "prospective_terminal_evidence.v1",
            "protocol": protocol.to_record(),
            "protocol_receipt": protocol_receipt.to_record(),
            "terminal_targets": [item.to_record() for item in terminal_targets],
            "terminal_target_receipts": [item.to_record() for item in terminal_target_receipts],
            "report": report.to_record(),
        }
    )


def _capture_summary_id(
    *,
    experiment_id: str,
    target_id: str,
    manifest: RunManifest,
    started_at: datetime,
    ended_at: datetime,
    completion_status: CompletionStatus,
    frame_count: int,
    accepted_count: int,
    duplicate_count: int,
    rejected_count: int,
    decode_failure_count: int,
    unknown_event_count: int,
    observation_schema_counts: Mapping[str, int],
    rejection_reason_counts: Mapping[str, int],
    ledger_sha256: str,
    ledger_byte_length: int,
    delivery_collection_digest: str,
    rejection_collection_digest: str,
) -> str:
    digest = record_sha256(
        {
            "version": "capture_run_summary_identity.v1",
            "experiment_id": experiment_id,
            "target_id": target_id,
            "manifest": manifest.to_record(),
            "started_at": ensure_utc(started_at).isoformat(),
            "ended_at": ensure_utc(ended_at).isoformat(),
            "completion_status": completion_status.value,
            "frame_count": frame_count,
            "accepted_count": accepted_count,
            "duplicate_count": duplicate_count,
            "rejected_count": rejected_count,
            "decode_failure_count": decode_failure_count,
            "unknown_event_count": unknown_event_count,
            "observation_schema_counts": dict(observation_schema_counts),
            "rejection_reason_counts": dict(rejection_reason_counts),
            "ledger_sha256": ledger_sha256,
            "ledger_byte_length": ledger_byte_length,
            "delivery_collection_digest": delivery_collection_digest,
            "rejection_collection_digest": rejection_collection_digest,
        }
    )
    return f"capture-summary-{digest[:32]}"


def _poll_evidence_id(
    observation: LifecycleObservationV1,
    receipt: EvidencePersistenceReceiptV1,
) -> str:
    digest = record_sha256(
        {
            "version": "lifecycle_poll_evidence_identity.v1",
            "observation": observation.to_record(),
            "receipt": receipt.to_record(),
        }
    )
    return f"lifecycle-poll-evidence-{digest[:32]}"


def _target_terminal_id(
    evidence: ProspectiveTargetTerminalEvidenceV1,
) -> str:
    digest = record_sha256(
        {
            "version": "prospective_target_terminal_identity.v1",
            "experiment_id": evidence.experiment_id,
            "protocol_receipt_id": evidence.protocol_receipt_id,
            "protocol_record_sha256": evidence.protocol_record_sha256,
            "target": evidence.target.to_record(),
            "target_receipt": evidence.target_receipt.to_record(),
            "capture_summary": evidence.capture_summary.to_record(),
            "lifecycle_polls": [item.to_record() for item in evidence.lifecycle_polls],
            "closed_at": evidence.closed_at.isoformat(),
            "last_admissible_observation_id": evidence.last_admissible_observation_id,
            "last_observed_finality": evidence.last_observed_finality.value,
            "final_polling_gap_microseconds": evidence.final_polling_gap_microseconds,
            "maximum_polling_gap_microseconds": evidence.maximum_polling_gap_microseconds,
            "continuity_status": evidence.continuity_status.value,
            "admissible_cutoff_count": evidence.admissible_cutoff_count,
            "disposition": evidence.disposition.value,
            "claims_observed_through_deadline": (evidence.claims_observed_through_deadline),
            "replacement_target_id": evidence.replacement_target_id,
            "rescue_applied": evidence.rescue_applied,
        }
    )
    return f"target-terminal-{digest[:32]}"


def _terminal_report(
    *,
    protocol: ProspectiveExperimentProtocolV2,
    terminal_targets: Sequence[ProspectiveTargetTerminalEvidenceV1],
    created_at: datetime,
) -> ProspectiveTerminalReportV1:
    created = ensure_utc(created_at)
    selected_count = protocol.minimum_intended_resolved_target_count
    terminal_count = len(terminal_targets)
    target_ids = {item.target.target_id for item in terminal_targets}
    accounting = (
        TargetAccountingStatus.COMPLETE
        if terminal_count == selected_count and len(target_ids) == terminal_count
        else TargetAccountingStatus.INCOMPLETE
    )
    continuity = (
        LifecycleContinuityStatus.COMPLETE
        if accounting is TargetAccountingStatus.COMPLETE
        and all(
            item.continuity_status is LifecycleContinuityStatus.COMPLETE
            for item in terminal_targets
        )
        else LifecycleContinuityStatus.INCOMPLETE
    )
    cutoff_count = sum(item.admissible_cutoff_count for item in terminal_targets)
    admissibility = (
        ResolutionAdmissibilityStatus.OBSERVED_ADMISSIBLE_CUTOFF
        if cutoff_count
        else ResolutionAdmissibilityStatus.NO_ADMISSIBLE_CUTOFF
    )
    return ProspectiveTerminalReportV1(
        experiment_id=protocol.experiment_id,
        created_at=created,
        experiment_closed_by_frozen_deadline=created >= protocol.lifecycle_deadline,
        target_accounting_status=accounting,
        lifecycle_continuity_status=continuity,
        resolution_admissibility_status=admissibility,
        selected_target_count=selected_count,
        terminal_target_count=terminal_count,
        admissible_cutoff_count=cutoff_count,
        measurement_layer_verdict=MeasurementLayerVerdict.BLOCKED,
        calibration_verdict=CalibrationVerdict.NOT_EVALUABLE,
        limitations=(
            "Terminality is limited to persisted observations; no source state is "
            "inferred for any unobserved interval.",
            "No admissible cutoff was observed, so no resolution, score, or "
            "calibration claim is made.",
            "Closed selected-target accounting and lifecycle continuity are "
            "reported as independent claims.",
        ),
    )


def _lifecycle_gaps(
    protocol: ProspectiveExperimentProtocolV2,
    observations: Sequence[LifecycleObservationV1],
) -> tuple[int, int, LifecycleContinuityStatus]:
    if not observations:
        raise ValueError("lifecycle gap computation requires observations")
    retrieved = tuple(item.retrieved_at for item in observations)
    if any(right < left for left, right in pairwise(retrieved)):
        raise ValueError("lifecycle retrieval times are not monotonic")
    if retrieved[0] < protocol.declared_at or retrieved[-1] > protocol.lifecycle_deadline:
        raise ValueError("lifecycle retrieval falls outside the frozen polling window")
    # Capture and lifecycle observation may overlap. The persisted first poll,
    # rather than the capture-window boundary, starts the measured cadence.
    anchors = (
        *retrieved,
        protocol.lifecycle_deadline,
    )
    gaps = tuple(_timedelta_microseconds(right - left) for left, right in pairwise(anchors))
    final_gap = gaps[-1]
    maximum_gap = max(gaps)
    cadence_limit = protocol.lifecycle_poll_interval_seconds * 2 * 1_000_000
    continuity = (
        LifecycleContinuityStatus.COMPLETE
        if maximum_gap <= cadence_limit
        else LifecycleContinuityStatus.INCOMPLETE
    )
    return final_gap, maximum_gap, continuity


def _timedelta_microseconds(value: timedelta) -> int:
    return ((value.days * 86_400 + value.seconds) * 1_000_000) + value.microseconds


def _verify_observation_against_raw(
    observation: LifecycleObservationV1,
    raw: bytes,
) -> None:
    try:
        decoded = orjson.loads(raw)
    except orjson.JSONDecodeError as error:
        raise ValueError("lifecycle source payload is not JSON") from error
    if not isinstance(decoded, dict):
        raise ValueError("lifecycle source payload is not a JSON object")
    payload = cast(dict[str, Any], decoded)
    normalized = normalize_gamma_resolution(
        payload,
        source_payload_sha256=observation.raw_payload_sha256,
        normalized_at=observation.retrieved_at,
    )
    resolution = normalized if isinstance(normalized, ResolutionV1) else None
    finality = resolution.resolution_status if resolution is not None else _nonfinal_status(payload)
    resolution_id = resolution.resolution_id if resolution is not None else None
    resolution_digest = record_sha256(resolution.to_record()) if resolution is not None else None
    if (
        observation.finality is not finality
        or observation.resolution_id != resolution_id
        or observation.resolution_record_sha256 != resolution_digest
    ):
        raise ValueError("lifecycle observation disagrees with normalized source bytes")
    raw_updated_at = payload.get("updatedAt")
    try:
        source_time = (
            ensure_utc(datetime.fromisoformat(raw_updated_at.replace("Z", "+00:00")))
            if isinstance(raw_updated_at, str) and raw_updated_at
            else None
        )
    except ValueError as error:
        raise ValueError("lifecycle source updatedAt is not an ISO timestamp") from error
    if observation.source_time != source_time:
        raise ValueError("lifecycle observation source time disagrees with raw payload")


def _nonfinal_status(payload: Mapping[str, Any]) -> ResolutionStatus:
    raw = payload.get("umaResolutionStatuses")
    try:
        statuses = orjson.loads(raw) if isinstance(raw, str) else raw
    except orjson.JSONDecodeError:
        return ResolutionStatus.UNKNOWN
    if not isinstance(statuses, list) or not statuses:
        return ResolutionStatus.UNKNOWN
    latest = str(statuses[-1]).lower()
    return {
        "proposed": ResolutionStatus.PROPOSED,
        "disputed": ResolutionStatus.DISPUTED,
    }.get(latest, ResolutionStatus.UNKNOWN)
