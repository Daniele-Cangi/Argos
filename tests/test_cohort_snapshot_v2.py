"""No-network integration proof from V2 admission to a frozen shared snapshot."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import orjson
import pytest
from pydantic import ValidationError
from test_cohort_protocol_v2 import START, _protocol
from test_cohort_selection_v2 import _entry, _persist_selection, _prepare

from argos.baselines import BaselineMethod, build_baseline_forecast_v2
from argos.baselines.quote import MarketQuoteV1
from argos.config.manifest import RunManifest, RunMode, WorkingTreeStatus
from argos.domain.observation import ObservationEnvelopeV1, RejectedObservationV1
from argos.domain.pricechange import PriceChangeV1
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.evaluation.cohort_selection_v2 import select_block_candidates_v2
from argos.evaluation.cohort_snapshot_v2 import (
    CohortCaptureCloseV1,
    CohortFrozenForecastSnapshotV1,
    build_cohort_capture_close_id,
    build_cohort_frozen_forecast_snapshot_id,
    verify_cohort_capture_close_archives,
)
from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    build_target_id,
    persist_evidence_record,
)
from argos.store.raw_archive import write_raw_payload


def _synthetic_snapshot(
    archive: Path,
    *,
    frame_count: int = 3,
    forecast_sequence: int | None = None,
    freeze_after_capture_close: bool = False,
) -> tuple[CohortCaptureCloseV1, object]:
    protocol = _protocol()
    protocol_receipt = persist_evidence_record(
        archive,
        record=protocol,
        experiment_id=protocol.experiment_id,
        artifact_kind=EvidenceArtifactKind.EXPERIMENT_PROTOCOL,
        artifact_id=protocol.experiment_id,
        persisted_at=protocol.declared_at,
    )
    raw, source, reviews, review_receipts, attempts, attempt_receipts, books = _prepare(
        archive, [_entry(701)], approved_ids=("701",)
    )
    selected_at = START + timedelta(seconds=30)
    selection = select_block_candidates_v2(
        protocol,
        block_ordinal=1,
        selected_at=selected_at,
        source_payload_bytes=raw,
        source_provenance=source,
        reviews=reviews,
        review_receipts=review_receipts,
        book_attempts=attempts,
        book_attempt_receipts=attempt_receipts,
        book_payload_bytes=books,
    )
    selection_receipt = _persist_selection(archive, selection)
    decision = selection.page_decisions[0]
    assert decision.market is not None
    market = decision.market
    frozen_sequence = frame_count if forecast_sequence is None else forecast_sequence
    review = next(item for item in selection.reviews if item.entry_index == decision.entry_index)
    target_id = build_target_id(
        experiment_id=protocol.experiment_id,
        market_id=market.market_id,
        condition_id=market.condition_id,
        yes_token_id=market.token_id_for("Yes"),
        no_token_id=market.token_id_for("No"),
    )
    started_at = START + timedelta(seconds=35)
    closed_at = START + timedelta(seconds=95)
    capture_run_id = "synthetic-capture-701"
    manifest = RunManifest(
        run_id=capture_run_id,
        mode=RunMode.CAPTURE,
        created_at=started_at,
        argos_version="synthetic",
        code_revision=protocol.code_revision,
        working_tree=WorkingTreeStatus.CLEAN,
        capture_run_id=capture_run_id,
        config_fingerprint=protocol.config_fingerprint,
        settings_snapshot={},
        run_parameters={
            "target_id": target_id,
            "subscribed_token_ids": (
                market.token_id_for("Yes"),
                market.token_id_for("No"),
            ),
            "max_seconds": protocol.capture_max_seconds_per_target,
            "max_frames": protocol.capture_max_frames_per_target,
            "max_bytes": protocol.capture_max_bytes_per_target,
            "raw_archive": True,
            "separate_database_per_target": True,
            "target_database_id": f"synthetic-db-{target_id}",
        },
        schema_versions=(
            ObservationEnvelopeV1.schema_version,
            RejectedObservationV1.schema_version,
            PriceChangeV1.schema_version,
            CohortCaptureCloseV1.schema_version,
            CohortFrozenForecastSnapshotV1.schema_version,
        ),
    )
    capture_bytes = orjson.dumps(
        [
            {
                "sequence": ordinal,
                "token_id": market.token_id_for("Yes")
                if ordinal % 2
                else market.token_id_for("No"),
                "condition_id": market.condition_id,
                "received_at": (START + timedelta(seconds=90)).isoformat(),
                "observation_id": f"synthetic-observation-{ordinal}",
                "information_state_hash": f"{ordinal:064x}",
            }
            for ordinal in range(1, frame_count + 1)
        ]
    )
    capture_provenance = SourceProvenanceV1(
        source="clob_ws",
        endpoint=f"clob-capture://{capture_run_id}/{target_id}",
        retrieved_at=closed_at + timedelta(seconds=1),
        raw_sha256=sha256_hex(capture_bytes),
        byte_length=len(capture_bytes),
    )
    write_raw_payload(archive, raw=capture_bytes, provenance=capture_provenance)
    quote_at = START + timedelta(seconds=90)
    quote = MarketQuoteV1(
        condition_id=market.condition_id,
        token_id=market.token_id_for("Yes"),
        best_bid=Decimal("0.40"),
        best_ask=Decimal("0.45"),
        best_bid_size=Decimal("10"),
        best_ask_size=Decimal("10"),
        midpoint=Decimal("0.425"),
        spread=Decimal("0.05"),
        last_trade_price=Decimal("0.42"),
        bid_levels=1,
        ask_levels=1,
        quote_time=quote_at,
    )
    forecasts = tuple(
        build_baseline_forecast_v2(
            method=method,
            quote=quote,
            as_of_received_time=quote_at,
            as_of_ingest_sequence=frozen_sequence,
            previous_score=None if method is BaselineMethod.PERSISTENCE else Decimal("0.42"),
            evaluation_run_id="synthetic-evaluation-701",
            source_capture_run_id=capture_run_id,
            source_observation_id=f"synthetic-observation-{frozen_sequence}",
            information_state_hash=f"{frozen_sequence:064x}",
            market_id=market.market_id,
            contract_id=review.contract_id,
        )
        for method in BaselineMethod
    )
    frozen_at = (
        START + timedelta(seconds=100)
        if freeze_after_capture_close
        else START + timedelta(seconds=92)
    )
    snapshot = CohortFrozenForecastSnapshotV1(
        snapshot_id=build_cohort_frozen_forecast_snapshot_id(
            protocol=protocol,
            protocol_receipt=protocol_receipt,
            selection=selection,
            selection_receipt=selection_receipt,
            entry_index=decision.entry_index,
            target_rank=1,
            target_id=target_id,
            capture_run_manifest=manifest,
            forecasts=forecasts,
            frozen_at=frozen_at,
        ),
        protocol=protocol,
        protocol_receipt=protocol_receipt,
        selection=selection,
        selection_receipt=selection_receipt,
        entry_index=decision.entry_index,
        target_rank=1,
        target_id=target_id,
        capture_run_manifest=manifest,
        forecasts=forecasts,
        frozen_at=frozen_at,
    )
    snapshot_receipt = persist_evidence_record(
        archive,
        record=snapshot,
        experiment_id=protocol.experiment_id,
        artifact_kind=EvidenceArtifactKind.FROZEN_FORECAST_SNAPSHOT,
        artifact_id=snapshot.snapshot_id,
        persisted_at=frozen_at,
    )
    close = CohortCaptureCloseV1(
        capture_close_id=build_cohort_capture_close_id(
            forecast_snapshot=snapshot,
            forecast_snapshot_receipt=snapshot_receipt,
            started_at=started_at,
            closed_at=closed_at,
            frame_count=frame_count,
            capture_archive_provenance=capture_provenance,
        ),
        forecast_snapshot=snapshot,
        forecast_snapshot_receipt=snapshot_receipt,
        started_at=started_at,
        closed_at=closed_at,
        frame_count=frame_count,
        capture_archive_provenance=capture_provenance,
    )
    close_receipt = persist_evidence_record(
        archive,
        record=close,
        experiment_id=protocol.experiment_id,
        artifact_kind=EvidenceArtifactKind.COHORT_CAPTURE_CLOSE,
        artifact_id=close.capture_close_id,
        persisted_at=closed_at + timedelta(seconds=2),
    )
    return close, close_receipt


def test_admitted_v2_target_freezes_before_close_and_replays_shared_snapshot(
    tmp_path: Path,
) -> None:
    close, receipt = _synthetic_snapshot(tmp_path)

    restored = CohortCaptureCloseV1.from_record(close.to_record())
    assert restored == close
    assert len(close.forecast_snapshot.forecasts) == 4
    assert sum(forecast.abstained for forecast in close.forecast_snapshot.forecasts) == 1
    assert close.forecast_snapshot_receipt.persisted_at < close.closed_at
    assert (
        verify_cohort_capture_close_archives(
            close,
            receipt,
            archive_dir=tmp_path,
        )
        == close
    )


def test_v2_capture_close_refuses_frame_cap_overrun(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="frame count exceeds"):
        _synthetic_snapshot(tmp_path, frame_count=501)


def test_v2_capture_close_rejects_a_postclose_forecast_freeze(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="freeze must occur during capture"):
        _synthetic_snapshot(tmp_path, freeze_after_capture_close=True)


def test_v2_freeze_must_reference_last_frame_available_by_freeze(tmp_path: Path) -> None:
    close, receipt = _synthetic_snapshot(tmp_path, forecast_sequence=2)

    with pytest.raises(ValueError, match="last shared state available"):
        verify_cohort_capture_close_archives(close, receipt, archive_dir=tmp_path)
