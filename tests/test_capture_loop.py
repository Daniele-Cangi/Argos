"""Tests for `argos.ingestion.capture.run_capture`.

No test opens a socket: `FrameSource` is driven by `ListFrameSource` below, a
bare async-generator fake that yields a fixed list of already-built
`MarketFrame`s -- exactly the protocol
`argos.sources.clob_ws.ClobMarketWsClient.frames()` also satisfies, but with
none of that class's transport, task-group, or reconnect machinery. Every
event dict mirrors `tests/test_price_change.py`'s own `_minimal_event`
fixture (same `TOKEN_YES`/`TOKEN_NO`/`CONDITION_ID` values), which is itself
built from the real recorded WebSocket capture
(`docs/research/fixtures/clob-ws-market-2026-08-10T184742Z.json`) rather than
invented from scratch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest

from argos.clock import ReplayClock
from argos.domain.observation import ObservationEnvelopeV1
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.errors import RejectionReason
from argos.ingestion.capture import CaptureHealth, run_capture
from argos.sources.clob_ws import MarketFrame
from argos.store.event_store import (
    CompletionStatus,
    Disposition,
    SQLiteEventStore,
    open_sqlite_event_store,
)

TOKEN_YES = "34691510069031117755834214800869745291092253295564665516660269316660628637961"
TOKEN_NO = "95561057794427123541889915407555646439882912350845258651794843110787555977699"
CONDITION_ID = "0x94a39addec8bd24a2d03deeaa43bdee6a2b11eca403cd12495eb24132cc23173"

START = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)
ENDPOINT = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _price_change_event(
    *,
    timestamp: str = "1786387666174",
    price: str = "0.49",
    size: str = "636",
    entry_hash: str = "5ce704dea0a2123a388f1d8b432aad0058f5c479",
    asset_id: str = TOKEN_YES,
    extra_entries: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """One well-formed `price_change` event, for one or more tokens.

    Mirrors `tests/test_price_change.py::_minimal_event` exactly (same key
    set, same field shapes) so it parses through the real
    `argos.domain.pricechange.parse_price_change_group`, not a
    capture-loop-only shortcut.
    """
    entries = [
        {
            "asset_id": asset_id,
            "price": price,
            "size": size,
            "side": "SELL",
            "hash": entry_hash,
            "best_bid": "0.28",
            "best_ask": "0.29",
        },
        *extra_entries,
    ]
    return {
        "event_type": "price_change",
        "market": CONDITION_ID,
        "timestamp": timestamp,
        "price_changes": entries,
    }


def _sibling_entry(
    *, entry_hash: str = "aaaa704dea0a2123a388f1d8b432aad0058f5c47"
) -> dict[str, Any]:
    return {
        "asset_id": TOKEN_NO,
        "price": "0.51",
        "size": "400",
        "side": "BUY",
        "hash": entry_hash,
        "best_bid": "0.28",
        "best_ask": "0.29",
    }


def _unknown_event(
    *, event_type: str = "tick_size_change", asset_id: str | None = TOKEN_YES
) -> dict[str, Any]:
    event: dict[str, Any] = {"event_type": event_type, "market": CONDITION_ID}
    if asset_id is not None:
        event["asset_id"] = asset_id
    return event


def _frame(
    payload: Any, *, text: str | None = None, received_time: datetime = START
) -> MarketFrame:
    """Build one `MarketFrame` the way the transport would, from a JSON-able payload.

    `text` overrides the encoded payload directly, for tests that need
    deliberately invalid JSON text.
    """
    body = text if text is not None else orjson.dumps(payload).decode()
    raw = body.encode("utf-8")
    return MarketFrame(
        text=body,
        received_time=received_time,
        provenance=SourceProvenanceV1(
            source="clob_market_ws",
            endpoint=ENDPOINT,
            http_status=None,
            retrieved_at=received_time,
            raw_sha256=sha256_hex(raw),
            byte_length=len(raw),
        ),
    )


@dataclass
class ListFrameSource:
    """Test fake `FrameSource`: yields a fixed, pre-built list of frames.

    Reconnect is, by design (see `argos.ingestion.capture`'s own module
    docstring, Decision 1), invisible to `run_capture` -- the transport keeps
    yielding from the same `frames()` generator across its own internal
    reconnects. This fake models that directly: a "reconnect" in a test is
    simply more frames later in the same list, with nothing distinguishing
    them from the frames before it, which is exactly the point.
    """

    frames_to_yield: Sequence[MarketFrame]

    async def frames(self) -> AsyncIterator[MarketFrame]:
        for frame in self.frames_to_yield:
            yield frame


class RaisingFrameSource:
    """Test fake: yields some frames, then raises."""

    def __init__(self, frames_to_yield: Sequence[MarketFrame], error: Exception) -> None:
        self._frames = frames_to_yield
        self._error = error

    async def frames(self) -> AsyncIterator[MarketFrame]:
        for frame in self._frames:
            yield frame
        raise self._error


@pytest.fixture
def store() -> SQLiteEventStore:
    return open_sqlite_event_store(":memory:")


@pytest.fixture
def clock() -> ReplayClock:
    return ReplayClock(START)


async def test_sequence_continues_across_a_simulated_reconnect_and_never_resets(
    store: SQLiteEventStore, clock: ReplayClock
) -> None:
    """Sequence keeps advancing across frames straddling a "reconnect", never resets to 1."""
    frames = [
        _frame(_price_change_event()),  # accepted, seq 1
        _frame(_unknown_event()),  # rejected (unknown event type), seq 2
        # "post-reconnect" traffic: a genuinely new price_change state for the
        # same token, arriving on what is -- from this loop's perspective --
        # simply the next frame in one uninterrupted stream.
        _frame(_price_change_event(timestamp="1786387666999", entry_hash="deadbeef00")),  # seq 3
        _frame(_price_change_event()),  # redelivery of the first state: duplicate, seq 4
    ]
    run_id = "run-reconnect"
    health = await run_capture(
        frame_source=ListFrameSource(frames),
        store=store,
        clock=clock,
        capture_run_id=run_id,
        subscribed_token_ids=[TOKEN_YES],
    )
    deliveries = list(store.iter_deliveries(run_id))
    rejections = list(store.iter_rejections(run_id))
    assert [record.ingest_sequence for record in deliveries] == [1, 3, 4]
    assert [record.ingest_sequence for record in rejections] == [2]
    assert deliveries[0].disposition is Disposition.ACCEPTED_NEW
    assert deliveries[1].disposition is Disposition.ACCEPTED_NEW
    assert deliveries[2].disposition is Disposition.DUPLICATE
    assert health.accepted == 2
    assert health.duplicate == 1
    assert health.unknown_event_type == 1


async def test_sequence_allocation_is_unchanged_by_an_unsubscribed_sibling_token(
    store: SQLiteEventStore, clock: ReplayClock
) -> None:
    """A frame naming an unsubscribed sibling token allocates no sequence for it.

    `run_capture` iterates the *configured* subscribed token set, never "the
    tokens this frame happens to mention" -- see the module docstring,
    Decision 2. The committed sequence for `TOKEN_YES` must be identical
    whether or not the same frame also carries an entry for the unsubscribed
    `TOKEN_NO`.
    """
    event_without_sibling = _price_change_event()
    event_with_sibling = _price_change_event(extra_entries=[_sibling_entry()])

    run_a = "run-no-sibling"
    await run_capture(
        frame_source=ListFrameSource([_frame(event_without_sibling)]),
        store=store,
        clock=clock,
        capture_run_id=run_a,
        subscribed_token_ids=[TOKEN_YES],
    )
    run_b = "run-with-sibling"
    await run_capture(
        frame_source=ListFrameSource([_frame(event_with_sibling)]),
        store=store,
        clock=clock,
        capture_run_id=run_b,
        subscribed_token_ids=[TOKEN_YES],
    )

    deliveries_a = list(store.iter_deliveries(run_a))
    deliveries_b = list(store.iter_deliveries(run_b))
    assert [record.ingest_sequence for record in deliveries_a] == [1]
    assert [record.ingest_sequence for record in deliveries_b] == [1]
    # No record of any kind exists for TOKEN_NO: it was never a configured
    # token, so the fan-out never looked at its entry at all.
    for record in [*deliveries_a, *deliveries_b]:
        envelope = store.get_observation(record.observation_id)
        assert envelope is not None
        assert envelope.token_id == TOKEN_YES


async def test_deterministic_ordering_across_two_identical_runs(clock: ReplayClock) -> None:
    """Two identical frame streams, against two independent stores, produce identical records.

    Deliberately *not* the shared `store` fixture: replaying the same content
    into the *same* store would make the second run's observations collide
    with the first run's (`observation_id` does not depend on
    `capture_run_id`), turning every `accepted_new` into a `duplicate` on the
    second pass for reasons that have nothing to do with sequence
    determinism. Two independent stores isolate the property actually under
    test -- that `run_capture` assigns the same sequence numbers, in the same
    order, to the same outcomes, given the same frames and configuration --
    from identity-level deduplication, which is a different, already-tested
    concern.
    """
    frames = [
        _frame(_price_change_event()),
        _frame(_unknown_event(event_type="tick_size_change")),
        _frame([_price_change_event(timestamp="1786387667000", entry_hash="c0ffee0000")]),
    ]
    tokens = [TOKEN_YES, TOKEN_NO]

    store_1 = open_sqlite_event_store(":memory:")
    run_1 = "run-deterministic-1"
    health_1 = await run_capture(
        frame_source=ListFrameSource(frames),
        store=store_1,
        clock=clock,
        capture_run_id=run_1,
        subscribed_token_ids=tokens,
    )
    store_2 = open_sqlite_event_store(":memory:")
    run_2 = "run-deterministic-2"
    health_2 = await run_capture(
        frame_source=ListFrameSource(frames),
        store=store_2,
        clock=clock,
        capture_run_id=run_2,
        subscribed_token_ids=tokens,
    )

    assert health_1 == health_2

    sequence_shapes_1 = [
        (record.ingest_sequence, record.disposition, record.observation_id)
        for record in store_1.iter_deliveries(run_1)
    ]
    sequence_shapes_2 = [
        (record.ingest_sequence, record.disposition, record.observation_id)
        for record in store_2.iter_deliveries(run_2)
    ]
    assert sequence_shapes_1 == sequence_shapes_2

    rejection_shapes_1 = [
        (record.ingest_sequence, record.rejection.reason, record.rejection.rejection_id)
        for record in store_1.iter_rejections(run_1)
    ]
    rejection_shapes_2 = [
        (record.ingest_sequence, record.rejection.reason, record.rejection.rejection_id)
        for record in store_2.iter_rejections(run_2)
    ]
    assert rejection_shapes_1 == rejection_shapes_2


async def test_a_decode_failure_produces_a_rejection_with_the_correct_raw_hash(
    store: SQLiteEventStore, clock: ReplayClock
) -> None:
    bad_text = "{not valid json"
    frame = _frame(None, text=bad_text)
    run_id = "run-decode-failure"
    health = await run_capture(
        frame_source=ListFrameSource([frame]),
        store=store,
        clock=clock,
        capture_run_id=run_id,
        subscribed_token_ids=[TOKEN_YES],
    )
    rejections = list(store.iter_rejections(run_id))
    assert len(rejections) == 1
    rejection = rejections[0].rejection
    assert rejection.reason is RejectionReason.MALFORMED_PAYLOAD
    assert rejection.raw_payload_sha256 == sha256_hex(bad_text.encode("utf-8"))
    assert health.decode_failures == 1
    assert health.rejected == 1


async def test_a_scalar_top_level_frame_is_also_a_decode_failure(
    store: SQLiteEventStore, clock: ReplayClock
) -> None:
    """Valid JSON that is neither an object nor an array is refused, not silently accepted."""
    frame = _frame(None, text="42")
    run_id = "run-scalar-frame"
    health = await run_capture(
        frame_source=ListFrameSource([frame]),
        store=store,
        clock=clock,
        capture_run_id=run_id,
        subscribed_token_ids=[TOKEN_YES],
    )
    assert health.decode_failures == 1
    rejections = list(store.iter_rejections(run_id))
    assert rejections[0].rejection.reason is RejectionReason.MALFORMED_PAYLOAD


async def test_an_unknown_event_type_is_counted_as_a_rejection(
    store: SQLiteEventStore, clock: ReplayClock
) -> None:
    frame = _frame(_unknown_event(event_type="last_trade_price"))
    run_id = "run-unknown-event-type"
    health = await run_capture(
        frame_source=ListFrameSource([frame]),
        store=store,
        clock=clock,
        capture_run_id=run_id,
        subscribed_token_ids=[TOKEN_YES],
    )
    rejections = list(store.iter_rejections(run_id))
    assert len(rejections) == 1
    rejection = rejections[0].rejection
    assert rejection.reason is RejectionReason.UNKNOWN_EVENT_TYPE
    assert rejection.source_event_type == "last_trade_price"
    assert "last_trade_price" in rejection.detail
    assert health.unknown_event_type == 1
    assert health.rejected == 1


async def test_an_array_frame_with_a_malformed_element_is_rejected(
    store: SQLiteEventStore, clock: ReplayClock
) -> None:
    """A frame decodes fine as JSON but one array element is not an object."""
    frame = _frame([_price_change_event(), "not-an-object"])
    run_id = "run-malformed-element"
    health = await run_capture(
        frame_source=ListFrameSource([frame]),
        store=store,
        clock=clock,
        capture_run_id=run_id,
        subscribed_token_ids=[TOKEN_YES],
    )
    assert health.events_seen == 2
    assert health.accepted == 1
    rejections = list(store.iter_rejections(run_id))
    assert len(rejections) == 1
    assert rejections[0].rejection.reason is RejectionReason.MALFORMED_PAYLOAD
    assert "not a JSON object" in rejections[0].rejection.detail


async def test_a_frame_not_about_the_configured_token_is_counted_not_applicable(
    store: SQLiteEventStore, clock: ReplayClock
) -> None:
    """Only the unsubscribed sibling has an entry: a counted non-event, not a rejection."""
    event = _price_change_event(asset_id=TOKEN_NO)
    frame = _frame(event)
    run_id = "run-not-applicable"
    health = await run_capture(
        frame_source=ListFrameSource([frame]),
        store=store,
        clock=clock,
        capture_run_id=run_id,
        subscribed_token_ids=[TOKEN_YES],
    )
    assert health.not_applicable == 1
    assert health.accepted == 0
    assert health.rejected == 0
    assert list(store.iter_deliveries(run_id)) == []
    assert list(store.iter_rejections(run_id)) == []


async def test_a_clean_run_closes_completed(store: SQLiteEventStore, clock: ReplayClock) -> None:
    run_id = "run-completed"
    await run_capture(
        frame_source=ListFrameSource([_frame(_price_change_event())]),
        store=store,
        clock=clock,
        capture_run_id=run_id,
        subscribed_token_ids=[TOKEN_YES],
    )
    record = store.get_capture_run(run_id)
    assert record is not None
    assert not record.is_open
    assert record.completion_status is CompletionStatus.COMPLETED
    assert record.ended_at is not None


async def test_an_exception_closes_failed_and_reraises(
    store: SQLiteEventStore, clock: ReplayClock
) -> None:
    class Boom(RuntimeError):
        pass

    run_id = "run-failed"
    source = RaisingFrameSource([_frame(_price_change_event())], Boom("connection exploded"))
    with pytest.raises(Boom):
        await run_capture(
            frame_source=source,
            store=store,
            clock=clock,
            capture_run_id=run_id,
            subscribed_token_ids=[TOKEN_YES],
        )
    record = store.get_capture_run(run_id)
    assert record is not None
    assert not record.is_open
    assert record.completion_status is CompletionStatus.FAILED
    # The one frame consumed before the raise is still durably recorded --
    # failure closes the run, it does not roll back what was already written.
    assert len(list(store.iter_deliveries(run_id))) == 1


async def test_a_killed_run_is_never_closed_and_appears_in_iter_open_capture_runs(
    store: SQLiteEventStore, clock: ReplayClock
) -> None:
    """A process kill runs neither of `run_capture`'s two closing branches.

    Nothing in this module can simulate a real `SIGKILL` from inside a test
    process, so this test goes straight to what `run_capture` itself relies
    on: opening a `capture_run` row and then never reaching either the
    `except`/`else` branch that closes it (exactly what a killed process
    would leave behind) is a store-level fact `iter_open_capture_runs`
    reports directly, per the module docstring's own reasoning.
    """
    run_id = "run-killed"
    store.open_capture_run(run_id, started_at=clock.now())
    open_runs = {record.capture_run_id: record for record in store.iter_open_capture_runs()}
    assert run_id in open_runs
    assert open_runs[run_id].is_open


async def test_duplicate_frames_collapse_to_one_observation_with_two_deliveries(
    store: SQLiteEventStore, clock: ReplayClock
) -> None:
    event = _price_change_event()
    frames = [_frame(event), _frame(event)]
    run_id = "run-duplicate"
    health = await run_capture(
        frame_source=ListFrameSource(frames),
        store=store,
        clock=clock,
        capture_run_id=run_id,
        subscribed_token_ids=[TOKEN_YES],
    )
    deliveries = list(store.iter_deliveries(run_id))
    assert len(deliveries) == 2
    assert deliveries[0].observation_id == deliveries[1].observation_id
    assert deliveries[0].disposition is Disposition.ACCEPTED_NEW
    assert deliveries[1].disposition is Disposition.DUPLICATE
    assert health.accepted == 1
    assert health.duplicate == 1

    # Exactly one observation row exists for the shared identity.
    envelope = store.get_observation(deliveries[0].observation_id)
    assert isinstance(envelope, ObservationEnvelopeV1)


async def test_health_counters_match_the_store_derived_capture_run_counts(
    store: SQLiteEventStore, clock: ReplayClock
) -> None:
    event = _price_change_event()
    frames = [
        _frame(event),  # accepted
        _frame(event),  # duplicate
        _frame(_unknown_event()),  # rejected: unknown event type
        _frame(None, text="not json"),  # rejected: decode failure
    ]
    run_id = "run-counters"
    health = await run_capture(
        frame_source=ListFrameSource(frames),
        store=store,
        clock=clock,
        capture_run_id=run_id,
        subscribed_token_ids=[TOKEN_YES],
    )
    counts = store.counts_for_capture_run(run_id)
    assert health.accepted == counts.accepted
    assert health.duplicate == counts.duplicate
    assert health.rejected == counts.rejected
    assert health.rejected == health.decode_failures + health.unknown_event_type


async def test_no_record_is_produced_for_an_empty_subscribed_token_set(
    store: SQLiteEventStore, clock: ReplayClock
) -> None:
    """Zero configured tokens still lets frame-level defects surface, but no fan-out happens."""
    frames = [_frame(_price_change_event()), _frame(None, text="not json")]
    run_id = "run-no-tokens"
    health = await run_capture(
        frame_source=ListFrameSource(frames),
        store=store,
        clock=clock,
        capture_run_id=run_id,
        subscribed_token_ids=[],
    )
    assert health.accepted == 0
    assert health.not_applicable == 0
    assert health.decode_failures == 1
    assert health.rejected == 1


def test_capture_health_is_a_frozen_dataclass() -> None:
    health = CaptureHealth()
    with pytest.raises(Exception):  # noqa: B017 - AttributeError or FrozenInstanceError
        health.accepted = 5  # type: ignore[misc]


# --- archived location is machine-independent (M3 blocker R4) ---------------------


async def test_the_stored_archive_location_is_relative_to_the_archive_root(
    tmp_path: Path,
) -> None:
    """The M3 readiness audit's fourth blocker.

    `run_capture` stored `str(write_raw_payload(...))`, and `write_raw_payload`
    resolves its directory, so every observation durably recorded an absolute
    filesystem path: unverified by anything, silently wrong the moment the
    capture directory moved, and different in two stores holding byte-identical
    evidence. Asserted two ways -- the value is the archive-relative layout, and
    it does not contain the root -- because "is not absolute" alone would pass
    for an empty string.
    """
    store = open_sqlite_event_store(":memory:")
    clock = ReplayClock(START)
    archive = tmp_path / "raw"

    await run_capture(
        frame_source=ListFrameSource([_frame(_price_change_event())]),
        store=store,
        clock=clock,
        capture_run_id="relative-location",
        subscribed_token_ids=[TOKEN_YES],
        raw_archive_dir=archive,
    )

    deliveries = list(store.iter_deliveries("relative-location"))
    assert deliveries, "the recorded capture must produce at least one observation"
    for delivery in deliveries:
        envelope = store.get_observation(delivery.observation_id)
        assert envelope is not None
        location = envelope.raw_payload_location
        assert location == f"clob_market_ws/{envelope.raw_payload_sha256}.raw.json"
        assert str(tmp_path) not in location
        # ...and the bytes really are there, so "relative" did not become
        # "wrong": joining the root back on finds the archived payload.
        assert (archive / location).is_file()
