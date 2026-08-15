"""Tests for `argos.domain.wsbook.WsBookSnapshotV1` and `parse_ws_book_snapshot`.

Built against the real recorded fixture
`tests/fixtures/clob/ws_market_price_change.raw.json`, not only constructed
data — per the M2 slice convention, every required property is checked at
least once against the real capture, including the two distinct wire shapes
it contains (the subscribe-time snapshot carries `tick_size` and
`last_trade_price`; the three later in-stream snapshots do not).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from argos.domain.orderbook import BookSide, OrderBookLevel
from argos.domain.wsbook import WsBookSnapshotV1, parse_ws_book_snapshot

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "clob" / "ws_market_price_change.raw.json"
)

TOKEN_YES = "34691510069031117755834214800869745291092253295564665516660269316660628637961"
CONDITION_ID = "0x94a39addec8bd24a2d03deeaa43bdee6a2b11eca403cd12495eb24132cc23173"

BOOK_MESSAGE_INDICES = (0, 16, 26, 37)
"""Every `book` event in the real fixture. Message 0 is the subscribe-time
snapshot (carries `tick_size`/`last_trade_price`); 16, 26, 37 are in-stream
snapshots (neither field)."""


# --- fixture reading, deliberately duplicated -- test modules are not a shared
# library in this repository (see tests/test_clob_price_change_ingestion.py) ---


def _load_messages() -> list[dict[str, Any]]:
    with FIXTURE_PATH.open() as handle:
        fixture: dict[str, Any] = json.load(handle, parse_float=Decimal)
    return fixture["messages"]  # type: ignore[no-any-return]


def _decode(raw: str) -> Any:
    try:
        return json.loads(raw, parse_float=Decimal)
    except json.JSONDecodeError:
        return None


def _book_event_at(index: int) -> dict[str, Any]:
    decoded = _decode(_load_messages()[index]["raw"])
    event = decoded[0] if isinstance(decoded, list) else decoded
    assert isinstance(event, dict)
    assert event.get("event_type") == "book", f"message {index} is not a book event"
    return event


def _minimal_event(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_type": "book",
        "market": CONDITION_ID,
        "asset_id": TOKEN_YES,
        "timestamp": "1786387645775",
        "hash": "57ccf97f291b9bcf0df6f674afa348f2652bd05f",
        "bids": [{"price": "0.40", "size": "10"}, {"price": "0.30", "size": "5"}],
        "asks": [{"price": "0.60", "size": "8"}, {"price": "0.70", "size": "3"}],
    }
    payload.update(overrides)
    return payload


# --- both real wire shapes parse ------------------------------------------------------


@pytest.mark.parametrize("index", BOOK_MESSAGE_INDICES)
def test_every_real_book_event_parses(index: int) -> None:
    event = _book_event_at(index)
    snapshot = parse_ws_book_snapshot(event)
    assert isinstance(snapshot, WsBookSnapshotV1)
    assert snapshot.condition_id == CONDITION_ID.lower()
    assert snapshot.asset_id == TOKEN_YES
    assert len(snapshot.bids) == 27
    assert len(snapshot.asks) == 55


def test_the_subscribe_time_snapshot_carries_tick_size_and_last_trade_price() -> None:
    snapshot = parse_ws_book_snapshot(_book_event_at(0))
    assert snapshot.tick_size == Decimal("0.01")
    assert snapshot.last_trade_price == Decimal("0.28")


@pytest.mark.parametrize("index", (16, 26, 37))
def test_every_in_stream_snapshot_omits_tick_size_and_last_trade_price(index: int) -> None:
    """Confirms the module docstring's own claim, per event, not assumed from research prose."""
    event = _book_event_at(index)
    assert "tick_size" not in event
    assert "last_trade_price" not in event
    snapshot = parse_ws_book_snapshot(event)
    assert snapshot.tick_size is None
    assert snapshot.last_trade_price is None


def test_no_real_book_event_carries_a_zero_size_level() -> None:
    """The evidence behind the module docstring's zero-size decision (point 3)."""
    for index in BOOK_MESSAGE_INDICES:
        event = _book_event_at(index)
        for side in ("bids", "asks"):
            for level in event[side]:
                assert Decimal(level["size"]) != 0


# --- wire order is never trusted -------------------------------------------------------


def test_wire_order_is_never_trusted_and_both_sides_end_up_best_level_first() -> None:
    event = _book_event_at(0)
    # Measured wire order: bids ascending, asks descending -- both end at top
    # of book, the reverse of the naive [0] reading.
    assert Decimal(event["bids"][0]["price"]) < Decimal(event["bids"][-1]["price"])
    assert Decimal(event["asks"][0]["price"]) > Decimal(event["asks"][-1]["price"])

    snapshot = parse_ws_book_snapshot(event)
    bid_prices = [level.price for level in snapshot.bids]
    ask_prices = [level.price for level in snapshot.asks]
    assert bid_prices == sorted(bid_prices, reverse=True)
    assert ask_prices == sorted(ask_prices)


