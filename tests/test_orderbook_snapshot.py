"""Tests for `OrderBookSnapshotV1` and `parse_order_book_snapshot`.

Built from the measured evidence in `docs/research/m2-clob-rest-book.md` and the
real recorded fixture `docs/research/fixtures/clob-book-yes-2026-08-10T181007Z.json`,
not only from synthetic data — per the M2 slice instructions, at least one test
per required property runs against the real payload.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from argos.domain.observation import (
    ObservationSource,
    build_observation_envelope,
)
from argos.domain.orderbook import (
    CANONICAL_DECIMAL_CONTEXT,
    MAX_DECIMAL_EXPONENT,
    MIN_DECIMAL_EXPONENT,
    BookSide,
    OrderBookAnomaly,
    OrderBookAnomalyKind,
    OrderBookLevel,
    OrderBookSnapshotV1,
    parse_order_book_snapshot,
    parse_wire_decimal,
)
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.errors import SchemaVersionError

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "research"
    / "fixtures"
    / "clob-book-yes-2026-08-10T181007Z.json"
)

START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _load_fixture() -> dict[str, Any]:
    with FIXTURE_PATH.open() as handle:
        # parse_float=Decimal mirrors the discipline the adapter slice must
        # apply when it decodes the real HTTP response text; parse_order_book_snapshot
        # itself does not assume this was done (see the float-rejection tests).
        return json.load(handle, parse_float=Decimal)  # type: ignore[no-any-return]


def _fixture_snapshot() -> OrderBookSnapshotV1:
    return parse_order_book_snapshot(_load_fixture())


def _minimal_wire(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "market": "0x" + "a" * 64,
        "asset_id": "123456789",
        "timestamp": "1786385407185",
        "hash": "4f5acf63ca0bba3aad4d9b888c6a05614ce9cf7a",
        "bids": [{"price": "0.40", "size": "10"}, {"price": "0.41", "size": "5"}],
        "asks": [{"price": "0.43", "size": "5"}, {"price": "0.44", "size": "10"}],
        "min_order_size": "5",
        "tick_size": "0.01",
        "neg_risk": True,
        "last_trade_price": "0.42",
    }
    payload.update(overrides)
    return payload


def _provenance(raw: bytes) -> SourceProvenanceV1:
    return SourceProvenanceV1(
        source="clob_rest",
        endpoint="/book",
        http_status=200,
        retrieved_at=START,
        raw_sha256=sha256_hex(raw),
        byte_length=len(raw),
    )


# --- real fixture: parsing succeeds and carries the right shape ---------------------


def test_the_real_fixture_parses() -> None:
    snapshot = _fixture_snapshot()
    assert snapshot.condition_id == (
        "0x876506d8b2bd7a0d3fa4fe18c024eee6e1dd81ee24c26795dadd6cfe4a7b5d0d"
    )
    assert snapshot.asset_id == (
        "63842529068710005716169325380315470359047749786610778647370693404952498013178"
    )
    assert snapshot.neg_risk is True
    assert snapshot.tick_size == Decimal("0.01")
    assert snapshot.min_order_size == Decimal("5")
    assert len(snapshot.bids) == 41
    assert len(snapshot.asks) == 45
    assert snapshot.anomalies == ()


def test_the_real_fixture_has_no_timestamp_or_hash_field() -> None:
    """Those name event time / source hash and belong on the envelope, not here."""
    assert not hasattr(OrderBookSnapshotV1, "timestamp")
    assert not hasattr(OrderBookSnapshotV1, "hash")
    assert "timestamp" not in OrderBookSnapshotV1.model_fields
    assert "hash" not in OrderBookSnapshotV1.model_fields


# --- level ordering: the counter-intuitive, measured wire order ---------------------


def test_best_bid_and_best_ask_match_the_researched_values() -> None:
    """docs/research/m2-clob-rest-book.md: book not crossed, best bid 0.42 < best ask 0.43."""
    snapshot = _fixture_snapshot()
    assert snapshot.best_bid == Decimal("0.42")
    assert snapshot.best_ask == Decimal("0.43")
    assert snapshot.spread == Decimal("0.01")


def test_a_naive_index_zero_reading_of_the_wire_arrays_would_be_catastrophically_wrong() -> None:
    """Regression for the exact failure the research doc warns about.

    The wire order is bids ascending / asks descending, so `bids[0]`/`asks[0]`
    read the two extreme ends of the book: a naive reader would compute a
    spread of 0.98 instead of 0.01 while looking entirely plausible. This
    module's sorted output must not reproduce that.
    """
    raw = _load_fixture()
    naive_bid = Decimal(raw["bids"][0]["price"])
    naive_ask = Decimal(raw["asks"][0]["price"])
    naive_spread = naive_ask - naive_bid
    assert naive_spread == Decimal("0.98")

    snapshot = parse_order_book_snapshot(raw)
    assert snapshot.spread != naive_spread
    assert snapshot.spread == Decimal("0.01")


def test_bids_are_sorted_descending_best_first() -> None:
    snapshot = _fixture_snapshot()
    prices = [level.price for level in snapshot.bids]
    assert prices == sorted(prices, reverse=True)
    assert prices[0] == Decimal("0.42")
    assert prices[-1] == Decimal("0.01")


def test_asks_are_sorted_ascending_best_first() -> None:
    snapshot = _fixture_snapshot()
    prices = [level.price for level in snapshot.asks]
    assert prices == sorted(prices)
    assert prices[0] == Decimal("0.43")
    assert prices[-1] == Decimal("0.99")


def test_wire_order_is_not_trusted_even_when_it_happens_to_be_sorted() -> None:
    """Levels supplied already in canonical order still go through the same sort/validate path."""
    wire = _minimal_wire(
        bids=[{"price": "0.41", "size": "5"}, {"price": "0.40", "size": "10"}],
        asks=[{"price": "0.43", "size": "5"}, {"price": "0.44", "size": "10"}],
    )
    snapshot = parse_order_book_snapshot(wire)
    assert [level.price for level in snapshot.bids] == [Decimal("0.41"), Decimal("0.40")]


def test_a_directly_constructed_snapshot_with_unsorted_bids_is_refused_not_resorted() -> None:
    """No silent coercion: unsorted input is refused, not corrected on the caller's behalf."""
    with pytest.raises(ValidationError):
        OrderBookSnapshotV1(
            condition_id="0x" + "a" * 64,
            asset_id="123",
            bids=(
                OrderBookLevel(price=Decimal("0.40"), size=Decimal("1")),
                OrderBookLevel(price=Decimal("0.41"), size=Decimal("1")),
            ),
            asks=(),
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("5"),
            neg_risk=True,
            last_trade_price=Decimal("0.4"),
        )


