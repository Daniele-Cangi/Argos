"""Falsification cases for synthetic journals; never mutate real evidence."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_capture_loop import _frame
from test_cohort_capture_replay_v2 import _book, _prepared, _run, _Source, _verify
from test_cohort_protocol_v2 import START

from argos.clock import RealPacer, ReplayClock
from argos.evaluation.bundle import record_sha256
from argos.evaluation.cohort_capture_replay_v2 import (
    CohortCaptureFrameV1,
    CohortCaptureJournalV1,
    verify_cohort_capture_journal_v2,
)
from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    EvidencePersistenceReceiptV1,
    build_persistence_receipt_id,
    persist_evidence_record,
)
from argos.ingestion.cohort_capture_owner_v2 import (
    CohortCaptureOwnerResult,
    run_cohort_capture_owner_v2,
)
from argos.store.event_store import open_sqlite_event_store


def _rebind_receipt(receipt, **updates):
    fields = {**dict(receipt), **updates}
    fields.pop("receipt_id")
    return EvidencePersistenceReceiptV1(receipt_id=build_persistence_receipt_id(**fields), **fields)


@pytest.mark.parametrize("case", ["foreign-source", "reconstructed", "processing-before-receipt"])
def test_frame_requires_firsthand_chronological_provenance(case):
    frame = _book(36)
    fields = dict(ordinal=1, provenance=frame.provenance, processed_at=frame.received_time)
    if case == "foreign-source":
        fields["provenance"] = frame.provenance.model_copy(update={"source": "foreign"})
    elif case == "reconstructed":
        fields["provenance"] = frame.provenance.model_copy(update={"reconstructed": True})
    else:
        fields["processed_at"] -= timedelta(microseconds=1)
    with pytest.raises(ValueError, match="first-hand WS provenance before processing"):
        CohortCaptureFrameV1(**fields)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("run-id", "identify different runs"),
        ("receipt-experiment", "receipt names different evidence"),
        ("outcome-receipt-early", "outcome receipt predates completion"),
        ("finalization-early", "finalization predates durable accounting"),
        ("missing-frame", "frame/byte inventory disagrees"),
        ("byte-count", "frame/byte inventory disagrees"),
        ("ordinal", "frame order or capture interval"),
        ("late-receipt", "frame order or capture interval"),
        ("late-processing", "frame order or capture interval"),
        ("processing-regression", "frame order or capture interval"),
        ("missing-snapshot", "snapshot and receipt must be present together"),
        ("missing-snapshot-receipt", "snapshot and receipt must be present together"),
        ("snapshot-manifest", "snapshot disagrees with capture accounting"),
        ("snapshot-receipt-early", "receipt violates the outcome-blind boundary"),
        ("accounting-after-freeze", "freeze predates durable capture accounting"),
        ("self-id", "identity disagrees with its inventory"),
    ],
)
async def test_journal_rejects_inconsistent_nested_evidence(tmp_path, case, message):
    _, result = await _run(tmp_path, [_book(36), _book(37, tag="def456")])
    journal = result.journal
    fields = dict(journal)
    frames = list(journal.frames)
    if case == "run-id":
        fields["capture_run_manifest"] = journal.capture_run_manifest.model_copy(
            update={"capture_run_id": "foreign"}
        )
    elif case == "receipt-experiment":
        fields["outcome_receipt"] = _rebind_receipt(
            journal.outcome_receipt, experiment_id="foreign"
        )
    elif case == "outcome-receipt-early":
        fields["outcome_receipt"] = _rebind_receipt(
            journal.outcome_receipt, persisted_at=START + timedelta(seconds=36)
        )
    elif case == "finalization-early":
        fields["finalized_at"] -= timedelta(microseconds=1)
    elif case == "missing-frame":
        fields["frames"] = tuple(frames[:-1])
    elif case == "byte-count":
        frames[0] = frames[0].model_copy(
            update={
                "provenance": frames[0].provenance.model_copy(
                    update={"byte_length": frames[0].provenance.byte_length + 1}
                )
            }
        )
        fields["frames"] = tuple(frames)
    elif case in {"ordinal", "late-receipt", "late-processing", "processing-regression"}:
        if case == "ordinal":
            frames[0] = frames[0].model_copy(update={"ordinal": 2})
        elif case == "late-receipt":
            at = START + timedelta(seconds=38)
            frames[1] = frames[1].model_copy(
                update={
                    "provenance": frames[1].provenance.model_copy(update={"retrieved_at": at}),
                    "processed_at": at,
                }
            )
        elif case == "late-processing":
            frames[1] = frames[1].model_copy(update={"processed_at": START + timedelta(seconds=38)})
        else:
            frames[0] = frames[0].model_copy(update={"processed_at": frames[1].processed_at})
            frames[1] = frames[1].model_copy(
                update={
                    "provenance": frames[1].provenance.model_copy(
                        update={"retrieved_at": START + timedelta(seconds=36)}
                    ),
                    "processed_at": START + timedelta(seconds=36),
                }
            )
        fields["frames"] = tuple(frames)
    elif case == "missing-snapshot":
        fields["snapshot"] = None
    elif case == "missing-snapshot-receipt":
        fields["snapshot_receipt"] = None
    elif case == "snapshot-manifest":
        fields["capture_run_manifest"] = journal.capture_run_manifest.model_copy(
            update={"argos_version": "different-version"}
        )
    elif case == "snapshot-receipt-early":
        fields["snapshot_receipt"] = _rebind_receipt(
            journal.snapshot_receipt, persisted_at=START + timedelta(seconds=36)
        )
    elif case == "accounting-after-freeze":
        fields["outcome_receipt"] = _rebind_receipt(
            journal.outcome_receipt, persisted_at=START + timedelta(seconds=38)
        )
        fields["finalized_at"] = START + timedelta(seconds=39)
    else:
        fields["journal_id"] = "foreign"
    with pytest.raises(ValueError, match=message):
        CohortCaptureJournalV1.model_validate(fields)


@pytest.mark.parametrize("case", ["missing-schema", "manifest-before-admission"])
async def test_owner_refuses_incomplete_manifest_before_creating_database(tmp_path, case):
    args = _prepared(tmp_path)
    if case == "missing-schema":
        updates = {
            "schema_versions": tuple(
                version
                for version in args["manifest"].schema_versions
                if version != CohortCaptureJournalV1.schema_version
            )
        }
        message = "omits journal/accounting schemas"
    else:
        updates = {"created_at": START + timedelta(seconds=29)}
        message = "manifest/start predates durable target admission"
    args["manifest"] = args["manifest"].model_copy(update=updates)
    clock = ReplayClock(START + timedelta(seconds=35))
    with pytest.raises(ValueError, match=message):
        await run_cohort_capture_owner_v2(
            **args, frame_source=_Source([], clock), clock=clock, pacer=RealPacer()
        )
    assert not args["database_path"].exists()
    assert not args["raw_archive_dir"].exists()


def _persist_altered_journal(args, journal, **updates):
    material = journal.to_record()
    material.update(updates)
    material.pop("journal_id")
    altered = CohortCaptureJournalV1.from_record(
        {**material, "journal_id": f"cohort-capture-journal-{record_sha256(material)[:32]}"}
    )
    receipt = persist_evidence_record(
        args["evidence_archive_dir"],
        record=altered,
        experiment_id=args["protocol"].experiment_id,
        artifact_kind=EvidenceArtifactKind.COHORT_CAPTURE_JOURNAL,
        artifact_id=altered.journal_id,
        persisted_at=altered.finalized_at,
    )
    return CohortCaptureOwnerResult(altered, receipt)


async def test_hash_valid_journal_cannot_omit_available_snapshot(tmp_path):
    args, result = await _run(tmp_path, [_book(36)])
    altered = _persist_altered_journal(args, result.journal, snapshot=None, snapshot_receipt=None)
    with pytest.raises(ValueError, match="omitted an available snapshot"):
        await _verify(args, altered)


async def test_hash_valid_journal_cannot_change_frame_endpoint(tmp_path):
    args, result = await _run(tmp_path, [_book(36)])
    frame = result.journal.frames[0].to_record()
    frame["provenance"]["endpoint"] = "wss://foreign.example/ws"
    altered = _persist_altered_journal(args, result.journal, frames=[frame])
    with pytest.raises(ValueError, match="raw archive provenance"):
        await _verify(args, altered)


async def test_terminal_journal_receipt_cannot_predate_children(tmp_path):
    args, result = await _run(tmp_path, [_book(36)])
    receipt = _rebind_receipt(result.receipt, persisted_at=START + timedelta(seconds=35))
    with pytest.raises(ValueError, match="journal receipt predates its child receipts"):
        await _verify(args, CohortCaptureOwnerResult(result.journal, receipt))


@pytest.mark.parametrize("case", ["missing-run", "rejection-after-processing"])
async def test_verifier_refuses_store_metadata_corruption(tmp_path, case):
    frames = [_book(36), _frame(None, text="not json", received_time=START + timedelta(seconds=37))]
    args, result = await _run(tmp_path, frames)
    store = open_sqlite_event_store(args["database_path"])

    class CorruptReadPort:
        def __getattr__(self, name):
            return getattr(store, name)

        def get_capture_run(self, run_id):
            return None if case == "missing-run" else store.get_capture_run(run_id)

        def iter_rejections(self, run_id):
            for row in store.iter_rejections(run_id):
                yield replace(
                    row,
                    rejection=row.rejection.model_copy(
                        update={"rejected_at": START + timedelta(seconds=38)}
                    ),
                )

    try:
        with pytest.raises(
            ValueError,
            match=(
                "EventStore run is missing"
                if case == "missing-run"
                else "rejection processing time is outside"
            ),
        ):
            await verify_cohort_capture_journal_v2(
                result.journal,
                result.receipt,
                store=CorruptReadPort(),
                **{
                    key: args[key]
                    for key in (
                        "protocol",
                        "protocol_receipt",
                        "selection",
                        "selection_receipt",
                        "raw_archive_dir",
                        "evidence_archive_dir",
                    )
                },
            )
    finally:
        store.close()