def test_an_unsorted_bid_side_is_refused_rather_than_silently_re_sorted() -> None:
    with pytest.raises(ValidationError, match="descending"):
        WsBookSnapshotV1(
            condition_id=CONDITION_ID,
            asset_id=TOKEN_YES,
            bids=(
                OrderBookLevel(price=Decimal("0.3"), size=Decimal(1)),
                OrderBookLevel(price=Decimal("0.4"), size=Decimal(1)),
            ),
            asks=(),
        )


def test_a_duplicate_bid_price_level_is_refused() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        WsBookSnapshotV1(
            condition_id=CONDITION_ID,
            asset_id=TOKEN_YES,
            bids=(
                OrderBookLevel(price=Decimal("0.4"), size=Decimal(1)),
                OrderBookLevel(price=Decimal("0.4"), size=Decimal(2)),
            ),
            asks=(),
        )


# --- zero-size level: refused, matching BookState.from_wire_levels ---------------------


def test_a_zero_size_level_is_refused_not_silently_dropped() -> None:
    event = _minimal_event(bids=[{"price": "0.40", "size": "0"}])
    with pytest.raises(ValueError, match="not a resting order"):
        parse_ws_book_snapshot(event)


def test_a_negative_size_level_is_refused() -> None:
    event = _minimal_event(asks=[{"price": "0.60", "size": "-1"}])
    with pytest.raises(ValueError, match="not a resting order"):
        parse_ws_book_snapshot(event)


# --- malformed shapes ------------------------------------------------------------------


def test_a_non_object_event_is_refused() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        parse_ws_book_snapshot([1, 2, 3])  # type: ignore[arg-type]


def test_the_wrong_event_type_is_refused() -> None:
    event = _minimal_event(event_type="price_change")
    with pytest.raises(ValueError, match="event_type"):
        parse_ws_book_snapshot(event)


def test_an_unexpected_top_level_field_is_refused() -> None:
    event = _minimal_event(min_order_size="0.01")
    with pytest.raises(ValueError, match="unexpected"):
        parse_ws_book_snapshot(event)


def test_bids_not_a_list_is_refused() -> None:
    event = _minimal_event(bids="not-a-list")
    with pytest.raises(ValueError, match="'bids' must be an array"):
        parse_ws_book_snapshot(event)


def test_a_level_missing_size_is_refused() -> None:
    event = _minimal_event(bids=[{"price": "0.4"}])
    with pytest.raises(ValueError, match="missing"):
        parse_ws_book_snapshot(event)


def test_a_float_price_is_refused() -> None:
    event = _minimal_event(bids=[{"price": 0.4, "size": "10"}])
    with pytest.raises(ValueError):
        parse_ws_book_snapshot(event)


def test_a_price_outside_zero_one_is_refused() -> None:
    event = _minimal_event(bids=[{"price": "1.5", "size": "10"}])
    with pytest.raises(ValueError, match="valid \\[0, 1\\] range"):
        parse_ws_book_snapshot(event)


def test_a_malformed_condition_id_is_refused() -> None:
    with pytest.raises(ValidationError, match="condition_id"):
        WsBookSnapshotV1(condition_id="not-a-hash", asset_id=TOKEN_YES, bids=(), asks=())


def test_a_malformed_asset_id_is_refused() -> None:
    with pytest.raises(ValidationError, match="asset_id"):
        WsBookSnapshotV1(condition_id=CONDITION_ID, asset_id="not-a-token", bids=(), asks=())


# --- best_bid / best_ask -----------------------------------------------------------------


def test_best_bid_and_best_ask_read_off_the_stored_levels() -> None:
    snapshot = parse_ws_book_snapshot(_minimal_event())
    assert snapshot.best_bid == Decimal("0.4")
    assert snapshot.best_ask == Decimal("0.6")


def test_best_bid_and_best_ask_are_none_for_an_empty_side() -> None:
    snapshot = WsBookSnapshotV1(condition_id=CONDITION_ID, asset_id=TOKEN_YES, bids=(), asks=())
    assert snapshot.best_bid is None
    assert snapshot.best_ask is None


# --- BookSide reuse sanity ---------------------------------------------------------------


def test_the_shared_book_side_enum_is_used_not_a_second_vocabulary() -> None:
    assert BookSide.BID.value == "bid"
    assert BookSide.ASK.value == "ask"