def test_a_directly_constructed_snapshot_with_unsorted_asks_is_refused() -> None:
    with pytest.raises(ValidationError):
        OrderBookSnapshotV1(
            condition_id="0x" + "a" * 64,
            asset_id="123",
            bids=(),
            asks=(
                OrderBookLevel(price=Decimal("0.44"), size=Decimal("1")),
                OrderBookLevel(price=Decimal("0.43"), size=Decimal("1")),
            ),
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("5"),
            neg_risk=True,
            last_trade_price=Decimal("0.4"),
        )


# --- decimal scale normalization: the single most important property ----------------


def test_the_real_fixtures_last_trade_price_normalizes_away_its_trailing_zero() -> None:
    """The wire text is "0.430"; the same price the dedicated endpoint spells "0.43"."""
    raw = _load_fixture()
    assert raw["last_trade_price"] == "0.430"
    assert Decimal(raw["last_trade_price"]) == Decimal("0.430")
    snapshot = parse_order_book_snapshot(raw)
    assert snapshot.last_trade_price == Decimal("0.43")
    assert str(snapshot.last_trade_price) == "0.43"


def test_the_same_price_at_two_text_precisions_produces_identical_canonical_json() -> None:
    """ADR-0010: identity is derived from the canonical payload hash.

    `Decimal("0.430")` and `Decimal("0.43")` must serialize identically or a
    cosmetic reformat upstream mints a new observation identity for the same
    price.
    """
    trailing_zero = parse_order_book_snapshot(_minimal_wire(last_trade_price="0.430"))
    minimal = parse_order_book_snapshot(_minimal_wire(last_trade_price="0.43"))
    assert trailing_zero.last_trade_price == minimal.last_trade_price
    assert trailing_zero.to_record() == minimal.to_record()
    assert json.dumps(trailing_zero.to_record(), sort_keys=True) == json.dumps(
        minimal.to_record(), sort_keys=True
    )


