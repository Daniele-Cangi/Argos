"""The WebSocket `book` payload: ARGOS's typed view of the public CLOB market-channel `book` event.

This closes the sharpest remaining M2 gap `docs/BACKLOG.md` records: a live
capture stored `price_change.v1` deltas only, so every `book` event became an
`unknown_event_type` rejection and a stored capture had nothing in the same
capture to seed a projection from — the seed had to come from a separate REST
poll. This model, plus :func:`argos.ingestion.clob_ws_book.normalize_clob_ws_book`,
makes a stored WebSocket capture self-sufficient: it can now seed
:class:`argos.projections.book.BookState` from its own `ws_book_snapshot.v1`
observations and apply its own `price_change.v1` deltas on top, with no REST
call anywhere in the path.

It is a normalized *payload*, not the envelope, the same relationship
:mod:`argos.domain.orderbook` and :mod:`argos.domain.pricechange` have to
:class:`argos.domain.observation.ObservationEnvelopeV1`: event time and source
hash live on the envelope, not duplicated here. Nothing here reads a clock,
performs I/O, or knows about the transport or the rejection ledger.

Every decision below is measured against the real recorded fixture
(`tests/fixtures/clob/ws_market_price_change.raw.json`, all four `book` events
inspected directly), not assumed:

1. **This is not `OrderBookSnapshotV1`, and must not become it.** The REST
   `/book` response and the WebSocket `book` event are measurably different
   wire schemas. The snapshot delivered on subscribe (fixture message 0)
   carries ``market``, ``asset_id``, ``timestamp``, ``hash``, ``bids``,
   ``asks``, ``tick_size``, ``last_trade_price`` — already missing
   ``min_order_size`` and ``neg_risk``, which
   :class:`argos.domain.orderbook.OrderBookSnapshotV1` requires. The three
   later in-stream `book` events (fixture messages 16, 26, 37) are narrower
   still: they drop ``tick_size`` and ``last_trade_price`` too, carrying only
   ``market``, ``asset_id``, ``timestamp``, ``hash``, ``bids``, ``asks``.
   Weakening the shipped REST model's required fields to accommodate this
   would degrade a contract real REST responses actually satisfy, to
   accommodate a WebSocket quirk those responses never have. So this is a
   second, distinct payload model, and its two option fields
   (:attr:`WsBookSnapshotV1.tick_size`, :attr:`WsBookSnapshotV1.last_trade_price`)
   are genuinely optional — their absence is a fact about which of the two
   observed shapes a given message is, not a parser failure.
2. **Levels are reused, not reimplemented.**
   :class:`argos.domain.orderbook.OrderBookLevel` and
   :func:`argos.domain.orderbook.parse_wire_decimal` /
   :func:`argos.domain.orderbook.normalize_decimal` are imported directly.
   ``docs/STATUS.md`` records the negative-zero/trailing-zero decimal-identity
   class recurring across the observation envelope, ``OrderBookSnapshotV1``,
   and ``PriceChangeV1`` independently; this module reuses the same helpers
   rather than becoming a fourth instance.
3. **Zero-size level inside a snapshot: refused, not dropped-with-anomaly.**
   :class:`argos.domain.orderbook.OrderBookSnapshotV1` drops a zero-size level
   and records a counted :class:`~argos.domain.orderbook.OrderBookAnomaly`
   because it carries an ``anomalies`` field to record the drop into.
   :class:`argos.projections.book.BookState` — the model this payload exists
   to seed — refuses one outright for the opposite reason: it has no anomaly
   sink, and a resting order of size zero is not a resting order. This model
   follows :class:`~argos.projections.book.BookState`'s choice, for the same
   reason: it reuses :class:`~argos.domain.orderbook.OrderBookLevel` verbatim,
   whose ``size`` field is already ``gt=0`` by construction, and carries no
   anomaly vocabulary of its own — adding one only for a shape that has never
   been observed here would be speculative surface, not evidence-driven
   design. **Checked directly against the real fixture before assuming this is
   safe**: all four recorded `book` events — the subscribe-time snapshot and
   all three in-stream snapshots, 27 bids and 55 asks apiece — were inspected
   for a `"size": "0"` entry on either side, and none exists. This matches
   both the REST research note's own suspicion and
   :meth:`argos.projections.book.BookState.from_wire_levels`'s docstring,
   which records the identical check against the identical fixture. If a
   zero-size level is ever observed on this specific stream, the fix belongs
   in a slice that can add a counted anomaly sink, not a silent drop added
   here without evidence.
4. **`hash` and `timestamp` are not fields here.** They name the
   observation's ``source_hash`` and ``event_time`` on
   :class:`argos.domain.observation.ObservationEnvelopeV1`, exactly as
   :mod:`argos.domain.orderbook` and :mod:`argos.domain.pricechange` already
   decided for the sibling channels; storing them here too would create two
   copies that can disagree.
5. **Level ordering is never trusted from the wire.** Matching both sibling
   modules: the measured wire order is bids ascending / asks descending
   (confirmed directly on this fixture, the same convention the REST research
   note found), the reverse of the naive ``[0]`` reading.
   :func:`parse_ws_book_snapshot` sorts explicitly into "best level first"
   form, and every construction path — not only parsing — re-validates that
   order and refuses a duplicate or unsorted side rather than silently
   re-sorting it.

Not decided here, left for the ingestion slice, exactly like
:mod:`argos.domain.orderbook` and :mod:`argos.domain.pricechange`: how a
raised :class:`ValueError` becomes a
:class:`argos.errors.RejectionReason`/``RejectedObservationV1`` entry, how a
`book` event naming a different token than the one requested becomes a
counted non-event rather than a rejection, and how ``ingest_sequence`` is
assigned. This module does not know about the rejection ledger, the event
store, or the WebSocket connection it will eventually sit behind.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, ClassVar, Final

from pydantic import Field, field_validator

from argos.domain.market import CONDITION_ID_PATTERN, TOKEN_ID_PATTERN
from argos.domain.orderbook import (
    MAX_PRICE,
    MIN_PRICE,
    BookSide,
    OrderBookLevel,
    parse_wire_decimal,
)
from argos.domain.versioning import VersionedModel

EXPECTED_EVENT_KEYS: Final = frozenset(
    {
        "event_type",
        "market",
        "asset_id",
        "timestamp",
        "hash",
        "bids",
        "asks",
        "tick_size",
        "last_trade_price",
    }
)
"""The union of top-level keys observed across both real `book` shapes.

