"""Synthetic-proofable V2 capture-close and shared forecast snapshot evidence.

These records deliberately do not use the V1 late-lifecycle monitor. They bind
one target admitted by the V2 page decision to a bounded capture and freeze all
baseline methods at one predeclared, outcome-blind information state. A live
capture owner and finality join remain separate work.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import orjson
from pydantic import Field, field_validator, model_validator

from argos.baselines import BaselineMethod, MarketBaselineForecastV2
from argos.clock import ensure_utc
from argos.config.manifest import RunManifest, RunMode, WorkingTreeStatus
from argos.domain.market import MarketDefinitionV1
from argos.domain.observation import ObservationEnvelopeV1, RejectedObservationV1
from argos.domain.pricechange import PriceChangeV1
from argos.domain.provenance import SHA256_LENGTH, SourceProvenanceV1
from argos.domain.versioning import VersionedModel, ensure_supported_version
from argos.evaluation.cohort_protocol_v2 import AsynchronousCohortProtocolV2
from argos.evaluation.cohort_review_v2 import SemanticReviewDecision
from argos.evaluation.cohort_selection_v2 import (
    BookAttemptStatus,
    OfflineBlockSelectionV2,
    PageDecisionV1,
    verify_block_selection_v2_archives,
)
from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    EvidencePersistenceReceiptV1,
    build_target_id,
    load_persisted_record,
    verify_receipt_for_record,
)
from argos.store.raw_archive import read_raw_payload

__all__ = [
    "CohortCaptureCloseV1",
    "CohortFrozenForecastSnapshotV1",
    "build_cohort_capture_close_id",
    "build_cohort_frozen_forecast_snapshot_id",
    "verify_cohort_capture_close_archives",
]


def _digest(value: object) -> str:
    return hashlib.sha256(orjson.dumps(value, option=orjson.OPT_SORT_KEYS)).hexdigest()


_FROZEN_SNAPSHOT_SCHEMA_VERSION = "m4_cohort_frozen_forecast_snapshot.v1"
_CAPTURE_CLOSE_SCHEMA_VERSION = "m4_cohort_capture_close.v1"
_REQUIRED_CAPTURE_SCHEMA_VERSIONS = frozenset(
    {
        ObservationEnvelopeV1.schema_version,
        RejectedObservationV1.schema_version,
        PriceChangeV1.schema_version,
        _CAPTURE_CLOSE_SCHEMA_VERSION,
        _FROZEN_SNAPSHOT_SCHEMA_VERSION,
    }
)


def _verify_receipt(
    receipt: EvidencePersistenceReceiptV1,
    record: VersionedModel,
    *,
    experiment_id: str,
    kind: EvidenceArtifactKind,
    artifact_id: str,
) -> None:
    verify_receipt_for_record(receipt, record)
    if (
        receipt.experiment_id != experiment_id
        or receipt.artifact_kind is not kind
        or receipt.artifact_id != artifact_id
    ):
        raise ValueError("V2 cohort receipt names different evidence")


def _selected_target(
    protocol: AsynchronousCohortProtocolV2,
    selection: OfflineBlockSelectionV2,
    entry_index: int,
) -> tuple[Any, Any, int]:
    if not 1 <= selection.block_ordinal <= len(protocol.blocks):
        raise ValueError("capture close names an unknown V2 block")
    block = protocol.blocks[selection.block_ordinal - 1]
    if (
        selection.experiment_id != protocol.experiment_id
        or selection.protocol_sha256 != _digest(protocol.to_record())
        or not block.start <= selection.selected_at < block.end
    ):
        raise ValueError("V2 selection does not belong to its declared protocol block")
    selected = [
        item
        for item in selection.page_decisions
        if item.exclusion_reason is None and item.market is not None
    ]
    ordered = sorted(
        selected,
        key=lambda item: (
            str(item.stratum_id),
            -(_selected_market(item).liquidity or Decimal(0)),
            int(_selected_market(item).market_id),
        ),
    )
    ranks = {item.entry_index: rank for rank, item in enumerate(ordered, start=1)}
    decision = next((item for item in selected if item.entry_index == entry_index), None)
    if decision is None:
        raise ValueError("capture close target was not admitted by the V2 selection")
    market = _selected_market(decision)
    review_match = next(
        (
            (review, receipt)
            for review, receipt in zip(selection.reviews, selection.review_receipts, strict=True)
            if receipt.receipt_id == decision.review_receipt_id
        ),
        None,
    )
    if review_match is None:
        raise ValueError("admitted V2 target has no matching semantic-review receipt")
    review, review_receipt = review_match
    if (
        review.entry_index != entry_index
        or review.market_id != decision.market_id
        or review.condition_id != market.condition_id
        or review.decision is not SemanticReviewDecision.APPROVED
        or review.event_group_id != decision.event_group_id
        or review.earliest_outcome_knowable_at is None
        or review_receipt.persisted_at > selection.selected_at
    ):
        raise ValueError("V2 semantic review does not bind the admitted target")
    _verify_receipt(
        review_receipt,
        review,
        experiment_id=protocol.experiment_id,
        kind=EvidenceArtifactKind.SEMANTIC_REVIEW,
        artifact_id=review.review_id,
    )
    book_match = next(
        (
            (attempt, receipt)
            for attempt, receipt in zip(
                selection.book_attempts, selection.book_attempt_receipts, strict=True
            )
            if receipt.receipt_id == decision.book_attempt_receipt_id
        ),
        None,
    )
    if book_match is None:
        raise ValueError("admitted V2 target has no matching CLOB book receipt")
    attempt, attempt_receipt = book_match
    if (
        attempt.market_id != decision.market_id
        or attempt.requested_token_id != market.token_id_for("Yes")
        or attempt.status is not BookAttemptStatus.RESPONSE
        or attempt_receipt.persisted_at > selection.selected_at
    ):
        raise ValueError("V2 admitted target does not bind a pre-selection Yes-token book")
    _verify_receipt(
        attempt_receipt,
        attempt,
        experiment_id=protocol.experiment_id,
        kind=EvidenceArtifactKind.COHORT_BOOK_ATTEMPT,
        artifact_id=attempt.attempt_id,
    )
    return decision, review, ranks[entry_index]


def _selected_market(decision: PageDecisionV1) -> MarketDefinitionV1:
    market = decision.market
    if market is None:
        raise ValueError("selected V2 entry has no market definition")
    return market


def _capture_target_id(
    protocol: AsynchronousCohortProtocolV2,
    market: Any,
) -> str:
    return build_target_id(
        experiment_id=protocol.experiment_id,
        market_id=market.market_id,
        condition_id=market.condition_id,
        yes_token_id=market.token_id_for("Yes"),
        no_token_id=market.token_id_for("No"),
    )


def _capture_manifest_matches(
    protocol: AsynchronousCohortProtocolV2,
    manifest: RunManifest,
    *,
    target_id: str,
    yes_token_id: str,
    no_token_id: str,
    started_at: datetime,
) -> None:
    if (
        manifest.mode is not RunMode.CAPTURE
        or manifest.capture_run_id is None
        or manifest.run_id != manifest.capture_run_id
        or manifest.created_at > started_at
        or manifest.code_revision != protocol.code_revision
        or manifest.working_tree is not WorkingTreeStatus.CLEAN
        or manifest.config_fingerprint != protocol.config_fingerprint
    ):
        raise ValueError("capture manifest disagrees with the frozen V2 runtime")
    parameters = manifest.run_parameters
    tokens = parameters.get("subscribed_token_ids")
    if (
        not isinstance(tokens, Sequence)
        or isinstance(tokens, (str, bytes))
        or len(tokens) != 2
        or any(not isinstance(item, str) or not item.strip() for item in tokens)
        or set(tokens) != {yes_token_id, no_token_id}
        or parameters.get("target_id") != target_id
    ):
        raise ValueError("capture manifest does not identify one bounded V2 target")
    if not _REQUIRED_CAPTURE_SCHEMA_VERSIONS.issubset(manifest.schema_versions):
        raise ValueError("capture manifest omits required V2 capture evidence schemas")
    max_seconds = parameters.get("max_seconds")
    if (
        not isinstance(max_seconds, int)
        or isinstance(max_seconds, bool)
        or max_seconds != protocol.capture_max_seconds_per_target
    ):
        raise ValueError("capture duration cap disagrees with the V2 protocol")
    for parameter, expected in (
        ("max_frames", protocol.capture_max_frames_per_target),
        ("max_bytes", protocol.capture_max_bytes_per_target),
    ):
        value = parameters.get(parameter)
        if not isinstance(value, int) or isinstance(value, bool) or value != expected:
            raise ValueError(f"capture {parameter} cap disagrees with the V2 protocol")
    if parameters.get("raw_archive") is not True:
        raise ValueError("V2 capture must preserve its raw archive")
    if parameters.get("separate_database_per_target") is not True:
        raise ValueError("V2 capture must use an isolated target database")
    database_id = parameters.get("target_database_id")
    if not isinstance(database_id, str) or not database_id.strip():
        raise ValueError("V2 capture manifest must identify its target database")


def _capture_frames(
    raw: bytes,
    *,
    yes_token_id: str,
    no_token_id: str,
    condition_id: str,
    started_at: datetime,
    closed_at: datetime,
) -> tuple[dict[str, Any], ...]:
    """Decode synthetic-proof archive frames and check their order and scope."""
    try:
        decoded = orjson.loads(raw)
    except orjson.JSONDecodeError as error:
        raise ValueError("V2 capture archive is not valid JSON") from error
    if (
        not isinstance(decoded, list)
        or not decoded
        or any(not isinstance(frame, dict) for frame in decoded)
    ):
        raise ValueError("V2 capture archive must be a nonempty JSON frame list")
    previous_time = started_at
    observation_ids: set[str] = set()
    for ordinal, frame in enumerate(decoded, start=1):
        if (
            not isinstance(frame.get("sequence"), int)
            or isinstance(frame.get("sequence"), bool)
            or frame.get("sequence") != ordinal
            or not isinstance(frame.get("token_id"), str)
            or frame.get("token_id") not in {yes_token_id, no_token_id}
            or frame.get("condition_id") != condition_id
            or not isinstance(frame.get("received_at"), str)
            or not isinstance(frame.get("observation_id"), str)
            or not frame["observation_id"].strip()
            or frame["observation_id"] in observation_ids
            or not isinstance(frame.get("information_state_hash"), str)
            or len(frame["information_state_hash"]) != SHA256_LENGTH
            or not all(char in "0123456789abcdef" for char in frame["information_state_hash"])
        ):
            raise ValueError("V2 capture archive frame identity or order is invalid")
        try:
            received_at = ensure_utc(datetime.fromisoformat(frame["received_at"]))
        except (TypeError, ValueError) as error:
            raise ValueError("V2 capture frame has an invalid receive time") from error
        if not previous_time <= received_at <= closed_at:
            raise ValueError("V2 capture archive frame times are outside the close interval")
        observation_ids.add(frame["observation_id"])
        previous_time = received_at
    return tuple(decoded)


class CohortFrozenForecastSnapshotV1(VersionedModel):
    """Four baselines durably frozen while one admitted V2 target is live."""

    schema_version: ClassVar[str] = _FROZEN_SNAPSHOT_SCHEMA_VERSION

    snapshot_id: str = Field(min_length=1)
    protocol: AsynchronousCohortProtocolV2
    protocol_receipt: EvidencePersistenceReceiptV1
    selection: OfflineBlockSelectionV2
    selection_receipt: EvidencePersistenceReceiptV1
    entry_index: int = Field(ge=0, strict=True)
    target_rank: int = Field(gt=0, strict=True)
    target_id: str = Field(min_length=1)
    capture_run_manifest: RunManifest
    forecasts: tuple[MarketBaselineForecastV2, ...]
    frozen_at: datetime

    @field_validator("frozen_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        record.update(
            {
                "protocol": self.protocol.to_record(),
                "protocol_receipt": self.protocol_receipt.to_record(),
                "selection": self.selection.to_record(),
                "selection_receipt": self.selection_receipt.to_record(),
                "capture_run_manifest": self.capture_run_manifest.to_record(),
            }
        )
        record["forecasts"] = [forecast.to_record() for forecast in self.forecasts]
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> CohortFrozenForecastSnapshotV1:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["protocol"] = AsynchronousCohortProtocolV2.from_record(dict(payload["protocol"]))
        payload["protocol_receipt"] = EvidencePersistenceReceiptV1.from_record(
            dict(payload["protocol_receipt"])
        )
        payload["selection"] = OfflineBlockSelectionV2.from_record(dict(payload["selection"]))
        payload["selection_receipt"] = EvidencePersistenceReceiptV1.from_record(
            dict(payload["selection_receipt"])
        )
        payload["capture_run_manifest"] = RunManifest.from_record(
            dict(payload["capture_run_manifest"])
        )
        payload["forecasts"] = tuple(
            MarketBaselineForecastV2.from_record(dict(item)) for item in payload["forecasts"]
        )
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def _one_blind_shared_information_state(self) -> CohortFrozenForecastSnapshotV1:
        protocol = self.protocol
        selection = self.selection
        _verify_receipt(
            self.protocol_receipt,
            protocol,
            experiment_id=protocol.experiment_id,
            kind=EvidenceArtifactKind.EXPERIMENT_PROTOCOL,
            artifact_id=protocol.experiment_id,
        )
        _verify_receipt(
            self.selection_receipt,
            selection,
            experiment_id=protocol.experiment_id,
            kind=EvidenceArtifactKind.COHORT_BLOCK_SELECTION,
            artifact_id=f"block-{selection.block_ordinal}",
        )
        if self.protocol_receipt.persisted_at > protocol.declared_at:
            raise ValueError("V2 protocol was not durable by its declared time")
        decision, review, expected_rank = _selected_target(protocol, selection, self.entry_index)
        assert decision.market is not None
        if self.target_rank != expected_rank or self.target_id != _capture_target_id(
            protocol, decision.market
        ):
            raise ValueError("forecast snapshot target identity or deterministic rank disagrees")
        admitted_at = max(selection.selected_at, self.selection_receipt.persisted_at)
        if self.frozen_at < admitted_at:
            raise ValueError("V2 forecast freeze predates durable target admission")
        if self.capture_run_manifest.created_at < admitted_at:
            raise ValueError("capture manifest predates durable V2 target admission")
        _capture_manifest_matches(
            protocol,
            self.capture_run_manifest,
            target_id=self.target_id,
            yes_token_id=decision.market.token_id_for("Yes"),
            no_token_id=decision.market.token_id_for("No"),
            started_at=self.frozen_at,
        )
        expected_methods = set(BaselineMethod)
        if (
            len(self.forecasts) != len(expected_methods)
            or {forecast.method for forecast in self.forecasts} != expected_methods
        ):
            raise ValueError("V2 snapshot requires exactly one forecast per baseline method")
        state_keys = {
            (
                forecast.evaluation_run_id,
                forecast.as_of_ingest_sequence,
                forecast.information_state_hash,
                forecast.source_observation_id,
                forecast.as_of_event_time,
                forecast.as_of_received_time,
                orjson.dumps(forecast.quote.to_record(), option=orjson.OPT_SORT_KEYS),
            )
            for forecast in self.forecasts
        }
        if len(state_keys) != 1:
            raise ValueError("V2 baselines must share one evaluation information state")
        blind_deadline = review.earliest_outcome_knowable_at - timedelta(
            seconds=protocol.outcome_blind_margin_seconds
        )
        if self.frozen_at >= blind_deadline:
            raise ValueError("V2 forecast snapshot violates the reviewed outcome-blind margin")
        for forecast in self.forecasts:
            if (
                forecast.source_capture_run_id != self.capture_run_manifest.capture_run_id
                or forecast.market_id != decision.market.market_id
                or forecast.condition_id != decision.market.condition_id
                or forecast.token_id != decision.market.token_id_for("Yes")
                or forecast.contract_id != review.contract_id
            ):
                raise ValueError("V2 snapshot forecast names a different target or capture")
            if (
                not max(admitted_at, self.capture_run_manifest.created_at)
                <= (forecast.as_of_received_time)
                <= self.frozen_at
            ):
                raise ValueError("V2 forecast was unavailable at the frozen information state")
        expected_id = build_cohort_frozen_forecast_snapshot_id(
            protocol=protocol,
            protocol_receipt=self.protocol_receipt,
            selection=selection,
            selection_receipt=self.selection_receipt,
            entry_index=self.entry_index,
            target_rank=self.target_rank,
            target_id=self.target_id,
            capture_run_manifest=self.capture_run_manifest,
            forecasts=self.forecasts,
            frozen_at=self.frozen_at,
        )
        if self.snapshot_id != expected_id:
            raise ValueError("V2 snapshot identity disagrees with its frozen evidence")
        return self


def build_cohort_frozen_forecast_snapshot_id(
    *,
    protocol: AsynchronousCohortProtocolV2,
    protocol_receipt: EvidencePersistenceReceiptV1,
    selection: OfflineBlockSelectionV2,
    selection_receipt: EvidencePersistenceReceiptV1,
    entry_index: int,
    target_rank: int,
    target_id: str,
    capture_run_manifest: RunManifest,
    forecasts: tuple[MarketBaselineForecastV2, ...],
    frozen_at: datetime,
) -> str:
    identity = (
        protocol_receipt.receipt_id,
        selection_receipt.receipt_id,
        entry_index,
        target_rank,
        target_id,
        capture_run_manifest.to_record(),
        [forecast.to_record() for forecast in forecasts],
        ensure_utc(frozen_at).isoformat(),
    )
    return f"cohort-frozen-forecast-{_digest(identity)[:32]}"


def build_cohort_capture_close_id(
    *,
    forecast_snapshot: CohortFrozenForecastSnapshotV1,
    forecast_snapshot_receipt: EvidencePersistenceReceiptV1,
    started_at: datetime,
    closed_at: datetime,
    frame_count: int,
    capture_archive_provenance: SourceProvenanceV1,
) -> str:
    identity = (
        forecast_snapshot.snapshot_id,
        forecast_snapshot_receipt.receipt_id,
        ensure_utc(started_at).isoformat(),
        ensure_utc(closed_at).isoformat(),
        frame_count,
        capture_archive_provenance.to_record(),
    )
    return f"cohort-capture-close-{_digest(identity)[:32]}"


class CohortCaptureCloseV1(VersionedModel):
    """Immutable close facts binding a prior durable V2 forecast freeze."""

    schema_version: ClassVar[str] = _CAPTURE_CLOSE_SCHEMA_VERSION

    capture_close_id: str = Field(min_length=1)
    forecast_snapshot: CohortFrozenForecastSnapshotV1
    forecast_snapshot_receipt: EvidencePersistenceReceiptV1
    started_at: datetime
    closed_at: datetime
    frame_count: int = Field(gt=0, strict=True)
    capture_archive_provenance: SourceProvenanceV1

    @field_validator("started_at", "closed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        record.update(
            {
                "forecast_snapshot": self.forecast_snapshot.to_record(),
                "forecast_snapshot_receipt": self.forecast_snapshot_receipt.to_record(),
                "capture_archive_provenance": self.capture_archive_provenance.to_record(),
            }
        )
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> CohortCaptureCloseV1:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["forecast_snapshot"] = CohortFrozenForecastSnapshotV1.from_record(
            dict(payload["forecast_snapshot"])
        )
        payload["forecast_snapshot_receipt"] = EvidencePersistenceReceiptV1.from_record(
            dict(payload["forecast_snapshot_receipt"])
        )
        payload["capture_archive_provenance"] = SourceProvenanceV1.from_record(
            dict(payload["capture_archive_provenance"])
        )
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def _capture_is_bound_and_bounded(self) -> CohortCaptureCloseV1:
        snapshot = self.forecast_snapshot
        protocol = snapshot.protocol
        selection = snapshot.selection
        _verify_receipt(
            self.forecast_snapshot_receipt,
            snapshot,
            experiment_id=protocol.experiment_id,
            kind=EvidenceArtifactKind.FROZEN_FORECAST_SNAPSHOT,
            artifact_id=snapshot.snapshot_id,
        )
        if not self.started_at <= snapshot.frozen_at < self.closed_at:
            raise ValueError("V2 forecast freeze must occur during capture and before close")
        if self.forecast_snapshot_receipt.persisted_at < snapshot.frozen_at:
            raise ValueError("V2 frozen forecast snapshot was persisted before it was frozen")
        if self.forecast_snapshot_receipt.persisted_at >= self.closed_at:
            raise ValueError("V2 forecast freeze receipt must precede capture close")
        if snapshot.capture_run_manifest.created_at > self.started_at:
            raise ValueError("V2 capture manifest was created after capture began")
        admitted_at = max(
            snapshot.selection.selected_at,
            snapshot.selection_receipt.persisted_at,
            snapshot.capture_run_manifest.created_at,
        )
        if self.started_at < admitted_at:
            raise ValueError("capture began before its admitted target/runtime was durable")
        if self.closed_at < self.started_at:
            raise ValueError("capture close predates its start")
        if (self.closed_at - self.started_at).total_seconds() > (
            protocol.capture_max_seconds_per_target
        ):
            raise ValueError("capture duration exceeds the per-target V2 cap")
        if self.frame_count > protocol.capture_max_frames_per_target:
            raise ValueError("capture frame count exceeds the per-target V2 cap")
        if self.capture_archive_provenance.byte_length <= 0:
            raise ValueError("capture archive must contain bytes")
        if self.capture_archive_provenance.byte_length > protocol.capture_max_bytes_per_target:
            raise ValueError("capture archive exceeds the per-target V2 byte cap")
        decision, review, _ = _selected_target(protocol, selection, snapshot.entry_index)
        assert decision.market is not None
        blind_deadline = review.earliest_outcome_knowable_at - timedelta(
            seconds=protocol.outcome_blind_margin_seconds
        )
        if self.closed_at > blind_deadline:
            raise ValueError("capture close violates the reviewed outcome-blind margin")
        if (
            self.capture_archive_provenance.reconstructed
            or self.capture_archive_provenance.retrieved_at < self.closed_at
        ):
            raise ValueError("capture archive provenance must be first-hand and post-close")
        if not _REQUIRED_CAPTURE_SCHEMA_VERSIONS.issubset(
            snapshot.capture_run_manifest.schema_versions
        ):
            raise ValueError("capture manifest omits required V2 capture evidence schemas")
        expected_id = build_cohort_capture_close_id(
            forecast_snapshot=snapshot,
            forecast_snapshot_receipt=self.forecast_snapshot_receipt,
            started_at=self.started_at,
            closed_at=self.closed_at,
            frame_count=self.frame_count,
            capture_archive_provenance=self.capture_archive_provenance,
        )
        if self.capture_close_id != expected_id:
            raise ValueError("capture close identity disagrees with its evidence")
        return self


def verify_cohort_capture_close_archives(
    close: CohortCaptureCloseV1,
    close_receipt: EvidencePersistenceReceiptV1,
    *,
    archive_dir: Path,
    prior_selections: tuple[OfflineBlockSelectionV2, ...] = (),
    prior_selection_receipts: tuple[EvidencePersistenceReceiptV1, ...] = (),
) -> CohortCaptureCloseV1:
    """Reload the pre-close freeze and replay its V2 admission/capture chain."""
    snapshot = close.forecast_snapshot
    protocol = snapshot.protocol
    if len(prior_selections) != len(prior_selection_receipts):
        raise ValueError("every predecessor V2 selection needs an archive receipt")
    _verify_receipt(
        close_receipt,
        close,
        experiment_id=protocol.experiment_id,
        kind=EvidenceArtifactKind.COHORT_CAPTURE_CLOSE,
        artifact_id=close.capture_close_id,
    )
    if close_receipt.persisted_at < max(
        close.closed_at, close.capture_archive_provenance.retrieved_at
    ):
        raise ValueError("V2 capture-close evidence was persisted before close/archive")
    _verify_receipt(
        snapshot.protocol_receipt,
        protocol,
        experiment_id=protocol.experiment_id,
        kind=EvidenceArtifactKind.EXPERIMENT_PROTOCOL,
        artifact_id=protocol.experiment_id,
    )
    if (
        load_persisted_record(archive_dir, snapshot.protocol_receipt, AsynchronousCohortProtocolV2)
        != protocol
    ):
        raise ValueError("archived V2 declaration disagrees with forecast snapshot")
    if (
        load_persisted_record(archive_dir, snapshot.selection_receipt, OfflineBlockSelectionV2)
        != snapshot.selection
    ):
        raise ValueError("archived V2 selection disagrees with forecast snapshot")
    verify_block_selection_v2_archives(
        protocol,
        snapshot.selection,
        archive_dir=archive_dir,
        selection_receipt=snapshot.selection_receipt,
        prior_selections=prior_selections,
        prior_selection_receipts=prior_selection_receipts,
    )
    if (
        load_persisted_record(
            archive_dir, close.forecast_snapshot_receipt, CohortFrozenForecastSnapshotV1
        )
        != snapshot
    ):
        raise ValueError("archived pre-close V2 forecast snapshot disagrees with close evidence")
    archived_capture, stored_provenance = read_raw_payload(
        archive_dir, close.capture_archive_provenance.raw_sha256
    )
    if (
        stored_provenance != close.capture_archive_provenance
        or not stored_provenance.matches(archived_capture)
        or len(archived_capture) > protocol.capture_max_bytes_per_target
    ):
        raise ValueError("archived V2 capture bytes disagree with their close evidence")
    decision, _, _ = _selected_target(protocol, snapshot.selection, snapshot.entry_index)
    assert decision.market is not None
    frames = _capture_frames(
        archived_capture,
        yes_token_id=decision.market.token_id_for("Yes"),
        no_token_id=decision.market.token_id_for("No"),
        condition_id=decision.market.condition_id,
        started_at=close.started_at,
        closed_at=close.closed_at,
    )
    if len(frames) != close.frame_count:
        raise ValueError("archived V2 frame count disagrees with capture-close evidence")
    state_sequence = snapshot.forecasts[0].as_of_ingest_sequence
    if state_sequence > len(frames):
        raise ValueError("frozen V2 forecast state is absent from the capture archive")
    frozen_frame = frames[state_sequence - 1]
    if any(
        frame["sequence"] > state_sequence
        and ensure_utc(datetime.fromisoformat(frame["received_at"])) <= snapshot.frozen_at
        for frame in frames
    ):
        raise ValueError("V2 forecast freeze does not use the last shared state available")
    if any(
        forecast.as_of_ingest_sequence != frozen_frame["sequence"]
        or forecast.source_observation_id != frozen_frame["observation_id"]
        or forecast.information_state_hash != frozen_frame["information_state_hash"]
        or forecast.as_of_received_time
        != ensure_utc(datetime.fromisoformat(frozen_frame["received_at"]))
        for forecast in snapshot.forecasts
    ):
        raise ValueError("V2 forecasts do not use their archived shared capture state")
    _verify_receipt(
        close.forecast_snapshot_receipt,
        snapshot,
        experiment_id=protocol.experiment_id,
        kind=EvidenceArtifactKind.FROZEN_FORECAST_SNAPSHOT,
        artifact_id=snapshot.snapshot_id,
    )
    _, review, _ = _selected_target(protocol, snapshot.selection, snapshot.entry_index)
    blind_deadline = review.earliest_outcome_knowable_at - timedelta(
        seconds=protocol.outcome_blind_margin_seconds
    )
    if close.forecast_snapshot_receipt.persisted_at >= min(blind_deadline, close.closed_at):
        raise ValueError("V2 forecast snapshot was not durable before close and outcome cutoff")
    if close_receipt.persisted_at >= blind_deadline:
        raise ValueError("V2 capture-close evidence missed the outcome-blind cutoff")
    return close