def test_decimal_normalization_does_not_use_scientific_notation_for_integral_sizes() -> None:
    """`Decimal("500").normalize()` alone collapses to `Decimal("5E+2")` — a second,
    different-looking canonical text for the same integer. Must not leak into storage."""
    snapshot = parse_order_book_snapshot(
        _minimal_wire(bids=[{"price": "0.40", "size": "500"}], asks=[])
    )
    assert snapshot.bids[0].size == Decimal("500")
    assert str(snapshot.bids[0].size) == "500"
    assert "E" not in str(snapshot.bids[0].size)
    record = snapshot.to_record()
    assert "E" not in json.dumps(record)


def test_observation_identity_collapses_across_a_cosmetic_decimal_reformat() -> None:
    """Direct ADR-0010 consequence: two payloads differing only in price text
    precision must mint the *same* observation_id, or a cosmetic reformat
    upstream would silently double-count one observation as two."""
    raw_bytes = b"{}"
    provenance = _provenance(raw_bytes)
    trailing_zero = parse_order_book_snapshot(_minimal_wire(last_trade_price="0.430"))
    minimal = parse_order_book_snapshot(_minimal_wire(last_trade_price="0.43"))

    envelope_a = build_observation_envelope(
        source=ObservationSource.CLOB_REST,
        source_event_type="book",
        condition_id=trailing_zero.condition_id,
        token_id=trailing_zero.asset_id,
        event_time=START,
        received_time=START,
        ingest_sequence=1,
        payload=trailing_zero,
        provenance=provenance,
        parser_version="test-parser-1",
        capture_run_id="run-1",
    )
    envelope_b = build_observation_envelope(
        source=ObservationSource.CLOB_REST,
        source_event_type="book",
        condition_id=minimal.condition_id,
        token_id=minimal.asset_id,
        event_time=START,
        received_time=START,
        ingest_sequence=2,
        payload=minimal,
        provenance=provenance,
        parser_version="test-parser-1",
        capture_run_id="run-1",
    )
    assert envelope_a.observation_id == envelope_b.observation_id


@given(
    digits=st.text(alphabet="0123456789", min_size=0, max_size=6),
)
def test_trailing_zeros_never_change_the_normalized_value_or_its_text(digits: str) -> None:
    base = Decimal("0.4")
    padded = Decimal("0.4" + "0" * len(digits))
    from argos.domain.orderbook import normalize_decimal

    assert normalize_decimal(base) == normalize_decimal(padded)
    assert str(normalize_decimal(base)) == str(normalize_decimal(padded))
    assert "E" not in str(normalize_decimal(padded))


# --- no float ever enters ------------------------------------------------------------


def test_a_float_price_is_refused() -> None:
    # Raised directly by `_build_level`'s pre-processing, not by a pydantic
    # field validator, so this surfaces as a plain ValueError rather than the
    # ValidationError subclass — see `parse_order_book_snapshot`'s docstring.
    with pytest.raises(ValueError, match="float"):
        parse_order_book_snapshot(_minimal_wire(bids=[{"price": 0.40, "size": "10"}]))


def test_a_float_size_is_refused() -> None:
    with pytest.raises(ValueError, match="float"):
        parse_order_book_snapshot(_minimal_wire(bids=[{"price": "0.40", "size": 10.0}]))


