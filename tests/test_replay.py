"""M3 exit criteria, evidenced on the recorded live capture.

Every test here drives the **real** recorded WebSocket capture
(`tests/fixtures/clob/ws_market_price_change.raw.json`, 40 seconds of live
public traffic) through the **real** capture path into a real
`SQLiteEventStore`, and then replays it back out through
`argos.replay.replay_capture`. Nothing is hand-assembled: the arrivals a replay
sees are the arrivals a capture wrote.

The criteria in `docs/07_MILESTONES.md`, and where each is closed:

- identical input + code + config produces an identical output hash across at
  least three runs -> `test_three_replays_of_one_capture_agree_exactly`
- replay never reads the wall clock inside domain logic ->
  `test_a_replay_reads_no_wall_clock`, plus the existing AST boundary tests
- late and invalid event behaviour is deterministic and counted ->
  `test_a_late_arrival_is_counted_and_still_applied`,
  `test_rejections_are_replayed_as_arrivals_and_tallied_by_reason`
- changing a source event produces a predictable hash change ->
  `test_changing_one_source_event_changes_the_hash_and_nothing_else`
- a live adapter can be replaced by a replay source without changing domain
  handlers -> `test_live_capture_and_replay_reach_the_same_state`
- replay performance is measured -> `test_replay_throughput_is_measured`
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from argos.clock import ReplayClock
from argos.config import Settings
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.errors import ReplayError
from argos.ingestion.capture import run_capture
from argos.projections.dispatch import ObservationDispatcher
from argos.replay import (
    ArrivalKind,
    ReplayMode,
    ReplayResultStatus,
    VirtualPacer,
    read_capture_arrivals,
    replay_capture,
)
from argos.sources.clob_ws import MarketFrame
from argos.store.event_store import open_sqlite_event_store

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "clob" / "ws_market_price_change.raw.json"
TOKEN = "34691510069031117755834214800869745291092253295564665516660269316660628637961"
CONDITION = "0x94a39addec8bd24a2d03deeaa43bdee6a2b11eca403cd12495eb24132cc23173"
CAPTURE_START = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)
WALL_START = datetime(2026, 9, 1, 9, 0, 0, tzinfo=UTC)
RUN_ID = "replay-fixture-capture"


def _recorded_frames(*, mutate_last_size: str | None = None) -> list[MarketFrame]:
    """Every frame the capture loop would actually have seen, in order.

    Two filters, and both matter for fidelity rather than convenience.
    `direction: "send"` entries are the client's own outbound PINGs and were
    never received by ARGOS at all. The four `PONG` replies *were* received, but
    `ClobMarketWsClient._classify` consumes them and never yields them onward,
    so feeding them into `run_capture` manufactures four `malformed_payload`
    rejections the shipped path cannot produce -- measured, and exactly the
    probe artifact `docs/STATUS.md` records from the M3 readiness audit. A
    golden hash computed over fabricated rejections would be a golden hash for a
    capture that cannot happen.
    """
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    connect_url: str = fixture["connect"]["connect_url"]
    frames: list[MarketFrame] = []
    received: list[dict[str, Any]] = [
        message
        for message in fixture["messages"]
        if message["direction"] == "recv" and message["raw"] != "PONG"
    ]
    for index, message in enumerate(received):
        raw_text: str = message["raw"]
        if mutate_last_size is not None and index == len(received) - 1:
            raw_text = _rewrite_first_size(raw_text, mutate_last_size)
        received_time = datetime.fromisoformat(message["wall_time"])
        raw_bytes = raw_text.encode("utf-8")
        frames.append(
            MarketFrame(
                text=raw_text,
                received_time=received_time,
                provenance=SourceProvenanceV1(
                    source="clob_market_ws",
                    endpoint=connect_url,
                    http_status=None,
                    retrieved_at=received_time,
                    raw_sha256=sha256_hex(raw_bytes),
                    byte_length=len(raw_bytes),
                ),
            )
        )
    return frames


def _rewrite_first_size(raw_text: str, new_size: str) -> str:
    """Change one price level's size in a recorded frame, leaving all else alone.

    Used only by the "a changed source event changes the hash" criterion. It
    edits the JSON rather than the text so the result is still a valid frame the
    real normalizer accepts -- a corrupted string would test rejection, not the
    criterion.
    """
    decoded = json.loads(raw_text)
    events = decoded if isinstance(decoded, list) else [decoded]
    for event in events:
        for entry in event.get("price_changes", []):
            if entry.get("asset_id") == TOKEN:
                entry["size"] = new_size
                return json.dumps(decoded, separators=(",", ":"))
    raise AssertionError("no price_changes entry for the subscribed token in the last frame")


class _ListFrameSource:
    def __init__(self, frames: list[MarketFrame]) -> None:
        self._frames = frames

    async def frames(self) -> Any:
        for frame in self._frames:
            yield frame


async def _captured(
    *,
    mutate_last_size: str | None = None,
    dispatcher: ObservationDispatcher | None = None,
) -> Any:
    """Build a real capture in an in-memory store from the recorded frames."""
    store = open_sqlite_event_store(":memory:")
    await run_capture(
        frame_source=_ListFrameSource(_recorded_frames(mutate_last_size=mutate_last_size)),
        store=store,
        clock=ReplayClock(CAPTURE_START),
        capture_run_id=RUN_ID,
        subscribed_token_ids=[TOKEN],
        dispatcher=dispatcher,
    )
    return store


def _replay(store: Any, **overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "store": store,
        "capture_run_id": RUN_ID,
        "settings": Settings(),
        "wall_clock": ReplayClock(WALL_START),
        "replay_run_id": "replay-1",
    }
    arguments.update(overrides)
    return replay_capture(**arguments)


# --- the reader -------------------------------------------------------------------


async def test_arrivals_come_back_in_the_order_the_capture_wrote_them() -> None:
    store = await _captured()
    arrivals = list(read_capture_arrivals(store, RUN_ID))
    sequences = [arrival.ingest_sequence for arrival in arrivals]
    assert sequences == sorted(sequences)
    assert sequences == list(range(1, len(sequences) + 1)), (
        "the capture loop allocates a sequence only when a record is written, so a "
        "healthy capture is contiguous"
    )
    assert {arrival.kind for arrival in arrivals} <= set(ArrivalKind)
    for arrival in arrivals:
        assert (arrival.envelope is None) != (arrival.rejection is None)


async def test_an_unknown_capture_run_is_refused_rather_than_replayed_empty() -> None:
    """ "This capture is empty" and "this capture does not exist" are different
    answers and must not look identical in a manifest."""
    store = await _captured()
    with pytest.raises(ReplayError):
        list(read_capture_arrivals(store, "never-opened"))


# --- the exit criteria ------------------------------------------------------------


async def test_three_replays_of_one_capture_agree_exactly() -> None:
    """ "Identical input + code + config produces identical output hash across at
    least three runs" -- and the counts too, which the criterion's own contract
    (`docs/04_DATA_CONTRACTS.md`) requires alongside it."""
    store = await _captured()
    results = [_replay(store, replay_run_id=f"replay-{n}") for n in range(3)]
    hashes = {result.state_hash for result in results}
    counts = {json.dumps(result.manifest.to_record()["output_record_counts"]) for result in results}
    assert len(hashes) == 1, hashes
    assert len(counts) == 1, counts
    assert results[0].counts.arrivals > 0


async def test_the_pacing_mode_cannot_reach_the_output_hash() -> None:
    """ADR-0009: "scheduler pacing ... must never influence the output hash."
    Asserted rather than asserted-about: all three modes run the same capture."""
    store = await _captured()
    hashes = {
        mode: _replay(store, mode=mode, replay_run_id=f"replay-{mode}").state_hash
        for mode in ReplayMode
    }
    assert len(set(hashes.values())) == 1, hashes


async def test_original_arrival_mode_asks_for_the_real_gaps_without_sleeping() -> None:
    """The pacing is real and inspectable; only the sleeping is virtual. A test
    that simply skipped pacing would prove nothing about whether the scheduler
    asked for the right pauses."""
    store = await _captured()
    pacer = VirtualPacer()
    _replay(store, mode=ReplayMode.ORIGINAL_ARRIVAL, pacer=pacer)
    assert pacer.waits, "original_arrival must pace between arrivals"
    assert all(wait > 0 for wait in pacer.waits)

    accelerated = VirtualPacer()
    _replay(store, mode=ReplayMode.ACCELERATED, pacer=accelerated)
    assert accelerated.waits == []


async def test_live_capture_and_replay_reach_the_same_state() -> None:
    """Core invariant 5, as a measurement rather than a claim.

    The live capture loop and the replay scheduler drive the *same*
    `ObservationDispatcher` class over the same frames -- one from the wire, one
    from storage. If they disagree, something between the two paths is not
    shared.
    """
    live = ObservationDispatcher()
    store = await _captured(dispatcher=live)
    replayed = _replay(store)
    assert live.state_hash() == replayed.state_hash
    assert live.counts.as_record() == replayed.dispatcher.counts.as_record()


async def test_changing_one_source_event_changes_the_hash_and_nothing_else() -> None:
    """ "Changing a source event produces a predictable hash change."

    One price level's size is changed in the final recorded frame. The change is
    made to the *source bytes*, so it travels the whole path -- normalization,
    identity, storage, replay -- rather than being poked into a projection. The
    arrival counts are unchanged, which is what makes the hash difference
    attributable to the book state and not to a different amount of input.
    """
    baseline = _replay(await _captured(), replay_run_id="baseline")
    mutated = _replay(await _captured(mutate_last_size="999"), replay_run_id="mutated")

    assert mutated.state_hash != baseline.state_hash
    assert mutated.counts.as_record() == baseline.counts.as_record()
    assert mutated.dispatcher.counts.as_record() == baseline.dispatcher.counts.as_record()


async def test_a_replay_reads_no_wall_clock() -> None:
    """ "Replay never reads the wall clock inside domain logic."

    Driven with a `ReplayClock` for the manifest's own timestamps as well, so
    the whole call is free of real time: if anything reached for it, the
    manifest would carry a moment neither clock was ever set to.
    """
    store = await _captured()
    result = _replay(store)
    assert result.manifest.started_at == WALL_START
    assert result.manifest.finished_at == WALL_START


async def test_rejections_are_replayed_as_arrivals_and_tallied_by_reason() -> None:
    """Invalid events are replayed, counted and named -- never applied.

    This capture contains no rejection of its own, so one is added at the
    source: a frame of text that is not JSON, which the real capture path
    refuses with `malformed_payload`. Using the real path rather than writing a
    rejection row directly is the point -- a hand-written row would prove only
    that the reader can read one.
    """
    store = open_sqlite_event_store(":memory:")
    frames = _recorded_frames()
    broken_text = "{not json"
    broken = MarketFrame(
        text=broken_text,
        received_time=frames[-1].received_time + timedelta(milliseconds=1),
        provenance=SourceProvenanceV1(
            source="clob_market_ws",
            endpoint=frames[0].provenance.endpoint,
            http_status=None,
            retrieved_at=frames[-1].received_time + timedelta(milliseconds=1),
            raw_sha256=sha256_hex(broken_text.encode()),
            byte_length=len(broken_text.encode()),
        ),
    )
    await run_capture(
        frame_source=_ListFrameSource([*frames, broken]),
        store=store,
        clock=ReplayClock(CAPTURE_START),
        capture_run_id=RUN_ID,
        subscribed_token_ids=[TOKEN],
    )

    clean = _replay(await _captured())
    result = _replay(store)
    assert result.counts.rejected == 1
    assert dict(result.counts.rejection_reasons) == {"malformed_payload": 1}
    # A rejection reaches no projection, so the reconstructed book is identical
    # to the one the same capture without it produces.
    assert result.state_hash == clean.state_hash


async def test_widening_the_tolerance_never_changes_the_reconstructed_state() -> None:
    """The half of the mark-only policy this capture *can* evidence.

    The recorded capture contains no out-of-order arrival, so nothing in it is
    ever late and this file cannot prove the classification. It can prove the
    other half: the tolerance is a label, and the reconstructed book is
    identical whatever it is set to. The classification itself is evidenced on a
    genuinely out-of-order capture in `tests/test_replay_late_events.py`, which
    replaced an assertion here that read `late >= 0` and was true of every
    possible run.
    """
    store = await _captured()
    strict = _replay(store, allowed_lateness=timedelta(0), replay_run_id="strict")
    lenient = _replay(store, allowed_lateness=timedelta(days=1), replay_run_id="lenient")

    assert strict.state_hash == lenient.state_hash
    assert strict.dispatcher.counts.late == 0, (
        "this capture is in order; a late event here would mean the fixture "
        "changed, not that the policy fired"
    )
    assert lenient.dispatcher.counts.late == 0
    assert strict.dispatcher.counts.on_time == lenient.dispatcher.counts.on_time


async def test_the_late_event_policy_is_recorded_in_the_manifest() -> None:
    store = await _captured()
    result = _replay(store, allowed_lateness=timedelta(milliseconds=250))
    policy = result.manifest.to_record()["late_event_policy"]
    assert policy == {"kind": "mark_only", "allowed_lateness_microseconds": 250_000}


# --- the manifest -----------------------------------------------------------------


async def test_the_manifest_describes_the_run_and_round_trips() -> None:
    from argos.replay import ReplayManifestV1

    store = await _captured()
    result = _replay(store)
    record = result.manifest.to_record()
    assert record["schema_version"] == "replay_manifest.v1"
    assert record["source_capture_run_id"] == RUN_ID
    assert record["result_status"] == ReplayResultStatus.COMPLETED.value
    assert record["state_hash_version"] == "state_hash.v1"
    assert record["input_first_sequence"] == 1
    assert record["input_last_sequence"] == result.counts.arrivals
    assert record["source_completion_status"] == "completed"
    assert ReplayManifestV1.from_record(record) == result.manifest


async def test_the_config_fingerprint_survives_a_different_output_location() -> None:
    """Blocker R1, seen from the far end: two replays writing elsewhere are the
    same experiment, and the manifest must say so."""
    store = await _captured()
    here = _replay(store, settings=Settings(data_dir=Path("/tmp/a")))
    there = _replay(store, settings=Settings(data_dir=Path("/tmp/b")))
    assert here.manifest.config_fingerprint == there.manifest.config_fingerprint


# --- the golden replay ------------------------------------------------------------

GOLDEN_STATE_HASH = "2a7fcb6a0ff4745f0e360fa927eb58d67a024e244c5905ac182826717cd69a34"
GOLDEN_ARRIVAL_COUNTS = {
    "arrivals": 38,
    "accepted": 38,
    "duplicate": 0,
    "rejected": 0,
    "sequence_gaps": 0,
    "received_time_regressions": 0,
    "rejection_reasons": {},
}
GOLDEN_DISPATCH_COUNTS = {
    "applied_snapshots": 4,
    "applied_deltas": 34,
    "applied_auxiliary": 0,
    "skipped_duplicates": 0,
    "unhandled_payloads": 0,
    "unscoped": 0,
    "on_time": 38,
    "late": 0,
    "undatable": 0,
}


async def test_the_golden_replay_still_produces_its_recorded_hash() -> None:
    """`docs/13_TEST_STRATEGY.md`: "a small ordered capture fixture must always
    produce the declared state hash and record counts. Changes require an
    explicit reason and golden update review."

    The fixture is the recorded live capture already committed to this
    repository, driven through the real capture and replay paths, so this pins
    the whole chain rather than a saved intermediate. If it fails, either the
    encoding changed -- in which case `STATE_HASH_VERSION` must move with it --
    or something in normalization, identity, storage, ordering or projection
    changed what ARGOS reconstructs from bytes it has already seen.
    """
    result = _replay(await _captured())
    assert result.state_hash == GOLDEN_STATE_HASH
    assert result.counts.as_record() == GOLDEN_ARRIVAL_COUNTS
    assert result.dispatcher.counts.as_record() == GOLDEN_DISPATCH_COUNTS
    # The counts are the capture's own shape, independently recorded in
    # `docs/STATUS.md` from the M2 projection slice: 4 `book` snapshots and 34
    # `price_change` frames for the subscribed token.
    assert sorted(result.dispatcher.projections) == [(CONDITION, TOKEN)]


async def test_the_golden_hash_describes_a_book_the_source_itself_asserted() -> None:
    """A stable hash of the wrong state would still be stable.

    So the golden value is anchored to something the *source* said, not to what
    ARGOS happened to compute. The capture carries four full `book` snapshots
    (arrivals 1, 15, 23 and 32) with deltas in between, so it contains three
    independent checkpoints: at each later snapshot, the state the deltas alone
    have built must already equal the book the source is about to restate. This
    walks the same arrivals through the same dispatcher, checking each
    checkpoint *before* the snapshot is applied -- comparing afterwards would be
    vacuous, because a snapshot replaces the state wholesale.

    Six deltas follow the last snapshot, so the final state is deliberately
    *not* equal to it; asserting that it was would be the more obvious test and
    the wrong one.
    """
    from argos.domain.observation import read_payload
    from argos.domain.wsbook import WsBookSnapshotV1

    store = await _captured()
    dispatcher = ObservationDispatcher()
    checkpoints = 0

    for arrival in read_capture_arrivals(store, RUN_ID):
        envelope = arrival.envelope
        assert envelope is not None, "this capture contains no rejections"
        if envelope.payload_schema_version == "ws_book_snapshot.v1":
            snapshot = read_payload(envelope, WsBookSnapshotV1)
            projection = dispatcher.projections.get((CONDITION, TOKEN))
            if projection is not None and projection.is_seeded:
                projected = projection.state()
                assert [(x.price, x.size) for x in projected.bids] == [
                    (x.price, x.size) for x in snapshot.bids
                ], (
                    "deltas did not rebuild the book the source restated at "
                    f"sequence {arrival.ingest_sequence}"
                )
                assert [(x.price, x.size) for x in projected.asks] == [
                    (x.price, x.size) for x in snapshot.asks
                ]
                checkpoints += 1
        dispatcher.dispatch(envelope)

    assert checkpoints == 3, "the recorded capture spans three snapshot-to-snapshot transitions"
    assert dispatcher.state_hash() == GOLDEN_STATE_HASH


# --- performance ------------------------------------------------------------------


async def test_replay_throughput_is_measured() -> None:
    """ "Replay performance is measured but correctness takes precedence."

    Measured, deliberately not asserted against a threshold: a wall-clock
    assertion in a unit suite is the flaky timing test `docs/13_TEST_STRATEGY.md`
    forbids. The number is printed so a regression is visible to somebody
    reading the output, and the only assertion is that the work actually
    happened.
    """
    store = await _captured()
    started = time.perf_counter()
    result = _replay(store)
    elapsed = time.perf_counter() - started
    rate = result.counts.arrivals / elapsed if elapsed > 0 else float("inf")
    print(f"\nreplay: {result.counts.arrivals} arrivals in {elapsed:.4f}s ({rate:,.0f}/s)")
    assert result.counts.arrivals > 0
    assert Decimal(str(elapsed)) >= 0
