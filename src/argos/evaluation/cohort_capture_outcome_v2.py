"""Durable, replay-checkable owner-reported accounting for one V2 capture.

The ingestion adapter's return value is intentionally ephemeral. This record
turns its stop reason, enforced bounds, frame/byte counts and excluded-boundary
digest into a receipt-backed accounting assertion. The counts are not
independently recomputed from the underlying EventStore or raw frame archive by
this module; ``cohort_capture_replay_v2`` establishes that link for a journal. A
V1 boundary payload is not retained. V2 instead declares retention in a separate
excluded-boundary archive, independently verified through the V2 arrival chain;
it is never fed into forecasts or charged to the included capture-byte counter.
Neither summary alone certifies a forecast snapshot or live readiness.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar, Literal

import orjson
from pydantic import Field, field_validator, model_validator

from argos.clock import ensure_utc
from argos.domain.provenance import SHA256_LENGTH
from argos.domain.versioning import VersionedModel
from argos.evaluation.cohort_protocol_v2 import AsynchronousCohortProtocolV2
from argos.evaluation.cohort_selection_v2 import (
    OfflineBlockSelectionV2,
    verify_block_selection_v2_archives,
)
from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    EvidencePersistenceReceiptV1,
    build_target_id,
    load_persisted_record,
    persist_evidence_record,
    verify_receipt_for_record,
)
from argos.ingestion.capture import CaptureHealth
from argos.ingestion.cohort_capture_v2 import (
    BoundedCaptureStopReasonV2,
    BoundedCaptureSummaryV2,
)

__all__ = [
    "CohortCaptureRunOutcomeV1",
    "CohortCaptureRunOutcomeV2",
    "build_cohort_capture_run_outcome",
    "build_cohort_capture_run_outcome_id",
    "persist_cohort_capture_run_outcome",
    "verify_cohort_capture_run_outcome_archives",
]


_SCHEMA_VERSION = "m4_cohort_capture_run_outcome.v1"


class CohortCaptureRunOutcomeV1(VersionedModel):
    """Receipt-ready summary of a completed bounded capture run."""

    schema_version: ClassVar[str] = _SCHEMA_VERSION

    outcome_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    protocol_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    protocol_receipt_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    capture_run_id: str = Field(min_length=1)
    selection_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    selection_receipt_id: str = Field(min_length=1)
    block_ordinal: int = Field(ge=1, strict=True)
    entry_index: int = Field(ge=0, strict=True)
    started_at: datetime
    finished_at: datetime
    stop_reason: BoundedCaptureStopReasonV2
    frames_archived: int = Field(ge=0, strict=True)
    raw_bytes_archived: int = Field(ge=0, strict=True)
    boundary_frame_bytes: int | None = Field(default=None, gt=0, strict=True)
    boundary_frame_sha256: str | None = Field(
        default=None, min_length=SHA256_LENGTH, max_length=SHA256_LENGTH
    )
    boundary_payload_archived: Literal[False] = False
    health_frames_consumed: int = Field(ge=0, strict=True)
    health_decode_failures: int = Field(ge=0, strict=True)
    health_events_seen: int = Field(ge=0, strict=True)
    health_accepted: int = Field(ge=0, strict=True)
    health_duplicate: int = Field(ge=0, strict=True)
    health_rejected: int = Field(ge=0, strict=True)
    health_not_applicable: int = Field(ge=0, strict=True)
    health_unknown_event_type: int = Field(ge=0, strict=True)

    @field_validator("started_at", "finished_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("protocol_sha256", "selection_sha256", "boundary_frame_sha256")
    @classmethod
    def _lower_hex(cls, value: str | None) -> str | None:
        if value is None:
            return None
        lowered = value.lower()
        if not all(character in "0123456789abcdef" for character in lowered):
            raise ValueError("capture outcome digests must be hexadecimal")
        return lowered

    @model_validator(mode="after")
    def _summary_is_self_consistent(self) -> CohortCaptureRunOutcomeV1:
        if self.started_at > self.finished_at:
            raise ValueError("bounded capture outcome finishes before it starts")
        if self.health_frames_consumed != self.frames_archived:
            raise ValueError("bounded capture frame count disagrees with ingestion health")
        if (
            self.health_decode_failures > self.health_rejected
            or self.health_unknown_event_type > self.health_rejected
        ):
            raise ValueError("capture health reason counters exceed rejected outcomes")
        event_outcomes = (
            self.health_accepted
            + self.health_duplicate
            + self.health_rejected
            + self.health_not_applicable
            - self.health_decode_failures
        )
        if event_outcomes > 2 * self.health_events_seen:
            raise ValueError("capture health outcomes exceed the two-token event fan-out")
        has_boundary_bytes = self.boundary_frame_bytes is not None
        has_boundary_hash = self.boundary_frame_sha256 is not None
        if has_boundary_bytes != has_boundary_hash:
            raise ValueError("excluded boundary size and digest must be present together")
        if self.stop_reason is BoundedCaptureStopReasonV2.BYTE_CAP and not has_boundary_bytes:
            raise ValueError("byte-cap outcome must account for its excluded boundary frame")
        if (
            self.stop_reason
            not in {
                BoundedCaptureStopReasonV2.BYTE_CAP,
                BoundedCaptureStopReasonV2.DURATION_CAP,
            }
            and has_boundary_bytes
        ):
            raise ValueError("only a time/byte cap can carry excluded boundary metadata")
        expected_id = build_cohort_capture_run_outcome_id(
            experiment_id=self.experiment_id,
            protocol_sha256=self.protocol_sha256,
            protocol_receipt_id=self.protocol_receipt_id,
            target_id=self.target_id,
            capture_run_id=self.capture_run_id,
            selection_sha256=self.selection_sha256,
            selection_receipt_id=self.selection_receipt_id,
            block_ordinal=self.block_ordinal,
            entry_index=self.entry_index,
            started_at=self.started_at,
            finished_at=self.finished_at,
            stop_reason=self.stop_reason,
            frames_archived=self.frames_archived,
            raw_bytes_archived=self.raw_bytes_archived,
            boundary_frame_bytes=self.boundary_frame_bytes,
            boundary_frame_sha256=self.boundary_frame_sha256,
            health=self.health,
            schema_version=self.schema_version,
        )
        if self.outcome_id != expected_id:
            raise ValueError("capture outcome identity disagrees with its run accounting")
        return self

    @property
    def health(self) -> CaptureHealth:
        return CaptureHealth(
            frames_consumed=self.health_frames_consumed,
            decode_failures=self.health_decode_failures,
            events_seen=self.health_events_seen,
            accepted=self.health_accepted,
            duplicate=self.health_duplicate,
            rejected=self.health_rejected,
            not_applicable=self.health_not_applicable,
            unknown_event_type=self.health_unknown_event_type,
        )


class CohortCaptureRunOutcomeV2(CohortCaptureRunOutcomeV1):
    """Accounting whose fetched boundary is retained in a separate evidence budget.

    Archival is verified by the V2 journal's independently pinned arrival chain,
    not by this summary alone. No change to the V1 non-retention contract.
    """

    schema_version: ClassVar[str] = "m4_cohort_capture_run_outcome.v2"
    # Deliberate widened field in a DIFFERENT schema; V1's literal stays unchanged.
    boundary_payload_archived: bool = Field(default=False, strict=True)  # type: ignore[assignment]

    @model_validator(mode="after")
    def _boundary_evidence(self) -> CohortCaptureRunOutcomeV2:
        if self.boundary_payload_archived != (self.boundary_frame_sha256 is not None):
            raise ValueError("V2 fetched boundary must have separate archived evidence")
        return self


def build_cohort_capture_run_outcome_id(
    *,
    experiment_id: str,
    protocol_sha256: str,
    protocol_receipt_id: str,
    target_id: str,
    capture_run_id: str,
    selection_sha256: str,
    selection_receipt_id: str,
    block_ordinal: int,
    entry_index: int,
    started_at: datetime,
    finished_at: datetime,
    stop_reason: BoundedCaptureStopReasonV2,
    frames_archived: int,
    raw_bytes_archived: int,
    boundary_frame_bytes: int | None,
    boundary_frame_sha256: str | None,
    health: CaptureHealth,
    schema_version: str = _SCHEMA_VERSION,
) -> str:
    material = {
        "version": schema_version.replace("_run_outcome.", "_run_outcome_identity."),
        "experiment_id": experiment_id,
        "protocol_sha256": protocol_sha256,
        "protocol_receipt_id": protocol_receipt_id,
        "target_id": target_id,
        "capture_run_id": capture_run_id,
        "selection_sha256": selection_sha256,
        "selection_receipt_id": selection_receipt_id,
        "block_ordinal": block_ordinal,
        "entry_index": entry_index,
        "started_at": ensure_utc(started_at).isoformat(),
        "finished_at": ensure_utc(finished_at).isoformat(),
        "stop_reason": stop_reason.value,
        "frames_archived": frames_archived,
        "raw_bytes_archived": raw_bytes_archived,
        "boundary_frame_bytes": boundary_frame_bytes,
        "boundary_frame_sha256": boundary_frame_sha256,
        "health": {
            "frames_consumed": health.frames_consumed,
            "decode_failures": health.decode_failures,
            "events_seen": health.events_seen,
            "accepted": health.accepted,
            "duplicate": health.duplicate,
            "rejected": health.rejected,
            "not_applicable": health.not_applicable,
            "unknown_event_type": health.unknown_event_type,
        },
    }
    digest = hashlib.sha256(orjson.dumps(material, option=orjson.OPT_SORT_KEYS)).hexdigest()
    return f"cohort-capture-outcome-{digest[:32]}"


def build_cohort_capture_run_outcome(
    *,
    protocol: AsynchronousCohortProtocolV2,
    protocol_receipt: EvidencePersistenceReceiptV1,
    selection: OfflineBlockSelectionV2,
    selection_receipt: EvidencePersistenceReceiptV1,
    entry_index: int,
    summary: BoundedCaptureSummaryV2,
    record_model: type[CohortCaptureRunOutcomeV1] = CohortCaptureRunOutcomeV1,
) -> CohortCaptureRunOutcomeV1:
    """Bind run accounting to the already-durable declaration and admission."""
    _verify_protocol_receipt(protocol, protocol_receipt)
    _verify_selection_receipt(protocol, selection, selection_receipt)
    _validate_summary_against_protocol(protocol, summary)
    _validate_admitted_target(protocol, selection, entry_index, summary.target_id)
    if selection_receipt.persisted_at > summary.started_at:
        raise ValueError("capture began before its admitted V2 target was durable")
    protocol_bytes = orjson.dumps(protocol.to_record(), option=orjson.OPT_SORT_KEYS)
    protocol_digest = hashlib.sha256(protocol_bytes).hexdigest()
    selection_bytes = orjson.dumps(selection.to_record(), option=orjson.OPT_SORT_KEYS)
    selection_digest = hashlib.sha256(selection_bytes).hexdigest()
    health = summary.health
    return record_model.model_validate(
        dict(
            outcome_id=build_cohort_capture_run_outcome_id(
                experiment_id=protocol.experiment_id,
                protocol_sha256=protocol_digest,
                protocol_receipt_id=protocol_receipt.receipt_id,
                target_id=summary.target_id,
                capture_run_id=summary.capture_run_id,
                selection_sha256=selection_digest,
                selection_receipt_id=selection_receipt.receipt_id,
                block_ordinal=selection.block_ordinal,
                entry_index=entry_index,
                started_at=summary.started_at,
                finished_at=summary.finished_at,
                stop_reason=summary.stop_reason,
                frames_archived=summary.frames_archived,
                raw_bytes_archived=summary.raw_bytes_archived,
                boundary_frame_bytes=summary.boundary_frame_bytes,
                boundary_frame_sha256=summary.boundary_frame_sha256,
                health=health,
                schema_version=record_model.schema_version,
            ),
            experiment_id=protocol.experiment_id,
            protocol_sha256=protocol_digest,
            protocol_receipt_id=protocol_receipt.receipt_id,
            target_id=summary.target_id,
            capture_run_id=summary.capture_run_id,
            selection_sha256=selection_digest,
            selection_receipt_id=selection_receipt.receipt_id,
            block_ordinal=selection.block_ordinal,
            entry_index=entry_index,
            started_at=summary.started_at,
            finished_at=summary.finished_at,
            stop_reason=summary.stop_reason,
            frames_archived=summary.frames_archived,
            raw_bytes_archived=summary.raw_bytes_archived,
            boundary_frame_bytes=summary.boundary_frame_bytes,
            boundary_frame_sha256=summary.boundary_frame_sha256,
            boundary_payload_archived=(
                record_model is CohortCaptureRunOutcomeV2
                and summary.boundary_frame_sha256 is not None
            ),
            health_frames_consumed=health.frames_consumed,
            health_decode_failures=health.decode_failures,
            health_events_seen=health.events_seen,
            health_accepted=health.accepted,
            health_duplicate=health.duplicate,
            health_rejected=health.rejected,
            health_not_applicable=health.not_applicable,
            health_unknown_event_type=health.unknown_event_type,
        )
    )


def persist_cohort_capture_run_outcome(
    archive_dir: Path,
    *,
    outcome: CohortCaptureRunOutcomeV1,
    persisted_at: datetime,
) -> EvidencePersistenceReceiptV1:
    """Persist and read back the versioned run outcome under its artifact ID."""
    if ensure_utc(persisted_at) < outcome.finished_at:
        raise ValueError("capture outcome cannot be durably persisted before run completion")
    return persist_evidence_record(
        archive_dir,
        record=outcome,
        experiment_id=outcome.experiment_id,
        artifact_kind=EvidenceArtifactKind.COHORT_CAPTURE_RUN_OUTCOME,
        artifact_id=outcome.outcome_id,
        persisted_at=persisted_at,
    )


def verify_cohort_capture_run_outcome_archives(
    outcome: CohortCaptureRunOutcomeV1,
    outcome_receipt: EvidencePersistenceReceiptV1,
    *,
    protocol: AsynchronousCohortProtocolV2,
    protocol_receipt: EvidencePersistenceReceiptV1,
    selection: OfflineBlockSelectionV2,
    selection_receipt: EvidencePersistenceReceiptV1,
    archive_dir: Path,
    prior_selections: tuple[OfflineBlockSelectionV2, ...] = (),
    prior_selection_receipts: tuple[EvidencePersistenceReceiptV1, ...] = (),
) -> CohortCaptureRunOutcomeV1:
    """Replay admission receipts and re-check persisted run counts and budgets."""
    _verify_protocol_receipt(protocol, protocol_receipt)
    _verify_selection_receipt(protocol, selection, selection_receipt)
    _validate_outcome_against_protocol(protocol, protocol_receipt, outcome)
    _validate_outcome_against_selection(protocol, selection, selection_receipt, outcome)
    verify_block_selection_v2_archives(
        protocol,
        selection,
        archive_dir=archive_dir,
        selection_receipt=selection_receipt,
        prior_selections=prior_selections,
        prior_selection_receipts=prior_selection_receipts,
    )
    verify_receipt_for_record(outcome_receipt, outcome)
    if (
        outcome_receipt.experiment_id != outcome.experiment_id
        or outcome_receipt.artifact_kind is not EvidenceArtifactKind.COHORT_CAPTURE_RUN_OUTCOME
        or outcome_receipt.artifact_id != outcome.outcome_id
    ):
        raise ValueError("capture outcome receipt identifies different evidence")
    if outcome_receipt.persisted_at < outcome.finished_at:
        raise ValueError("capture outcome receipt predates run completion")
    persisted_protocol = load_persisted_record(
        archive_dir, protocol_receipt, AsynchronousCohortProtocolV2
    )
    persisted_outcome = load_persisted_record(archive_dir, outcome_receipt, type(outcome))
    if persisted_protocol != protocol or persisted_outcome != outcome:
        raise ValueError("archived capture outcome chain disagrees with supplied evidence")
    return persisted_outcome


def _verify_protocol_receipt(
    protocol: AsynchronousCohortProtocolV2,
    receipt: EvidencePersistenceReceiptV1,
) -> None:
    verify_receipt_for_record(receipt, protocol)
    if (
        receipt.experiment_id != protocol.experiment_id
        or receipt.artifact_kind is not EvidenceArtifactKind.EXPERIMENT_PROTOCOL
        or receipt.artifact_id != protocol.experiment_id
    ):
        raise ValueError("capture outcome requires the durable V2 protocol receipt")
    if receipt.persisted_at > protocol.declared_at:
        raise ValueError("V2 protocol receipt postdates its declaration")


def _verify_selection_receipt(
    protocol: AsynchronousCohortProtocolV2,
    selection: OfflineBlockSelectionV2,
    receipt: EvidencePersistenceReceiptV1,
) -> None:
    verify_receipt_for_record(receipt, selection)
    if (
        selection.experiment_id != protocol.experiment_id
        or selection.protocol_sha256
        != hashlib.sha256(
            orjson.dumps(protocol.to_record(), option=orjson.OPT_SORT_KEYS)
        ).hexdigest()
        or receipt.experiment_id != protocol.experiment_id
        or receipt.artifact_kind is not EvidenceArtifactKind.COHORT_BLOCK_SELECTION
        or receipt.artifact_id != f"block-{selection.block_ordinal}"
        or receipt.persisted_at < selection.selected_at
    ):
        raise ValueError("capture outcome requires the selected block's durable receipt")


def _validate_admitted_target(
    protocol: AsynchronousCohortProtocolV2,
    selection: OfflineBlockSelectionV2,
    entry_index: int,
    target_id: str,
) -> None:
    if (
        selection.block_ordinal > len(protocol.blocks)
        or selection.selected_at < protocol.blocks[selection.block_ordinal - 1].start
        or selection.selected_at >= protocol.blocks[selection.block_ordinal - 1].end
    ):
        raise ValueError("capture outcome selection does not belong to a V2 protocol block")
    decision = next(
        (item for item in selection.page_decisions if item.entry_index == entry_index), None
    )
    if decision is None or decision.exclusion_reason is not None or decision.market is None:
        raise ValueError("capture outcome target was not admitted by its V2 selection")
    market = decision.market
    expected_target_id = build_target_id(
        experiment_id=protocol.experiment_id,
        market_id=market.market_id,
        condition_id=market.condition_id,
        yes_token_id=market.token_id_for("Yes"),
        no_token_id=market.token_id_for("No"),
    )
    if target_id != expected_target_id:
        raise ValueError("capture outcome target identity disagrees with admitted market")


def _validate_summary_against_protocol(
    protocol: AsynchronousCohortProtocolV2,
    summary: BoundedCaptureSummaryV2,
) -> None:
    if summary.health.frames_consumed != summary.frames_archived:
        raise ValueError("bounded capture frame count disagrees with ingestion health")
    if summary.frames_archived > protocol.capture_max_frames_per_target:
        raise ValueError("bounded capture frame count exceeds its declared V2 cap")
    if summary.raw_bytes_archived > protocol.capture_max_bytes_per_target:
        raise ValueError("bounded capture byte count exceeds its declared V2 cap")
    elapsed = summary.finished_at - summary.started_at
    if elapsed < timedelta(0) or elapsed > timedelta(
        seconds=protocol.capture_max_seconds_per_target
    ):
        raise ValueError("bounded capture duration exceeds its declared V2 cap")
    if (
        summary.stop_reason is BoundedCaptureStopReasonV2.FRAME_CAP
        and summary.frames_archived != protocol.capture_max_frames_per_target
    ):
        raise ValueError("frame-cap stop does not reach the declared V2 frame limit")
    if (
        summary.stop_reason is BoundedCaptureStopReasonV2.SOURCE_EXHAUSTED
        and summary.frames_archived >= protocol.capture_max_frames_per_target
    ):
        raise ValueError("source-exhausted stop conflicts with the reached V2 frame cap")
    if summary.stop_reason is BoundedCaptureStopReasonV2.BYTE_CAP and (
        summary.boundary_frame_bytes is None
        or summary.boundary_frame_sha256 is None
        or summary.raw_bytes_archived + summary.boundary_frame_bytes
        <= protocol.capture_max_bytes_per_target
    ):
        raise ValueError("byte-cap stop does not prove an excluded boundary over the cap")
    if (summary.boundary_frame_bytes is None) != (summary.boundary_frame_sha256 is None):
        raise ValueError("excluded boundary size and digest must be present together")
    if (
        summary.stop_reason
        not in {
            BoundedCaptureStopReasonV2.BYTE_CAP,
            BoundedCaptureStopReasonV2.DURATION_CAP,
        }
        and summary.boundary_frame_bytes is not None
    ):
        raise ValueError("non-cap stop cannot carry excluded boundary metadata")
    if summary.boundary_frame_sha256 is not None and (
        len(summary.boundary_frame_sha256) != SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in summary.boundary_frame_sha256)
    ):
        raise ValueError("excluded boundary digest must be lowercase SHA-256 hex")


def _validate_outcome_against_protocol(
    protocol: AsynchronousCohortProtocolV2,
    protocol_receipt: EvidencePersistenceReceiptV1,
    outcome: CohortCaptureRunOutcomeV1,
) -> None:
    if (
        outcome.experiment_id != protocol.experiment_id
        or outcome.protocol_receipt_id != protocol_receipt.receipt_id
        or outcome.protocol_sha256
        != hashlib.sha256(
            orjson.dumps(protocol.to_record(), option=orjson.OPT_SORT_KEYS)
        ).hexdigest()
    ):
        raise ValueError("capture outcome belongs to a different V2 declaration")
    if protocol_receipt.persisted_at > outcome.started_at:
        raise ValueError("capture began before its V2 protocol receipt was durable")
    _validate_summary_against_protocol(
        protocol,
        BoundedCaptureSummaryV2(
            target_id=outcome.target_id,
            capture_run_id=outcome.capture_run_id,
            started_at=outcome.started_at,
            finished_at=outcome.finished_at,
            stop_reason=outcome.stop_reason,
            frames_archived=outcome.frames_archived,
            raw_bytes_archived=outcome.raw_bytes_archived,
            boundary_frame_bytes=outcome.boundary_frame_bytes,
            boundary_frame_sha256=outcome.boundary_frame_sha256,
            health=outcome.health,
        ),
    )
    if type(outcome) is CohortCaptureRunOutcomeV1 and outcome.boundary_payload_archived:
        raise ValueError("excluded boundary payload must not be claimed as archived")


def _validate_outcome_against_selection(
    protocol: AsynchronousCohortProtocolV2,
    selection: OfflineBlockSelectionV2,
    selection_receipt: EvidencePersistenceReceiptV1,
    outcome: CohortCaptureRunOutcomeV1,
) -> None:
    selection_digest = hashlib.sha256(
        orjson.dumps(selection.to_record(), option=orjson.OPT_SORT_KEYS)
    ).hexdigest()
    if (
        outcome.selection_sha256 != selection_digest
        or outcome.selection_receipt_id != selection_receipt.receipt_id
        or outcome.block_ordinal != selection.block_ordinal
        or selection_receipt.persisted_at > outcome.started_at
    ):
        raise ValueError("capture outcome disagrees with durable target admission")
    _validate_admitted_target(
        protocol=protocol,
        selection=selection,
        entry_index=outcome.entry_index,
        target_id=outcome.target_id,
    )
