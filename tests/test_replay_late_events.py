"""A real late arrival, and what the watermark does and does not do to it (F7).

The test this replaces asserted `late >= 0`, which is true of every possible
run. It could not have been otherwise with the inputs it had: the recorded
capture contains **zero** out-of-order arrivals, so nothing in it is ever late
and the mark-only policy was never exercised at all.

A genuine late arrival is constructed here **without editing a single field of
a single frame**. Two real `price_change` frames are replayed in the opposite of
their recorded order: the one the source stamped `1786387698985` arrives first,
then the one it stamped `1786387667390`. Every payload, price, size, hash and
event time is exactly what Polymarket sent on 2026-08-10; only the order they
reach ARGOS in is ours, which is precisely what "out of order" means. Nothing is
fabricated, and the shape being tested is one this repository has never observed
live -- which is stated rather than implied.

`received_time` is assigned monotonically, because that is the one thing a
transport genuinely controls and a replay genuinely requires: arrival order is
what ADR-0003 makes replay reproduce.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_replay import CONDITION, RUN_ID, TOKEN, _ListFrameSource, _recorded_frames

from argos.clock import ReplayClock
from argos.config import Settings
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.ingestion.capture import run_capture
from argos.projections.book import BookProjectionAnomalyKind
from argos.projections.dispatch import Lateness
from argos.replay import ReplayMode, read_capture_arrivals, replay_capture
from argos.sources.clob_ws import MarketFrame
from argos.store.event_store import open_sqlite_event_store

ARRIVAL_START = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)
WALL_START = datetime(2026, 9, 1, 9, 0, 0, tzinfo=UTC)

# Indices into the recorded capture's received frames.
SEED = 0  # the `book` snapshot that seeds the projection
LATER = 37  # price_change, source timestamp 1786387698985
EARLIER = 4  # price_change, source timestamp 1786387667390


def _event_time_ms(frame: MarketFrame) -> int:
    decoded = json.loads(frame.text)
    events = decoded if isinstance(decoded, list) else [decoded]
    return int(events[0]["timestamp"])


def _reordered_frames() -> list[MarketFrame]:
    """The seed, then a later real frame, then an earlier real frame.

    Only `received_time` is rewritten, and only to make arrival order explicit
    and monotonic. The frame text -- and therefore every event time, price and
    hash -- is byte-for-byte what was recorded.
    """
    recorded = _recorded_frames()
    chosen = [recorded[SEED], recorded[LATER], recorded[EARLIER]]
    frames: list[MarketFrame] = []
    for position, frame in enumerate(chosen):
        arrived = ARRIVAL_START + timedelta(seconds=position)
        frames.append(
            MarketFrame(
                text=frame.text,
                received_time=arrived,
                provenance=SourceProvenanceV1(
                    source=frame.provenance.source,
                    endpoint=frame.provenance.endpoint,
                    http_status=None,
                    retrieved_at=arrived,
                    raw_sha256=sha256_hex(frame.text.encode("utf-8")),
                    byte_length=len(frame.text.encode("utf-8")),
                ),
            )
        )
    return frames


def test_the_constructed_capture_really_is_out_of_order() -> None:
    """The premise, asserted rather than assumed.

    A test of late-event handling whose input turned out to be in order would
    pass for the wrong reason -- which is exactly how the assertion this replaces
    came to be vacuous.
    """
    frames = _reordered_frames()
    times = [_event_time_ms(frame) for frame in frames]
    assert times[2] < times[1], f"the third frame must be strictly behind the second: {times}"
    assert [frame.received_time for frame in frames] == sorted(
        frame.received_time for frame in frames
    ), "arrival order must still be monotonic"


async def _captured() -> Any:
    store = open_sqlite_event_store(":memory:")
    await run_capture(
        frame_source=_ListFrameSource(_reordered_frames()),
        store=store,
        clock=ReplayClock(ARRIVAL_START),
        capture_run_id=RUN_ID,
        subscribed_token_ids=[TOKEN],
    )
    return store


def _replay(store: Any, **overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "store": store,
        "capture_run_id": RUN_ID,
        "settings": Settings(),
        "wall_clock": ReplayClock(WALL_START),
        "replay_run_id": "late-1",
    }
    arguments.update(overrides)
    return replay_capture(**arguments)


async def test_exactly_one_arrival_is_classified_late() -> None:
    """Exactly one, not "at least one" and not "some". Three arrivals, one of
    which is behind the watermark its predecessor advanced."""
    result = _replay(await _captured())
    counts = result.dispatcher.counts
    assert counts.late == 1
    assert counts.on_time == 2
    assert counts.undatable == 0
    assert counts.applied_snapshots + counts.applied_deltas == 3


async def test_the_late_arrival_is_still_applied_and_is_the_last_thing_applied() -> None:
    """ADR-0003 forbids reordering late data into the past. If the watermark
    quietly held the late delta back, or applied it before the one it arrived
    after, the projection's last applied event time would be the *later*
    timestamp rather than the earlier one."""
    result = _replay(await _captured())
    projection = result.dispatcher.projections[(CONDITION, TOKEN)]
    frames = _reordered_frames()

    assert projection.applied_delta_count == 2, "both deltas were applied"
    last_applied_ms = int(projection.last_event_time.timestamp() * 1000)
    assert last_applied_ms == _event_time_ms(frames[2]), (
        "the last applied input must be the late one; a different value means it "
        "was reordered ahead of the delta it arrived after"
    )
    # The projection counts the regression rather than repairing it -- the M2
    # counter, still doing its job under the M3 watermark.
    assert projection.anomaly_counts[BookProjectionAnomalyKind.EVENT_TIME_REGRESSION] == 1


async def test_no_hidden_reordering_between_the_store_and_the_dispatcher() -> None:
    """Arrival order is what replay reproduces (ADR-0003). Read back from the
    store, the event times must appear in the order they *arrived*, not sorted."""
    store = await _captured()
    event_times = [
        arrival.envelope.event_time
        for arrival in read_capture_arrivals(store, RUN_ID)
        if arrival.envelope is not None and arrival.envelope.event_time is not None
    ]
    assert len(event_times) == 3
    assert event_times[2] < event_times[1], "the stored order is arrival order, not event order"
    assert event_times != sorted(event_times), "a sorted stream would mean silent reordering"


async def test_the_late_arrival_is_deterministic_across_three_runs() -> None:
    store = await _captured()
    results = [_replay(store, replay_run_id=f"late-{index}") for index in range(3)]
    assert len({r.state_hash for r in results}) == 1
    assert len({r.dispatcher.counts.late for r in results}) == 1
    assert results[0].dispatcher.counts.late == 1
    assert len({json.dumps(r.counts.as_record(), sort_keys=True) for r in results}) == 1


async def test_the_pacing_mode_does_not_change_the_state_a_late_arrival_produces() -> None:
    """ADR-0009: scheduler pacing must never influence the output hash --
    including when the stream contains an out-of-order event, which is the case
    where a buffering implementation would differ between modes."""
    store = await _captured()
    hashes = {
        mode: _replay(store, mode=mode, replay_run_id=f"late-{mode}").state_hash
        for mode in ReplayMode
    }
    assert len(set(hashes.values())) == 1, hashes


async def test_the_recorded_policy_matches_what_the_run_actually_did() -> None:
    """A manifest that recorded a policy the run did not follow would be worse
    than one that recorded none."""
    result = _replay(await _captured())
    record = result.manifest.to_record()
    assert record["late_event_policy"] == {
        "kind": "mark_only",
        "allowed_lateness_microseconds": 0,
    }
    dispatch = record["output_record_counts"]["dispatch"]
    assert dispatch["late"] == 1
    # "mark only" means marked *and applied*: nothing dropped, nothing skipped,
    # nothing unhandled. Three arrivals in, three applications out.
    assert dispatch["applied_snapshots"] + dispatch["applied_deltas"] == 3
    assert dispatch["skipped_duplicates"] == 0
    assert dispatch["unhandled_payloads"] == 0
    assert dispatch["unscoped"] == 0
    assert record["output_record_counts"]["arrivals"]["arrivals"] == 3


@pytest.mark.parametrize("tolerance_seconds", [0, 60, 3600])
async def test_the_tolerance_changes_the_classification_and_never_the_state(
    tolerance_seconds: int,
) -> None:
    """The whole content of "mark only": widening the tolerance moves an event
    from late to on time and leaves the reconstructed book byte-identical. If
    the state hash moved with the tolerance, the watermark would be doing
    something to the data rather than to the label."""
    store = await _captured()
    strict = _replay(store, allowed_lateness=timedelta(0), replay_run_id="strict")
    relaxed = _replay(
        store,
        allowed_lateness=timedelta(seconds=tolerance_seconds),
        replay_run_id=f"relaxed-{tolerance_seconds}",
    )
    assert relaxed.state_hash == strict.state_hash
    expected_late = 1 if tolerance_seconds < 32 else 0
    assert relaxed.dispatcher.counts.late == expected_late, (
        f"the two frames are ~31.6 s apart in event time, so a {tolerance_seconds} s "
        "tolerance should" + (" still flag" if expected_late else " absorb") + " the gap"
    )
    assert strict.dispatcher.counts.late == 1


async def test_a_late_classification_is_reported_per_event_not_only_in_totals() -> None:
    """An operator looking at one event must be able to see that *this* one was
    late, not only that the run contained a late one somewhere."""
    from argos.projections.dispatch import ObservationDispatcher

    store = await _captured()
    dispatcher = ObservationDispatcher()
    lateness = [
        dispatcher.dispatch(arrival.envelope).lateness
        for arrival in read_capture_arrivals(store, RUN_ID)
        if arrival.envelope is not None
    ]
    assert lateness == [Lateness.ON_TIME, Lateness.ON_TIME, Lateness.LATE]
