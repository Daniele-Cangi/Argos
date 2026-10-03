"""Real ingestion/store/raw replay, entirely synthetic and without network I/O."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_capture_loop import _frame
from test_cohort_protocol_v2 import START, _protocol
from test_cohort_selection_v2 import _entry, _persist_selection, _prepare
from test_ws_book import _minimal_event

from argos.baselines import BaselineMethod, build_baseline_forecast_v2
from argos.clock import RealPacer, ReplayClock
from argos.config.manifest import RunManifest, RunMode, WorkingTreeStatus
from argos.domain.lasttrade import LastTradePriceV1
from argos.domain.observation import ObservationEnvelopeV1, RejectedObservationV1
from argos.domain.pricechange import PriceChangeV1
from argos.domain.wsbook import WsBookSnapshotV1
from argos.errors import ImmutabilityViolationError
from argos.evaluation.bundle import record_sha256
from argos.evaluation.cohort_capture_outcome_v2 import CohortCaptureRunOutcomeV1
from argos.evaluation.cohort_capture_replay_v2 import (
    REQUIRED_OWNER_CAPTURE_SCHEMAS_V2,
    CohortCaptureFrameV1,
    CohortCaptureJournalV1,
    CohortCaptureJournalV2,
    verify_cohort_capture_journal_v2,
)
from argos.evaluation.cohort_selection_v2 import select_block_candidates_v2
from argos.evaluation.cohort_snapshot_v2 import (
    CohortCaptureCloseV1,
    CohortFrozenForecastSnapshotV1,
    build_cohort_frozen_forecast_snapshot_id,
)
from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    build_target_id,
    persist_evidence_record,
)
from argos.ingestion.cohort_capture_owner_v2 import (
    CohortCaptureOwnerResult,
    run_cohort_capture_owner_v2,
)
from argos.ingestion.cohort_capture_v2 import BoundedCaptureStopReasonV2
from argos.sources.clob_ws import MarketFrame
from argos.store.event_store import CompletionStatus, open_sqlite_event_store


def _prepared(tmp_path: Path, **protocol_updates):
    archive = tmp_path / "evidence"
    protocol = _protocol(**protocol_updates)
    protocol_receipt = persist_evidence_record(
        archive,
        record=protocol,
        experiment_id=protocol.experiment_id,
        artifact_kind=EvidenceArtifactKind.EXPERIMENT_PROTOCOL,
        artifact_id=protocol.experiment_id,
        persisted_at=protocol.declared_at,
    )
    raw, source, reviews, receipts, attempts, attempt_receipts, books = _prepare(
        archive,
        [_entry(801)],
        approved_ids=("801",),
    )
    selection = select_block_candidates_v2(
        protocol,
        block_ordinal=1,
        selected_at=START + timedelta(seconds=30),
        source_payload_bytes=raw,
        source_provenance=source,
        reviews=reviews,
        review_receipts=receipts,
        book_attempts=attempts,
        book_attempt_receipts=attempt_receipts,
        book_payload_bytes=books,
    )
    selection_receipt = _persist_selection(archive, selection)
    market = selection.page_decisions[0].market
    assert market is not None
    target_id = build_target_id(
        experiment_id=protocol.experiment_id,
        market_id=market.market_id,
        condition_id=market.condition_id,
        yes_token_id=market.token_id_for("Yes"),
        no_token_id=market.token_id_for("No"),
    )
    manifest = RunManifest(
        run_id="synthetic-owner-801",
        mode=RunMode.CAPTURE,
        created_at=START + timedelta(seconds=35),
        argos_version="synthetic",
        code_revision=protocol.code_revision,
        working_tree=WorkingTreeStatus.CLEAN,
        capture_run_id="synthetic-owner-801",
        config_fingerprint=protocol.config_fingerprint,
        settings_snapshot={},
        run_parameters={
            "target_id": target_id,
            "subscribed_token_ids": (market.token_id_for("Yes"), market.token_id_for("No")),
            "max_seconds": protocol.capture_max_seconds_per_target,
            "max_frames": protocol.capture_max_frames_per_target,
            "max_bytes": protocol.capture_max_bytes_per_target,
            "raw_archive": True,
            "separate_database_per_target": True,
            "target_database_id": "capture.sqlite",
        },
        schema_versions=(
            *REQUIRED_OWNER_CAPTURE_SCHEMAS_V2,
            ObservationEnvelopeV1.schema_version,
            RejectedObservationV1.schema_version,
            PriceChangeV1.schema_version,
            CohortCaptureCloseV1.schema_version,
            CohortFrozenForecastSnapshotV1.schema_version,
            CohortCaptureFrameV1.schema_version,
            CohortCaptureJournalV1.schema_version,
            CohortCaptureRunOutcomeV1.schema_version,
            LastTradePriceV1.schema_version,
            WsBookSnapshotV1.schema_version,
        ),
    )
    return dict(
        protocol=protocol,
        protocol_receipt=protocol_receipt,
        selection=selection,
        selection_receipt=selection_receipt,
        entry_index=0,
        manifest=manifest,
        database_path=tmp_path / "capture.sqlite",
        raw_archive_dir=tmp_path / "raw",
        evidence_archive_dir=archive,
    )


def _book(at: int, *, token="1602", bid="0.4", ask="0.6", tag="abc123"):
    return _frame(
        _minimal_event(
            market="0x" + f"{801:064x}",
            asset_id=token,
            timestamp=str(int((START + timedelta(seconds=at)).timestamp() * 1000)),
            hash=tag,
            bids=[{"price": bid, "size": "10"}],
            asks=[{"price": ask, "size": "10"}],
        ),
        received_time=START + timedelta(seconds=at),
    )


class _Source:
    def __init__(self, frames, clock, *, end_at=None, failure=False):
        self.items = frames
        self.clock = clock
        self.end_at = end_at
        self.failure = failure

    async def frames(self) -> AsyncIterator[MarketFrame]:
        for frame in self.items:
            self.clock.advance_to(frame.received_time)
            yield frame
        if self.end_at is not None:
            self.clock.advance_to(self.end_at)
        if self.failure:
            raise RuntimeError("synthetic transport failed")


async def _run(tmp_path: Path, frames, *, end_at=None, failure=False, **updates):
    args = _prepared(tmp_path, **updates)
    clock = ReplayClock(START + timedelta(seconds=35))
    result = await run_cohort_capture_owner_v2(
        **args,
        frame_source=_Source(frames, clock, end_at=end_at, failure=failure),
        clock=clock,
        pacer=RealPacer(),
    )
    return args, result


async def _verify(args, result):
    store = open_sqlite_event_store(args["database_path"])
    try:
        return await verify_cohort_capture_journal_v2(
            result.journal,
            result.receipt,
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
            store=store,
        )
    finally:
        store.close()


async def test_owner_freezes_last_yes_state_not_last_frame_and_replays_all_baselines(tmp_path):
    first = _book(36)
    frames = [
        first,
        _frame(None, text=first.text, received_time=START + timedelta(seconds=37)),
        _frame([], received_time=START + timedelta(seconds=38)),
        _frame(None, text="not json", received_time=START + timedelta(seconds=39)),
        _book(40, bid="0.5", ask="0.6", tag="def456"),
        _book(41, token="1603"),
    ]
    args, result = await _run(tmp_path, frames)
    journal = result.journal
    assert CohortCaptureJournalV2.from_record(journal.to_record()) == journal
    assert await _verify(args, result) == journal
    assert journal.outcome.frames_archived == 6
    assert journal.outcome.health_accepted == 3
    assert journal.outcome.health_duplicate == 1
    assert journal.outcome.health_decode_failures == journal.outcome.health_rejected == 1
    assert journal.snapshot is not None
    assert {f.as_of_ingest_sequence for f in journal.snapshot.forecasts} == {4}
    assert {f.as_of_received_time for f in journal.snapshot.forecasts} == {
        START + timedelta(seconds=40)
    }
    methods = {f.method: f for f in journal.snapshot.forecasts}
    assert methods[BaselineMethod.MIDPOINT].raw_score == Decimal("0.55")
    assert methods[BaselineMethod.PERSISTENCE].raw_score == Decimal("0.5")
    assert methods[BaselineMethod.LAST_TRADE].abstained
    assert all(f.p_yes is None for f in journal.snapshot.forecasts)
    assert journal.snapshot_receipt.persisted_at >= journal.outcome.finished_at


async def test_empty_capture_retains_outcome_without_inventing_snapshot(tmp_path):
    args, result = await _run(tmp_path, [])
    assert result.journal.frames == ()
    assert result.journal.snapshot is None
    assert await _verify(args, result) == result.journal


async def test_frame_cap_does_not_read_or_journal_a_boundary_payload(tmp_path):
    _, result = await _run(
        tmp_path, [_book(36), _book(37), _book(38)], capture_max_frames_per_target=2
    )
    assert result.journal.outcome.stop_reason is BoundedCaptureStopReasonV2.FRAME_CAP
    assert len(result.journal.frames) == 2
    assert result.journal.outcome.boundary_frame_sha256 is None


async def test_byte_cap_archives_excluded_boundary_separately(tmp_path):
    first, boundary = _book(36), _book(37)
    args, result = await _run(
        tmp_path, [first, boundary], capture_max_bytes_per_target=len(first.text.encode()) + 1
    )
    outcome = result.journal.outcome
    assert outcome.stop_reason is BoundedCaptureStopReasonV2.BYTE_CAP
    assert outcome.boundary_frame_sha256 == boundary.provenance.raw_sha256
    assert outcome.raw_bytes_archived == len(first.text.encode())
    assert not (
        args["raw_archive_dir"] / "clob_market_ws" / f"{boundary.provenance.raw_sha256}.raw.json"
    ).exists()
    assert (
        args["raw_archive_dir"]
        / "excluded-boundary"
        / "clob_market_ws"
        / f"{boundary.provenance.raw_sha256}.raw.json"
    ).read_bytes() == boundary.text.encode()
    assert await _verify(args, result) == result.journal


async def test_duration_cap_keeps_real_post_close_finalization_time(tmp_path):
    args, result = await _run(tmp_path, [_book(36), _book(200)])
    outcome = result.journal.outcome
    assert outcome.stop_reason is BoundedCaptureStopReasonV2.DURATION_CAP
    assert outcome.finished_at == START + timedelta(seconds=155)
    assert result.journal.snapshot.frozen_at == START + timedelta(seconds=200)
    assert await _verify(args, result) == result.journal


async def test_one_sided_state_freezes_four_explicit_abstentions(tmp_path):
    first = _book(36)
    import orjson

    payload = orjson.loads(first.text)
    payload["asks"] = []
    args, result = await _run(tmp_path, [_frame(payload, received_time=first.received_time)])
    assert len(result.journal.snapshot.forecasts) == 4
    assert all(f.abstained for f in result.journal.snapshot.forecasts)
    assert await _verify(args, result) == result.journal


async def test_standalone_trade_is_part_of_replayed_final_information_state(tmp_path):
    from test_last_trade import _event

    event = _event()
    event.update(
        market="0x" + f"{801:064x}",
        asset_id="1602",
        price="0.52",
        timestamp=str(int((START + timedelta(seconds=37)).timestamp() * 1000)),
    )
    args, result = await _run(
        tmp_path, [_book(36), _frame(event, received_time=START + timedelta(seconds=37))]
    )
    methods = {f.method: f for f in result.journal.snapshot.forecasts}
    assert methods[BaselineMethod.LAST_TRADE].raw_score == Decimal("0.52")
    assert methods[BaselineMethod.DISPLAYED_PRICE].raw_score == Decimal("0.52")
    assert methods[BaselineMethod.MIDPOINT].raw_score == Decimal("0.5")
    assert methods[BaselineMethod.PERSISTENCE].raw_score == Decimal("0.5")
    assert {f.as_of_ingest_sequence for f in methods.values()} == {2}
    assert await _verify(args, result) == result.journal


async def test_unseeded_capture_is_not_converted_into_available_forecast(tmp_path):
    args, result = await _run(tmp_path, [_frame([], received_time=START + timedelta(seconds=36))])
    assert result.journal.outcome.frames_archived == 1
    assert result.journal.outcome.health_accepted == 0
    assert result.journal.snapshot is None
    assert await _verify(args, result) == result.journal


async def test_capture_refuses_missing_blind_finalization_reserve_before_acquisition(tmp_path):
    args = _prepared(tmp_path)
    review = args["selection"].reviews[0]
    clock = ReplayClock(review.earliest_outcome_knowable_at - timedelta(seconds=1))
    with pytest.raises(ValueError, match="reserve finalization"):
        await run_cohort_capture_owner_v2(
            **args, frame_source=_Source([], clock), clock=clock, pacer=RealPacer()
        )
    assert not args["database_path"].exists()


async def test_actual_late_freeze_is_refused_without_backdating(tmp_path, monkeypatch):
    args = _prepared(tmp_path)
    review = args["selection"].reviews[0]
    deadline = review.earliest_outcome_knowable_at - timedelta(
        seconds=args["protocol"].outcome_blind_margin_seconds
    )
    clock = ReplayClock(START + timedelta(seconds=35))
    import argos.ingestion.cohort_capture_owner_v2 as owner

    original_persist = owner.persist_cohort_capture_run_outcome

    def delayed_finalization(*args, **kwargs):
        receipt = original_persist(*args, **kwargs)
        clock.advance_to(deadline)
        return receipt

    monkeypatch.setattr(owner, "persist_cohort_capture_run_outcome", delayed_finalization)
    with pytest.raises(ValueError, match="outcome-blind margin"):
        await run_cohort_capture_owner_v2(
            **args,
            frame_source=_Source([_book(36)], clock),
            clock=clock,
            pacer=RealPacer(),
        )
    assert not list((args["evidence_archive_dir"] / "cohort_capture_journal").glob("*.raw.json"))


async def test_slow_snapshot_write_cannot_cross_blind_boundary_silently(tmp_path, monkeypatch):
    import argos.ingestion.cohort_capture_owner_v2 as owner

    args = _prepared(tmp_path)
    review = args["selection"].reviews[0]
    deadline = review.earliest_outcome_knowable_at - timedelta(
        seconds=args["protocol"].outcome_blind_margin_seconds
    )
    clock = ReplayClock(START + timedelta(seconds=35))
    original_persist = owner.persist_evidence_record

    def slow_write(*values, **kwargs):
        receipt = original_persist(*values, **kwargs)
        if kwargs["artifact_kind"] is EvidenceArtifactKind.FROZEN_FORECAST_SNAPSHOT:
            clock.advance_to(deadline)
        return receipt

    monkeypatch.setattr(owner, "persist_evidence_record", slow_write)
    with pytest.raises(ValueError, match="durability confirmation"):
        await run_cohort_capture_owner_v2(
            **args, frame_source=_Source([_book(36)], clock), clock=clock, pacer=RealPacer()
        )
    assert not list((args["evidence_archive_dir"] / "cohort_capture_journal").glob("*.raw.json"))


async def test_no_capture_after_declared_acquisition_block(tmp_path):
    args = _prepared(tmp_path)
    clock = ReplayClock(args["protocol"].blocks[0].end)
    with pytest.raises(ValueError, match="declared block"):
        await run_cohort_capture_owner_v2(
            **args, frame_source=_Source([], clock), clock=clock, pacer=RealPacer()
        )
    assert not args["database_path"].exists()


async def test_capture_refuses_predecessor_raw_archive(tmp_path):
    args = _prepared(tmp_path)
    args["raw_archive_dir"].mkdir()
    clock = ReplayClock(START + timedelta(seconds=35))
    with pytest.raises(FileExistsError):
        await run_cohort_capture_owner_v2(
            **args, frame_source=_Source([], clock), clock=clock, pacer=RealPacer()
        )
    assert not args["database_path"].exists()
    assert args["raw_archive_dir"].is_dir()


@pytest.mark.parametrize("failure", ["directory", "file", "permission"])
async def test_raw_setup_failure_removes_only_new_empty_database(tmp_path, monkeypatch, failure):
    args = _prepared(tmp_path)
    raw = args["raw_archive_dir"]
    if failure == "directory":
        raw.mkdir()
        sentinel = raw / "predecessor.txt"
        sentinel.write_bytes(b"synthetic predecessor; never remove")
    elif failure == "file":
        raw.write_bytes(b"synthetic predecessor file; never remove")
    original_mkdir = Path.mkdir

    def refuse_raw(path, *values, **kwargs):
        if path == raw:
            assert args["database_path"].stat().st_size == 0
            raise PermissionError("synthetic raw setup denial")
        return original_mkdir(path, *values, **kwargs)

    class ForbiddenSource:
        async def frames(self):
            raise AssertionError("setup failure must not consume a source frame")
            yield

    with monkeypatch.context() as patch:
        if failure == "permission":
            patch.setattr(Path, "mkdir", refuse_raw)
        clock = ReplayClock(START + timedelta(seconds=35))
        with pytest.raises(OSError):
            await run_cohort_capture_owner_v2(
                **args, frame_source=ForbiddenSource(), clock=clock, pacer=RealPacer()
            )
    assert not args["database_path"].exists()
    if failure == "directory":
        assert sentinel.read_bytes() == b"synthetic predecessor; never remove"
    elif failure == "file":
        assert raw.read_bytes() == b"synthetic predecessor file; never remove"
    else:
        assert not raw.exists()
        # A caller may try corrected setup because no capture ever began.
        clock = ReplayClock(START + timedelta(seconds=35))
        result = await run_cohort_capture_owner_v2(
            **args, frame_source=_Source([], clock), clock=clock, pacer=RealPacer()
        )
        assert await _verify(args, result) == result.journal


@pytest.mark.parametrize("change", ["written", "replaced", "removed"])
async def test_raw_setup_failure_does_not_remove_changed_database(tmp_path, monkeypatch, change):
    args = _prepared(tmp_path)
    original_mkdir = Path.mkdir
    replacement = tmp_path / "synthetic-replacement.sqlite"
    replacement.write_bytes(b"")
    replacement_identity = replacement.stat().st_ino

    def refuse_raw(path, *values, **kwargs):
        if path == args["raw_archive_dir"]:
            if change == "written":
                args["database_path"].write_bytes(b"synthetic concurrent data; preserve")
            elif change == "replaced":
                replacement.replace(args["database_path"])
            else:
                args["database_path"].unlink()  # only this synthetic empty reservation
            raise PermissionError("synthetic raw setup denial")
        return original_mkdir(path, *values, **kwargs)

    monkeypatch.setattr(Path, "mkdir", refuse_raw)
    clock = ReplayClock(START + timedelta(seconds=35))
    with pytest.raises(PermissionError, match="synthetic raw setup denial"):
        await run_cohort_capture_owner_v2(
            **args, frame_source=_Source([], clock), clock=clock, pacer=RealPacer()
        )
    if change == "written":
        assert args["database_path"].read_bytes() == b"synthetic concurrent data; preserve"
    elif change == "replaced":
        assert args["database_path"].stat().st_ino == replacement_identity
        assert args["database_path"].read_bytes() == b""
    else:
        assert not args["database_path"].exists()


async def test_existing_database_is_preserved_before_raw_setup(tmp_path):
    args = _prepared(tmp_path)
    args["database_path"].write_bytes(b"synthetic predecessor database; preserve")
    clock = ReplayClock(START + timedelta(seconds=35))
    with pytest.raises(FileExistsError):
        await run_cohort_capture_owner_v2(
            **args, frame_source=_Source([], clock), clock=clock, pacer=RealPacer()
        )
    assert args["database_path"].read_bytes() == b"synthetic predecessor database; preserve"
    assert not args["raw_archive_dir"].exists()


async def test_owner_refuses_reusing_database_without_consuming_source(tmp_path):
    args, _ = await _run(tmp_path, [_book(36)])

    class ForbiddenSource:
        async def frames(self):
            raise AssertionError("must not consume a restarted capture")
            yield

    with pytest.raises(FileExistsError):
        await run_cohort_capture_owner_v2(
            **args,
            frame_source=ForbiddenSource(),
            clock=ReplayClock(START + timedelta(seconds=35)),
            pacer=RealPacer(),
        )


async def test_source_failure_keeps_failed_run_and_never_writes_terminal_journal(tmp_path):
    with pytest.raises(RuntimeError, match="transport failed"):
        await _run(tmp_path, [_book(36)], failure=True)
    store = open_sqlite_event_store(tmp_path / "capture.sqlite")
    try:
        assert (
            store.get_capture_run("synthetic-owner-801").completion_status
            is CompletionStatus.FAILED
        )
    finally:
        store.close()
    assert not list((tmp_path / "evidence" / "cohort_capture_journal").glob("*.raw.json"))


async def test_corrupt_raw_bytes_are_refused_by_independent_replay(tmp_path):
    args, result = await _run(tmp_path, [_book(36)])
    digest = result.journal.frames[0].provenance.raw_sha256
    (args["raw_archive_dir"] / "clob_market_ws" / f"{digest}.raw.json").write_bytes(b"[]")
    with pytest.raises(ImmutabilityViolationError):
        await _verify(args, result)


async def test_extra_store_delivery_cannot_pass_as_original_capture(tmp_path):
    args, result = await _run(tmp_path, [_book(36)])
    store = open_sqlite_event_store(args["database_path"])
    try:
        delivery = next(store.iter_deliveries("synthetic-owner-801"))
        envelope = store.get_observation(delivery.observation_id)

        # Store refuses append after close; corruption is simulated through a
        # read-port wrapper, not by weakening the production store invariant.
        class ExtraDelivery:
            def __getattr__(self, name):
                return getattr(store, name)

            def iter_deliveries(self, run_id):
                from dataclasses import replace

                yield from store.iter_deliveries(run_id)
                yield replace(delivery, ingest_sequence=2)

        assert envelope is not None
        with pytest.raises(ValueError, match="raw replay disagrees"):
            await verify_cohort_capture_journal_v2(
                result.journal,
                result.receipt,
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
                store=ExtraDelivery(),
            )
    finally:
        store.close()


async def test_valid_receipts_do_not_mask_wrong_persistence_baseline(tmp_path):
    args, result = await _run(tmp_path, [_book(36), _book(37, bid="0.5", tag="def456")])
    snapshot = result.journal.snapshot
    forecasts = tuple(
        build_baseline_forecast_v2(
            method=f.method,
            quote=f.quote,
            as_of_received_time=f.as_of_received_time,
            as_of_ingest_sequence=f.as_of_ingest_sequence,
            previous_score=Decimal("0.2"),
            evaluation_run_id=f.evaluation_run_id,
            source_capture_run_id=f.source_capture_run_id,
            source_observation_id=f.source_observation_id,
            information_state_hash=f.information_state_hash,
            market_id=f.market_id,
            contract_id=f.contract_id,
        )
        for f in snapshot.forecasts
    )
    fields = {**dict(snapshot), "forecasts": forecasts}
    fields.pop("snapshot_id")
    changed = CohortFrozenForecastSnapshotV1(
        snapshot_id=build_cohort_frozen_forecast_snapshot_id(**fields), **fields
    )
    snapshot_receipt = persist_evidence_record(
        args["evidence_archive_dir"],
        record=changed,
        experiment_id=args["protocol"].experiment_id,
        artifact_kind=EvidenceArtifactKind.FROZEN_FORECAST_SNAPSHOT,
        artifact_id=changed.snapshot_id,
        persisted_at=snapshot.frozen_at,
    )
    material = result.journal.to_record()
    material.update(snapshot=changed.to_record(), snapshot_receipt=snapshot_receipt.to_record())
    material.pop("journal_id")
    material["journal_id"] = f"cohort-capture-journal-{record_sha256(material)[:32]}"
    journal = CohortCaptureJournalV2.from_record(material)
    receipt = persist_evidence_record(
        args["evidence_archive_dir"],
        record=journal,
        experiment_id=args["protocol"].experiment_id,
        artifact_kind=EvidenceArtifactKind.COHORT_CAPTURE_JOURNAL,
        artifact_id=journal.journal_id,
        persisted_at=snapshot.frozen_at,
    )
    with pytest.raises(ValueError, match="replayed baseline forecasts"):
        await _verify(args, CohortCaptureOwnerResult(journal, receipt))