def test_a_float_tick_size_is_refused() -> None:
    """The dedicated /tick-size endpoint returns a JSON number; the book's own
    tick_size is a string. This model must refuse the float form regardless.

    `tick_size` is parsed eagerly in `parse_order_book_snapshot` itself (it is
    needed before construction, to compute off-tick anomalies), so this also
    surfaces as a plain ValueError rather than ValidationError."""
    with pytest.raises(ValueError, match="float"):
        parse_order_book_snapshot(_minimal_wire(tick_size=0.01))


def test_a_float_last_trade_price_is_refused() -> None:
    with pytest.raises(ValidationError):
        parse_order_book_snapshot(_minimal_wire(last_trade_price=0.43))


def test_a_float_min_order_size_is_refused() -> None:
    with pytest.raises(ValidationError):
        parse_order_book_snapshot(_minimal_wire(min_order_size=5.0))


def test_a_bool_is_refused_where_a_decimal_is_expected() -> None:
    """bool is an int subclass; True/False must not silently become 1/0."""
    with pytest.raises(ValidationError):
        parse_order_book_snapshot(_minimal_wire(last_trade_price=True))


def test_direct_construction_of_a_level_also_refuses_a_float() -> None:
    with pytest.raises(ValidationError):
        OrderBookLevel(price=0.4, size=Decimal("1"))  # type: ignore[arg-type]


@given(price=st.floats(allow_nan=False, allow_infinity=False, min_value=0.0, max_value=1.0))
def test_no_float_price_can_ever_construct_a_level(price: float) -> None:
    with pytest.raises(ValidationError):
        OrderBookLevel(price=price, size=Decimal("1"))  # type: ignore[arg-type]


# --- round trip through to_record()/from_record() -----------------------------------


def test_round_trip_through_to_record_and_from_record_is_lossless() -> None:
    snapshot = _fixture_snapshot()
    record = snapshot.to_record()
    restored = OrderBookSnapshotV1.from_record(record)
    assert restored == snapshot
    assert restored.to_record() == record


def test_round_trip_preserves_best_bid_ask_after_json_serialization() -> None:
    snapshot = _fixture_snapshot()
    # model_dump(mode="json") stands in for what actually crosses a storage boundary.
    as_json_text = json.dumps(snapshot.to_record())
    restored = OrderBookSnapshotV1.from_record(json.loads(as_json_text))
    assert restored.best_bid == snapshot.best_bid
    assert restored.best_ask == snapshot.best_ask


def test_from_record_rejects_a_foreign_schema_version() -> None:
    snapshot = _fixture_snapshot()
    record = snapshot.to_record()
    record["schema_version"] = "order_book_snapshot.v2"
    with pytest.raises(SchemaVersionError):
        OrderBookSnapshotV1.from_record(record)


# --- zero-size levels: dropped, never silent -----------------------------------------


def test_a_zero_size_bid_level_is_dropped_and_recorded_as_an_anomaly() -> None:
    wire = _minimal_wire(
        bids=[
            {"price": "0.40", "size": "10"},
            {"price": "0.39", "size": "0"},
        ]
    )
    snapshot = parse_order_book_snapshot(wire)
    prices = [level.price for level in snapshot.bids]
    assert Decimal("0.39") not in prices
    dropped = [
        a for a in snapshot.anomalies if a.kind == OrderBookAnomalyKind.ZERO_SIZE_LEVEL_DROPPED
    ]
    assert len(dropped) == 1
    assert dropped[0].side == BookSide.BID
    assert dropped[0].price == Decimal("0.39")


def test_a_zero_size_ask_level_is_dropped_and_recorded_as_an_anomaly() -> None:
    wire = _minimal_wire(asks=[{"price": "0.43", "size": "0"}, {"price": "0.50", "size": "5"}])
    snapshot = parse_order_book_snapshot(wire)
    prices = [level.price for level in snapshot.asks]
    assert Decimal("0.43") not in prices
    dropped = [
        a for a in snapshot.anomalies if a.kind == OrderBookAnomalyKind.ZERO_SIZE_LEVEL_DROPPED
    ]
    assert len(dropped) == 1
    assert dropped[0].side == BookSide.ASK


