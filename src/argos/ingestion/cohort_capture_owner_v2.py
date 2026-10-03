"""One-shot V2 acquisition owner; pacing remains an ingestion-adapter concern.

Only synthetic integration is qualified. No network client, total-storage
enforcement, crash/resume path or finality scheduler is provided here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from argos.clock import Clock, Pacer
from argos.config.manifest import RunManifest
from argos.domain.versioning import VersionedModel
from argos.evaluation.bundle import record_sha256
from argos.evaluation.cohort_capture_arrivals_v2 import (
    CohortCaptureArrivalSealV1,
    CohortCaptureArrivalV1,
    persist_capture_arrival,
    persist_capture_arrival_seal,
)
from argos.evaluation.cohort_capture_outcome_v2 import (
    CohortCaptureRunOutcomeV2,
    _verify_protocol_receipt,
    _verify_selection_receipt,
    build_cohort_capture_run_outcome,
    persist_cohort_capture_run_outcome,
)
from argos.evaluation.cohort_capture_replay_v2 import (
    CohortCaptureFrameV1,
    CohortCaptureJournalV2,
    _last_forecasts,
    _runtime_reserve,
    verify_cohort_capture_journal_v2,
)
from argos.evaluation.cohort_protocol_v2 import AsynchronousCohortProtocolV2
from argos.evaluation.cohort_selection_v2 import (
    OfflineBlockSelectionV2,
    verify_block_selection_v2_archives,
)
from argos.evaluation.cohort_snapshot_v2 import (
    CohortFrozenForecastSnapshotV1,
    _capture_manifest_matches,
    _capture_target_id,
    _selected_target,
    build_cohort_frozen_forecast_snapshot_id,
)
from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    EvidencePersistenceReceiptV1,
    persist_evidence_record,
)
from argos.ingestion.capture import FrameSource
from argos.ingestion.cohort_capture_v2 import run_bounded_cohort_capture_v2
from argos.projections.dispatch import ObservationDispatcher
from argos.sources.clob_ws import MarketFrame
from argos.store.event_store import open_sqlite_event_store

__all__ = ["CohortCaptureOwnerResult", "run_cohort_capture_owner_v2"]


@dataclass(frozen=True, slots=True)
class CohortCaptureOwnerResult:
    journal: CohortCaptureJournalV2
    receipt: EvidencePersistenceReceiptV1


async def run_cohort_capture_owner_v2(
    *,
    protocol: AsynchronousCohortProtocolV2,
    protocol_receipt: EvidencePersistenceReceiptV1,
    selection: OfflineBlockSelectionV2,
    selection_receipt: EvidencePersistenceReceiptV1,
    entry_index: int,
    manifest: RunManifest,
    frame_source: FrameSource,
    clock: Clock,
    pacer: Pacer,
    database_path: Path,
    raw_archive_dir: Path,
    evidence_archive_dir: Path,
    prior_selections: tuple[OfflineBlockSelectionV2, ...] = (),
    prior_selection_receipts: tuple[EvidencePersistenceReceiptV1, ...] = (),
) -> CohortCaptureOwnerResult:
    """Acquire once into a new isolated DB, then freeze the last available state.

    Finalization occurs after acquisition closes, with actual clock/receipt times,
    strictly before the reviewed knowable-time margin (ADR-0020). This is NOT a
    CohortCaptureCloseV1, whose synthetic proof requires a receipt before close.
    Empty/unseeded captures remain inventoried without manufacturing a snapshot.
    """
    _verify_protocol_receipt(protocol, protocol_receipt)
    _verify_selection_receipt(protocol, selection, selection_receipt)
    verify_block_selection_v2_archives(
        protocol,
        selection,
        archive_dir=evidence_archive_dir,
        selection_receipt=selection_receipt,
        prior_selections=prior_selections,
        prior_selection_receipts=prior_selection_receipts,
    )
    decision, review, rank = _selected_target(protocol, selection, entry_index)
    market = decision.market
    assert market is not None
    target_id = _capture_target_id(protocol, market)
    tokens = (market.token_id_for("Yes"), market.token_id_for("No"))
    started_at = clock.now()
    _capture_manifest_matches(
        protocol,
        manifest,
        target_id=target_id,
        yes_token_id=tokens[0],
        no_token_id=tokens[1],
        started_at=started_at,
    )
    if started_at < max(selection.selected_at, selection_receipt.persisted_at, manifest.created_at):
        raise ValueError("capture starts before durable admission or manifest")
    deadline = review.earliest_outcome_knowable_at - timedelta(
        seconds=protocol.outcome_blind_margin_seconds
    )
    _runtime_reserve(protocol, selection, selection_receipt, manifest, started_at, deadline)
    if manifest.run_parameters.get("target_database_id") != database_path.name:
        raise ValueError("capture manifest does not name the newly isolated database")
    # Exclusive creation is also a restart guard: never reuse a predecessor DB.
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with database_path.open("xb") as reservation:
        created_database = os.fstat(reservation.fileno())
    # Content-addressing alone cannot isolate runs: repeated bytes would reuse
    # a predecessor's sidecar. Never adopt an already populated target archive.
    try:
        raw_archive_dir.mkdir(parents=True, exist_ok=False)
    except OSError:
        # No store was opened and no capture started. Only remove this call's
        # unchanged empty reservation; never delete predecessor/changed data.
        try:
            current = database_path.stat(follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if (current.st_dev, current.st_ino, current.st_mode, current.st_size) == (
                created_database.st_dev,
                created_database.st_ino,
                created_database.st_mode,
                0,
            ):
                database_path.unlink()
        raise
    store = open_sqlite_event_store(database_path)
    frames: list[CohortCaptureFrameV1] = []
    live_dispatcher = ObservationDispatcher()
    last_arrival_receipt: EvidencePersistenceReceiptV1 | None = None
    boundary: CohortCaptureArrivalV1 | None = None

    def record_arrival(
        frame: MarketFrame, ordinal: int, *, included: bool
    ) -> CohortCaptureArrivalV1:
        nonlocal last_arrival_receipt
        arrival = CohortCaptureArrivalV1(
            capture_run_id=manifest.run_id,
            target_id=target_id,
            ordinal=ordinal,
            included=included,
            provenance=frame.provenance,
            processed_at=clock.now(),
            previous_receipt_id=last_arrival_receipt.receipt_id if last_arrival_receipt else None,
        )
        last_arrival_receipt = persist_capture_arrival(
            arrival,
            experiment_id=protocol.experiment_id,
            raw=frame.text.encode("utf-8"),
            raw_archive_dir=raw_archive_dir,
            evidence_archive_dir=evidence_archive_dir,
            persisted_at=clock.now(),
        )
        return arrival

    def after_frame(frame: MarketFrame, ordinal: int) -> None:
        arrival = record_arrival(frame, ordinal, included=True)
        frames.append(
            CohortCaptureFrameV1(
                ordinal=ordinal, provenance=frame.provenance, processed_at=arrival.processed_at
            )
        )

    def after_boundary(frame: MarketFrame, ordinal: int) -> None:
        nonlocal boundary
        boundary = record_arrival(frame, ordinal, included=False)

    try:
        summary = await run_bounded_cohort_capture_v2(
            target_id=target_id,
            frame_source=frame_source,
            store=store,
            clock=clock,
            pacer=pacer,
            capture_run_id=manifest.run_id,
            subscribed_token_ids=tokens,
            max_seconds=protocol.capture_max_seconds_per_target,
            max_frames=protocol.capture_max_frames_per_target,
            max_bytes=protocol.capture_max_bytes_per_target,
            raw_archive_dir=raw_archive_dir,
            after_frame=after_frame,
            dispatcher=live_dispatcher,
            after_boundary=after_boundary,
        )
        seal = CohortCaptureArrivalSealV1(
            capture_run_id=manifest.run_id,
            target_id=target_id,
            arrival_count=len(frames) + int(boundary is not None),
            last_receipt_id=last_arrival_receipt.receipt_id if last_arrival_receipt else None,
            sealed_at=clock.now(),
        )
        seal_receipt = persist_capture_arrival_seal(
            seal,
            experiment_id=protocol.experiment_id,
            raw_archive_dir=raw_archive_dir,
            evidence_archive_dir=evidence_archive_dir,
            persisted_at=clock.now(),
        )
        outcome = build_cohort_capture_run_outcome(
            protocol=protocol,
            protocol_receipt=protocol_receipt,
            selection=selection,
            selection_receipt=selection_receipt,
            entry_index=entry_index,
            summary=summary,
            record_model=CohortCaptureRunOutcomeV2,
        )
        outcome_receipt = persist_cohort_capture_run_outcome(
            evidence_archive_dir, outcome=outcome, persisted_at=clock.now()
        )
        forecasts, replay_dispatcher = _last_forecasts(
            store,
            capture_run_id=manifest.run_id,
            condition_id=market.condition_id,
            yes_token_id=tokens[0],
            market_id=market.market_id,
            contract_id=review.contract_id,
        )
        if (
            live_dispatcher.state_hash() != replay_dispatcher.state_hash()
            or live_dispatcher.last_trades != replay_dispatcher.last_trades
        ):
            raise ValueError("live and stored replay dispatch state disagree")
        snapshot = None
        snapshot_receipt = None
        if forecasts:
            frozen_at = clock.now()
            args: dict[str, Any] = dict(
                protocol=protocol,
                protocol_receipt=protocol_receipt,
                selection=selection,
                selection_receipt=selection_receipt,
                entry_index=entry_index,
                target_rank=rank,
                target_id=target_id,
                capture_run_manifest=manifest,
                forecasts=forecasts,
                frozen_at=frozen_at,
            )
            snapshot = CohortFrozenForecastSnapshotV1(
                snapshot_id=build_cohort_frozen_forecast_snapshot_id(**args), **args
            )
            snapshot_receipt = persist_evidence_record(
                evidence_archive_dir,
                record=snapshot,
                experiment_id=protocol.experiment_id,
                artifact_kind=EvidenceArtifactKind.FROZEN_FORECAST_SNAPSHOT,
                artifact_id=snapshot.snapshot_id,
                persisted_at=clock.now(),
            )
        finalized_at = clock.now()
        args_journal: dict[str, Any] = dict(
            capture_run_manifest=manifest,
            outcome=outcome,
            outcome_receipt=outcome_receipt,
            frames=tuple(frames),
            finalized_at=finalized_at,
            snapshot=snapshot,
            snapshot_receipt=snapshot_receipt,
            arrival_seal=seal,
            arrival_seal_receipt=seal_receipt,
            boundary=boundary,
            blind_deadline=deadline,
            finalization_status=(
                "snapshot_frozen"
                if snapshot is not None
                else "late_no_snapshot_excluded"
                if finalized_at >= deadline
                else "no_snapshot"
            ),
        )
        material = {
            "schema_version": CohortCaptureJournalV2.schema_version,
            **{
                name: (
                    [item.to_record() for item in value]
                    if isinstance(value, tuple)
                    else value.to_record()
                    if isinstance(value, VersionedModel)
                    else TypeAdapter(datetime).dump_python(value, mode="json")
                    if isinstance(value, datetime)
                    else value
                )
                for name, value in args_journal.items()
            },
        }
        journal = CohortCaptureJournalV2(
            journal_id=f"cohort-capture-journal-{record_sha256(material)[:32]}", **args_journal
        )
        receipt = persist_evidence_record(
            evidence_archive_dir,
            record=journal,
            experiment_id=protocol.experiment_id,
            artifact_kind=EvidenceArtifactKind.COHORT_CAPTURE_JOURNAL,
            artifact_id=journal.journal_id,
            persisted_at=clock.now(),
        )
        await verify_cohort_capture_journal_v2(
            journal,
            receipt,
            protocol=protocol,
            protocol_receipt=protocol_receipt,
            selection=selection,
            selection_receipt=selection_receipt,
            store=store,
            raw_archive_dir=raw_archive_dir,
            evidence_archive_dir=evidence_archive_dir,
            prior_selections=prior_selections,
            prior_selection_receipts=prior_selection_receipts,
        )
        return CohortCaptureOwnerResult(journal=journal, receipt=receipt)
    finally:
        store.close()
