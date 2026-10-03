"""Separate source book time from the triggering event, never migrate old records."""

from datetime import timedelta, timezone
from decimal import Decimal

import orjson
import pytest
from test_baselines import NOW, _state
from test_capture_loop import _frame
from test_cohort_capture_journal_guards_v2 import _persist_altered_journal
from test_cohort_capture_replay_v2 import _book, _run, _verify
from test_cohort_protocol_v2 import START
from test_cohort_snapshot_v2 import _synthetic_snapshot
from test_last_trade import _event

from argos.baselines import (
    BaselineMethod,
    MarketBaselineForecastV2,
    MarketBaselineForecastV3,
    build_baseline_forecast_v2,
    build_baseline_forecast_v3,
    quote_from_book_state,
)
from argos.errors import NaiveDatetimeError, SchemaVersionError
from argos.evaluation.bundle import record_sha256
from argos.evaluation.cohort_capture_outcome_v2 import (
    CohortCaptureRunOutcomeV1,
    build_cohort_capture_run_outcome_id,
    persist_cohort_capture_run_outcome,
)
from argos.evaluation.cohort_capture_replay_v2 import (
    CohortCaptureJournalV1,
    CohortCaptureJournalV2,
)
from argos.evaluation.cohort_snapshot_v2 import (
    CohortCaptureCloseV1,
    CohortFrozenForecastSnapshotV1,
    CohortFrozenForecastSnapshotV2,
    build_cohort_capture_close_id,
    build_cohort_frozen_forecast_snapshot_id,
)
from argos.evaluation.prospective import EvidenceArtifactKind, persist_evidence_record
from argos.ingestion.cohort_capture_owner_v2 import CohortCaptureOwnerResult


def _forecast_args():
    return dict(
        method=BaselineMethod.MIDPOINT,
        quote=quote_from_book_state(
            _state(bids=[("0.4", "10")], asks=[("0.6", "10")]),
            quote_time=NOW,
        ),
        as_of_received_time=NOW + timedelta(seconds=2),
        as_of_ingest_sequence=2,
        previous_score=None,
        evaluation_run_id="synthetic-evaluation",
        source_capture_run_id="synthetic-capture",
        source_observation_id="synthetic-trigger",
        information_state_hash="a" * 64,
        market_id="synthetic-market",
        contract_id="synthetic-contract",
    )


@pytest.mark.parametrize("trigger", [None, NOW - timedelta(seconds=1), NOW + timedelta(seconds=1)])
def test_v3_preserves_separate_clocks_identity_and_roundtrip(trigger):
    args = _forecast_args()
    legacy = build_baseline_forecast_v2(**args)
    current = build_baseline_forecast_v3(**args, trigger_event_time=trigger)
    assert current.quote.quote_time == current.as_of_event_time == NOW
    assert current.trigger_event_time == trigger
    assert current.raw_score == legacy.raw_score == Decimal("0.5")
    assert current.forecast_id != legacy.forecast_id
    assert current == build_baseline_forecast_v3(**args, trigger_event_time=trigger)
    assert MarketBaselineForecastV3.from_record(current.to_record()) == current
    assert MarketBaselineForecastV2.from_record(legacy.to_record()) == legacy
    assert "trigger_event_time" not in legacy.to_record()
    with pytest.raises(SchemaVersionError):
        MarketBaselineForecastV2.from_record(current.to_record())
    with pytest.raises(SchemaVersionError):
        MarketBaselineForecastV3.from_record(legacy.to_record())


def test_v3_requires_explicit_trigger_and_binds_identity_to_clocks():
    args = _forecast_args()
    current = build_baseline_forecast_v3(**args, trigger_event_time=NOW)
    offset = NOW.astimezone(timezone(timedelta(hours=2)))
    assert build_baseline_forecast_v3(**args, trigger_event_time=offset) == current
    changed = build_baseline_forecast_v3(**args, trigger_event_time=None)
    assert changed.forecast_id != current.forecast_id
    with pytest.raises(NaiveDatetimeError, match="timezone"):
        build_baseline_forecast_v3(**args, trigger_event_time=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="book time must equal"):
        current.model_copy(update={"as_of_event_time": NOW - timedelta(seconds=1)})
    with pytest.raises(ValueError, match="identity disagrees"):
        current.model_copy(update={"trigger_event_time": None})
    record = current.to_record()
    record.pop("trigger_event_time")
    with pytest.raises(ValueError, match="trigger_event_time"):
        MarketBaselineForecastV3.from_record(record)


