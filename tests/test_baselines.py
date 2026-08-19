"""Quotes and baseline scores (M4).

The two exit criteria this file carries: **"midpoint is never labeled executable
price"** and, through `raw_score`/`p_yes`, ADR-0006's rule that an uncalibrated
number never wears a probability's name.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from argos.baselines import (
    AbstentionReason,
    BaselineMethod,
    CalibrationStatus,
    MarketBaselineForecastV1,
    MarketQuoteV1,
    build_baseline_forecast,
    quote_from_book_state,
)
from argos.domain.orderbook import OrderBookLevel
from argos.projections.book import BookState

TOKEN = "34691510069031117755834214800869745291092253295564665516660269316660628637961"
CONDITION = "0x94a39addec8bd24a2d03deeaa43bdee6a2b11eca403cd12495eb24132cc23173"
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _state(*, bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> BookState:
    return BookState(
        condition_id=CONDITION,
        asset_id=TOKEN,
        bids=tuple(
            OrderBookLevel(price=Decimal(p), size=Decimal(s))
            for p, s in sorted(bids, key=lambda x: Decimal(x[0]), reverse=True)
        ),
        asks=tuple(
            OrderBookLevel(price=Decimal(p), size=Decimal(s))
            for p, s in sorted(asks, key=lambda x: Decimal(x[0]))
        ),
    )


# --- the quote --------------------------------------------------------------------


def test_a_two_sided_book_yields_a_midpoint_and_a_spread() -> None:
    quote = quote_from_book_state(
        _state(bids=[("0.48", "100"), ("0.47", "5")], asks=[("0.52", "80")]), quote_time=NOW
    )
    assert quote.best_bid == Decimal("0.48")
    assert quote.best_ask == Decimal("0.52")
    assert quote.midpoint == Decimal("0.5")
    assert quote.spread == Decimal("0.04")
    assert quote.best_bid_size == Decimal("100")
    assert (quote.bid_levels, quote.ask_levels) == (2, 1)


@pytest.mark.parametrize(
    ("bids", "asks"),
    [([("0.48", "100")], []), ([], [("0.52", "80")]), ([], [])],
)
def test_a_one_sided_book_has_no_midpoint_rather_than_a_substitute(
    bids: list[tuple[str, str]], asks: list[tuple[str, str]]
) -> None:
    """The M4 criterion's real content.

    Substituting the side that exists would invent a price the market never
    showed, and 9 of the 100 highest-volume open markets have no two-sided book
    (`docs/research/m4-gamma-resolution.md`) -- so this is nine invented prices
    per hundred markets, not a corner case.
    """
    quote = quote_from_book_state(_state(bids=bids, asks=asks), quote_time=NOW)
    assert quote.midpoint is None
    assert quote.spread is None


def test_a_crossed_book_keeps_its_negative_spread() -> None:
    """A crossed book is a real, observed state that `OrderBookSnapshotV1`
    already counts as an anomaly where the snapshot is parsed. Clamping the
    spread to zero here would erase the evidence at the point where it is most
    interesting."""
    quote = quote_from_book_state(
        _state(bids=[("0.55", "10")], asks=[("0.45", "10")]), quote_time=NOW
    )
    assert quote.spread == Decimal("-0.1")
    assert quote.midpoint == Decimal("0.5")


def test_a_quote_whose_midpoint_disagrees_with_its_sides_is_refused() -> None:
    """Recomputed rather than trusted: this is the number every downstream score
    is computed from, and a record that can disagree with itself would be
    corrected silently on the way out."""
    with pytest.raises(ValidationError):
        MarketQuoteV1(
            condition_id=CONDITION,
            token_id=TOKEN,
            best_bid=Decimal("0.4"),
            best_ask=Decimal("0.6"),
            midpoint=Decimal("0.9"),
            spread=Decimal("0.2"),
            bid_levels=1,
            ask_levels=1,
        )


def test_a_midpoint_without_both_sides_is_refused_even_when_hand_built() -> None:
    with pytest.raises(ValidationError):
        MarketQuoteV1(
            condition_id=CONDITION,
            token_id=TOKEN,
            best_bid=Decimal("0.4"),
            midpoint=Decimal("0.4"),
            bid_levels=1,
            ask_levels=0,
        )


def test_the_quote_keeps_executable_and_derived_prices_in_separate_fields() -> None:
    """The naming half of "midpoint is never labeled executable price".

    Asserted over the field names rather than in prose, so a future field called
    `price` or `executable_price` -- the shape the mistake would actually take --
    fails here instead of in review.
    """
    fields = set(MarketQuoteV1.model_fields)
    assert {"best_bid", "best_ask", "midpoint", "spread"} <= fields
    assert not [name for name in fields if "executable" in name]
    assert "price" not in fields


def test_a_missing_source_timestamp_is_kept_missing() -> None:
    quote = quote_from_book_state(
        _state(bids=[("0.48", "1")], asks=[("0.52", "1")]), quote_time=None
    )
    assert quote.quote_time is None


# --- the baselines ----------------------------------------------------------------


def _quote(**overrides: object) -> MarketQuoteV1:
    base = quote_from_book_state(
        _state(bids=[("0.48", "100")], asks=[("0.52", "80")]), quote_time=NOW
    )
    return base.model_copy(update=overrides) if overrides else base


def test_the_midpoint_baseline_scores_and_never_calls_itself_a_probability() -> None:
    forecast = build_baseline_forecast(
        method=BaselineMethod.MIDPOINT,
        quote=_quote(),
        as_of_received_time=NOW + timedelta(seconds=1),
        as_of_ingest_sequence=7,
    )
    assert forecast.raw_score == Decimal("0.5")
    assert forecast.p_yes is None
    assert forecast.calibration_status is CalibrationStatus.UNCALIBRATED
    assert forecast.abstained is False
    assert forecast.as_of_event_time == NOW
    assert forecast.as_of_ingest_sequence == 7


def test_the_midpoint_baseline_abstains_on_a_one_sided_book() -> None:
    """Invariant 10: abstention is a first-class result, and a record that
    exists. "ARGOS declined" and "ARGOS was never asked" must not look identical
    when the evaluation counts coverage."""
    one_sided = quote_from_book_state(_state(bids=[("0.48", "100")], asks=[]), quote_time=NOW)
    forecast = build_baseline_forecast(
        method=BaselineMethod.MIDPOINT,
        quote=one_sided,
        as_of_received_time=NOW,
        as_of_ingest_sequence=1,
    )
    assert forecast.abstained is True
    assert forecast.abstention_reason is AbstentionReason.NO_TWO_SIDED_BOOK
    assert forecast.raw_score is None


def test_the_last_trade_baseline_is_not_a_fallback_for_a_missing_midpoint() -> None:
    """Two separate baselines in `docs/05_RESEARCH_PROTOCOL.md`. If last trade
    filled in for an absent midpoint, comparing them would be comparing a
    quantity with a mixture of itself and another."""
    one_sided = quote_from_book_state(
        _state(bids=[("0.48", "100")], asks=[]),
        quote_time=NOW,
        last_trade_price=Decimal("0.49"),
    )
    midpoint = build_baseline_forecast(
        method=BaselineMethod.MIDPOINT,
        quote=one_sided,
        as_of_received_time=NOW,
        as_of_ingest_sequence=1,
    )
    last_trade = build_baseline_forecast(
        method=BaselineMethod.LAST_TRADE,
        quote=one_sided,
        as_of_received_time=NOW,
        as_of_ingest_sequence=1,
    )
    assert midpoint.abstained is True
    assert last_trade.raw_score == Decimal("0.49")


def test_the_persistence_baseline_abstains_rather_than_seeding_itself() -> None:
    """A persistence baseline that started at the current midpoint would be the
    midpoint baseline with a delay, and comparing the two would be comparing a
    quantity with itself."""
    first = build_baseline_forecast(
        method=BaselineMethod.PERSISTENCE,
        quote=_quote(),
        as_of_received_time=NOW,
        as_of_ingest_sequence=1,
    )
    assert first.abstained is True
    assert first.abstention_reason is AbstentionReason.NO_PRIOR_FORECAST

    second = build_baseline_forecast(
        method=BaselineMethod.PERSISTENCE,
        quote=_quote(),
        as_of_received_time=NOW,
        as_of_ingest_sequence=2,
        previous_score=Decimal("0.42"),
    )
    assert second.raw_score == Decimal("0.42")


def test_p_yes_without_a_calibration_version_is_refused() -> None:
    """ADR-0006 enforced rather than documented -- this is the exact shape the
    ADR was written about."""
    with pytest.raises(ValidationError):
        MarketBaselineForecastV1(
            condition_id=CONDITION,
            token_id=TOKEN,
            method=BaselineMethod.MIDPOINT,
            as_of_received_time=NOW,
            as_of_ingest_sequence=1,
            p_yes=Decimal("0.5"),
            quote=_quote(),
        )


def test_a_calibrated_status_without_a_version_is_refused() -> None:
    with pytest.raises(ValidationError):
        MarketBaselineForecastV1(
            condition_id=CONDITION,
            token_id=TOKEN,
            method=BaselineMethod.MIDPOINT,
            as_of_received_time=NOW,
            as_of_ingest_sequence=1,
            raw_score=Decimal("0.5"),
            calibration_status=CalibrationStatus.CALIBRATED,
            quote=_quote(),
        )


def test_an_abstention_without_a_reason_is_refused() -> None:
    with pytest.raises(ValidationError):
        MarketBaselineForecastV1(
            condition_id=CONDITION,
            token_id=TOKEN,
            method=BaselineMethod.MIDPOINT,
            as_of_received_time=NOW,
            as_of_ingest_sequence=1,
            abstained=True,
            quote=_quote(),
        )


def test_a_forecast_cannot_name_a_token_its_quote_does_not() -> None:
    with pytest.raises(ValidationError):
        MarketBaselineForecastV1(
            condition_id=CONDITION,
            token_id="999",
            method=BaselineMethod.MIDPOINT,
            as_of_received_time=NOW,
            as_of_ingest_sequence=1,
            raw_score=Decimal("0.5"),
            quote=_quote(),
        )


def test_a_forecast_round_trips_with_its_quote() -> None:
    forecast = build_baseline_forecast(
        method=BaselineMethod.MIDPOINT,
        quote=_quote(),
        as_of_received_time=NOW,
        as_of_ingest_sequence=3,
    )
    record = forecast.to_record()
    assert record["schema_version"] == "market_baseline_forecast.v1"
    assert MarketBaselineForecastV1.from_record(record) == forecast