def test_dropping_a_zero_size_level_is_never_silent_it_is_always_counted() -> None:
    """The dropped level's price/side is preserved in the anomaly so nothing about
    the source event vanishes without a trace, per `.claude/rules/data-integrity.md`."""
    wire = _minimal_wire(bids=[{"price": "0.10", "size": "0"}])
    snapshot = parse_order_book_snapshot(wire)
    assert snapshot.anomalies == (
        OrderBookAnomaly(
            kind=OrderBookAnomalyKind.ZERO_SIZE_LEVEL_DROPPED,
            side=BookSide.BID,
            price=Decimal("0.10"),
            detail=snapshot.anomalies[0].detail,
        ),
    )


def test_a_negative_size_level_refuses_the_whole_snapshot() -> None:
    """Unlike zero, a negative size has no honest interpretation as "no order"."""
    wire = _minimal_wire(bids=[{"price": "0.10", "size": "-5"}])
    with pytest.raises(ValueError, match="negative"):
        parse_order_book_snapshot(wire)


def test_zero_size_levels_were_never_observed_in_the_real_fixture() -> None:
    """Documents the UNVERIFIED status in the research note: this is a synthetic
    case, not something the real recorded payload exhibits."""
    snapshot = _fixture_snapshot()
    assert not any(
        a.kind == OrderBookAnomalyKind.ZERO_SIZE_LEVEL_DROPPED for a in snapshot.anomalies
    )


# --- crossed / locked book: accepted, flagged, never silently dropped ---------------


def test_a_crossed_book_is_accepted_and_flagged_not_rejected() -> None:
    wire = _minimal_wire(
        bids=[{"price": "0.50", "size": "5"}],
        asks=[{"price": "0.45", "size": "5"}],
    )
    snapshot = parse_order_book_snapshot(wire)
    assert snapshot.best_bid == Decimal("0.50")
    assert snapshot.best_ask == Decimal("0.45")
    assert any(a.kind == OrderBookAnomalyKind.CROSSED_BOOK for a in snapshot.anomalies)


def test_a_locked_book_is_accepted_and_flagged() -> None:
    wire = _minimal_wire(
        bids=[{"price": "0.45", "size": "5"}],
        asks=[{"price": "0.45", "size": "5"}],
    )
    snapshot = parse_order_book_snapshot(wire)
    assert snapshot.best_bid == snapshot.best_ask == Decimal("0.45")
    assert any(a.kind == OrderBookAnomalyKind.LOCKED_BOOK for a in snapshot.anomalies)


def test_the_real_fixture_is_neither_crossed_nor_locked() -> None:
    snapshot = _fixture_snapshot()
    assert not any(
        a.kind in (OrderBookAnomalyKind.CROSSED_BOOK, OrderBookAnomalyKind.LOCKED_BOOK)
        for a in snapshot.anomalies
    )


# --- duplicate price levels: refused outright ----------------------------------------


def test_a_duplicate_bid_price_level_refuses_the_whole_snapshot() -> None:
    wire = _minimal_wire(
        bids=[{"price": "0.40", "size": "10"}, {"price": "0.40", "size": "5"}],
    )
    with pytest.raises(ValueError, match="duplicate"):
        parse_order_book_snapshot(wire)


def test_a_duplicate_ask_price_level_refuses_the_whole_snapshot() -> None:
    wire = _minimal_wire(
        asks=[{"price": "0.43", "size": "10"}, {"price": "0.43", "size": "5"}],
    )
    with pytest.raises(ValueError, match="duplicate"):
        parse_order_book_snapshot(wire)


def test_duplicate_detection_compares_normalized_value_not_source_text() -> None:
    """ "0.400" and "0.40" are the same price; the duplicate check must not be fooled
    by a cosmetic reformat the same way observation identity must not be."""
    wire = _minimal_wire(
        bids=[{"price": "0.400", "size": "10"}, {"price": "0.40", "size": "5"}],
    )
    with pytest.raises(ValueError, match="duplicate"):
        parse_order_book_snapshot(wire)


