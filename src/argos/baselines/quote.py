"""The market quote a baseline is computed from, with executability preserved.

Core invariant 2: the market is a benchmark and an evidence source, and
midpoint, bid, ask, depth, spread and lifecycle state are all preserved rather
than collapsed into one number. M4's first exit criterion — **"midpoint is never
labeled executable price"** — is a naming rule here, and this module keeps the
two apart structurally rather than by convention:

- ``best_bid`` and ``best_ask`` are the prices somebody could actually trade
  against, and each carries the size resting at it. They are the only fields on
  this record that describe an executable quantity;
- ``midpoint`` is an arithmetic construct that no order rests at, and it is
  ``None`` whenever both sides do not exist, rather than falling back to the
  side that does.

That last point is the load-bearing one. Substituting a one-sided book's only
price for a midpoint would invent a number the market never showed, and
measured on the 100 highest-volume open markets, **9 of 100 have no two-sided
book** (``docs/research/m4-gamma-resolution.md``). A baseline that silently
filled those in would be scoring nine invented prices per hundred markets.

The same research established that Polymarket's own displayed price *is* this
midpoint — 91 of 91 two-sided markets agree exactly — so
``docs/05_RESEARCH_PROTOCOL.md``'s "displayed-price proxy" and "midpoint"
baselines are one quantity on this source, and are implemented once.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from pydantic import Field, field_validator, model_validator

from argos.clock import ensure_utc
from argos.domain.market import CONDITION_ID_PATTERN, TOKEN_ID_PATTERN
from argos.domain.orderbook import MAX_PRICE, MIN_PRICE, normalize_decimal
from argos.domain.versioning import VersionedModel
from argos.projections.book import BookState

__all__ = ["MarketQuoteV1", "quote_from_book_state"]


class MarketQuoteV1(VersionedModel):
    """One token's top of book at one instant, with nothing collapsed."""

    schema_version: ClassVar[str] = "market_quote.v1"

    condition_id: str
    token_id: str

    best_bid: Decimal | None = Field(default=None, ge=MIN_PRICE, le=MAX_PRICE)
    """The highest price somebody is offering to buy at. **Executable.**"""

    best_ask: Decimal | None = Field(default=None, ge=MIN_PRICE, le=MAX_PRICE)
    """The lowest price somebody is offering to sell at. **Executable.**"""

    best_bid_size: Decimal | None = Field(default=None, ge=0)
    best_ask_size: Decimal | None = Field(default=None, ge=0)
    """Size resting at the top of each side. Present so a later depth
    assumption is a stated quantity rather than a shrug: an edge analysis needs
    exact executable bid/ask *and* a depth assumption
    (``docs/05_RESEARCH_PROTOCOL.md``), and half of that pair is not enough."""

    midpoint: Decimal | None = Field(default=None, ge=MIN_PRICE, le=MAX_PRICE)
    """``(best_bid + best_ask) / 2``, or ``None`` when both sides do not exist.

    **Not an executable price**, and never a fallback: no order rests at the
    midpoint, and a one-sided book has no midpoint at all rather than having its
    one price. Polymarket's displayed price equals this value on every two-sided
    market measured (91 of 91), which makes it a good *benchmark* and does not
    make it tradable.
    """

    spread: Decimal | None = None
    """``best_ask - best_bid``, or ``None`` when both sides do not exist.

    Deliberately **not** floored at zero. A negative spread is a crossed
    book — a real, observed market state that
    :class:`argos.domain.orderbook.OrderBookSnapshotV1` already counts as an
    anomaly where the snapshot is parsed. Clamping it here would erase the
    evidence at the point where it is most interesting, and would make a
    crossed book indistinguishable from a touching one."""

    last_trade_price: Decimal | None = Field(default=None, ge=MIN_PRICE, le=MAX_PRICE)
    """The last price that actually traded, when the caller supplies one.

    A trade is executable evidence about the past, not a quote available now —
    which is why it is a separate baseline in
    ``docs/05_RESEARCH_PROTOCOL.md`` rather than a substitute for a missing
    side.
    """

    bid_levels: int = Field(ge=0)
    ask_levels: int = Field(ge=0)
    """How deep each side was. A quote from a book with one resting order and
    one from a book with two hundred are different evidence, and a record that
    kept only the top price could not tell them apart."""

    quote_time: datetime | None = None
    """The source event time of the book state this was taken from, or ``None``
    when the source sent no usable timestamp. Never substituted with a receipt
    time — ``.claude/rules/data-integrity.md`` forbids exactly that."""

    @field_validator("condition_id")
    @classmethod
    def _validate_condition_id(cls, value: str) -> str:
        if not CONDITION_ID_PATTERN.fullmatch(value.lower()):
            raise ValueError(f"condition_id must be a 0x-prefixed 32-byte hash, got {value!r}")
        return value

    @field_validator("token_id")
    @classmethod
    def _validate_token_id(cls, value: str) -> str:
        if not TOKEN_ID_PATTERN.fullmatch(value):
            raise ValueError(f"token_id must be a decimal token id string, got {value!r}")
        return value

    @field_validator(
        "best_bid",
        "best_ask",
        "best_bid_size",
        "best_ask_size",
        "midpoint",
        "spread",
        "last_trade_price",
    )
    @classmethod
    def _canonicalize(cls, value: Decimal | None) -> Decimal | None:
        """Reuse the shared canonicalization rather than reimplement it.

        ``docs/STATUS.md`` records the decimal-identity class recurring three
        times independently in this repository. This record is not the fourth.
        """
        return None if value is None else normalize_decimal(value)

    @field_validator("quote_time")
    @classmethod
    def _anchor(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @model_validator(mode="after")
    def _derived_values_agree_with_the_sides(self) -> MarketQuoteV1:
        """Refuse a record whose midpoint or spread disagrees with its own sides.

        Recomputed rather than trusted, the same discipline
        ``PriceLevelChangeV1`` applies to ``kind`` versus ``size``: a stored
        record that can disagree with its own meaning will eventually be
        corrected silently on the way out, and this one is the number every
        downstream score is computed from.
        """
        two_sided = self.best_bid is not None and self.best_ask is not None
        if not two_sided:
            if self.midpoint is not None:
                raise ValueError(
                    "midpoint must be unset without both sides; a one-sided book has "
                    "no midpoint, and substituting the side that exists would invent "
                    "a price the market never showed"
                )
            if self.spread is not None:
                raise ValueError("spread must be unset without both sides")
            return self

        assert self.best_bid is not None and self.best_ask is not None
        expected_mid = normalize_decimal((self.best_bid + self.best_ask) / 2)
        expected_spread = normalize_decimal(self.best_ask - self.best_bid)
        if self.midpoint != expected_mid:
            raise ValueError(f"midpoint {self.midpoint} disagrees with its sides ({expected_mid})")
        if self.spread != expected_spread:
            raise ValueError(f"spread {self.spread} disagrees with its sides ({expected_spread})")
        return self


def quote_from_book_state(
    state: BookState,
    *,
    quote_time: datetime | None,
    last_trade_price: Decimal | None = None,
) -> MarketQuoteV1:
    """Read a quote off a projected book state.

    Derives midpoint and spread only when both sides exist, and leaves them
    unset otherwise. A **crossed** book (bid above ask) still produces a quote:
    it is a real, observed market state, `OrderBookSnapshotV1` already counts it
    as an anomaly where the snapshot is parsed, and refusing it here would
    discard evidence at the point where it is most interesting. The negative
    spread is visible in the record — which is why ``spread`` is derived rather
    than clamped.
    """
    best_bid = state.best_bid
    best_ask = state.best_ask
    two_sided = best_bid is not None and best_ask is not None
    return MarketQuoteV1(
        condition_id=state.condition_id,
        token_id=state.asset_id,
        best_bid=best_bid,
        best_ask=best_ask,
        best_bid_size=state.bids[0].size if state.bids else None,
        best_ask_size=state.asks[0].size if state.asks else None,
        midpoint=((best_bid + best_ask) / 2) if two_sided else None,  # type: ignore[operator]
        spread=(best_ask - best_bid) if two_sided else None,  # type: ignore[operator]
        last_trade_price=last_trade_price,
        bid_levels=len(state.bids),
        ask_levels=len(state.asks),
        quote_time=quote_time,
    )
