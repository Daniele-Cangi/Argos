"""Tests for `argos.projections.book`, the order-book projection.

The M2 exit criterion this file exists to close is "book snapshot plus deltas
reconstruct a tested projection". It is closed against **real recorded live
traffic**, not constructed data: the capture
`tests/fixtures/clob/ws_market_price_change.raw.json` contains four full `book`
snapshots for the subscribed token and 34 `price_change` frames, and all three
snapshot-to-snapshot transitions it spans are reconstructed here exactly, level
for level, on both sides:

    book@msg0  + 13 deltas -> book@msg16
    book@msg16 +  7 deltas -> book@msg26
    book@msg26 +  8 deltas -> book@msg37

Two facts about that capture are verified here rather than assumed from the
research note or from the slice brief:

- The four `book` events do **not** share one schema. Message 0 (delivered on
  subscribe) carries `tick_size` and `last_trade_price`; messages 16, 26 and 37
  carry neither, and none of the four carries `min_order_size` or `neg_risk`.
  `test_no_websocket_book_event_carries_the_fields_the_rest_model_requires`
  pins this, because it is the whole reason `BookState` exists separately from
  `OrderBookSnapshotV1`.
- The capture contains exactly two zero-size `price_change` entries, and only
  **one** of them belongs to the subscribed token (message 33, `BUY 0.17`); the
  other belongs to its unsubscribed binary sibling (`SELL 0.83`), for which the
  capture carries no `book` event at all, so no projection of it can be
  verified against a source snapshot.
  `test_the_capture_contains_exactly_two_zero_size_entries_one_per_token`
  pins both, and the removal is then driven through the projection.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from argos.domain.orderbook import (
    BookSide,
    OrderBookLevel,
    parse_order_book_snapshot,
)
from argos.domain.pricechange import (
    PriceChangeV1,
    PriceLevelChangeKind,
    PriceLevelChangeV1,
    parse_price_change_group,
)
from argos.projections.book import (
    BOOK_STATE_DIGEST_VERSION,
    BookProjectionAnomalyKind,
    BookState,
    OrderBookProjection,
)

# A byte-identical copy of docs/research/fixtures/clob-ws-market-2026-08-10T184742Z.json
# with a provenance sidecar, so `tests/test_fixtures.py`'s drift guard covers
# the ground truth these tests depend on. Same convention as
# `tests/test_price_change.py`.
FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "clob" / "ws_market_price_change.raw.json"
)
REST_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "clob" / "book_yes.raw.json"

TOKEN_YES = "34691510069031117755834214800869745291092253295564665516660269316660628637961"
TOKEN_NO = "95561057794427123541889915407555646439882912350845258651794843110787555977699"
CONDITION_ID = "0x94a39addec8bd24a2d03deeaa43bdee6a2b11eca403cd12495eb24132cc23173"
OTHER_CONDITION_ID = "0x" + "ab" * 32

# The three snapshot-to-snapshot transitions the capture spans, as
# (seeding book message index, closing book message index, expected number of
# delta groups for the subscribed token in between).
REAL_TRANSITIONS = ((0, 16, 13), (16, 26, 7), (26, 37, 8))

START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


# --- fixture reading --------------------------------------------------------------


def _load_messages() -> list[dict[str, Any]]:
    with FIXTURE_PATH.open() as handle:
        # parse_float=Decimal mirrors the discipline the WebSocket adapter must
        # apply when decoding a real frame; the domain models defend against a
        # float either way.
        fixture: dict[str, Any] = json.load(handle, parse_float=Decimal)
    messages: list[dict[str, Any]] = fixture["messages"]
    return messages


def _decode(raw: str) -> Any:
    """Decode one captured frame, or return None for the PING/PONG control text."""
    try:
        return json.loads(raw, parse_float=Decimal)
    except json.JSONDecodeError:
        return None


def _event_at(index: int) -> dict[str, Any]:
    """The single event carried by message `index`.

    Message 0 is a JSON *array* holding one `book` event; every later message
    is a bare object. Verified directly against the fixture rather than assumed
    from the research note's prose.
    """
    decoded = _decode(_load_messages()[index]["raw"])
    if isinstance(decoded, list):
        assert len(decoded) == 1, f"message {index} carries {len(decoded)} events, expected 1"
        first: dict[str, Any] = decoded[0]
        return first
    assert isinstance(decoded, dict), f"message {index} did not decode to an object"
    return decoded


def _event_time(event: dict[str, Any]) -> datetime:
    """The source's own millisecond `timestamp`, as a UTC instant.

    The projection is given the *event* time, never a wall clock — core
    invariant 6 and `.claude/rules/data-integrity.md` keep event time and
    arrival separate, and this module has no clock to read even if it wanted
    one.
    """
    return datetime.fromtimestamp(int(event["timestamp"]) / 1000, tz=UTC)


def _book_state_at(index: int) -> BookState:
    """Seed a `BookState` from the `book` event at message `index`."""
    event = _event_at(index)
    assert event["event_type"] == "book", f"message {index} is not a book event"
    return BookState.from_wire_levels(
        condition_id=event["market"],
        asset_id=event["asset_id"],
        bids=event["bids"],
        asks=event["asks"],
        source_asserted_hash=event["hash"],
    )


def _delta_events_between(start: int, end: int) -> list[dict[str, Any]]:
    """Every `price_change` event strictly between two message indices, in arrival order."""
    messages = _load_messages()
    events: list[dict[str, Any]] = []
    for index in range(start + 1, end):
        decoded = _decode(messages[index]["raw"])
        if isinstance(decoded, dict) and decoded.get("event_type") == "price_change":
            events.append(decoded)
    return events


_Levels = list[tuple[Decimal, Decimal]]


def _levels(state: BookState) -> tuple[_Levels, _Levels]:
    return (
        [(level.price, level.size) for level in state.bids],
        [(level.price, level.size) for level in state.asks],
    )


def _projection() -> OrderBookProjection:
    return OrderBookProjection(condition_id=CONDITION_ID, asset_id=TOKEN_YES)


def _delta(
    *changes: PriceLevelChangeV1,
    asset_id: str = TOKEN_YES,
    condition_id: str = CONDITION_ID,
) -> PriceChangeV1:
    ordered = tuple(sorted(changes, key=lambda change: (change.side.value, change.price)))
    return PriceChangeV1(condition_id=condition_id, asset_id=asset_id, changes=ordered)


def _set(side: BookSide, price: str, size: str) -> PriceLevelChangeV1:
    return PriceLevelChangeV1(
        side=side, price=Decimal(price), size=Decimal(size), kind=PriceLevelChangeKind.SET
    )


def _remove(side: BookSide, price: str) -> PriceLevelChangeV1:
    return PriceLevelChangeV1(
        side=side, price=Decimal(price), size=Decimal(0), kind=PriceLevelChangeKind.REMOVE
    )


# --- the capture's own shape ------------------------------------------------------


def test_the_capture_holds_four_book_events_for_the_subscribed_token() -> None:
    """The ground truth the three transitions below are measured against."""
    book_indices = []
    for index, message in enumerate(_load_messages()):
        decoded = _decode(message["raw"])
        events = decoded if isinstance(decoded, list) else [decoded]
        for event in events:
            if isinstance(event, dict) and event.get("event_type") == "book":
                assert event["asset_id"] == TOKEN_YES
                book_indices.append(index)
    assert book_indices == [0, 16, 26, 37]


def test_no_websocket_book_event_carries_the_fields_the_rest_model_requires() -> None:
    """Why `BookState` exists and `OrderBookSnapshotV1` is not reused as the seed.

    `OrderBookSnapshotV1` requires `tick_size`, `min_order_size`, `neg_risk`
    and `last_trade_price`. No WebSocket `book` event in the capture carries
    `min_order_size` or `neg_risk`, and the three *in-stream* ones carry
    neither `tick_size` nor `last_trade_price` either — so a WS book event
    cannot be parsed into that model, and weakening the model to fit would
    degrade a shipped REST contract for a WebSocket quirk.
    """
    initial = _event_at(0)
    assert set(initial) == {
        "asks",
        "asset_id",
        "bids",
        "event_type",
        "hash",
        "last_trade_price",
        "market",
        "tick_size",
        "timestamp",
    }
    for index in (16, 26, 37):
        assert set(_event_at(index)) == {
            "asks",
            "asset_id",
            "bids",
            "event_type",
            "hash",
            "market",
            "timestamp",
        }, f"message {index} changed shape"


def test_the_capture_contains_exactly_two_zero_size_entries_one_per_token() -> None:
    """The live evidence for "zero-size level update is represented as removal".

    Recorded precisely rather than rounded up: two zero-size entries exist in
    the whole capture, but only one belongs to the *subscribed* token. The
    other belongs to the unsubscribed binary sibling, whose book the capture
    never carries a snapshot of — so "it removed a level that was present"
    cannot be verified against a source snapshot for that one, and this suite
    does not claim it can.
    """
    found = []
    for index, message in enumerate(_load_messages()):
        decoded = _decode(message["raw"])
        if not isinstance(decoded, dict) or decoded.get("event_type") != "price_change":
            continue
        for entry in decoded["price_changes"]:
            if Decimal(entry["size"]) == 0:
                found.append((index, entry["asset_id"], entry["side"], entry["price"]))
    assert found == [
        (33, TOKEN_YES, "BUY", "0.17"),
        (33, TOKEN_NO, "SELL", "0.83"),
    ]


# --- the criterion: snapshot + deltas reconstruct the next snapshot ---------------


@pytest.mark.parametrize(("start", "end", "expected_deltas"), REAL_TRANSITIONS)
def test_a_real_snapshot_plus_real_deltas_reconstructs_the_next_real_snapshot(
    start: int, end: int, expected_deltas: int
) -> None:
    """The M2 exit criterion, on real live traffic, for all three transitions.

    Asserted level for level on both sides, not by digest alone: a digest
    comparison could in principle agree for a reason other than the books being
    equal, and this criterion is the one thing in the milestone that must not
    be shown by a hash.
    """
    projection = _projection()
    seed = _book_state_at(start)
    projection.apply_snapshot(seed, event_time=_event_time(_event_at(start)))

    applied = 0
    for event in _delta_events_between(start, end):
        group = parse_price_change_group(event, asset_id=TOKEN_YES)
        anomalies = projection.apply_delta(
            group.payload,
            event_time=_event_time(event),
            source_asserted_hash=group.entry_hash,
        )
        assert [a.kind for a in anomalies] == [], f"unexpected anomaly applying {event}"
        applied += 1
    assert applied == expected_deltas

    expected = _book_state_at(end)
    projected_bids, projected_asks = _levels(projection.state())
    expected_bids, expected_asks = _levels(expected)
    assert projected_bids == expected_bids
    assert projected_asks == expected_asks
    assert projection.digest() == expected.digest()


@pytest.mark.parametrize(("start", "end", "expected_deltas"), REAL_TRANSITIONS)
def test_the_closing_real_snapshot_reports_no_divergence_through_the_projection(
    start: int, end: int, expected_deltas: int
) -> None:
    """The same three transitions, checked by the projection's own divergence detector.

    This is the operator-facing form of the criterion: applying the closing
    snapshot on top of the delta-projected state records **no**
    `SNAPSHOT_DISAGREES_WITH_PROJECTION` anomaly. It is the only divergence
    signal ARGOS has, since it cannot compute the source's book hash, so it is
    worth exercising positively rather than only in the negative test below.
    """
    projection = _projection()
    projection.apply_snapshot(_book_state_at(start), event_time=_event_time(_event_at(start)))
    for event in _delta_events_between(start, end):
        group = parse_price_change_group(event, asset_id=TOKEN_YES)
        projection.apply_delta(group.payload, event_time=_event_time(event))

    closing = _event_at(end)
    anomalies = projection.apply_snapshot(_book_state_at(end), event_time=_event_time(closing))

    assert anomalies == ()
    assert (
        projection.anomaly_counts[BookProjectionAnomalyKind.SNAPSHOT_DISAGREES_WITH_PROJECTION] == 0
    )
    assert projection.applied_snapshot_count == 2
    assert projection.applied_delta_count == expected_deltas


def test_all_three_transitions_chain_into_one_continuous_projection() -> None:
    """The whole capture as one run: seed once, apply 28 real deltas, end at book@37.

    Stronger than the three transitions taken separately, because it never
    re-seeds from an intermediate snapshot. If any delta were applied wrongly,
    a later snapshot could otherwise paper over it.
    """
    projection = _projection()
    projection.apply_snapshot(_book_state_at(0), event_time=_event_time(_event_at(0)))
    for event in _delta_events_between(0, 37):
        group = parse_price_change_group(event, asset_id=TOKEN_YES)
        projection.apply_delta(group.payload, event_time=_event_time(event))

    assert projection.applied_delta_count == 28
    assert _levels(projection.state()) == _levels(_book_state_at(37))
    assert projection.digest() == _book_state_at(37).digest()


def test_the_real_zero_size_entry_removes_a_level_that_was_present() -> None:
    """The M2 criterion "zero-size level update is represented as removal", end to end.

    Driven through the projection on the one real zero-size entry belonging to
    the subscribed token (message 33, `BUY 0.17`): the level is present before,
    the change is typed `REMOVE`, the level is gone after, and no
    `REMOVE_OF_ABSENT_LEVEL` anomaly is recorded — that is what makes it a
    removal of something real rather than a no-op.
    """
    projection = _projection()
    projection.apply_snapshot(_book_state_at(26), event_time=_event_time(_event_at(26)))

    seen_removal = False
    for event in _delta_events_between(26, 37):
        group = parse_price_change_group(event, asset_id=TOKEN_YES)
        removals = [
            change for change in group.payload.changes if change.kind is PriceLevelChangeKind.REMOVE
        ]
        if removals:
            assert [(c.side, c.price) for c in removals] == [(BookSide.BID, Decimal("0.17"))]
            before = {level.price for level in projection.state().bids}
            assert Decimal("0.17") in before
            anomalies = projection.apply_delta(group.payload, event_time=_event_time(event))
            assert anomalies == ()
            assert Decimal("0.17") not in {level.price for level in projection.state().bids}
            seen_removal = True
        else:
            projection.apply_delta(group.payload, event_time=_event_time(event))

    assert seen_removal, "the capture's real zero-size removal was never reached"
    assert projection.anomaly_counts[BookProjectionAnomalyKind.REMOVE_OF_ABSENT_LEVEL] == 0


def test_the_rest_snapshot_fixture_seeds_the_projection_through_the_convenience_path() -> None:
    """`BookState.from_order_book_snapshot` on the real recorded REST `/book` body."""
    with REST_FIXTURE_PATH.open() as handle:
        body = json.load(handle, parse_float=Decimal)
    snapshot = parse_order_book_snapshot(body)

    projection = OrderBookProjection(condition_id=snapshot.condition_id, asset_id=snapshot.asset_id)
    projection.apply_snapshot(
        BookState.from_order_book_snapshot(snapshot, source_asserted_hash=body["hash"]),
        event_time=START,
    )

    assert projection.is_seeded
    assert _levels(projection.state()) == (
        [(level.price, level.size) for level in snapshot.bids],
        [(level.price, level.size) for level in snapshot.asks],
    )
    assert projection.state().best_bid == snapshot.best_bid
    assert projection.state().best_ask == snapshot.best_ask
    assert projection.source_asserted_hash == body["hash"]


# --- refusals and counted anomalies -----------------------------------------------


def test_a_delta_for_another_token_is_refused_and_never_applied() -> None:
    """Core: one token's deltas must never touch another token's book.

    The research note found a single real frame carrying entries for an
    unsubscribed sibling, so this is a live-traffic property, not a defensive
    one.
    """
    projection = _projection()
    projection.apply_snapshot(_book_state_at(0), event_time=_event_time(_event_at(0)))
    before = projection.digest()

    anomalies = projection.apply_delta(
        _delta(_set(BookSide.BID, "0.05", "1000"), asset_id=TOKEN_NO),
        event_time=START,
    )

    assert [a.kind for a in anomalies] == [BookProjectionAnomalyKind.ASSET_ID_MISMATCH]
    assert projection.digest() == before
    assert projection.applied_delta_count == 0
    assert projection.refused_input_count == 1


def test_a_delta_for_another_market_on_the_same_token_is_refused() -> None:
    """A condition mismatch is a distinct, separately counted refusal."""
    projection = _projection()
    projection.apply_snapshot(_book_state_at(0), event_time=_event_time(_event_at(0)))
    before = projection.digest()

    anomalies = projection.apply_delta(
        _delta(_set(BookSide.ASK, "0.55", "10"), condition_id=OTHER_CONDITION_ID),
        event_time=START,
    )

    assert [a.kind for a in anomalies] == [BookProjectionAnomalyKind.CONDITION_ID_MISMATCH]
    assert projection.digest() == before
    assert projection.refused_input_count == 1


def test_a_snapshot_for_another_token_is_refused_and_leaves_the_projection_unseeded() -> None:
    """The scope check guards seeding too, not only deltas."""
    projection = _projection()
    foreign = BookState(
        condition_id=CONDITION_ID,
        asset_id=TOKEN_NO,
        bids=(OrderBookLevel(price=Decimal("0.4"), size=Decimal("10")),),
        asks=(),
    )

    anomalies = projection.apply_snapshot(foreign, event_time=START)

    assert [a.kind for a in anomalies] == [BookProjectionAnomalyKind.ASSET_ID_MISMATCH]
    assert not projection.is_seeded
    assert projection.applied_snapshot_count == 0


def test_a_delta_before_any_snapshot_is_counted_not_applied_and_does_not_crash() -> None:
    """Real: the sibling token receives deltas and never a `book` event.

    Driven with the sibling token's own real entries from the capture, parsed
    through the same parser, into a projection scoped to that token. Nothing is
    applied and nothing raises.
    """
    projection = OrderBookProjection(condition_id=CONDITION_ID, asset_id=TOKEN_NO)

    counted = 0
    for event in _delta_events_between(0, 16):
        group = parse_price_change_group(event, asset_id=TOKEN_NO)
        anomalies = projection.apply_delta(group.payload, event_time=_event_time(event))
        assert [a.kind for a in anomalies] == [BookProjectionAnomalyKind.DELTA_BEFORE_SNAPSHOT]
        counted += 1

    assert counted == 13
    assert not projection.is_seeded
    assert projection.applied_delta_count == 0
    assert projection.applied_level_change_count == 0
    assert projection.refused_input_count == 13
    assert projection.state().bids == ()
    assert projection.state().asks == ()
    assert projection.last_event_time is None


def test_removing_a_level_that_is_not_present_is_counted_and_leaves_the_state_unchanged() -> None:
    projection = _projection()
    projection.apply_snapshot(
        BookState(
            condition_id=CONDITION_ID,
            asset_id=TOKEN_YES,
            bids=(OrderBookLevel(price=Decimal("0.4"), size=Decimal("10")),),
            asks=(),
        ),
        event_time=START,
    )
    before = projection.digest()

    anomalies = projection.apply_delta(_delta(_remove(BookSide.BID, "0.39")), event_time=START)

    assert [a.kind for a in anomalies] == [BookProjectionAnomalyKind.REMOVE_OF_ABSENT_LEVEL]
    assert anomalies[0].side is BookSide.BID
    assert anomalies[0].price == Decimal("0.39")
    assert projection.digest() == before
    # Counted, but the delta itself was applied: the source's statement about
    # the resulting book is honoured, only the missing level is reported.
    assert projection.applied_delta_count == 1


def test_an_event_time_regression_is_counted_and_the_delta_is_still_applied() -> None:
    """M2 applies in arrival order and counts a regression. This is not a late-event policy."""
    projection = _projection()
    projection.apply_snapshot(
        BookState(condition_id=CONDITION_ID, asset_id=TOKEN_YES, bids=(), asks=()),
        event_time=START,
    )

    anomalies = projection.apply_delta(
        _delta(_set(BookSide.BID, "0.4", "10")), event_time=START - timedelta(seconds=1)
    )

    assert [a.kind for a in anomalies] == [BookProjectionAnomalyKind.EVENT_TIME_REGRESSION]
    assert [(level.price, level.size) for level in projection.state().bids] == [
        (Decimal("0.4"), Decimal(10))
    ]
    assert projection.last_event_time == START - timedelta(seconds=1)
    assert projection.refused_input_count == 0


def test_an_equal_event_time_is_not_a_regression() -> None:
    """One logical transition really does span frames sharing a `(timestamp, hash)` pair."""
    projection = _projection()
    projection.apply_snapshot(
        BookState(condition_id=CONDITION_ID, asset_id=TOKEN_YES, bids=(), asks=()),
        event_time=START,
    )

    anomalies = projection.apply_delta(_delta(_set(BookSide.BID, "0.4", "10")), event_time=START)

    assert anomalies == ()


def test_a_disagreeing_snapshot_is_reported_and_still_wins() -> None:
    """The only divergence detector ARGOS has, exercised in the negative."""
    projection = _projection()
    projection.apply_snapshot(
        BookState(
            condition_id=CONDITION_ID,
            asset_id=TOKEN_YES,
            bids=(OrderBookLevel(price=Decimal("0.4"), size=Decimal("10")),),
            asks=(),
        ),
        event_time=START,
    )

    truth = BookState(
        condition_id=CONDITION_ID,
        asset_id=TOKEN_YES,
        bids=(OrderBookLevel(price=Decimal("0.4"), size=Decimal("99")),),
        asks=(OrderBookLevel(price=Decimal("0.6"), size=Decimal("5")),),
    )
    anomalies = projection.apply_snapshot(truth, event_time=START + timedelta(seconds=1))

    assert [a.kind for a in anomalies] == [
        BookProjectionAnomalyKind.SNAPSHOT_DISAGREES_WITH_PROJECTION
    ]
    assert "0.4: projected 10 vs snapshot 99" in anomalies[0].detail
    assert projection.digest() == truth.digest()


def test_anomaly_counts_report_every_kind_including_the_zeros() -> None:
    """An absent counter and a zero counter read identically in a report."""
    projection = _projection()
    counts = projection.anomaly_counts
    assert set(counts) == set(BookProjectionAnomalyKind)
    assert set(counts.values()) == {0}


def test_the_source_asserted_hash_is_carried_and_never_recomputed() -> None:
    """ARGOS cannot compute the source's book hash; it must not appear to.

    The projection's own digest and the source's asserted hash are unrelated
    values over the same state, and this pins that they are never conflated.
    """
    projection = _projection()
    seed = _book_state_at(0)
    projection.apply_snapshot(seed, event_time=_event_time(_event_at(0)))

    assert seed.source_asserted_hash == "57ccf97f291b9bcf0df6f674afa348f2652bd05f"
    assert projection.source_asserted_hash == seed.source_asserted_hash
    assert projection.digest() != seed.source_asserted_hash

    # A delta supplying a new asserted hash replaces it; a delta supplying none
    # leaves the previous claim in place rather than inventing one.
    projection.apply_delta(
        _delta(_set(BookSide.BID, "0.05", "1")), event_time=START, source_asserted_hash="deadbeef"
    )
    assert projection.source_asserted_hash == "deadbeef"
    projection.apply_delta(_delta(_set(BookSide.BID, "0.06", "1")), event_time=START)
    assert projection.source_asserted_hash == "deadbeef"


# --- determinism ------------------------------------------------------------------


def test_the_same_inputs_produce_the_same_digest() -> None:
    digests = set()
    for _ in range(3):
        projection = _projection()
        projection.apply_snapshot(_book_state_at(0), event_time=_event_time(_event_at(0)))
        for event in _delta_events_between(0, 16):
            group = parse_price_change_group(event, asset_id=TOKEN_YES)
            projection.apply_delta(group.payload, event_time=_event_time(event))
        digests.add(projection.digest())
    assert len(digests) == 1


def test_a_genuinely_different_book_produces_a_different_digest() -> None:
    projection = _projection()
    projection.apply_snapshot(_book_state_at(0), event_time=_event_time(_event_at(0)))
    before = projection.digest()

    projection.apply_delta(_delta(_set(BookSide.BID, "0.01", "420946.16")), event_time=START)

    assert projection.digest() != before


def test_the_digest_is_insensitive_to_the_order_levels_were_inserted_in() -> None:
    """Two routes to one book state must agree — the property M3 golden replay needs."""
    forward = _projection()
    forward.apply_snapshot(
        BookState(condition_id=CONDITION_ID, asset_id=TOKEN_YES, bids=(), asks=()),
        event_time=START,
    )
    for price in ("0.10", "0.20", "0.30"):
        forward.apply_delta(_delta(_set(BookSide.BID, price, "5")), event_time=START)

    backward = _projection()
    backward.apply_snapshot(
        BookState(condition_id=CONDITION_ID, asset_id=TOKEN_YES, bids=(), asks=()),
        event_time=START,
    )
    for price in ("0.30", "0.20", "0.10"):
        backward.apply_delta(_delta(_set(BookSide.BID, price, "5")), event_time=START)

    assert forward.digest() == backward.digest()


def test_a_snapshot_and_the_delta_route_to_the_same_state_agree() -> None:
    """Route independence on real data: book@16 direct versus book@0 plus 13 deltas."""
    projected = _projection()
    projected.apply_snapshot(_book_state_at(0), event_time=_event_time(_event_at(0)))
    for event in _delta_events_between(0, 16):
        group = parse_price_change_group(event, asset_id=TOKEN_YES)
        projected.apply_delta(group.payload, event_time=_event_time(event))

    direct = _projection()
    direct.apply_snapshot(_book_state_at(16), event_time=_event_time(_event_at(16)))

    assert projected.digest() == direct.digest()


def test_the_digest_covers_the_book_and_nothing_else() -> None:
    """Event times, anomaly history, and the source's asserted hash must not leak in.

    Otherwise two runs that reached the same book by different routes — exactly
    what live and replay do — could disagree on a hash that is supposed to
    identify the state.
    """
    plain = _projection()
    plain.apply_snapshot(
        BookState(
            condition_id=CONDITION_ID,
            asset_id=TOKEN_YES,
            bids=(OrderBookLevel(price=Decimal("0.4"), size=Decimal("10")),),
            asks=(),
        ),
        event_time=START,
    )

    eventful = _projection()
    eventful.apply_snapshot(
        BookState(
            condition_id=CONDITION_ID,
            asset_id=TOKEN_YES,
            bids=(OrderBookLevel(price=Decimal("0.4"), size=Decimal("10")),),
            asks=(),
            source_asserted_hash="a" * 40,
        ),
        event_time=START + timedelta(days=400),
    )
    eventful.apply_delta(_delta(_remove(BookSide.ASK, "0.9")), event_time=START)

    assert eventful.anomalies != ()
    assert eventful.digest() == plain.digest()


def test_the_digest_distinguishes_the_token_a_state_belongs_to() -> None:
    """Identical levels under two tokens are two different states, not one."""
    levels = (OrderBookLevel(price=Decimal("0.4"), size=Decimal("10")),)
    yes = BookState(condition_id=CONDITION_ID, asset_id=TOKEN_YES, bids=levels, asks=())
    no = BookState(condition_id=CONDITION_ID, asset_id=TOKEN_NO, bids=levels, asks=())
    assert yes.digest() != no.digest()


def test_the_digest_version_is_part_of_the_hashed_material() -> None:
    """A pinned golden digest must move visibly if the encoding ever changes."""
    assert BOOK_STATE_DIGEST_VERSION == "book_state_digest.v1"
    empty = BookState(condition_id=CONDITION_ID, asset_id=TOKEN_YES, bids=(), asks=())
    # Pinned literal: if this changes without BOOK_STATE_DIGEST_VERSION
    # changing, the encoding drifted silently and every M3 golden replay hash
    # would move for a reason nobody recorded.
    assert empty.digest() == "a15412bb2d40d2139cd64c1be090e5b8346c15d72ccf841c137dd63dceb6f2e8"


def test_the_real_reconstructed_book_state_has_a_pinned_golden_digest() -> None:
    """The first golden replay anchor: book@msg0 plus all 28 real deltas.

    A literal, so a future change to the projection, to the digest encoding, or
    to decimal canonicalization cannot silently move the state hash that M3
    replay determinism will be measured against. The digest is ARGOS's own and
    is unrelated to the source's asserted hash for the same state, which is
    asserted alongside it here precisely so the two can never be confused.
    """
    projection = _projection()
    projection.apply_snapshot(_book_state_at(0), event_time=_event_time(_event_at(0)))
    for event in _delta_events_between(0, 37):
        group = parse_price_change_group(event, asset_id=TOKEN_YES)
        projection.apply_delta(
            group.payload, event_time=_event_time(event), source_asserted_hash=group.entry_hash
        )

    assert projection.digest() == "fc5b2a46ffc44fecdc6616aab5ab14939ef472ca705d7fb62a15612dc4a88ec9"
    assert projection.source_asserted_hash == "e7fa398119f1c26fff077d6f850a07f55348b321"


# --- re-seeding -------------------------------------------------------------------


def test_a_snapshot_reseed_discards_prior_state_wholesale_rather_than_merging() -> None:
    """A full book is a complete statement; a merge would keep phantom levels forever."""
    projection = _projection()
    projection.apply_snapshot(
        BookState(
            condition_id=CONDITION_ID,
            asset_id=TOKEN_YES,
            bids=(
                OrderBookLevel(price=Decimal("0.4"), size=Decimal("10")),
                OrderBookLevel(price=Decimal("0.3"), size=Decimal("7")),
            ),
            asks=(OrderBookLevel(price=Decimal("0.6"), size=Decimal("5")),),
        ),
        event_time=START,
    )

    replacement = BookState(
        condition_id=CONDITION_ID,
        asset_id=TOKEN_YES,
        bids=(OrderBookLevel(price=Decimal("0.2"), size=Decimal("1")),),
        asks=(),
    )
    projection.apply_snapshot(replacement, event_time=START + timedelta(seconds=1))

    assert _levels(projection.state()) == ([(Decimal("0.2"), Decimal(1))], [])
    assert projection.digest() == replacement.digest()


def test_reseeding_from_a_real_snapshot_discards_a_deliberately_corrupted_state() -> None:
    """The same property on real data, with a level the closing snapshot does not hold."""
    projection = _projection()
    projection.apply_snapshot(_book_state_at(26), event_time=_event_time(_event_at(26)))
    projection.apply_delta(_delta(_set(BookSide.BID, "0.005", "1")), event_time=START)
    assert Decimal("0.005") in {level.price for level in projection.state().bids}

    projection.apply_snapshot(_book_state_at(37), event_time=_event_time(_event_at(37)))

    assert Decimal("0.005") not in {level.price for level in projection.state().bids}
    assert _levels(projection.state()) == _levels(_book_state_at(37))


# --- BookState construction -------------------------------------------------------


def test_wire_order_is_never_trusted_and_both_sides_end_up_best_level_first() -> None:
    """Measured wire order is bids ascending / asks descending — the reverse of `[0]`."""
    event = _event_at(0)
    assert [Decimal(level["price"]) for level in event["bids"]] == sorted(
        Decimal(level["price"]) for level in event["bids"]
    )
    state = _book_state_at(0)
    assert state.bids[0].price == max(level.price for level in state.bids)
    assert state.asks[0].price == min(level.price for level in state.asks)
    assert state.best_bid == Decimal("0.28")
    assert state.best_ask == Decimal("0.29")


def test_an_unsorted_book_state_is_refused_rather_than_silently_re_sorted() -> None:
    with pytest.raises(ValueError, match="must be sorted descending"):
        BookState(
            condition_id=CONDITION_ID,
            asset_id=TOKEN_YES,
            bids=(
                OrderBookLevel(price=Decimal("0.3"), size=Decimal("1")),
                OrderBookLevel(price=Decimal("0.4"), size=Decimal("1")),
            ),
            asks=(),
        )


def test_a_duplicate_price_level_in_a_book_state_is_refused() -> None:
    with pytest.raises(ValueError, match="duplicate bid price level"):
        BookState(
            condition_id=CONDITION_ID,
            asset_id=TOKEN_YES,
            bids=(
                OrderBookLevel(price=Decimal("0.3"), size=Decimal("1")),
                OrderBookLevel(price=Decimal("0.3"), size=Decimal("2")),
            ),
            asks=(),
        )


def test_a_zero_size_level_in_a_snapshot_is_refused_loudly() -> None:
    """Never observed in any recorded snapshot; refusing beats an uncounted silent drop."""
    with pytest.raises(ValueError, match="not a resting order"):
        BookState.from_wire_levels(
            condition_id=CONDITION_ID,
            asset_id=TOKEN_YES,
            bids=[{"price": "0.3", "size": "0"}],
            asks=[],
        )


def test_a_float_price_in_a_wire_level_is_refused() -> None:
    """Inherited from `parse_wire_decimal`, not reimplemented here."""
    with pytest.raises(ValueError, match="float is not permitted"):
        BookState.from_wire_levels(
            condition_id=CONDITION_ID,
            asset_id=TOKEN_YES,
            bids=[{"price": 0.3, "size": "1"}],
            asks=[],
        )


def test_a_projection_scoped_to_a_malformed_token_id_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="asset_id must be a decimal token id"):
        OrderBookProjection(condition_id=CONDITION_ID, asset_id="../../admin")


def test_the_same_price_spelled_at_two_precisions_is_one_level() -> None:
    """The decimal-identity class this repository has already hit three times.

    Not reimplemented here: the collapse comes from
    `argos.domain.orderbook.parse_wire_decimal`, and this test exists to prove
    the projection inherits it rather than reintroducing a second spelling of
    the same price as a second level.
    """
    projection = _projection()
    projection.apply_snapshot(
        BookState(condition_id=CONDITION_ID, asset_id=TOKEN_YES, bids=(), asks=()),
        event_time=START,
    )
    projection.apply_delta(_delta(_set(BookSide.BID, "0.430", "7.0")), event_time=START)
    projection.apply_delta(_delta(_set(BookSide.BID, "0.43", "7")), event_time=START)

    assert _levels(projection.state()) == ([(Decimal("0.43"), Decimal(7))], [])

    other = _projection()
    other.apply_snapshot(
        BookState(
            condition_id=CONDITION_ID,
            asset_id=TOKEN_YES,
            bids=(OrderBookLevel(price=Decimal("0.43"), size=Decimal("7")),),
            asks=(),
        ),
        event_time=START,
    )
    assert projection.digest() == other.digest()
