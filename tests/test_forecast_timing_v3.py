"""Separate source book time from the triggering event, never migrate old records."""

from datetime import timedelta, timezone
from decimal import Decimal

import pytest
from test_baselines import NOW, _state
from test_capture_loop import _frame
from test_cohort_capture_journal_guards_v2 import _persist_altered_journal
from test_cohort_capture_replay_v2 import _book, _run, _verify
from test_cohort_protocol_v2 import START
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
from argos.evaluation.cohort_capture_replay_v2 import CohortCaptureJournalV2
from argos.evaluation.cohort_snapshot_v2 import (
    CohortFrozenForecastSnapshotV2,
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
    import orjson

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