def test_the_real_fixture_has_no_duplicate_price_levels() -> None:
    snapshot = _fixture_snapshot()
    assert len({level.price for level in snapshot.bids}) == len(snapshot.bids)
    assert len({level.price for level in snapshot.asks}) == len(snapshot.asks)


# --- off-tick prices: accepted, flagged --------------------------------------------


def test_an_off_tick_price_is_accepted_and_flagged() -> None:
    wire = _minimal_wire(
        tick_size="0.01",
        bids=[{"price": "0.405", "size": "5"}],
    )
    snapshot = parse_order_book_snapshot(wire)
    assert snapshot.bids[0].price == Decimal("0.405")
    off_tick = [a for a in snapshot.anomalies if a.kind == OrderBookAnomalyKind.OFF_TICK_PRICE]
    assert len(off_tick) == 1
    assert off_tick[0].side == BookSide.BID
    assert off_tick[0].price == Decimal("0.405")


def test_an_on_tick_price_is_not_flagged() -> None:
    wire = _minimal_wire(tick_size="0.01", bids=[{"price": "0.40", "size": "5"}])
    snapshot = parse_order_book_snapshot(wire)
    assert not any(a.kind == OrderBookAnomalyKind.OFF_TICK_PRICE for a in snapshot.anomalies)


def test_the_real_fixture_has_no_off_tick_prices() -> None:
    snapshot = _fixture_snapshot()
    assert not any(a.kind == OrderBookAnomalyKind.OFF_TICK_PRICE for a in snapshot.anomalies)


# --- anomaly self-consistency: stored anomalies must be recomputable ----------------


def test_a_snapshot_claiming_a_crossed_book_anomaly_it_does_not_exhibit_is_refused() -> None:
    with pytest.raises(ValidationError):
        OrderBookSnapshotV1(
            condition_id="0x" + "a" * 64,
            asset_id="123",
            bids=(OrderBookLevel(price=Decimal("0.40"), size=Decimal("1")),),
            asks=(OrderBookLevel(price=Decimal("0.50"), size=Decimal("1")),),
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("5"),
            neg_risk=True,
            last_trade_price=Decimal("0.4"),
            anomalies=(OrderBookAnomaly(kind=OrderBookAnomalyKind.CROSSED_BOOK, detail="forged"),),
        )


def test_a_snapshot_omitting_a_real_crossed_book_anomaly_is_refused() -> None:
    with pytest.raises(ValidationError):
        OrderBookSnapshotV1(
            condition_id="0x" + "a" * 64,
            asset_id="123",
            bids=(OrderBookLevel(price=Decimal("0.50"), size=Decimal("1")),),
            asks=(OrderBookLevel(price=Decimal("0.40"), size=Decimal("1")),),
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("5"),
            neg_risk=True,
            last_trade_price=Decimal("0.4"),
            anomalies=(),
        )


# --- full book preserved; midpoint is clearly not executable ------------------------


def test_every_level_from_the_real_fixture_survives_normalization() -> None:
    raw = _load_fixture()
    snapshot = parse_order_book_snapshot(raw)
    raw_bid_prices = {Decimal(level["price"]) for level in raw["bids"]}
    raw_ask_prices = {Decimal(level["price"]) for level in raw["asks"]}
    assert {level.price for level in snapshot.bids} == raw_bid_prices
    assert {level.price for level in snapshot.asks} == raw_ask_prices


def test_derived_midpoint_matches_the_researched_value_but_is_not_a_stored_field() -> None:
    """docs/research/m2-clob-rest-book.md: GET /midpoint returned {"mid": "0.425"}."""
    snapshot = _fixture_snapshot()
    assert snapshot.derived_midpoint == Decimal("0.425")
    assert "derived_midpoint" not in type(snapshot).model_fields
    assert "mid" not in snapshot.to_record()


