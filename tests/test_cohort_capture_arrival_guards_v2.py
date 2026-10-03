"""Corruption/failed-durability guards using disposable synthetic archives."""

from datetime import timedelta

import orjson
import pytest
from test_cohort_capture_arrival_integrity_v2 import _chain
from test_cohort_capture_journal_guards_v2 import _rebind_receipt
from test_cohort_capture_replay_v2 import _book, _run

from argos.evaluation.cohort_capture_arrivals_v2 import (
    CohortCaptureArrivalSealV1,
    CohortCaptureArrivalV1,
    _pin_receipt,
    persist_capture_arrival,
    persist_capture_arrival_seal,
)
from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    persist_evidence_record,
)


@pytest.mark.parametrize(
    "case", ["source", "reconstructed", "early", "first-linked", "second-unlinked"]
)
def test_arrival_contract_refuses_nonfirsthand_or_unlinked_record(case):
    frame = _book(36)
    fields = dict(
        capture_run_id="run",
        target_id="target",
        ordinal=1,
        included=True,
        provenance=frame.provenance,
        processed_at=frame.received_time,
    )
    if case in {"source", "reconstructed"}:
        fields["provenance"] = frame.provenance.model_copy(
            update=({"source": "foreign"} if case == "source" else {"reconstructed": True})
        )
    elif case == "early":
        fields["processed_at"] -= timedelta(microseconds=1)
    elif case == "first-linked":
        fields["previous_receipt_id"] = "fake"
    else:
        fields["ordinal"] = 2
    with pytest.raises(ValueError, match="firsthand chronology and a linked ordinal"):
        CohortCaptureArrivalV1(**fields)


@pytest.mark.parametrize("count,root", [(0, "fake"), (1, None)])
def test_seal_cannot_invent_empty_or_missing_root(count, root):
    with pytest.raises(ValueError, match="count/root disagree"):
        CohortCaptureArrivalSealV1(
            capture_run_id="run",
            target_id="target",
            arrival_count=count,
            last_receipt_id=root,
            sealed_at=_book(36).received_time,
        )


@pytest.mark.parametrize("case", ["early-arrival", "early-seal", "after-seal", "re-seal", "re-pin"])
async def test_persistence_refuses_backdating_and_replacing_pinned_chain(tmp_path, case):
    args, result = await _run(tmp_path, [_book(36)])
    journal = result.journal
    if case == "re-pin":
        with pytest.raises(FileExistsError):
            _pin_receipt(
                args["raw_archive_dir"] / "arrival-chain" / "seal.json",
                journal.arrival_seal_receipt,
            )
        return
    if case in {"early-seal", "re-seal"}:
        with pytest.raises(
            ValueError if case == "early-seal" else FileExistsError,
            match="predates seal" if case == "early-seal" else None,
        ):
            persist_capture_arrival_seal(
                journal.arrival_seal,
                experiment_id=journal.outcome.experiment_id,
                raw_archive_dir=args["raw_archive_dir"],
                evidence_archive_dir=args["evidence_archive_dir"],
                persisted_at=journal.arrival_seal.sealed_at - timedelta(seconds=1)
                if case == "early-seal"
                else journal.arrival_seal.sealed_at,
            )
    else:
        arrival = _chain(args, journal)[0]
        with pytest.raises(
            ValueError,
            match="predates processing" if case == "early-arrival" else "after its exclusive seal",
        ):
            persist_capture_arrival(
                arrival,
                experiment_id=journal.outcome.experiment_id,
                raw=_book(36).text.encode(),
                raw_archive_dir=args["raw_archive_dir"],
                evidence_archive_dir=args["evidence_archive_dir"],
                persisted_at=arrival.processed_at - timedelta(seconds=1)
                if case == "early-arrival"
                else arrival.processed_at,
            )
    assert _chain(args, journal)


async def test_failed_anchor_readback_cannot_report_durability(tmp_path, monkeypatch):
    _, result = await _run(tmp_path, [])
    path = tmp_path / "new-anchor.json"
    original = type(path).read_bytes
    monkeypatch.setattr(
        type(path), "read_bytes", lambda self: b"wrong" if self == path else original(self)
    )
    with pytest.raises(ValueError, match="anchor read-back"):
        _pin_receipt(path, result.receipt)


@pytest.mark.parametrize("case", ["gap", "extra-tail"])
async def test_terminal_anchor_inventory_refuses_gap_or_extra_tail(tmp_path, case):
    args, result = await _run(tmp_path, [_book(36)])
    directory = args["raw_archive_dir"] / "arrival-chain"
    if case == "gap":
        (directory / "00000001.json").unlink()  # test fixture only
    else:
        (directory / "00000002.json").write_bytes((directory / "00000001.json").read_bytes())
    with pytest.raises(ValueError, match="gap or extra tail"):
        _chain(args, result.journal)


