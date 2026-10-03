"""Versioned capture inventory and independent raw/store/forecast replay.

Frame ordinals are NOT ingest sequences. The journal retains every included
frame (including identical resends and empty arrays), while the EventStore
retains normalized arrivals. Re-normalizing the archived bytes must reproduce
both the arrival ledger and all ingestion counters before a snapshot is trusted.

Only capture acquisition is bounded here, not total DB/WAL/artifact storage.
No network client, campaign launcher, recovery or finality polling is provided.
The terminal journal is not a crash-resume checkpoint. V2 pins every arrival
independently and retains a fetched excluded boundary separately. V3 separates
book quote time from information-change trigger time in the frozen forecasts.
V1/V2 journals remain readable but cannot establish the current timing claim.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, Literal, Self, get_args

from pydantic import Field, field_validator, model_validator

from argos.baselines import (
    BaselineMethod,
    MarketBaselineForecastV3,
    build_baseline_forecast_v3,
    quote_from_book_state,
)
from argos.clock import ReplayClock, ensure_utc
from argos.compiler.contract import CompiledMarketContractV1
from argos.config.manifest import RunManifest
from argos.domain.lasttrade import LastTradePriceV1
from argos.domain.observation import ObservationEnvelopeV1, RejectedObservationV1
from argos.domain.orderbook import OrderBookSnapshotV1
from argos.domain.pricechange import PriceChangeV1
from argos.domain.provenance import SourceProvenanceV1
from argos.domain.versioning import VersionedModel, ensure_supported_version
from argos.domain.wsbook import WsBookSnapshotV1
from argos.evaluation.bundle import record_sha256
from argos.evaluation.cohort_capture_arrivals_v2 import (
    CohortCaptureArrivalSealV1,
    CohortCaptureArrivalV1,
    verify_capture_arrival_chain,
)
from argos.evaluation.cohort_capture_outcome_v2 import (
    CohortCaptureRunOutcomeV1,
    CohortCaptureRunOutcomeV2,
    verify_cohort_capture_run_outcome_archives,
)
from argos.evaluation.cohort_protocol_v2 import AsynchronousCohortProtocolV2
from argos.evaluation.cohort_selection_v2 import (
    OfflineBlockSelectionV2,
)
from argos.evaluation.cohort_snapshot_v2 import (
    CohortCaptureCloseV1,
    CohortFrozenForecastSnapshotV1,
    CohortFrozenForecastSnapshotV2,
    _capture_manifest_matches,
    _selected_target,
)
from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    EvidencePersistenceReceiptV1,
    load_persisted_record,
    verify_receipt_for_record,
)
from argos.evaluation.run_v2 import _information_state_hash
from argos.ingestion.capture import run_capture
from argos.projections.dispatch import DispatchOutcomeKind, ObservationDispatcher
from argos.replay.reader import ArrivalKind, read_capture_arrivals
from argos.sources.clob_ws import MarketFrame
from argos.store.event_store import CompletionStatus, EventStore, open_sqlite_event_store
from argos.store.raw_archive import archive_relative_location, read_raw_payload

__all__ = [
    "REQUIRED_OWNER_CAPTURE_SCHEMAS_V2",
    "CohortCaptureFrameV1",
    "CohortCaptureJournalV1",
    "CohortCaptureJournalV2",
    "CohortCaptureJournalV3",
    "verify_cohort_capture_journal_v2",
]


class CohortCaptureFrameV1(VersionedModel):
    """An included frame's arrival provenance and processing-time upper bound."""

    schema_version: ClassVar[str] = "m4_cohort_capture_frame.v1"
    ordinal: int = Field(gt=0, strict=True)
    provenance: SourceProvenanceV1
    processed_at: datetime

    @field_validator("processed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _first_hand(self) -> CohortCaptureFrameV1:
        if (
            self.provenance.source != "clob_market_ws"
            or self.provenance.reconstructed
            or self.processed_at < self.provenance.retrieved_at
        ):
            raise ValueError("journal frame requires first-hand WS provenance before processing")
        return self

    def to_record(self) -> dict[str, Any]:
        return {**super().to_record(), "provenance": self.provenance.to_record()}

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> CohortCaptureFrameV1:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["provenance"] = SourceProvenanceV1.from_record(payload["provenance"])
        return cls.model_validate(payload)


class CohortCaptureJournalV1(VersionedModel):
    """Terminal inventory linked to admission, accounting and optional blind freeze."""

    schema_version: ClassVar[str] = "m4_cohort_capture_journal.v1"
    _outcome_model: ClassVar[type[CohortCaptureRunOutcomeV1]] = CohortCaptureRunOutcomeV1
    _snapshot_model: ClassVar[type[CohortFrozenForecastSnapshotV1]] = CohortFrozenForecastSnapshotV1
    journal_id: str = Field(min_length=1)
    capture_run_manifest: RunManifest
    outcome: CohortCaptureRunOutcomeV1
    outcome_receipt: EvidencePersistenceReceiptV1
    frames: tuple[CohortCaptureFrameV1, ...]
    finalized_at: datetime
    snapshot: CohortFrozenForecastSnapshotV1 | None = None
    snapshot_receipt: EvidencePersistenceReceiptV1 | None = None

    @field_validator("finalized_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        for name in (
            "capture_run_manifest",
            "outcome",
            "outcome_receipt",
            "snapshot",
            "snapshot_receipt",
        ):
            value = getattr(self, name)
            record[name] = value.to_record() if value is not None else None
        record["frames"] = [frame.to_record() for frame in self.frames]
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Self:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        for name, model in (
            ("capture_run_manifest", RunManifest),
            ("outcome", cls._outcome_model),
            ("outcome_receipt", EvidencePersistenceReceiptV1),
            ("snapshot", cls._snapshot_model),
            ("snapshot_receipt", EvidencePersistenceReceiptV1),
        ):
            if payload.get(name) is not None:
                payload[name] = model.from_record(payload[name])
        payload["frames"] = tuple(
            CohortCaptureFrameV1.from_record(frame) for frame in payload["frames"]
        )
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def _inventory(self) -> CohortCaptureJournalV1:
        outcome = self.outcome
        if self.capture_run_manifest.capture_run_id != outcome.capture_run_id:
            raise ValueError("journal manifest and outcome identify different runs")
        _receipt(
            self.outcome_receipt,
            outcome,
            outcome.experiment_id,
            EvidenceArtifactKind.COHORT_CAPTURE_RUN_OUTCOME,
            outcome.outcome_id,
        )
        if self.outcome_receipt.persisted_at < outcome.finished_at:
            raise ValueError("journal outcome receipt predates completion")
        if self.finalized_at < self.outcome_receipt.persisted_at:
            raise ValueError("journal finalization predates durable accounting")
        if (
            len(self.frames) != outcome.frames_archived
            or sum(frame.provenance.byte_length for frame in self.frames)
            != outcome.raw_bytes_archived
        ):
            raise ValueError("journal frame/byte inventory disagrees with outcome")
        previous = outcome.started_at
        previous_processed = outcome.started_at
        for ordinal, frame in enumerate(self.frames, 1):
            if (
                frame.ordinal != ordinal
                or not previous <= frame.provenance.retrieved_at <= outcome.finished_at
                or not previous_processed <= frame.processed_at <= outcome.finished_at
            ):
                raise ValueError("journal frame order or capture interval is invalid")
            previous = frame.provenance.retrieved_at
            previous_processed = frame.processed_at
        if (self.snapshot is None) != (self.snapshot_receipt is None):
            raise ValueError("journal snapshot and receipt must be present together")
        if self.snapshot is not None:
            assert self.snapshot_receipt is not None
            snapshot = self.snapshot
            if (
                snapshot.capture_run_manifest != self.capture_run_manifest
                or snapshot.target_id != outcome.target_id
                or snapshot.entry_index != outcome.entry_index
                or snapshot.protocol_receipt.receipt_id != outcome.protocol_receipt_id
                or snapshot.selection_receipt.receipt_id != outcome.selection_receipt_id
                or snapshot.frozen_at < outcome.finished_at
            ):
                raise ValueError("journal snapshot disagrees with capture accounting")
            _receipt(
                self.snapshot_receipt,
                snapshot,
                outcome.experiment_id,
                EvidenceArtifactKind.FROZEN_FORECAST_SNAPSHOT,
                snapshot.snapshot_id,
            )
            _, review, _ = _selected_target(
                snapshot.protocol, snapshot.selection, snapshot.entry_index
            )
            deadline = review.earliest_outcome_knowable_at - timedelta(
                seconds=snapshot.protocol.outcome_blind_margin_seconds
            )
            if not snapshot.frozen_at <= self.snapshot_receipt.persisted_at < deadline:
                raise ValueError("snapshot receipt violates the outcome-blind boundary")
            if not self.snapshot_receipt.persisted_at <= self.finalized_at < deadline:
                raise ValueError("snapshot durability confirmation violates outcome-blind boundary")
            if self.outcome_receipt.persisted_at > snapshot.frozen_at:
                raise ValueError("snapshot freeze predates durable capture accounting")
        material = self.to_record()
        material.pop("journal_id")
        if self.journal_id != f"cohort-capture-journal-{record_sha256(material)[:32]}":
            raise ValueError("journal identity disagrees with its inventory")
        return self


class CohortCaptureJournalV2(CohortCaptureJournalV1):
    """Anchored inventory; late snapshotless accounting is explicitly excluded.

    V1 remains readable but is not independently qualified by this verifier.
    Boundary bytes live outside the included raw budget and never feed forecasts.
    """

    schema_version: ClassVar[str] = "m4_cohort_capture_journal.v2"
    _outcome_model: ClassVar[type[CohortCaptureRunOutcomeV1]] = CohortCaptureRunOutcomeV2
    outcome: CohortCaptureRunOutcomeV2
    arrival_seal: CohortCaptureArrivalSealV1
    arrival_seal_receipt: EvidencePersistenceReceiptV1
    boundary: CohortCaptureArrivalV1 | None = None
    blind_deadline: datetime
    finalization_status: Literal["snapshot_frozen", "no_snapshot", "late_no_snapshot_excluded"]

    @field_validator("blind_deadline")
    @classmethod
    def _deadline_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    def to_record(self) -> dict[str, Any]:
        return {
            **super().to_record(),
            "arrival_seal": self.arrival_seal.to_record(),
            "arrival_seal_receipt": self.arrival_seal_receipt.to_record(),
            "boundary": self.boundary.to_record() if self.boundary else None,
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Self:
        payload = dict(record)
        payload["arrival_seal"] = CohortCaptureArrivalSealV1.from_record(payload["arrival_seal"])
        payload["arrival_seal_receipt"] = EvidencePersistenceReceiptV1.from_record(
            payload["arrival_seal_receipt"]
        )
        if payload.get("boundary") is not None:
            payload["boundary"] = CohortCaptureArrivalV1.from_record(payload["boundary"])
        return super().from_record(payload)

    @model_validator(mode="after")
    def _anchored_inventory(self) -> CohortCaptureJournalV2:
        outcome, seal = self.outcome, self.arrival_seal
        _receipt(
            self.arrival_seal_receipt,
            seal,
            outcome.experiment_id,
            EvidenceArtifactKind.COHORT_CAPTURE_ARRIVAL_SEAL,
            seal.seal_id,
        )
        if (
            (seal.capture_run_id, seal.target_id) != (outcome.capture_run_id, outcome.target_id)
            or seal.arrival_count != len(self.frames) + int(self.boundary is not None)
            or not outcome.finished_at
            <= seal.sealed_at
            <= self.arrival_seal_receipt.persisted_at
            <= self.finalized_at
        ):
            raise ValueError("journal disagrees with arrival seal identity/count/chronology")
        if self.boundary is not None:
            boundary = self.boundary
            if (
                boundary.included
                or boundary.ordinal != len(self.frames) + 1
                or (boundary.capture_run_id, boundary.target_id)
                != (outcome.capture_run_id, outcome.target_id)
                or (boundary.provenance.byte_length, boundary.provenance.raw_sha256)
                != (outcome.boundary_frame_bytes, outcome.boundary_frame_sha256)
                or boundary.processed_at > seal.sealed_at
                or boundary.provenance.retrieved_at
                < (self.frames[-1].provenance.retrieved_at if self.frames else outcome.started_at)
            ):
                raise ValueError("journal boundary differs from excluded arrival accounting")
        elif outcome.boundary_frame_sha256 is not None:
            raise ValueError("journal omitted fetched boundary evidence")
        expected = (
            "snapshot_frozen"
            if self.snapshot is not None
            else "late_no_snapshot_excluded"
            if self.finalized_at >= self.blind_deadline
            else "no_snapshot"
        )
        if self.finalization_status != expected:
            raise ValueError("journal finalization requires explicit late/non-predictive status")
        return self


class CohortCaptureJournalV3(CohortCaptureJournalV2):
    """Anchored capture with book/trigger clocks separated in V2 snapshots.

    Legacy V1/V2 journals remain readable, but cannot establish V3 timing.
    """

    schema_version: ClassVar[str] = "m4_cohort_capture_journal.v3"
    _snapshot_model: ClassVar[type[CohortFrozenForecastSnapshotV1]] = CohortFrozenForecastSnapshotV2
    snapshot: CohortFrozenForecastSnapshotV2 | None = None


def _schema_closure(*models: type[VersionedModel]) -> frozenset[str]:
    """All nested declared models, including optional/empty collection branches.

    Envelope payloads are opaque mappings, so their dispatched payload models and
    the REST/compiled contracts read during admission replay are explicit roots.
    """
    schemas: set[str] = set()
    visited: set[type[VersionedModel]] = set()

    def visit(annotation: Any) -> None:
        if isinstance(annotation, type) and issubclass(annotation, VersionedModel):
            if annotation not in visited:
                visited.add(annotation)
                # A manifest names its own envelope version separately from
                # its data inventory, but its nested provenance still matters.
                if annotation is not RunManifest:
                    schemas.add(annotation.schema_version)
                for field in annotation.model_fields.values():
                    visit(field.annotation)
        else:
            for arg in get_args(annotation):
                visit(arg)

    for model in models:
        visit(model)
    return frozenset(schemas)


REQUIRED_OWNER_CAPTURE_SCHEMAS_V2 = _schema_closure(
    CohortCaptureJournalV3,
    CohortCaptureArrivalV1,
    ObservationEnvelopeV1,
    RejectedObservationV1,
    PriceChangeV1,
    LastTradePriceV1,
    WsBookSnapshotV1,
    OrderBookSnapshotV1,
    CompiledMarketContractV1,
    CohortCaptureCloseV1,
)


def _receipt(
    receipt: EvidencePersistenceReceiptV1,
    record: VersionedModel,
    experiment_id: str,
    kind: EvidenceArtifactKind,
    artifact_id: str,
) -> None:
    verify_receipt_for_record(receipt, record)
    if (receipt.experiment_id, receipt.artifact_kind, receipt.artifact_id) != (
        experiment_id,
        kind,
        artifact_id,
    ):
        raise ValueError("journal receipt names different evidence")


def _runtime_reserve(
    protocol: AsynchronousCohortProtocolV2,
    selection: OfflineBlockSelectionV2,
    selection_receipt: EvidencePersistenceReceiptV1,
    manifest: RunManifest,
    started_at: datetime,
    blind_deadline: datetime,
) -> None:
    admitted_at = max(selection.selected_at, selection_receipt.persisted_at)
    if manifest.created_at < admitted_at or started_at < max(admitted_at, manifest.created_at):
        raise ValueError("capture manifest/start predates durable target admission")
    if not REQUIRED_OWNER_CAPTURE_SCHEMAS_V2.issubset(manifest.schema_versions):
        raise ValueError("capture manifest omits journal/accounting schemas")
    reserved_end = started_at + timedelta(
        seconds=protocol.capture_max_seconds_per_target + protocol.finalization_reserve_seconds
    )
    if reserved_end >= blind_deadline:
        raise ValueError("capture cannot reserve finalization before outcome-blind boundary")
    block = protocol.blocks[selection.block_ordinal - 1]
    if not block.start <= started_at or reserved_end > block.end:
        raise ValueError("capture cannot fit acquisition/finalization in its declared block")


def _last_forecasts(
    store: EventStore,
    *,
    capture_run_id: str,
    condition_id: str,
    yes_token_id: str,
    market_id: str,
    contract_id: str,
) -> tuple[tuple[MarketBaselineForecastV3, ...], ObservationDispatcher]:
    """Same information-change/persistence semantics as the M4 evaluator, no labels."""
    dispatcher = ObservationDispatcher()
    previous_hash: str | None = None
    previous_midpoint: Decimal | None = None
    forecasts: tuple[MarketBaselineForecastV3, ...] = ()
    expected_sequence = 1
    previous_received: datetime | None = None
    book_event_time: datetime | None = None
    for arrival in read_capture_arrivals(store, capture_run_id):
        if arrival.ingest_sequence != expected_sequence or (
            previous_received is not None and arrival.received_time < previous_received
        ):
            raise ValueError("capture replay has sequence gaps or receive-time regression")
        expected_sequence += 1
        previous_received = arrival.received_time
        if arrival.kind is ArrivalKind.REJECTED:
            continue
        envelope = arrival.envelope
        assert envelope is not None
        if envelope.condition_id != condition_id:
            raise ValueError("isolated capture contains an observation for another condition")
        result = dispatcher.dispatch(envelope)
        if result.kind in {DispatchOutcomeKind.UNHANDLED_PAYLOAD, DispatchOutcomeKind.UNSCOPED}:
            raise ValueError("capture replay has an unsupported or unscoped observation")
        if result.kind is DispatchOutcomeKind.SKIPPED_DUPLICATE or (
            envelope.condition_id,
            envelope.token_id,
        ) != (condition_id, yes_token_id):
            continue
        projection = dispatcher.projections.get((condition_id, yes_token_id))
        if projection is None or not projection.is_seeded:
            continue
        if result.kind in {DispatchOutcomeKind.APPLIED_SNAPSHOT, DispatchOutcomeKind.APPLIED_DELTA}:
            # Projection.last_event_time retains the last *datable* event for
            # regression detection. It cannot attest the current book's time
            # after an undatable book update. Preserve that missingness here.
            book_event_time = envelope.event_time
        information_hash = _information_state_hash(dispatcher, condition_id, yes_token_id)
        if information_hash == previous_hash:
            continue
        trade = dispatcher.last_trades.get((condition_id, yes_token_id))
        quote = quote_from_book_state(
            projection.state(),
            quote_time=book_event_time,
            last_trade_price=trade.price if trade else None,
        )
        forecasts = tuple(
            build_baseline_forecast_v3(
                method=method,
                quote=quote,
                trigger_event_time=envelope.event_time,
                as_of_received_time=arrival.received_time,
                as_of_ingest_sequence=arrival.ingest_sequence,
                previous_score=previous_midpoint,
                evaluation_run_id=f"cohort-freeze-{capture_run_id}",
                source_capture_run_id=capture_run_id,
                source_observation_id=envelope.observation_id,
                information_state_hash=information_hash,
                market_id=market_id,
                contract_id=contract_id,
            )
            for method in BaselineMethod
        )
        previous_hash = information_hash
        if quote.midpoint is not None:
            previous_midpoint = quote.midpoint
    return forecasts, dispatcher


def _ledger(store: EventStore, run_id: str) -> tuple[object, ...]:
    # rejected_at is a processing clock sample, not raw source data. It is range
    # checked separately; replay reproduces every other field, including offset.
    rejections = []
    for row in store.iter_rejections(run_id):
        record = row.rejection.to_record()
        record.pop("rejected_at")
        rejections.append((row.ingest_sequence, row.duplicate_of_observation_id, record))
    return (
        tuple(store.iter_deliveries(run_id)),
        tuple(rejections),
        tuple(store.get_observation(row.observation_id) for row in store.iter_deliveries(run_id)),
    )


async def _verify_raw_store(
    journal: CohortCaptureJournalV1,
    *,
    store: EventStore,
    raw_archive_dir: Path,
    tokens: tuple[str, str],
) -> None:
    outcome = journal.outcome
    run = store.get_capture_run(outcome.capture_run_id)
    if run is None or (run.started_at, run.ended_at, run.completion_status) != (
        outcome.started_at,
        outcome.finished_at,
        CompletionStatus.COMPLETED,
    ):
        raise ValueError("EventStore run is missing, incomplete or differs from outcome")
    clock = ReplayClock(outcome.started_at)

    class ArchivedFrames:
        async def frames(self) -> AsyncIterator[MarketFrame]:
            for entry in journal.frames:
                raw, first_provenance = read_raw_payload(
                    raw_archive_dir, entry.provenance.raw_sha256
                )
                if (
                    not entry.provenance.matches(raw)
                    or first_provenance.reconstructed
                    or first_provenance.source != entry.provenance.source
                    or first_provenance.endpoint != entry.provenance.endpoint
                    or first_provenance.retrieved_at > entry.provenance.retrieved_at
                ):
                    raise ValueError("journal frame disagrees with raw archive provenance")
                clock.advance_to(entry.processed_at)
                yield MarketFrame(
                    text=raw.decode("utf-8"),
                    received_time=entry.provenance.retrieved_at,
                    provenance=entry.provenance,
                )
            clock.advance_to(outcome.finished_at)

    replay_store = open_sqlite_event_store(":memory:")
    try:
        health = await run_capture(
            frame_source=ArchivedFrames(),
            store=replay_store,
            clock=clock,
            capture_run_id=outcome.capture_run_id,
            subscribed_token_ids=tokens,
            raw_archive_dir=None,
        )
        # Replay does not write raw files. Archive locations are checked against
        # original provenance separately, not compared to replay's null value.
        actual = _ledger(store, outcome.capture_run_id)
        rebuilt = _ledger(replay_store, outcome.capture_run_id)
        if health != outcome.health or _without_locations(actual) != _without_locations(rebuilt):
            raise ValueError("raw replay disagrees with ingestion health or EventStore ledger")
    finally:
        replay_store.close()
    for rejection in store.iter_rejections(outcome.capture_run_id):
        record = rejection.rejection
        if not any(
            entry.provenance == record.provenance
            and outcome.started_at <= record.rejected_at <= entry.processed_at
            for entry in journal.frames
        ):
            raise ValueError("rejection processing time is outside its journal frame interval")


def _without_locations(value: object) -> object:
    if isinstance(value, VersionedModel):
        record = value.to_record()
        # All envelopes' archive references are independently checked below.
        record.pop("raw_payload_location", None)
        return record
    if isinstance(value, tuple):
        return tuple(_without_locations(item) for item in value)
    return value


async def verify_cohort_capture_journal_v2(
    journal: CohortCaptureJournalV2,
    receipt: EvidencePersistenceReceiptV1,
    *,
    protocol: AsynchronousCohortProtocolV2,
    protocol_receipt: EvidencePersistenceReceiptV1,
    selection: OfflineBlockSelectionV2,
    selection_receipt: EvidencePersistenceReceiptV1,
    store: EventStore,
    raw_archive_dir: Path,
    evidence_archive_dir: Path,
    prior_selections: tuple[OfflineBlockSelectionV2, ...] = (),
    prior_selection_receipts: tuple[EvidencePersistenceReceiptV1, ...] = (),
) -> CohortCaptureJournalV3:
    """Verify the V2 protocol owner's current V3 journal, including source clocks.

    The function suffix identifies the cohort protocol, not the journal schema.
    Older journal versions remain readable through their explicit record models;
    they cannot be qualified by reinterpreting their historical forecast times.
    """
    if not isinstance(journal, CohortCaptureJournalV3):
        raise ValueError(
            "legacy journal does not provide independently anchored V3 timing integrity"
        )
    outcome = journal.outcome
    _receipt(
        receipt,
        journal,
        protocol.experiment_id,
        EvidenceArtifactKind.COHORT_CAPTURE_JOURNAL,
        journal.journal_id,
    )
    if receipt.persisted_at < max(
        journal.finalized_at,
        journal.outcome_receipt.persisted_at,
        journal.snapshot_receipt.persisted_at if journal.snapshot_receipt else outcome.finished_at,
    ):
        raise ValueError("journal receipt predates its child receipts")
    if load_persisted_record(evidence_archive_dir, receipt, CohortCaptureJournalV3) != journal:
        raise ValueError("archived journal differs from supplied evidence")
    verify_cohort_capture_run_outcome_archives(
        outcome,
        journal.outcome_receipt,
        protocol=protocol,
        protocol_receipt=protocol_receipt,
        selection=selection,
        selection_receipt=selection_receipt,
        archive_dir=evidence_archive_dir,
        prior_selections=prior_selections,
        prior_selection_receipts=prior_selection_receipts,
    )
    decision, review, _ = _selected_target(protocol, selection, outcome.entry_index)
    market = decision.market
    assert market is not None
    tokens = (market.token_id_for("Yes"), market.token_id_for("No"))
    deadline = review.earliest_outcome_knowable_at - timedelta(
        seconds=protocol.outcome_blind_margin_seconds
    )
    if journal.blind_deadline != deadline:
        raise ValueError("journal blind deadline differs from reviewed admission")
    arrivals = verify_capture_arrival_chain(
        journal.arrival_seal,
        journal.arrival_seal_receipt,
        experiment_id=protocol.experiment_id,
        raw_archive_dir=raw_archive_dir,
        evidence_archive_dir=evidence_archive_dir,
    )
    included = tuple(arrival for arrival in arrivals if arrival.included)
    excluded = tuple(arrival for arrival in arrivals if not arrival.included)
    if tuple((item.ordinal, item.provenance, item.processed_at) for item in included) != tuple(
        (item.ordinal, item.provenance, item.processed_at) for item in journal.frames
    ) or excluded != ((journal.boundary,) if journal.boundary else ()):
        raise ValueError("journal differs from independently pinned frame arrivals")
    if journal.boundary is not None:
        boundary = journal.boundary
        if outcome.stop_reason.value == "duration_cap" and (
            max(boundary.provenance.retrieved_at, boundary.processed_at)
            <= outcome.started_at + timedelta(seconds=protocol.capture_max_seconds_per_target)
        ):
            raise ValueError("duration boundary does not exceed capture deadline")
    if outcome.stop_reason.value == "duration_cap" and outcome.finished_at != (
        outcome.started_at + timedelta(seconds=protocol.capture_max_seconds_per_target)
    ):
        raise ValueError("duration-cap accounting did not reach capture deadline")
    _capture_manifest_matches(
        protocol,
        journal.capture_run_manifest,
        target_id=outcome.target_id,
        yes_token_id=tokens[0],
        no_token_id=tokens[1],
        started_at=outcome.started_at,
    )
    _runtime_reserve(
        protocol,
        selection,
        selection_receipt,
        journal.capture_run_manifest,
        outcome.started_at,
        review.earliest_outcome_knowable_at
        - timedelta(seconds=protocol.outcome_blind_margin_seconds),
    )
    await _verify_raw_store(journal, store=store, raw_archive_dir=raw_archive_dir, tokens=tokens)
    for row in store.iter_deliveries(outcome.capture_run_id):
        envelope = store.get_observation(row.observation_id)
        assert envelope is not None
        if envelope.condition_id != market.condition_id or envelope.token_id not in tokens:
            raise ValueError("isolated capture store contains a different target")
        if envelope.raw_payload_location != archive_relative_location(envelope.provenance):
            raise ValueError("observation raw archive reference disagrees with provenance")
    forecasts, _ = _last_forecasts(
        store,
        capture_run_id=outcome.capture_run_id,
        condition_id=market.condition_id,
        yes_token_id=tokens[0],
        market_id=market.market_id,
        contract_id=review.contract_id,
    )
    if (journal.snapshot is None) != (not forecasts):
        raise ValueError("journal omitted an available snapshot or invented an unavailable one")
    if journal.snapshot is not None:
        assert journal.snapshot_receipt is not None
        if (
            load_persisted_record(
                evidence_archive_dir, journal.snapshot_receipt, CohortFrozenForecastSnapshotV2
            )
            != journal.snapshot
        ):
            raise ValueError("archived snapshot differs from journal")
        if forecasts != journal.snapshot.forecasts:
            raise ValueError("replayed baseline forecasts disagree with frozen snapshot")
    return journal
