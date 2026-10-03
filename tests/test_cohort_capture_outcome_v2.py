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
    CohortCaptureRunOutcomeV2,
    build_cohort_capture_run_outcome,
    build_cohort_capture_run_outcome_id,
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
    ("health", "message"),
    [
        (
            CaptureHealth(
                frames_consumed=2,
                events_seen=1,
                decode_failures=1,
                unknown_event_type=1,
                rejected=1,
            ),
            "combined reason counters",
        ),
        (CaptureHealth(frames_consumed=2, events_seen=2), "fewer outcomes than decoded events"),
    ],
)
@pytest.mark.parametrize("record_model", [CohortCaptureRunOutcomeV1, CohortCaptureRunOutcomeV2])
def test_builder_refuses_impossible_health_with_fresh_identity(
    tmp_path, health, message, record_model
):
    protocol, protocol_receipt, selection, selection_receipt, summary, original = _prepared_outcome(
        tmp_path
    )
    with pytest.raises(ValueError, match=message):
        build_cohort_capture_run_outcome(
            protocol=protocol,
            protocol_receipt=protocol_receipt,
            selection=selection,
            selection_receipt=selection_receipt,
            entry_index=0,
            summary=replace(summary, health=health),
            record_model=record_model,
        )
    record = original.to_record()
    record.update(
        schema_version=record_model.schema_version,
        health_decode_failures=health.decode_failures,
        health_events_seen=health.events_seen,
        health_unknown_event_type=health.unknown_event_type,
        health_rejected=health.rejected,
        health_accepted=health.accepted,
    )
    with pytest.raises(ValueError, match=message):
        record_model.from_record(record)


@pytest.mark.parametrize("record_model", [CohortCaptureRunOutcomeV1, CohortCaptureRunOutcomeV2])
def test_duration_cap_requires_exact_deadline_in_builder_and_archive(tmp_path, record_model):
    protocol, protocol_receipt, selection, selection_receipt, summary, original = _prepared_outcome(
        tmp_path
    )
    updates = dict(
        stop_reason=BoundedCaptureStopReasonV2.DURATION_CAP,
        finished_at=summary.started_at + timedelta(seconds=1),
    )
    original = build_cohort_capture_run_outcome(
        protocol=protocol,
        protocol_receipt=protocol_receipt,
        selection=selection,
        selection_receipt=selection_receipt,
        entry_index=0,
        summary=summary,
        record_model=record_model,
    )
    with pytest.raises(ValueError, match="duration-cap stop must reach"):
        build_cohort_capture_run_outcome(
            protocol=protocol,
            protocol_receipt=protocol_receipt,
            selection=selection,
            selection_receipt=selection_receipt,
            entry_index=0,
            summary=replace(summary, **updates),
            record_model=record_model,
        )
    # A content-valid assertion and real receipt do not prove a reached deadline.
    altered = _reidentified_outcome(original, **updates)
    receipt = persist_cohort_capture_run_outcome(
        tmp_path, outcome=altered, persisted_at=altered.finished_at
    )
    with pytest.raises(ValueError, match="duration-cap stop must reach"):
        verify_cohort_capture_run_outcome_archives(
            altered,
            receipt,
            protocol=protocol,
            protocol_receipt=protocol_receipt,
            selection=selection,
            selection_receipt=selection_receipt,
            archive_dir=tmp_path,
        )


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


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"protocol_sha256": "z" * 64}, "digests must be hexadecimal"),
        ({"finished_at": START}, "finishes before it starts"),
        ({"health_frames_consumed": 1}, "frame count disagrees"),
        ({"boundary_frame_bytes": 1}, "size and digest must be present together"),
        ({"boundary_frame_sha256": "a" * 64}, "size and digest must be present together"),
        ({"stop_reason": "byte_cap"}, "must account for its excluded boundary"),
        (
            {"boundary_frame_bytes": 1, "boundary_frame_sha256": "a" * 64},
            "only a time/byte cap",
        ),
        ({"raw_bytes_archived": 257}, "identity disagrees"),
        ({"boundary_payload_archived": True}, "False"),
    ],
)
def test_outcome_record_refuses_inconsistent_accounting(tmp_path, updates, message):
    *_, outcome = _prepared_outcome(tmp_path)
    with pytest.raises(ValueError, match=message):
        CohortCaptureRunOutcomeV1.from_record({**outcome.to_record(), **updates})


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"health": CaptureHealth(frames_consumed=1)}, "frame count disagrees"),
        ({"raw_bytes_archived": 1_000_001}, "byte count exceeds"),
        ({"finished_at": START}, "duration exceeds"),
        ({"finished_at": START + timedelta(seconds=156)}, "duration exceeds"),
        ({"stop_reason": BoundedCaptureStopReasonV2.FRAME_CAP}, "does not reach"),
        (
            {
                "frames_archived": 500,
                "health": CaptureHealth(frames_consumed=500),
            },
            "source-exhausted stop conflicts",
        ),
        ({"stop_reason": BoundedCaptureStopReasonV2.BYTE_CAP}, "excluded boundary over"),
        ({"boundary_frame_bytes": 1}, "size and digest must be present together"),
        (
            {"boundary_frame_bytes": 1, "boundary_frame_sha256": "a" * 64},
            "non-cap stop cannot carry",
        ),
        (
            {
                "stop_reason": BoundedCaptureStopReasonV2.DURATION_CAP,
                "boundary_frame_bytes": 1,
                "boundary_frame_sha256": "A" * 64,
            },
            "lowercase SHA-256",
        ),
        ({"target_id": "different-target"}, "target identity disagrees"),
        ({"started_at": START + timedelta(seconds=29)}, "before its admitted V2 target"),
    ],
)
def test_builder_refuses_invalid_bounded_summary(tmp_path, updates, message):
    protocol, protocol_receipt, selection, selection_receipt, summary, _ = _prepared_outcome(
        tmp_path
    )
    with pytest.raises(ValueError, match=message):
        build_cohort_capture_run_outcome(
            protocol=protocol,
            protocol_receipt=protocol_receipt,
            selection=selection,
            selection_receipt=selection_receipt,
            entry_index=0,
            summary=replace(summary, **updates),
        )