def test_midpoint_is_none_when_one_side_of_the_book_is_empty() -> None:
    wire = _minimal_wire(bids=[], asks=[{"price": "0.5", "size": "1"}])
    snapshot = parse_order_book_snapshot(wire)
    assert snapshot.best_bid is None
    assert snapshot.derived_midpoint is None
    assert snapshot.spread is None


# --- malformed input: refused, not coerced -------------------------------------------


def test_a_non_object_payload_is_refused() -> None:
    with pytest.raises(ValueError):
        parse_order_book_snapshot([])  # type: ignore[arg-type]


def test_bids_must_be_an_array() -> None:
    with pytest.raises(ValueError):
        parse_order_book_snapshot(_minimal_wire(bids="not-a-list"))


def test_a_level_missing_size_is_refused() -> None:
    with pytest.raises(ValueError):
        parse_order_book_snapshot(_minimal_wire(bids=[{"price": "0.40"}]))


def test_a_level_with_an_unexpected_field_is_refused() -> None:
    with pytest.raises(ValueError):
        parse_order_book_snapshot(
            _minimal_wire(bids=[{"price": "0.40", "size": "5", "extra": "x"}])
        )


def test_a_price_outside_zero_one_is_refused() -> None:
    with pytest.raises(ValueError, match="outside the valid"):
        parse_order_book_snapshot(_minimal_wire(bids=[{"price": "1.5", "size": "5"}]))


def test_a_non_decimal_price_string_is_refused() -> None:
    with pytest.raises(ValueError):
        parse_order_book_snapshot(_minimal_wire(bids=[{"price": "not-a-number", "size": "5"}]))


def test_neg_risk_must_be_a_real_boolean_not_a_truthy_string() -> None:
    with pytest.raises(ValidationError):
        parse_order_book_snapshot(_minimal_wire(neg_risk="true"))


def test_a_malformed_condition_id_is_refused() -> None:
    with pytest.raises(ValidationError):
        parse_order_book_snapshot(_minimal_wire(market="not-a-condition-id"))


def test_extra_top_level_fields_are_ignored_by_from_wire_but_not_by_direct_construction() -> None:
    """`parse_order_book_snapshot` only reads the keys it knows; unrelated wire
    fields do not break it. Direct model construction still forbids them (`extra="forbid"`),
    which is the layer that actually protects the persisted record shape."""
    wire = _minimal_wire()
    wire["some_future_field"] = "unrecognized"
    snapshot = parse_order_book_snapshot(wire)
    assert snapshot.condition_id == wire["market"]

    with pytest.raises(ValidationError):
        OrderBookSnapshotV1(
            condition_id=snapshot.condition_id,
            asset_id=snapshot.asset_id,
            bids=snapshot.bids,
            asks=snapshot.asks,
            tick_size=snapshot.tick_size,
            min_order_size=snapshot.min_order_size,
            neg_risk=snapshot.neg_risk,
            last_trade_price=snapshot.last_trade_price,
            some_future_field="unrecognized",  # type: ignore[call-arg]
        )


# --- immutability ---------------------------------------------------------------------


def test_the_snapshot_and_its_levels_are_frozen() -> None:
    snapshot = _fixture_snapshot()
    with pytest.raises(ValidationError):
        snapshot.tick_size = Decimal("0.02")
    with pytest.raises(ValidationError):
        snapshot.bids[0].price = Decimal("0.99")


def test_mutating_a_dict_after_parsing_does_not_reach_the_snapshot() -> None:
    """`from_wire` does not hold a live reference to the caller's mutable input."""
    wire = _minimal_wire()
    original = copy.deepcopy(wire)
    snapshot = parse_order_book_snapshot(wire)
    wire["bids"][0]["price"] = "0.99"
    assert snapshot.to_record()["bids"] != []
    # the snapshot's own levels are independent objects built from parsed Decimals,
    # not references into the caller's dict, so mutating the caller's dict cannot
    # retroactively change what was already validated and stored.
    resnapshot = parse_order_book_snapshot(original)
    assert snapshot == resnapshot


# --- determinism ------------------------------------------------------------------------