@pytest.mark.parametrize(
    "case",
    [
        "run",
        "target",
        "previous-root",
        "experiment",
        "kind",
        "id",
        "early",
        "late",
        "terminal-root",
    ],
)
async def test_chain_refuses_fresh_content_matching_receipts_with_wrong_binding(tmp_path, case):
    args, result = await _run(tmp_path, [_book(36), _book(37)], end_at=_book(38).received_time)
    arrivals = _chain(args, result.journal)
    arrival = arrivals[-1]
    if case in {"run", "target", "previous-root"}:
        arrival = arrival.model_copy(
            update={
                {
                    "run": "capture_run_id",
                    "target": "target_id",
                    "previous-root": "previous_receipt_id",
                }[case]: "foreign"
            }
        )
    if case in {"early", "late", "terminal-root"}:
        # New record bytes, so this is a fresh receipt rather than idempotent
        # retrieval of the original receipt's first persistence timestamp.
        arrival = arrival.model_copy(
            update={"processed_at": arrival.processed_at + timedelta(seconds=0.25)}
        )
    metadata = dict(
        experiment_id=result.journal.outcome.experiment_id,
        artifact_kind=EvidenceArtifactKind.COHORT_CAPTURE_ARRIVAL,
        artifact_id=arrival.arrival_id,
        persisted_at=arrival.processed_at,
    )
    if case in {"experiment", "kind", "id"}:
        key = {"experiment": "experiment_id", "kind": "artifact_kind", "id": "artifact_id"}[case]
        metadata[key] = EvidenceArtifactKind.EXPERIMENT_PROTOCOL if case == "kind" else "foreign"
    if case in {"early", "late", "terminal-root"}:
        metadata["persisted_at"] += timedelta(
            seconds=-1 if case == "early" else 2 if case == "late" else 0.5
        )
    receipt = persist_evidence_record(args["evidence_archive_dir"], record=arrival, **metadata)
    (args["raw_archive_dir"] / "arrival-chain" / "00000002.json").write_bytes(
        orjson.dumps(receipt.to_record())
    )
    with pytest.raises(
        ValueError,
        match="terminate the original"
        if case == "terminal-root"
        else "identity, order or chronology",
    ):
        _chain(args, result.journal)


@pytest.mark.parametrize(
    "case", ["reconstructed", "source", "endpoint", "late-first", "wrong-bytes"]
)
async def test_chain_refuses_raw_provenance_not_just_matching_hash(tmp_path, monkeypatch, case):
    import argos.evaluation.cohort_capture_arrivals_v2 as module

    args, result = await _run(tmp_path, [_book(36)])
    original = module.read_raw_payload

    def corrupt(*values):
        raw, provenance = original(*values)
        changes = {
            "reconstructed": {"reconstructed": True},
            "source": {"source": "foreign"},
            "endpoint": {"endpoint": "wss://foreign.example"},
            "late-first": {"retrieved_at": provenance.retrieved_at + timedelta(seconds=1)},
            "wrong-bytes": {},
        }[case]
        return (b"[]" if case == "wrong-bytes" else raw), provenance.model_copy(update=changes)

    monkeypatch.setattr(module, "read_raw_payload", corrupt)
    with pytest.raises(ValueError, match="arrival bytes/provenance"):
        _chain(args, result.journal)


@pytest.mark.parametrize("case", ["wrong-record", "early-receipt"])
async def test_seal_archive_identity_and_chronology_are_checked(tmp_path, monkeypatch, case):
    import argos.evaluation.cohort_capture_arrivals_v2 as module

    args, result = await _run(tmp_path, [])
    journal = result.journal
    if case == "wrong-record":
        original = module.load_persisted_record
        monkeypatch.setattr(
            module,
            "load_persisted_record",
            lambda *values: (
                journal.arrival_seal.model_copy(update={"capture_run_id": "foreign"})
                if values[-1] is CohortCaptureArrivalSealV1
                else original(*values)
            ),
        )
        message = "archived arrival seal differs"
    else:
        receipt = _rebind_receipt(
            journal.arrival_seal_receipt,
            persisted_at=journal.arrival_seal.sealed_at - timedelta(seconds=1),
        )
        # Match the supplied/pinned receipt so this specifically exercises chronology.
        (args["raw_archive_dir"] / "arrival-chain" / "seal.json").write_bytes(
            orjson.dumps(receipt.to_record())
        )
        journal = type(journal).model_construct(
            **{**dict(journal), "arrival_seal_receipt": receipt}
        )
        monkeypatch.setattr(module, "load_persisted_record", lambda *values: journal.arrival_seal)
        message = "seal receipt predates seal"
    with pytest.raises(ValueError, match=message):
        _chain(args, journal)
