"""No-network proof for durable owner-reported V2 capture accounting."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from test_cohort_protocol_v2 import START, _protocol
from test_cohort_selection_v2 import _entry, _persist_selection, _prepare

from argos.domain.provenance import SHA256_LENGTH
from argos.errors import ImmutabilityViolationError
from argos.evaluation.cohort_capture_outcome_v2 import (
    CohortCaptureRunOutcomeV1,
    build_cohort_capture_run_outcome,
    persist_cohort_capture_run_outcome,
    verify_cohort_capture_run_outcome_archives,
)
from argos.evaluation.cohort_protocol_v2 import AsynchronousCohortProtocolV2
from argos.evaluation.cohort_selection_v2 import (
    OfflineBlockSelectionV2,
    select_block_candidates_v2,
)
from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    EvidencePersistenceReceiptV1,
    build_target_id,
    persist_evidence_record,
)
from argos.ingestion.capture import CaptureHealth
from argos.ingestion.cohort_capture_v2 import (
    BoundedCaptureStopReasonV2,
    BoundedCaptureSummaryV2,
)


def _prepared_outcome(
    archive: Path,
) -> tuple[
    AsynchronousCohortProtocolV2,
    EvidencePersistenceReceiptV1,
    OfflineBlockSelectionV2,
    EvidencePersistenceReceiptV1,
    BoundedCaptureSummaryV2,
    CohortCaptureRunOutcomeV1,
]:
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
        archive, [_entry(801)], approved_ids=("801",)
    )
    selection = select_block_candidates_v2(
        protocol,
        block_ordinal=1,
        selected_at=START + timedelta(seconds=30),
        source_payload_bytes=raw,
        source_provenance=source,
        reviews=reviews,
        review_receipts=review_receipts,
        book_attempts=attempts,
        book_attempt_receipts=attempt_receipts,
        book_payload_bytes=books,
    )
    selection_receipt = _persist_selection(archive, selection)
    market = selection.page_decisions[0].market
    assert market is not None
    summary = BoundedCaptureSummaryV2(
        target_id=build_target_id(
            experiment_id=protocol.experiment_id,
            market_id=market.market_id,
            condition_id=market.condition_id,
            yes_token_id=market.token_id_for("Yes"),
            no_token_id=market.token_id_for("No"),
        ),
        capture_run_id="synthetic-capture-801",
        started_at=START + timedelta(seconds=35),
        finished_at=START + timedelta(seconds=45),
        stop_reason=BoundedCaptureStopReasonV2.SOURCE_EXHAUSTED,
        frames_archived=2,
        raw_bytes_archived=256,
        boundary_frame_bytes=None,
        boundary_frame_sha256=None,
        health=CaptureHealth(frames_consumed=2, events_seen=2, accepted=2),
    )
    outcome = build_cohort_capture_run_outcome(
        protocol=protocol,
        protocol_receipt=protocol_receipt,
        selection=selection,
        selection_receipt=selection_receipt,
        entry_index=selection.page_decisions[0].entry_index,
        summary=summary,
    )
    return protocol, protocol_receipt, selection, selection_receipt, summary, outcome


def test_capture_outcome_persists_and_replays_admission_chain(tmp_path: Path) -> None:
    protocol, protocol_receipt, selection, selection_receipt, _, outcome = _prepared_outcome(
        tmp_path
    )
    outcome_receipt = persist_cohort_capture_run_outcome(
        tmp_path, outcome=outcome, persisted_at=outcome.finished_at + timedelta(seconds=1)
    )

    assert CohortCaptureRunOutcomeV1.from_record(outcome.to_record()) == outcome
    assert outcome.boundary_payload_archived is False
    assert outcome.boundary_frame_bytes is None
    assert len(outcome.protocol_sha256) == SHA256_LENGTH
    assert (
        verify_cohort_capture_run_outcome_archives(
            outcome,
            outcome_receipt,
            protocol=protocol,
            protocol_receipt=protocol_receipt,
            selection=selection,
            selection_receipt=selection_receipt,
            archive_dir=tmp_path,
        )
        == outcome
    )


def test_capture_outcome_accounts_for_byte_cap_boundary_and_persisted_time(
    tmp_path: Path,
) -> None:
    protocol, protocol_receipt, selection, selection_receipt, summary, _ = _prepared_outcome(
        tmp_path
    )
    byte_cap_summary = replace(
        summary,
        stop_reason=BoundedCaptureStopReasonV2.BYTE_CAP,
        raw_bytes_archived=999_900,
        boundary_frame_bytes=101,
        boundary_frame_sha256="a" * SHA256_LENGTH,
    )
    outcome = build_cohort_capture_run_outcome(
        protocol=protocol,
        protocol_receipt=protocol_receipt,
        selection=selection,
        selection_receipt=selection_receipt,
        entry_index=selection.page_decisions[0].entry_index,
        summary=byte_cap_summary,
    )
    assert outcome.boundary_frame_bytes is not None
    assert outcome.raw_bytes_archived + outcome.boundary_frame_bytes == 1_000_001
    assert outcome.boundary_payload_archived is False

    with pytest.raises(ValueError, match="excluded boundary over the cap"):
        build_cohort_capture_run_outcome(
            protocol=protocol,
            protocol_receipt=protocol_receipt,
            selection=selection,
            selection_receipt=selection_receipt,
            entry_index=selection.page_decisions[0].entry_index,
            summary=replace(byte_cap_summary, boundary_frame_bytes=100),
        )
    with pytest.raises(ValueError, match="persisted before run completion"):
        persist_cohort_capture_run_outcome(
            tmp_path,
            outcome=outcome,
            persisted_at=outcome.finished_at - timedelta(microseconds=1),
        )


def test_capture_outcome_allows_decode_rejection_without_decoded_event(
    tmp_path: Path,
) -> None:
    protocol, protocol_receipt, selection, selection_receipt, summary, _ = _prepared_outcome(
        tmp_path
    )
    malformed_frame_summary = replace(
        summary,
        health=CaptureHealth(
            frames_consumed=2,
            decode_failures=1,
            rejected=1,
        ),
    )
    outcome = build_cohort_capture_run_outcome(
        protocol=protocol,
        protocol_receipt=protocol_receipt,
        selection=selection,
        selection_receipt=selection_receipt,
        entry_index=selection.page_decisions[0].entry_index,
        summary=malformed_frame_summary,
    )
    assert outcome.health_decode_failures == outcome.health_rejected == 1
    assert outcome.health_events_seen == 0


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("health_decode_failures", 1, "reason counters exceed"),
        ("health_unknown_event_type", 1, "reason counters exceed"),
        ("health_accepted", 5, "outcomes exceed the two-token"),
    ],
)
def test_capture_outcome_rejects_impossible_health_accounting(
    tmp_path: Path, field: str, value: int, message: str
) -> None:
    *_, outcome = _prepared_outcome(tmp_path)
    record = outcome.to_record()
    record[field] = value
    with pytest.raises(ValueError, match=message):
        CohortCaptureRunOutcomeV1.from_record(record)


def test_capture_outcome_rejects_changed_counts_and_corrupt_archived_record(
    tmp_path: Path,
) -> None:
    (
        protocol,
        protocol_receipt,
        selection,
        selection_receipt,
        summary,
        outcome,
    ) = _prepared_outcome(tmp_path)
    outcome_receipt = persist_cohort_capture_run_outcome(
        tmp_path, outcome=outcome, persisted_at=outcome.finished_at + timedelta(seconds=1)
    )

    over_cap_summary = replace(
        summary,
        frames_archived=501,
        health=CaptureHealth(frames_consumed=501, events_seen=2, accepted=2),
    )
    with pytest.raises(ValueError, match="frame count exceeds its declared V2 cap"):
        build_cohort_capture_run_outcome(
            protocol=protocol,
            protocol_receipt=protocol_receipt,
            selection=selection,
            selection_receipt=selection_receipt,
            entry_index=selection.page_decisions[0].entry_index,
            summary=over_cap_summary,
        )

    archived_outcome = tmp_path / outcome_receipt.storage_identity
    archived_outcome.write_bytes(b"{}")
    with pytest.raises(ImmutabilityViolationError):
        verify_cohort_capture_run_outcome_archives(
            outcome,
            outcome_receipt,
            protocol=protocol,
            protocol_receipt=protocol_receipt,
            selection=selection,
            selection_receipt=selection_receipt,
            archive_dir=tmp_path,
        )