@pytest.mark.parametrize("event_second", [None, 35])
async def test_undatable_or_older_trade_does_not_retime_book(tmp_path, event_second):
    event = _event()
    event.update(market="0x" + f"{801:064x}", asset_id="1602", price="0.52")
    event["timestamp"] = (
        None
        if event_second is None
        else str(int((START + timedelta(seconds=event_second)).timestamp() * 1000))
    )
    args, result = await _run(
        tmp_path,
        [_book(36), _frame(event, received_time=START + timedelta(seconds=37))],
    )
    forecasts = result.journal.snapshot.forecasts
    assert {f.as_of_event_time for f in forecasts} == {START + timedelta(seconds=36)}
    assert {f.trigger_event_time for f in forecasts} == {
        None if event_second is None else START + timedelta(seconds=event_second)
    }
    assert {f.as_of_received_time for f in forecasts} == {START + timedelta(seconds=37)}
    assert {f.as_of_ingest_sequence for f in forecasts} == {2}
    assert await _verify(args, result) == result.journal


async def test_undatable_book_does_not_borrow_prior_book_time(tmp_path):
    frame = _book(37, bid="0.5", tag="undatable")
    event = orjson.loads(frame.text)
    event["timestamp"] = None
    args, result = await _run(
        tmp_path,
        [_book(36), _frame(event, received_time=frame.received_time)],
    )
    assert {f.quote.quote_time for f in result.journal.snapshot.forecasts} == {None}
    assert {f.trigger_event_time for f in result.journal.snapshot.forecasts} == {None}
    assert await _verify(args, result) == result.journal


@pytest.mark.parametrize("event_second", [None, 35, 39])
async def test_unchanged_book_retimes_without_advancing_persistence(tmp_path, event_second):
    # Same levels AND provider hash; only the source clock changes.
    frame = _book(38, bid="0.4", ask="0.8")
    event = orjson.loads(frame.text)
    event["timestamp"] = (
        None
        if event_second is None
        else str(int((START + timedelta(seconds=event_second)).timestamp() * 1000))
    )
    args, result = await _run(
        tmp_path,
        [
            _book(36, bid="0.2", ask="0.6"),
            _book(37, bid="0.4", ask="0.8"),
            _frame(event, received_time=frame.received_time),
        ],
    )
    forecasts = result.journal.snapshot.forecasts
    book_time = None if event_second is None else START + timedelta(seconds=event_second)
    assert {f.quote.quote_time for f in forecasts} == {book_time}
    assert {f.as_of_event_time for f in forecasts} == {book_time}
    assert {f.trigger_event_time for f in forecasts} == {book_time}
    assert {f.as_of_received_time for f in forecasts} == {frame.received_time}
    assert {f.as_of_ingest_sequence for f in forecasts} == {3}
    persistence = next(f for f in forecasts if f.method is BaselineMethod.PERSISTENCE)
    assert persistence.raw_score == Decimal("0.4")
    assert await _verify(args, result) == result.journal


async def test_legacy_snapshot_refuses_current_forecasts_on_direct_construction(tmp_path):
    _, result = await _run(tmp_path, [_book(36)])
    snapshot = result.journal.snapshot
    with pytest.raises(ValueError, match="forecast runtime type"):
        CohortFrozenForecastSnapshotV1(**dict(snapshot))
    assert CohortFrozenForecastSnapshotV2.from_record(snapshot.to_record()) == snapshot


