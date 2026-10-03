"""Review regressions: fully receipted alternate journals, never real evidence."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_capture_loop import _frame
from test_cohort_capture_journal_guards_v2 import _persist_altered_journal
from test_cohort_capture_replay_v2 import _book, _prepared, _run, _Source, _verify
from test_cohort_protocol_v2 import START

from argos.clock import RealPacer, ReplayClock
from argos.evaluation.bundle import record_sha256
from argos.evaluation.cohort_capture_arrivals_v2 import (
    CohortCaptureArrivalV1,
    verify_capture_arrival_chain,
)
from argos.evaluation.cohort_capture_outcome_v2 import (
    CohortCaptureRunOutcomeV1,
    CohortCaptureRunOutcomeV2,
    build_cohort_capture_run_outcome,
    persist_cohort_capture_run_outcome,
)
from argos.evaluation.cohort_capture_replay_v2 import (
    REQUIRED_OWNER_CAPTURE_SCHEMAS_V2,
    CohortCaptureFrameV1,
    CohortCaptureJournalV1,
    CohortCaptureJournalV2,
)
from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    persist_evidence_record,
)
from argos.ingestion.cohort_capture_owner_v2 import (
    CohortCaptureOwnerResult,
    run_cohort_capture_owner_v2,
)
from argos.ingestion.cohort_capture_v2 import BoundedCaptureSummaryV2


def _outcome(args, original, frames, *, record_model=CohortCaptureRunOutcomeV2, **updates):
    summary = BoundedCaptureSummaryV2(
        target_id=original.target_id,
        capture_run_id=original.capture_run_id,
        started_at=original.started_at,
        finished_at=original.finished_at,
        stop_reason=original.stop_reason,
        frames_archived=len(frames),
        raw_bytes_archived=sum(frame.provenance.byte_length for frame in frames),
        boundary_frame_bytes=original.boundary_frame_bytes,
        boundary_frame_sha256=original.boundary_frame_sha256,
        health=replace(original.health, frames_consumed=len(frames)),
    )
    outcome = build_cohort_capture_run_outcome(
        protocol=args["protocol"],
        protocol_receipt=args["protocol_receipt"],
        selection=args["selection"],
        selection_receipt=args["selection_receipt"],
        entry_index=0,
        summary=replace(summary, **updates),
        record_model=record_model,
    )
    receipt = persist_cohort_capture_run_outcome(
        args["evidence_archive_dir"], outcome=outcome, persisted_at=original.finished_at
    )
    return outcome, receipt


def _chain(args, journal):
    return verify_capture_arrival_chain(
        journal.arrival_seal,
        journal.arrival_seal_receipt,
        experiment_id=args["protocol"].experiment_id,
        raw_archive_dir=args["raw_archive_dir"],
        evidence_archive_dir=args["evidence_archive_dir"],
    )


@pytest.mark.parametrize("mutation", ["insert", "delete", "retime", "processing", "reorder"])
async def test_fresh_receipts_cannot_replace_ledger_silent_arrival_chain(tmp_path, mutation):
    args, result = await _run(
        tmp_path,
        [
            _frame([], received_time=START + timedelta(seconds=36)),
            _frame(
                None,
                text=" [] " if mutation == "reorder" else "[]",
                received_time=START + timedelta(seconds=36 if mutation == "reorder" else 37),
            ),
        ],
        end_at=START + timedelta(seconds=39),
    )
    journal = result.journal
    frames = list(journal.frames)
    if mutation == "insert":
        frames.append(
            CohortCaptureFrameV1(
                ordinal=3,
                provenance=frames[-1].provenance.model_copy(
                    update={"retrieved_at": START + timedelta(seconds=38)}
                ),
                processed_at=START + timedelta(seconds=38),
            )
        )
    elif mutation == "delete":
        frames.pop()
    elif mutation == "retime":
        frames[-1] = frames[-1].model_copy(
            update={
                "provenance": frames[-1].provenance.model_copy(
                    update={"retrieved_at": START + timedelta(seconds=38)}
                ),
                "processed_at": START + timedelta(seconds=38),
            }
        )
    elif mutation == "processing":
        # Distinguish processing time from the original receipt time.
        frames[0] = frames[0].model_copy(update={"processed_at": frames[-1].processed_at})
    else:
        # Different ledger-silent bytes at equal times: swapping them does not
        # violate chronological validators or counters, only the arrival proof.
        frames = [
            frame.model_copy(update={"ordinal": ordinal})
            for ordinal, frame in enumerate(reversed(frames), 1)
        ]
    outcome, outcome_receipt = _outcome(args, journal.outcome, frames)
    previous = None
    for frame in frames:
        arrival = CohortCaptureArrivalV1(
            capture_run_id=outcome.capture_run_id,
            target_id=outcome.target_id,
            ordinal=frame.ordinal,
            included=True,
            provenance=frame.provenance,
            processed_at=frame.processed_at,
            previous_receipt_id=previous,
        )
        receipt = persist_evidence_record(
            args["evidence_archive_dir"],
            record=arrival,
            experiment_id=outcome.experiment_id,
            artifact_kind=EvidenceArtifactKind.COHORT_CAPTURE_ARRIVAL,
            artifact_id=arrival.arrival_id,
            persisted_at=frame.processed_at,
        )
        previous = receipt.receipt_id
    seal = journal.arrival_seal.model_copy(
        update={
            "arrival_count": len(frames),
            "last_receipt_id": previous,
        }
    )
    seal_receipt = persist_evidence_record(
        args["evidence_archive_dir"],
        record=seal,
        experiment_id=outcome.experiment_id,
        artifact_kind=EvidenceArtifactKind.COHORT_CAPTURE_ARRIVAL_SEAL,
        artifact_id=seal.seal_id,
        persisted_at=seal.sealed_at,
    )
    alternate = _persist_altered_journal(
        args,
        journal,
        outcome=outcome.to_record(),
        outcome_receipt=outcome_receipt.to_record(),
        frames=[frame.to_record() for frame in frames],
        arrival_seal=seal.to_record(),
        arrival_seal_receipt=seal_receipt.to_record(),
    )
    with pytest.raises(ValueError, match="independently pinned arrival seal"):
        await _verify(args, alternate)
    assert await _verify(args, result) == journal


async def test_not_applicable_and_identical_resends_have_separate_receipts(tmp_path):
    # A known event for another token normalizes to NOT_APPLICABLE for both tokens.
    first = _book(36, token="9999")
    frames = [first, _frame(None, text=first.text, received_time=START + timedelta(seconds=37))]
    args, result = await _run(tmp_path, frames)
    arrivals = _chain(args, result.journal)
    assert len(arrivals) == 2
    assert arrivals[0].provenance.raw_sha256 == arrivals[1].provenance.raw_sha256
    assert result.journal.outcome.health_not_applicable == 4
    assert result.journal.snapshot is None
    assert await _verify(args, result) == result.journal


@pytest.mark.parametrize("schema", sorted(REQUIRED_OWNER_CAPTURE_SCHEMAS_V2))
async def test_each_consumed_or_emitted_schema_is_required_before_capture(tmp_path, schema):
    args = _prepared(tmp_path)
    args["manifest"] = args["manifest"].model_copy(
        update={
            "schema_versions": tuple(v for v in args["manifest"].schema_versions if v != schema)
        }
    )
    clock = ReplayClock(START + timedelta(seconds=35))
    with pytest.raises(ValueError, match="schema"):
        await run_cohort_capture_owner_v2(
            **args, frame_source=_Source([], clock), clock=clock, pacer=RealPacer()
        )
    assert not args["database_path"].exists()
    assert not args["raw_archive_dir"].exists()


def test_schema_inventory_covers_nested_and_dispatched_dependencies():
    assert {
        "m4_asynchronous_cohort_protocol.v2",
        "evidence_persistence_receipt.v1",
        "market_baseline_forecast.v2",
        "market_quote.v1",
        "source_provenance.v1",
        "compiled_market_contract.v1",
        "order_book_snapshot.v1",
    }.issubset(REQUIRED_OWNER_CAPTURE_SCHEMAS_V2)


@pytest.mark.parametrize("frame", [[], None])
@pytest.mark.parametrize("offset", [0, 1])
async def test_late_snapshotless_finalization_preserves_explicit_excluded_accounting(
    tmp_path, monkeypatch, frame, offset
):
    import argos.ingestion.cohort_capture_owner_v2 as owner

    args = _prepared(tmp_path)
    review = args["selection"].reviews[0]
    deadline = review.earliest_outcome_knowable_at - timedelta(
        seconds=args["protocol"].outcome_blind_margin_seconds
    )
    clock = ReplayClock(START + timedelta(seconds=35))
    original = owner.persist_cohort_capture_run_outcome

    def delay(*values, **kwargs):
        receipt = original(*values, **kwargs)
        clock.advance_to(deadline + timedelta(seconds=offset))
        return receipt

    monkeypatch.setattr(owner, "persist_cohort_capture_run_outcome", delay)
    frames = [] if frame is None else [_frame(frame, received_time=START + timedelta(seconds=36))]
    result = await run_cohort_capture_owner_v2(
        **args, frame_source=_Source(frames, clock), clock=clock, pacer=RealPacer()
    )
    assert result.journal.snapshot is None
    assert result.journal.finalization_status == "late_no_snapshot_excluded"
    assert result.journal.finalized_at >= deadline
    assert await _verify(args, result) == result.journal
    with pytest.raises(ValueError, match="explicit late/non-predictive"):
        _persist_altered_journal(args, result.journal, finalization_status="no_snapshot")


async def test_freshly_receipted_deadline_cannot_override_human_review(tmp_path):
    args, result = await _run(tmp_path, [])
    alternate = _persist_altered_journal(
        args, result.journal, blind_deadline=result.journal.blind_deadline + timedelta(hours=1)
    )
    with pytest.raises(ValueError, match="deadline differs from reviewed admission"):
        await _verify(args, alternate)


@pytest.mark.parametrize("mode", ["bytes", "time"])
async def test_excluded_boundary_is_retained_without_feeding_forecasts(tmp_path, mode):
    first = _book(36)
    boundary = _book(200 if mode == "time" else 37, bid="0.8", ask="0.9", tag="late")
    args, result = await _run(
        tmp_path,
        [first, boundary],
        **(
            {"capture_max_bytes_per_target": len(first.text.encode()) + 1}
            if mode == "bytes"
            else {}
        ),
    )
    journal = result.journal
    assert journal.outcome.boundary_payload_archived
    assert journal.outcome.frames_archived == 1
    assert journal.outcome.raw_bytes_archived == len(first.text.encode())
    assert journal.boundary.provenance == boundary.provenance
    assert not _chain(args, journal)[-1].included
    assert {f.as_of_received_time for f in journal.snapshot.forecasts} == {first.received_time}
    assert await _verify(args, result) == journal


async def test_invented_boundary_hash_with_valid_summary_receipts_is_refused(tmp_path):
    first = _book(36)
    args, result = await _run(
        tmp_path, [first, _book(37)], capture_max_bytes_per_target=len(first.text.encode()) + 1
    )
    journal = result.journal
    outcome, receipt = _outcome(
        args, journal.outcome, journal.frames, boundary_frame_sha256="b" * 64
    )
    with pytest.raises(ValueError, match="boundary differs from excluded arrival"):
        _persist_altered_journal(
            args, journal, outcome=outcome.to_record(), outcome_receipt=receipt.to_record()
        )


async def test_altered_boundary_receipt_cannot_replace_original_anchor(tmp_path):
    first = _book(36)
    args, result = await _run(
        tmp_path, [first, _book(37)], capture_max_bytes_per_target=len(first.text.encode()) + 1
    )
    boundary = result.journal.boundary
    changed = boundary.model_copy(
        update={
            "processed_at": boundary.processed_at - timedelta(seconds=1),
            "provenance": boundary.provenance.model_copy(
                update={"retrieved_at": boundary.provenance.retrieved_at - timedelta(seconds=1)}
            ),
        }
    )
    alternate = _persist_altered_journal(args, result.journal, boundary=changed.to_record())
    with pytest.raises(ValueError, match="independently pinned frame arrivals"):
        await _verify(args, alternate)


@pytest.mark.parametrize("mutation", ["missing", "corrupt"])
async def test_boundary_archive_missing_or_corrupt_is_not_verified(tmp_path, mutation):
    first = _book(36)
    args, result = await _run(
        tmp_path, [first, _book(37)], capture_max_bytes_per_target=len(first.text.encode()) + 1
    )
    boundary = result.journal.boundary
    path = (
        args["raw_archive_dir"]
        / "excluded-boundary"
        / "clob_market_ws"
        / f"{boundary.provenance.raw_sha256}.raw.json"
    )
    if mutation == "missing":
        path.unlink()  # destructive mutation of this test's synthetic fixture only
    else:
        path.write_bytes(b"[]")
    from argos.errors import ArgosError

    with pytest.raises((ArgosError, FileNotFoundError)):
        await _verify(args, result)


async def test_v2_records_cannot_be_deserialized_as_legacy(tmp_path):
    _, result = await _run(tmp_path, [])
    legacy_outcome = result.journal.outcome.to_record()
    # Actual legacy shape is tested by the V1 accounting fixtures; dispatch refuses V2.
    from argos.errors import SchemaVersionError
    from argos.evaluation.cohort_capture_outcome_v2 import CohortCaptureRunOutcomeV1

    with pytest.raises(SchemaVersionError):
        CohortCaptureRunOutcomeV1.from_record(legacy_outcome)
    with pytest.raises(SchemaVersionError):
        CohortCaptureJournalV1.from_record(result.journal.to_record())
    assert CohortCaptureJournalV2.from_record(result.journal.to_record()) == result.journal


async def test_legacy_shapes_roundtrip_without_inheriting_v2_integrity(tmp_path):
    args, result = await _run(tmp_path, [])
    outcome, receipt = _outcome(
        args, result.journal.outcome, (), record_model=CohortCaptureRunOutcomeV1
    )
    material = result.journal.to_record()
    for field in (
        "journal_id",
        "arrival_seal",
        "arrival_seal_receipt",
        "boundary",
        "blind_deadline",
        "finalization_status",
    ):
        material.pop(field)
    material.update(
        schema_version=CohortCaptureJournalV1.schema_version,
        outcome=outcome.to_record(),
        outcome_receipt=receipt.to_record(),
    )
    legacy = CohortCaptureJournalV1.from_record(
        {**material, "journal_id": f"cohort-capture-journal-{record_sha256(material)[:32]}"}
    )
    assert CohortCaptureJournalV1.from_record(legacy.to_record()) == legacy
    assert CohortCaptureRunOutcomeV1.from_record(outcome.to_record()) == outcome
    assert (
        CohortCaptureRunOutcomeV1.model_json_schema()["properties"]["boundary_payload_archived"][
            "const"
        ]
        is False
    )
    with pytest.raises(ValueError, match="legacy journal does not provide"):
        await _verify(args, CohortCaptureOwnerResult(legacy, result.receipt))


async def test_manifest_enumerates_schemas_in_actual_serialized_evidence(tmp_path):
    args, result = await _run(tmp_path, [_book(36)])
    observed = set()

    def walk(value):
        if isinstance(value, dict):
            if "schema_version" in value:
                observed.add(value["schema_version"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(result.journal.to_record())
    walk(result.receipt.to_record())
    for arrival in _chain(args, result.journal):
        walk(arrival.to_record())
    assert observed.issubset(args["manifest"].schema_versions)