Checked for the same reason :data:`argos.domain.pricechange.EXPECTED_EVENT_KEYS`
is: an unrecognized top-level key would otherwise be silently ignored rather
than counted and reasoned about, and refusing means a source schema change
halts ingestion loudly with a ledger entry rather than being absorbed in
silence."""


class WsBookSnapshotV1(VersionedModel):
    """A normalized public CLOB WebSocket `book` event for one token.

    Distinct from :class:`argos.domain.orderbook.OrderBookSnapshotV1` — see the
    module docstring, point 1. ``market_id`` in the wire event is Polymarket's
    condition id; this model names the field ``condition_id`` to match
    :class:`argos.domain.market.MarketDefinitionV1`'s terminology, the same
    convention the sibling payload models already follow.
    """

    schema_version: ClassVar[str] = "ws_book_snapshot.v1"

    condition_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)

    bids: tuple[OrderBookLevel, ...]
    """Sorted descending by price: ``bids[0]`` is always the best bid, or the
    tuple is empty. Never trusted from wire order — see
    :func:`_validate_side_ordering`."""

    asks: tuple[OrderBookLevel, ...]
    """Sorted ascending by price: ``asks[0]`` is always the best ask, or the
    tuple is empty."""

    tick_size: Decimal | None = Field(default=None, gt=0, le=MAX_PRICE)
    """Present only on the subscribe-time snapshot in the real fixture; absent
    on every in-stream `book` event. See the module docstring, point 1."""

    last_trade_price: Decimal | None = Field(default=None, ge=MIN_PRICE, le=MAX_PRICE)
    """Present only on the subscribe-time snapshot in the real fixture; absent
    on every in-stream `book` event. See the module docstring, point 1."""

    @field_validator("condition_id")
    @classmethod
    def _validate_condition_id(cls, value: str) -> str:
        lowered = value.lower()
        if not CONDITION_ID_PATTERN.fullmatch(lowered):
            raise ValueError(f"condition_id must be a 0x-prefixed 32-byte hash, got {value!r}")
        return lowered

    @field_validator("asset_id")
    @classmethod
    def _validate_asset_id(cls, value: str) -> str:
        if not TOKEN_ID_PATTERN.fullmatch(value):
            raise ValueError(f"asset_id must be a decimal token id string, got {value!r}")
        return value

    @field_validator("tick_size", "last_trade_price", mode="before")
    @classmethod
    def _parse_optional_scalar_decimal(cls, value: Any) -> Any:
        return value if value is None else parse_wire_decimal(value)

    @field_validator("bids")
    @classmethod
    def _validate_bids_ordering(
        cls, value: tuple[OrderBookLevel, ...]
    ) -> tuple[OrderBookLevel, ...]:
        _validate_side_ordering(value, side=BookSide.BID, descending=True)
        return value

    @field_validator("asks")
    @classmethod
    def _validate_asks_ordering(
        cls, value: tuple[OrderBookLevel, ...]
    ) -> tuple[OrderBookLevel, ...]:
        _validate_side_ordering(value, side=BookSide.ASK, descending=False)
        return value

    @property
    def best_bid(self) -> Decimal | None:
        """The highest resting bid price, or ``None`` if the book has no bids."""
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        """The lowest resting ask price, or ``None`` if the book has no asks."""
        return self.asks[0].price if self.asks else None


def parse_ws_book_snapshot(event: Mapping[str, Any]) -> WsBookSnapshotV1:
    """Build a canonical snapshot from one already-JSON-decoded `book` event.

    ``event`` is a plain mapping (ideally, though not required, decoded with
    ``parse_float=Decimal`` — this function defends against a float slipping
    through either way, matching the sibling parsers). No network, filesystem,
    or clock access happens here.

    Raises :class:`ValueError` (via a nested Pydantic ``ValidationError`` for
    the final construction step) for anything this function judges cannot be
    honestly represented: a non-object event, an ``event_type`` other than
    ``"book"``, an unrecognized top-level field, a missing or malformed
    ``bids``/``asks`` array, a zero-size or negative-size level (see the
    module docstring, point 3), or a duplicate price level on one side. It
    never returns a partially-valid snapshot.
    """
    if not isinstance(event, Mapping):
        raise ValueError(f"book event must be a JSON object, got {type(event).__name__}")

    event_type = event.get("event_type")
    if event_type != "book":
        raise ValueError(f"expected event_type 'book', got {event_type!r}")

    unexpected_keys = set(event.keys()) - EXPECTED_EVENT_KEYS
    if unexpected_keys:
        raise ValueError(f"book event has unexpected top-level fields: {sorted(unexpected_keys)}")

    raw_bids = event.get("bids")
    raw_asks = event.get("asks")
    if not isinstance(raw_bids, list):
        raise ValueError(f"'bids' must be an array, got {type(raw_bids).__name__}")
    if not isinstance(raw_asks, list):
        raise ValueError(f"'asks' must be an array, got {type(raw_asks).__name__}")

    bid_levels = [_build_level(entry, side=BookSide.BID) for entry in raw_bids]
    ask_levels = [_build_level(entry, side=BookSide.ASK) for entry in raw_asks]

    # Canonical order is "best level first" on both sides. The measured wire
    # order is the opposite for bids (ascending) — see the module docstring,
    # point 5. Do not trust or preserve the wire order; sort explicitly.
    bids_sorted = tuple(sorted(bid_levels, key=lambda level: level.price, reverse=True))
    asks_sorted = tuple(sorted(ask_levels, key=lambda level: level.price))

    # These two go through the model's own before-validator
    # (`_parse_optional_scalar_decimal`), which accepts arbitrary wire input
    # (including absence) and either parses it or raises `ValueError` — the
    # `Any` annotations here describe the wire value, not the validated field.
    tick_size_raw: Any = event.get("tick_size")
    last_trade_price_raw: Any = event.get("last_trade_price")

    return WsBookSnapshotV1(
        condition_id=event.get("market", ""),
        asset_id=event.get("asset_id", ""),
        bids=bids_sorted,
        asks=asks_sorted,
        tick_size=tick_size_raw,
        last_trade_price=last_trade_price_raw,
    )


def _build_level(entry: Any, *, side: BookSide) -> OrderBookLevel:
    """Parse one wire level. See the module docstring, point 3, for the zero-size decision."""
    if not isinstance(entry, Mapping):
        raise ValueError(f"{side.value} level must be a JSON object, got {type(entry).__name__}")
    extra_keys = set(entry.keys()) - {"price", "size"}
    if extra_keys:
        raise ValueError(f"{side.value} level has unexpected fields: {sorted(extra_keys)}")
    if "price" not in entry or "size" not in entry:
        raise ValueError(f"{side.value} level is missing 'price' or 'size': {dict(entry)!r}")

    price = parse_wire_decimal(entry["price"])
    if not (MIN_PRICE <= price <= MAX_PRICE):
        raise ValueError(f"{side.value} level price {price} is outside the valid [0, 1] range")

    size = parse_wire_decimal(entry["size"])
    if size <= 0:
        raise ValueError(
            f"{side.value} level at price {price} has size {size}, which is not a resting "
            "order; a zero-size level inside a book snapshot has no anomaly sink in this "
            "model and is refused rather than silently dropped. Never observed in any of "
            "the four real `book` events in the recorded fixture — see the module "
            "docstring, point 3"
        )
    return OrderBookLevel(price=price, size=size)


def _validate_side_ordering(
    levels: Sequence[OrderBookLevel], *, side: BookSide, descending: bool
) -> None:
    """Refuse a snapshot whose stored levels are not canonically ordered.

    The same rule, and the same refusal-over-coercion reasoning, as
    ``argos.domain.orderbook._validate_side_ordering`` and
    ``argos.projections.book._validate_side_order``: this runs on *every*
    construction path, not only :func:`parse_ws_book_snapshot`, and a
    directly built or replayed snapshot with unsorted or duplicated levels is
    refused, never silently re-sorted or deduplicated.
    """
    prices = [level.price for level in levels]
    if len(set(prices)) != len(prices):
        raise ValueError(f"duplicate {side.value} price level(s) in {prices}")
    direction = "descending" if descending else "ascending"
    ordered = sorted(prices, reverse=descending)
    if prices != ordered:
        raise ValueError(
            f"{side.value} levels must be sorted {direction} by price (best level first), "
            f"got {prices}"
        )