@pytest.mark.parametrize("journal_model", [CohortCaptureJournalV1, CohortCaptureJournalV2])
async def test_legacy_journal_refuses_current_snapshot_with_fresh_identity(tmp_path, journal_model):
    args, result = await _run(tmp_path, [_book(36)])
    fields = dict(result.journal)
    if journal_model is CohortCaptureJournalV1:
        outcome = result.journal.outcome
        identity = {
            name: getattr(outcome, name)
            for name in (
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
        fields["outcome"] = CohortCaptureRunOutcomeV1(
            **{
                **dict(outcome),
                "outcome_id": build_cohort_capture_run_outcome_id(
                    **identity,
                    health=outcome.health,
                    schema_version=CohortCaptureRunOutcomeV1.schema_version,
                ),
            },
        )
        fields["outcome_receipt"] = persist_cohort_capture_run_outcome(
            args["evidence_archive_dir"],
            outcome=fields["outcome"],
            persisted_at=result.journal.outcome_receipt.persisted_at,
        )
    fields = {name: value for name, value in fields.items() if name in journal_model.model_fields}
    # Bind the actual nested schema to a fresh identity, not a stale digest.
    material = result.journal.to_record()
    material = {name: material[name] for name in fields}
    material.update(
        schema_version=journal_model.schema_version,
        outcome=fields["outcome"].to_record(),
        outcome_receipt=fields["outcome_receipt"].to_record(),
    )
    material.pop("journal_id")
    fields["journal_id"] = f"cohort-capture-journal-{record_sha256(material)[:32]}"
    with pytest.raises(ValueError, match="snapshot runtime type"):
        journal_model(**fields)
    forecasts = tuple(
        build_baseline_forecast_v2(
            **{name: getattr(f, name) for name in _forecast_args() if name != "previous_score"},
            previous_score=None,
        )
        for f in result.journal.snapshot.forecasts
    )
    snapshot_fields = {**dict(result.journal.snapshot), "forecasts": forecasts}
    snapshot_fields.pop("snapshot_id")
    fields["snapshot"] = CohortFrozenForecastSnapshotV1(
        **snapshot_fields,
        snapshot_id=build_cohort_frozen_forecast_snapshot_id(**snapshot_fields),
    )
    fields["snapshot_receipt"] = persist_evidence_record(
        args["evidence_archive_dir"],
        record=fields["snapshot"],
        experiment_id=args["protocol"].experiment_id,
        artifact_kind=EvidenceArtifactKind.FROZEN_FORECAST_SNAPSHOT,
        artifact_id=fields["snapshot"].snapshot_id,
        persisted_at=result.journal.snapshot_receipt.persisted_at,
    )
    material.update(
        snapshot=fields["snapshot"].to_record(),
        snapshot_receipt=fields["snapshot_receipt"].to_record(),
    )
    fields["journal_id"] = f"cohort-capture-journal-{record_sha256(material)[:32]}"
    legacy = journal_model(**fields)
    assert journal_model.from_record(legacy.to_record()) == legacy
    assert legacy.model_copy() == legacy


async def test_legacy_journal_refuses_newer_outcome_with_fresh_identity(tmp_path):
    _, result = await _run(tmp_path, [])
    fields = {
        name: value
        for name, value in dict(result.journal).items()
        if name in CohortCaptureJournalV1.model_fields
    }
    material = {name: value for name, value in result.journal.to_record().items() if name in fields}
    material.pop("journal_id")
    material["schema_version"] = CohortCaptureJournalV1.schema_version
    fields["journal_id"] = f"cohort-capture-journal-{record_sha256(material)[:32]}"
    with pytest.raises(ValueError, match="outcome runtime type"):
        CohortCaptureJournalV1(**fields)


async def test_clock_only_refresh_preserves_next_price_transition(tmp_path):
    event = orjson.loads(_book(38, bid="0.4", ask="0.8").text)
    event["timestamp"] = None
    args, result = await _run(
        tmp_path,
        [
            _book(36, bid="0.2", ask="0.6"),
            _book(37, bid="0.4", ask="0.8"),
            _frame(event, received_time=START + timedelta(seconds=38)),
            _book(39, bid="0.6", ask="1"),
        ],
    )
    forecasts = result.journal.snapshot.forecasts
    persistence = next(f for f in forecasts if f.method is BaselineMethod.PERSISTENCE)
    assert persistence.raw_score == Decimal("0.6")
    assert {f.quote.midpoint for f in forecasts} == {Decimal("0.8")}
    assert {f.as_of_ingest_sequence for f in forecasts} == {4}
    assert await _verify(args, result) == result.journal


def test_legacy_close_refuses_current_snapshot_with_fresh_receipt(tmp_path):
    close, _ = _synthetic_snapshot(tmp_path)
    legacy = close.forecast_snapshot
    assert CohortFrozenForecastSnapshotV1.from_record(legacy.to_record()) == legacy
    assert CohortCaptureCloseV1.from_record(close.to_record()) == close
    forecasts = tuple(
        build_baseline_forecast_v3(
            **{name: getattr(f, name) for name in _forecast_args() if name != "previous_score"},
            previous_score=None,
            trigger_event_time=f.as_of_event_time,
        )
        for f in legacy.forecasts
    )
    fields = {**dict(legacy), "forecasts": forecasts}
    fields.pop("snapshot_id")
    current = CohortFrozenForecastSnapshotV2(
        **fields,
        snapshot_id=build_cohort_frozen_forecast_snapshot_id(**fields),
    )
    receipt = persist_evidence_record(
        tmp_path,
        record=current,
        experiment_id=current.protocol.experiment_id,
        artifact_kind=EvidenceArtifactKind.FROZEN_FORECAST_SNAPSHOT,
        artifact_id=current.snapshot_id,
        persisted_at=close.forecast_snapshot_receipt.persisted_at,
    )
    fields = {
        **dict(close),
        "forecast_snapshot": current,
        "forecast_snapshot_receipt": receipt,
    }
    fields.pop("capture_close_id")
    with pytest.raises(ValueError, match="snapshot runtime type"):
        CohortCaptureCloseV1(**fields, capture_close_id=build_cohort_capture_close_id(**fields))


def _rebuild(forecast, trigger):
    args = {key: getattr(forecast, key) for key in _forecast_args() if key != "previous_score"}
    args["previous_score"] = None
    return build_baseline_forecast_v3(**args, trigger_event_time=trigger)


async def test_fresh_receipts_and_ids_cannot_invent_trigger_time(tmp_path):
    args, result = await _run(tmp_path, [_book(36)])
    snapshot = result.journal.snapshot
    wrong_time = START + timedelta(seconds=35)
    forecasts = tuple(_rebuild(f, wrong_time) for f in snapshot.forecasts)
    fields = {**dict(snapshot), "forecasts": forecasts}
    fields.pop("snapshot_id")
    altered = CohortFrozenForecastSnapshotV2(
        **fields,
        snapshot_id=build_cohort_frozen_forecast_snapshot_id(**fields),
    )
    receipt = persist_evidence_record(
        args["evidence_archive_dir"],
        record=altered,
        experiment_id=args["protocol"].experiment_id,
        artifact_kind=EvidenceArtifactKind.FROZEN_FORECAST_SNAPSHOT,
        artifact_id=altered.snapshot_id,
        persisted_at=snapshot.frozen_at,
    )
    changed = _persist_altered_journal(
        args,
        result.journal,
        snapshot=altered.to_record(),
        snapshot_receipt=receipt.to_record(),
    )
    with pytest.raises(ValueError, match="replayed baseline forecasts"):
        await _verify(args, changed)
    inconsistent = (forecasts[0], *snapshot.forecasts[1:])
    fields["forecasts"] = inconsistent
    with pytest.raises(ValueError, match="share one information-change trigger"):
        CohortFrozenForecastSnapshotV2(
            **fields,
            snapshot_id=build_cohort_frozen_forecast_snapshot_id(**fields),
        )


async def test_legacy_v2_journal_remains_readable_without_claiming_v3_timing(tmp_path):
    args, result = await _run(tmp_path, [])
    record = result.journal.to_record()
    record.pop("journal_id")
    record["schema_version"] = CohortCaptureJournalV2.schema_version
    record["journal_id"] = f"cohort-capture-journal-{record_sha256(record)[:32]}"
    legacy = CohortCaptureJournalV2.from_record(record)
    assert CohortCaptureJournalV2.from_record(legacy.to_record()) == legacy
    with pytest.raises(ValueError, match="legacy journal does not provide"):
        await _verify(args, CohortCaptureOwnerResult(legacy, result.receipt))