def test_parsing_the_same_payload_twice_is_byte_identical() -> None:
    raw_a = _load_fixture()
    raw_b = _load_fixture()
    snap_a = parse_order_book_snapshot(raw_a)
    snap_b = parse_order_book_snapshot(raw_b)
    assert snap_a == snap_b
    assert json.dumps(snap_a.to_record(), sort_keys=True) == json.dumps(
        snap_b.to_record(), sort_keys=True
    )


@given(st.integers(min_value=0, max_value=0))  # deterministic re-run harness
def test_repeated_parsing_is_deterministic_across_hypothesis_examples(_seed: int) -> None:
    raw = _load_fixture()
    first = parse_order_book_snapshot(raw)
    second = parse_order_book_snapshot(_load_fixture())
    assert first.to_record() == second.to_record()


# --- canonicalization must never silently change a finite non-zero value -------------
#
# Reported against a9b9802 and reproduced before fixing. The one-sided exponent
# bound introduced with CANONICAL_DECIMAL_CONTEXT was argued on TEXT LENGTH:
# only a positive exponent reaches the quantize branch that expands a value
# into long plain text, so bounding the negative side looked like needless
# narrowing of a shipped contract. The argument was about the wrong thing.
# `Decimal.normalize()` runs inside a context with a finite Emin, so a
# sufficiently small non-zero value UNDERFLOWS to zero: `1E-1000064` returned
# `Decimal(0)` and rendered as the canonical text "0", numerically identical to
# a genuine zero. A tiny non-zero price and a real zero therefore produced the
# same identity-bearing payload -- a silent mutation of a price, the exact
# class the pinned context was introduced to close, reintroduced one field
# over by the fix for it.


def test_a_tiny_non_zero_value_is_refused_rather_than_underflowing_to_zero() -> None:
    with pytest.raises(ValueError, match="below the"):
        parse_wire_decimal("1E-1000064")


def test_a_tiny_non_zero_value_and_a_genuine_zero_cannot_share_one_identity() -> None:
    """The property the refusal exists to protect, stated directly."""
    zero_text = str(parse_wire_decimal("0"))
    with pytest.raises(ValueError):
        parse_wire_decimal("1E-1000064")
    # And a small-but-accepted value stays distinguishable from zero.
    assert str(parse_wire_decimal("1E-1000")) != zero_text


def test_the_committed_tiny_tick_size_is_still_accepted_exactly() -> None:
    """The guard must not be degenerate: 1E-50 was already a shipped contract."""
    assert parse_wire_decimal("1E-50") == Decimal("1E-50")


@pytest.mark.parametrize(
    "spelling",
    ["0.43", "0.430", "500", "1E+29", "1E-50", "1E-1000", "0.123456789012345678901234567890123"],
)
def test_no_accepted_non_zero_value_changes_numerically_during_canonicalization(
    spelling: str,
) -> None:
    assert parse_wire_decimal(spelling) == Decimal(spelling)


@pytest.mark.parametrize("spelling", ["-0", "-0.0", "-0E+5", "-0.00000", "0E+3", "0E-100000"])
def test_every_spelling_of_zero_still_canonicalizes_to_positive_zero(spelling: str) -> None:
    """Including `0E-100000`, whose exponent is far outside the non-zero budget.

    A zero carries no magnitude, so the exponent bounds -- which describe
    non-zero magnitudes -- must not refuse it.
    """
    assert str(parse_wire_decimal(spelling)) == "0"


def test_the_pinned_context_sets_its_exponent_range_explicitly() -> None:
    """An unspecified `Context` field is filled from `decimal.DefaultContext`.

    That is mutable process-global state, so leaving Emin/Emax to default only
    moves the hidden-global-state dependency from read time to import time --
    and Emin is exactly what the underflow above turned on.
    """
    assert CANONICAL_DECIMAL_CONTEXT.Emin < MIN_DECIMAL_EXPONENT
    assert CANONICAL_DECIMAL_CONTEXT.Emax > MAX_DECIMAL_EXPONENT