@pytest.mark.parametrize(
    ("receipt_name", "updates", "message"),
    [
        ("protocol", {"experiment_id": "foreign"}, "durable V2 protocol receipt"),
        (
            "protocol",
            {"artifact_kind": EvidenceArtifactKind.COHORT_BLOCK_SELECTION},
            "protocol receipt",
        ),
        ("protocol", {"artifact_id": "foreign"}, "protocol receipt"),
        ("protocol", {"persisted_at": START}, "postdates its declaration"),
        ("selection", {"experiment_id": "foreign"}, "selected block's durable receipt"),
        (
            "selection",
            {"artifact_kind": EvidenceArtifactKind.EXPERIMENT_PROTOCOL},
            "selected block",
        ),
        ("selection", {"artifact_id": "block-2"}, "selected block"),
        ("selection", {"persisted_at": START}, "selected block"),
    ],
)
def test_builder_rejects_receipts_with_valid_bytes_but_wrong_binding(
    tmp_path, receipt_name, updates, message
):
    protocol, protocol_receipt, selection, selection_receipt, summary, _ = _prepared_outcome(
        tmp_path
    )
    original = protocol_receipt if receipt_name == "protocol" else selection_receipt
    # A real content-matching receipt, issued in an independent synthetic archive.
    # Do not merely mutate its ID and fail the generic digest check first.
    receipt = persist_evidence_record(
        tmp_path / "foreign-receipt",
        record=protocol if receipt_name == "protocol" else selection,
        **{
            **{
                key: getattr(original, key)
                for key in ("experiment_id", "artifact_kind", "artifact_id", "persisted_at")
            },
            **updates,
        },
    )
    with pytest.raises(ValueError, match=message):
        build_cohort_capture_run_outcome(
            protocol=protocol,
            protocol_receipt=receipt if receipt_name == "protocol" else protocol_receipt,
            selection=selection,
            selection_receipt=receipt if receipt_name == "selection" else selection_receipt,
            entry_index=0,
            summary=summary,
        )


def _reidentified_outcome(outcome, **updates):
    altered = {**dict(outcome), **updates}
    identity = {
        key: altered[key]
        for key in (
            "experiment_id",
            "protocol_sha256",
            "protocol_receipt_id",
            "target_id",
            "capture_run_id",
            "selection_sha256",
            "selection_receipt_id",
            "block_ordinal",
            "entry_index",
            "started_at",
            "finished_at",
            "stop_reason",
            "frames_archived",
            "raw_bytes_archived",
            "boundary_frame_bytes",
            "boundary_frame_sha256",
        )
    }
    identity["health"] = outcome.health
    return type(outcome).model_validate(
        {
            **altered,
            "outcome_id": build_cohort_capture_run_outcome_id(
                **identity,
                schema_version=outcome.schema_version,
            ),
        }
    )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"experiment_id": "foreign"}, "different V2 declaration"),
        ({"protocol_sha256": "b" * 64}, "different V2 declaration"),
        ({"protocol_receipt_id": "foreign"}, "different V2 declaration"),
        ({"selection_sha256": "b" * 64}, "disagrees with durable target admission"),
        ({"selection_receipt_id": "foreign"}, "disagrees with durable target admission"),
        ({"block_ordinal": 2}, "disagrees with durable target admission"),
        ({"entry_index": 99}, "target was not admitted"),
        ({"target_id": "foreign"}, "target identity disagrees"),
        (
            {
                "started_at": START - timedelta(days=1, seconds=1),
                "finished_at": START - timedelta(days=1),
            },
            "before its V2 protocol receipt",
        ),
        ({"started_at": START + timedelta(seconds=29)}, "durable target admission"),
    ],
)
def test_archive_verifier_rejects_self_identified_but_wrong_admission(tmp_path, updates, message):
    protocol, protocol_receipt, selection, selection_receipt, _, outcome = _prepared_outcome(
        tmp_path
    )
    altered = _reidentified_outcome(outcome, **updates)
    receipt = persist_cohort_capture_run_outcome(
        tmp_path, outcome=altered, persisted_at=outcome.finished_at
    )
    with pytest.raises(ValueError, match=message):
        verify_cohort_capture_run_outcome_archives(
            altered,
            receipt,
            protocol=protocol,
            protocol_receipt=protocol_receipt,
            selection=selection,
            selection_receipt=selection_receipt,
            archive_dir=tmp_path,
        )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"experiment_id": "foreign"}, "identifies different evidence"),
        ({"artifact_kind": EvidenceArtifactKind.EXPERIMENT_PROTOCOL}, "different evidence"),
        ({"artifact_id": "foreign"}, "different evidence"),
        ({"persisted_at": START}, "predates run completion"),
    ],
)
def test_archive_verifier_checks_outcome_receipt_metadata(tmp_path, updates, message):
    protocol, protocol_receipt, selection, selection_receipt, _, outcome = _prepared_outcome(
        tmp_path
    )
    receipt = persist_evidence_record(
        tmp_path / "foreign-receipt",
        record=outcome,
        **{
            "experiment_id": outcome.experiment_id,
            "artifact_kind": EvidenceArtifactKind.COHORT_CAPTURE_RUN_OUTCOME,
            "artifact_id": outcome.outcome_id,
            "persisted_at": outcome.finished_at,
            **updates,
        },
    )
    with pytest.raises(ValueError, match=message):
        verify_cohort_capture_run_outcome_archives(
            outcome,
            receipt,
            protocol=protocol,
            protocol_receipt=protocol_receipt,
            selection=selection,
            selection_receipt=selection_receipt,
            archive_dir=tmp_path,
        )
