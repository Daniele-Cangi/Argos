"""The order-book snapshot payload: ARGOS's typed view of a public CLOB `/book` response.

This is the first typed canonical payload named by
:attr:`argos.domain.observation.ObservationEnvelopeV1.payload_schema_version`
(``"order_book_snapshot.v1"``). It is a normalized *payload*, not the envelope:
event time, source hash, and provenance live on
:class:`argos.domain.observation.ObservationEnvelopeV1`, per
:func:`argos.domain.observation.build_observation_envelope`. Nothing here reads a
clock, performs I/O, or knows about HTTP; the source adapter that fetches the
raw bytes and calls :func:`parse_order_book_snapshot` is a later M2 slice.

Every decision below is measured against
``docs/research/m2-clob-rest-book.md``, not assumed:

1. **Decimal identity.** ADR-0010 hashes this model's canonical JSON to derive
   ``observation_id``. The real endpoint returns the same price at two text
   precisions (``"0.430"`` from ``/book``, ``"0.43"`` from ``/last-trade-price``),
   so every ``Decimal`` field is normalized on the way in (see
   :func:`normalize_decimal`) — trailing zeros are stripped, and a normalized
   ``Decimal`` never renders in scientific notation, so ``str(value)`` is a
   deterministic canonical text for identical prices regardless of how the
   source spelled them.
2. **No float on the path.** Every price/size validator rejects a Python
   ``float`` outright, even if a caller's own JSON decoding produced one (for
   example by not passing ``parse_float=Decimal``). ``bool`` is also rejected
   explicitly, since it is a ``int`` subclass and would otherwise silently
   coerce to ``Decimal(0)``/``Decimal(1)``.
3. **Level ordering.** The measured wire order is bids ascending / asks
   descending — both arrays end at top of book, which is exactly backwards from
   the naive ``[0]`` reading. This module does not preserve or trust the wire
   order: :func:`parse_order_book_snapshot` sorts explicitly into the
   conventional "best level first" form (bids descending, asks ascending), and
   the stored ``bids``/``asks`` tuples are validated to already be in that order
   on every construction path, not only when built from wire data — a directly
   constructed or replayed snapshot with unsorted levels is refused, not
   silently re-sorted, per the project's no-silent-coercion rule.
4. **Full book preserved.** Every level survives (core invariant 2): this model
   carries the whole book, not a summary. ``best_bid``/``best_ask``/``spread``/
   ``derived_midpoint`` are computed properties, never stored fields, so there
   is exactly one source of truth for them and no way for a stored midpoint to
   be mistaken for a field the source actually sent. ``derived_midpoint`` is
   explicitly documented as not a tradeable price.
5. **Zero-size levels.** Never observed in a REST snapshot per the research
   note, but handled explicitly rather than assumed impossible: a zero-size
   level is *dropped* from the stored book (a resting order of size zero is not
   a resting order) and the drop is recorded as a counted, reasoned
   :class:`OrderBookAnomaly` — never silently discarded. A *negative* size,
   which has no honest interpretation, refuses the whole snapshot instead.
6. **Other anomalies.** A crossed book (best bid > best ask), a locked book
   (best bid == best ask), and an off-tick price are all accepted, not
   rejected, and recorded as :class:`OrderBookAnomaly` entries — the source
   really did report that state, and refusing it would erase a genuine
   observation the way ADR-0010 already documents for a reverted book. A
   *duplicate* price level on one side is refused outright: unlike a zero-size
   level, there is no non-arbitrary way to decide which of two conflicting
   entries for the same price is the real one, so keeping either would be a
   guess dressed up as data.

Not decided here, left for the adapter/ingestion slice: how a raised
:class:`ValueError` becomes a
:class:`argos.errors.RejectionReason`/``RejectedObservationV1`` entry. This
mirrors ``argos.domain.market`` exactly: :class:`MarketDefinitionV1` raises
plain ``ValueError`` from its validators, and ``argos.ingestion.gamma_markets``
is the layer that catches it and writes ``QuarantinedMarketV1``. This module
does the same for the same reason: a domain payload model must not need to
know about the rejection ledger to be tested or reused.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Context, Decimal, InvalidOperation, localcontext
from enum import StrEnum
from typing import Any, ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from argos.domain.market import CONDITION_ID_PATTERN, TOKEN_ID_PATTERN
from argos.domain.versioning import VersionedModel

# Prices are share prices on a $0-$1 payout, not a claim of calibrated
# probability (CLAUDE.md forbids that word for anything uncalibrated) — this is
# simply the range every price on this source is contractually confined to.
MIN_PRICE = Decimal(0)
MAX_PRICE = Decimal(1)

MAX_SIGNIFICANT_DIGITS: Final = 34
"""Most significant digits a wire price/size may carry and still be canonically
representable. See :func:`normalize_decimal` for why this is a refusal rather
than a wider decimal context. 34 is decimal128's coefficient width — far above
the ~10 digits the real recorded fixtures use, and low enough that a canonical
text stays short."""

MAX_DECIMAL_EXPONENT: Final = 30
"""Largest adjusted exponent a non-zero wire price/size may carry. Bounds the
length of the canonical text: without it, the 11 wire bytes ``"1E+1000000"``
would render as a one-megabyte canonical integer."""

MIN_DECIMAL_EXPONENT: Final = -1000
"""Smallest adjusted exponent a non-zero wire price/size may carry.

This bound existed, was removed as unnecessary, and is restored here because
removing it created a defect. The argument for removal was that only a
*positive* exponent reaches the quantize branch that expands a value into long
plain text, while a very negative exponent keeps a short scientific rendering
that is just as deterministic. That reasoning was about *text length* and
overlooked *arithmetic*: ``Decimal.normalize()`` runs inside
:data:`CANONICAL_DECIMAL_CONTEXT`, whose ``Emin`` is finite, so a sufficiently
small non-zero value **underflows to zero**. Reproduced:
``parse_wire_decimal("1E-1000064")`` returned ``Decimal(0)`` and rendered as
the canonical text ``"0"`` — numerically identical to a genuine zero, so a
tiny non-zero price and a real zero produced the **same identity-bearing
payload**. That is a silent mutation of a price, precisely the class the
canonical context was introduced to close, reintroduced one field over.

``-1000`` is far below anything this source can produce (the real tick size is
``0.001``, adjusted exponent ``-3``) and comfortably preserves the committed
``1E-50`` case. It is enforced together with an equality postcondition in
:func:`normalize_decimal`, so the bound is a fast, legible refusal and the
postcondition is the fail-closed backstop that does not depend on anyone
having reasoned correctly about ``Emin``."""

_MAX_INT_BITS: Final = 4096
"""Bit-length ceiling for an ``int`` handed to :func:`parse_wire_decimal`.

Comfortably above any legal value — the budgets below cap accepted magnitudes
far lower — and checked before the superlinear ``Decimal(int)`` conversion
rather than after it. See :func:`parse_wire_decimal` for the measurement."""

_CONTEXT_EXPONENT_MARGIN: Final = MAX_SIGNIFICANT_DIGITS + 100
"""How far the pinned context's exponent range sits outside the accepted
budget, so no value this module accepts can underflow or overflow inside it."""

CANONICAL_DECIMAL_CONTEXT: Final = Context(
    prec=MAX_SIGNIFICANT_DIGITS + MAX_DECIMAL_EXPONENT + 1,
    Emin=MIN_DECIMAL_EXPONENT - _CONTEXT_EXPONENT_MARGIN,
    Emax=MAX_DECIMAL_EXPONENT + _CONTEXT_EXPONENT_MARGIN,
)
"""The one decimal context every identity-bearing decimal operation in ARGOS
runs inside.

``normalize()``, ``quantize()`` and ``%`` all read ``decimal.getcontext()``,
which is thread-local mutable global state — precisely the hidden global state
CLAUDE.md prohibits, and a direct threat to ADR-0010 identity determinism and
core invariant 5. Pinning it here makes the canonical form of a wire value a
pure function of that value. The precision is deliberately wide enough that no
value passing the budgets above can round inside it, so the budgets, not the
context, are what refuse an out-of-range value — and they do it with a reason,
before any context-sensitive operation runs.

``Emin``/``Emax`` are set **explicitly** rather than left to default. An
unspecified ``Context`` field is filled from ``decimal.DefaultContext`` at
construction time, which is itself mutable process-global state — the same
class of dependency this context exists to remove, merely moved from read time
to import time. They are also what makes the underflow described in
:data:`MIN_DECIMAL_EXPONENT` structurally impossible for an accepted value,
rather than merely unlikely."""


class BookSide(StrEnum):
    """Which side of the book a level or anomaly belongs to."""

    BID = "bid"
    ASK = "ask"


class OrderBookAnomalyKind(StrEnum):
    """A specific, named defect in an otherwise-accepted order book snapshot.

    Distinct from :class:`argos.domain.observation.ObservationQualityFlag`,
    which lives on the envelope and is out of scope for this file to add to
    (see the module docstring). If the adapter slice wants an envelope-level
    flag for "this observation's payload carries a book anomaly", these are
    the values it would fold in.
    """

    ZERO_SIZE_LEVEL_DROPPED = "zero_size_level_dropped"
    """A level with size 0 was present in the source response and was removed
    from the stored book rather than kept as a phantom resting order."""

    OFF_TICK_PRICE = "off_tick_price"
    """A level's price is not a multiple of the book's own declared tick size."""

    CROSSED_BOOK = "crossed_book"
    """The best bid strictly exceeds the best ask."""

    LOCKED_BOOK = "locked_book"
    """The best bid equals the best ask."""


class OrderBookAnomaly(BaseModel):
    """One recorded defect. Never silent — core invariant 14, `.claude/rules/data-integrity.md`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: OrderBookAnomalyKind
    side: BookSide | None = None
    price: Decimal | None = None
    detail: str = Field(min_length=1)

    @field_validator("price", mode="before")
    @classmethod
    def _parse_price(cls, value: Any) -> Any:
        return value if value is None else parse_wire_decimal(value)


class OrderBookLevel(BaseModel):
    """One resting price level. ``size`` is never zero or negative by construction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    price: Decimal = Field(ge=MIN_PRICE, le=MAX_PRICE)
    size: Decimal = Field(gt=0)

    @field_validator("price", "size", mode="before")
    @classmethod
    def _parse(cls, value: Any) -> Decimal:
        return parse_wire_decimal(value)


class OrderBookSnapshotV1(VersionedModel):
    """A normalized public CLOB order-book snapshot for one token.

    Carries exactly the fields the real ``/book`` response has, minus
    ``timestamp`` and ``hash``: those name the *event time* and *content hash*
    of the observation, which belong on
    :class:`argos.domain.observation.ObservationEnvelopeV1` (``event_time`` and
    ``source_hash``), not duplicated here. Putting them on both would create two
    copies that can disagree, exactly the failure
    ``MarketDefinitionV1``/``payload_schema_version`` are designed to avoid
    elsewhere in this codebase.

    ``market_id`` in the wire response is Polymarket's condition id, not a
    Gamma market id; this model names the field ``condition_id`` to match
    :class:`argos.domain.market.MarketDefinitionV1`'s terminology instead of
    perpetuating the source's overloaded ``market`` field name.
    """

    schema_version: ClassVar[str] = "order_book_snapshot.v1"

    condition_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)

    bids: tuple[OrderBookLevel, ...]
    """Sorted descending by price: ``bids[0]`` is always the best bid, or the
    tuple is empty. Never trust an externally-supplied order without this
    validator re-checking it — see :func:`_validate_side_ordering`."""

    asks: tuple[OrderBookLevel, ...]
    """Sorted ascending by price: ``asks[0]`` is always the best ask, or the
    tuple is empty."""

    tick_size: Decimal = Field(gt=0, le=MAX_PRICE)
    min_order_size: Decimal = Field(gt=0)
    neg_risk: bool
    last_trade_price: Decimal = Field(ge=MIN_PRICE, le=MAX_PRICE)

    anomalies: tuple[OrderBookAnomaly, ...] = ()
    """Every defect this snapshot carries, dropped-level and structural alike.
    A directly-constructed snapshot's *structural* anomalies (off-tick,
    crossed, locked) must exactly match what ``bids``/``asks``/``tick_size``
    actually contain — see :func:`_derive_structural_anomalies` and the
    recompute check below. ``ZERO_SIZE_LEVEL_DROPPED`` entries are the one
    exception: the dropped level is gone from ``bids``/``asks`` by
    construction, so nothing here can recompute it from the final state, and it
    is trusted as a source-time fact instead, the same way
    ``recompute_observation_id`` cannot recover fields an identity was never
    given."""

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

    @field_validator("tick_size", "min_order_size", "last_trade_price", mode="before")
    @classmethod
    def _parse_scalar_decimal(cls, value: Any) -> Decimal:
        return parse_wire_decimal(value)

    @field_validator("neg_risk", mode="before")
    @classmethod
    def _reject_non_bool(cls, value: Any) -> Any:
        if not isinstance(value, bool):
            raise ValueError(f"neg_risk must be a boolean, got {value!r}")
        return value

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

    @model_validator(mode="after")
    def _validate_anomalies_are_recomputable(self) -> OrderBookSnapshotV1:
        """A stored anomaly list cannot silently drift from the book it describes.

        Every *structural* anomaly kind is re-derived from ``bids``/``asks``/
        ``tick_size`` and compared as a set (order is not semantically
        meaningful here, unlike level ordering). A snapshot claiming an
        anomaly its own levels do not exhibit — or omitting one they do — is
        refused, the same auditability ``recompute_observation_id`` gives the
        envelope.
        """
        recomputed = _derive_structural_anomalies(self.bids, self.asks, self.tick_size)
        expected = {_anomaly_key(a) for a in recomputed}
        actual = {
            _anomaly_key(a)
            for a in self.anomalies
            if a.kind != OrderBookAnomalyKind.ZERO_SIZE_LEVEL_DROPPED
        }
        if actual != expected:
            raise ValueError(
                "anomalies do not match what bids/asks/tick_size actually contain: "
                f"stored={sorted(actual, key=repr)} recomputed={sorted(expected, key=repr)}"
            )
        return self

    @property
    def best_bid(self) -> Decimal | None:
        """The highest resting bid price, or ``None`` if the book has no bids."""
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        """The lowest resting ask price, or ``None`` if the book has no asks."""
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> Decimal | None:
        """``best_ask - best_bid``, or ``None`` if either side is empty."""
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def derived_midpoint(self) -> Decimal | None:
        """The arithmetic mean of best bid and best ask.

        This is **not an executable price** — core invariant 2 forbids treating
        a midpoint as tradeable. It exists only because it is trivially
        derivable and the research doc shows a dedicated ``/midpoint`` endpoint
        this book snapshot already makes redundant; the name says "derived" so
        no downstream reader can mistake it for a quote the source sent.
        """
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2


def parse_order_book_snapshot(payload: Mapping[str, Any]) -> OrderBookSnapshotV1:
    """Build a canonical snapshot from an already-JSON-decoded ``/book`` response body.

    ``payload`` is the plain mapping produced by decoding the response text
    (ideally, though not required, with ``parse_float=Decimal`` — this function
    defends against a float slipping through either way, see the module
    docstring). No network, filesystem, or clock access happens here.

    Raises :class:`ValueError` (via a nested Pydantic ``ValidationError`` for
    the final construction step) for anything this function judges cannot be
    honestly represented: a non-object payload, a missing or malformed
    required field, a negative-size level, a duplicate price level, or a
    level/scalar field this module cannot parse as a finite, non-float
    decimal. It never returns a partially-valid snapshot.
    """
    if not isinstance(payload, Mapping):
        raise ValueError(f"order book payload must be a JSON object, got {type(payload).__name__}")

    raw_bids = payload.get("bids")
    raw_asks = payload.get("asks")
    if not isinstance(raw_bids, list):
        raise ValueError(f"'bids' must be an array, got {type(raw_bids).__name__}")
    if not isinstance(raw_asks, list):
        raise ValueError(f"'asks' must be an array, got {type(raw_asks).__name__}")

    tick_size = parse_wire_decimal(payload.get("tick_size"))
    if tick_size <= 0:
        raise ValueError(f"tick_size must be positive, got {tick_size}")

    dropped: list[OrderBookAnomaly] = []
    bid_levels: list[OrderBookLevel] = []
    ask_levels: list[OrderBookLevel] = []
    for raw_entry in raw_bids:
        level, anomaly = _build_level(raw_entry, side=BookSide.BID)
        if anomaly is not None:
            dropped.append(anomaly)
        elif level is not None:
            bid_levels.append(level)
    for raw_entry in raw_asks:
        level, anomaly = _build_level(raw_entry, side=BookSide.ASK)
        if anomaly is not None:
            dropped.append(anomaly)
        elif level is not None:
            ask_levels.append(level)

    # Canonical order is "best level first" on both sides. The measured wire
    # order is the opposite for bids (ascending) — see the module docstring.
    # Do not trust or preserve the wire order; sort explicitly every time.
    bids_sorted = tuple(sorted(bid_levels, key=lambda level: level.price, reverse=True))
    asks_sorted = tuple(sorted(ask_levels, key=lambda level: level.price))

    structural = _derive_structural_anomalies(bids_sorted, asks_sorted, tick_size)

    # These three fields go through the model's own before-validators
    # (``_parse_scalar_decimal``/``_reject_non_bool``), which accept arbitrary
    # wire input and either parse it or raise ``ValueError`` — the ``Any``
    # annotations here are load-bearing, not a shortcut, because the static
    # field types describe the *validated* result, not what a caller may pass.
    min_order_size_raw: Any = payload.get("min_order_size")
    neg_risk_raw: Any = payload.get("neg_risk")
    last_trade_price_raw: Any = payload.get("last_trade_price")

    return OrderBookSnapshotV1(
        condition_id=payload.get("market", ""),
        asset_id=payload.get("asset_id", ""),
        bids=bids_sorted,
        asks=asks_sorted,
        tick_size=tick_size,
        min_order_size=min_order_size_raw,
        neg_risk=neg_risk_raw,
        last_trade_price=last_trade_price_raw,
        anomalies=tuple(dropped) + structural,
    )


def _build_level(
    entry: Any, *, side: BookSide
) -> tuple[OrderBookLevel | None, OrderBookAnomaly | None]:
    """Parse one wire level, or explain why it is not represented as one.

    Exactly one of the two return values is not ``None``: a level, or the
    anomaly explaining its absence. Never both, never neither.
    """
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
    if size == 0:
        return None, OrderBookAnomaly(
            kind=OrderBookAnomalyKind.ZERO_SIZE_LEVEL_DROPPED,
            side=side,
            price=price,
            detail=(
                f"{side.value} level at price {price} had size 0 in the source response; "
                "dropped rather than stored as a resting order. Never observed in REST "
                "research to date (docs/research/m2-clob-rest-book.md); handled explicitly "
                "in case it appears."
            ),
        )
    if size < 0:
        raise ValueError(f"{side.value} level at price {price} has a negative size: {size}")
    return OrderBookLevel(price=price, size=size), None


def _validate_side_ordering(
    levels: Sequence[OrderBookLevel], *, side: BookSide, descending: bool
) -> None:
    """Refuse a snapshot whose stored levels are not canonically ordered.

    This runs on *every* construction path, not only :func:`parse_order_book_snapshot`
    — a directly built or replayed snapshot with unsorted or duplicated levels
    is refused, never silently re-sorted or deduplicated. Silent coercion of a
    caller's data is exactly what the project rules forbid; a caller that wants
    a canonical snapshot from raw wire data should call
    :func:`parse_order_book_snapshot`, which sorts explicitly before
    construction.
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


def _derive_structural_anomalies(
    bids: Sequence[OrderBookLevel], asks: Sequence[OrderBookLevel], tick_size: Decimal
) -> tuple[OrderBookAnomaly, ...]:
    """Anomalies fully determined by the final ``bids``/``asks``/``tick_size``.

    Deliberately excludes ``ZERO_SIZE_LEVEL_DROPPED``: that fact only exists at
    parse time, before the offending level is removed, and cannot be recovered
    from the levels that remain.
    """
    anomalies: list[OrderBookAnomaly] = []
    for side, levels in ((BookSide.BID, bids), (BookSide.ASK, asks)):
        for level in levels:
            if _is_off_tick(level.price, tick_size):
                anomalies.append(
                    OrderBookAnomaly(
                        kind=OrderBookAnomalyKind.OFF_TICK_PRICE,
                        side=side,
                        price=level.price,
                        detail=f"price {level.price} is not aligned to tick size {tick_size}",
                    )
                )
    if bids and asks:
        best_bid = bids[0].price
        best_ask = asks[0].price
        if best_bid > best_ask:
            anomalies.append(
                OrderBookAnomaly(
                    kind=OrderBookAnomalyKind.CROSSED_BOOK,
                    detail=f"best bid {best_bid} exceeds best ask {best_ask}",
                )
            )
        elif best_bid == best_ask:
            anomalies.append(
                OrderBookAnomaly(
                    kind=OrderBookAnomalyKind.LOCKED_BOOK,
                    detail=f"best bid equals best ask at {best_bid}",
                )
            )
    return tuple(anomalies)


def _anomaly_key(
    anomaly: OrderBookAnomaly,
) -> tuple[OrderBookAnomalyKind, BookSide | None, Decimal | None]:
    return (anomaly.kind, anomaly.side, anomaly.price)


def _is_off_tick(price: Decimal, tick_size: Decimal) -> bool:
    # Inside the pinned context for the same reason :func:`normalize_decimal`
    # is: ``%`` is a context-sensitive decimal operation, and the anomaly set it
    # feeds is a *stored field* of ``OrderBookSnapshotV1``, so an ambient
    # context could otherwise change the stored payload and therefore the
    # ``observation_id`` for identical wire bytes.
    with localcontext(CANONICAL_DECIMAL_CONTEXT):
        return price % tick_size != 0


def parse_wire_decimal(value: Any) -> Decimal:
    """Parse a wire price/size into a scale-normalized, finite ``Decimal``.

    Refuses ``float`` and ``bool`` outright — see the module docstring, point
    2. ``str`` and ``int`` are parsed from their own text via ``Decimal()``
    directly, never via a ``float`` intermediate, so no binary rounding ever
    touches a price or size on this path.

    Public and reused outside this module: ``docs/STATUS.md`` records that the
    negative-zero and trailing-zero decimal-identity class (see
    :func:`normalize_decimal` below) has already recurred twice independently
    across two payload models. Every future payload model with a price/size
    field must call this function rather than reimplement decimal parsing.
    """
    if isinstance(value, bool):
        raise ValueError(f"a boolean is not a valid decimal value, got {value!r}")
    if isinstance(value, float):
        raise ValueError(
            "a float is not permitted for a price/size/decimal field; source values must "
            f"be parsed as Decimal from their original text, got {value!r}"
        )
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str | int):
        if isinstance(value, int):
            # Checked *before* ``Decimal(value)`` because converting a huge
            # Python ``int`` to ``Decimal`` is superlinear: measured at 63.1 s
            # of CPU for a 1,000,001-digit int, which no ``Pacer`` can bound
            # (a cancel scope cannot interrupt synchronous CPU work). The
            # magnitude budget in :func:`normalize_decimal` would have refused
            # the value, but only *after* paying that cost — the same
            # "the bound runs downstream of the work it is meant to bound"
            # shape as the earlier ``neutralize_and_bound`` finding.
            #
            # ``bit_length`` is O(1) on CPython. Not currently reachable from
            # this project's own JSON decoding, which is a good reason to keep
            # the check cheap rather than a good reason to omit it: CPython's
            # 4300-digit ``int_max_str_digits`` default is what blocks it
            # today, and that is a third-party default this module does not
            # own and must not silently depend on.
            _refuse_oversized_int(value)
        try:
            parsed = Decimal(value)
        except InvalidOperation as error:
            raise ValueError(f"not a valid decimal string: {value!r}") from error
    else:
        raise ValueError(f"unsupported type for a decimal field: {type(value).__name__}")
    if not parsed.is_finite():
        raise ValueError(f"decimal value must be finite, got {value!r}")
    return normalize_decimal(parsed)


def _refuse_oversized_int(value: int) -> None:
    """Refuse an ``int`` too large to convert cheaply. See :func:`parse_wire_decimal`."""
    bits = value.bit_length()
    if bits > _MAX_INT_BITS:
        raise ValueError(
            f"integer decimal value spans {bits} bits, above the {_MAX_INT_BITS}-bit "
            "budget; refused before conversion, which is superlinear in the operand size"
        )


def normalize_decimal(value: Decimal) -> Decimal:
    """Return ``value`` at minimal scale, never in scientific notation.

    ``Decimal("0.430").normalize()`` correctly collapses to ``Decimal("0.43")``,
    but ``Decimal("500").normalize()`` collapses to ``Decimal("5E+2")`` —
    ``str()`` of that is ``"5E+2"``, not ``"500"``, which would be a second,
    different-looking canonical text for the same integer value. Quantizing
    back to a zero exponent whenever ``normalize()`` produced a positive one
    restores plain-integer text while keeping the trailing-zero stripping that
    ADR-0010 requires.

    **Negative zero is collapsed onto positive zero.** ``Decimal("-0")``
    survives ``normalize()`` as ``Decimal('-0')`` with exponent 0, so the
    quantize branch above never fires, while ``Decimal('-0') == Decimal('0')``
    is ``True`` — the value passes a ``ge=MIN_PRICE`` range check and then
    renders as the distinct canonical text ``"-0"``. Because identity hashes
    the rendered canonical payload rather than the ``Decimal`` value, one
    economic price spelled two ways minted two ``observation_id``s: exactly
    the failure ADR-0010 names as a constraint on every payload model, and
    exactly what this module's own docstring claims not to allow
    ("identical prices regardless of how the source spelled them"). Only the
    two fields whose valid range includes the boundary 0 are reachable —
    ``last_trade_price`` and a level's ``price``; ``tick_size`` and
    ``min_order_size`` are ``gt=0``, which ``-0`` fails. Found by adversarial
    testing, not by review of this function.

    **The magnitude budget, and why it is a refusal rather than a wider
    context.** ``normalize()`` and ``quantize()`` are both *context-sensitive*
    decimal operations, so this function's output used to depend on
    ``decimal.getcontext()`` — thread-local mutable global state. Reproduced
    directly: the wire price
    ``"0.123456789012345678901234567890123"`` normalized to
    ``0.1234567890123456789012345679`` under the default precision of 28,
    to ``0.12346`` under an ambient ``prec=5``, and to its full text under
    ``prec=50``. Three different canonical texts, therefore three different
    ``observation_id``s, for one set of wire bytes. That is the hidden global
    state CLAUDE.md prohibits outright, and it breaks core invariant 5: a
    replay under a different ambient context would mint different identities
    for identical input. Separately, ``quantize`` raised a bare
    ``decimal.InvalidOperation`` — outside the ARGOS error taxonomy, and
    therefore past ``argos.ingestion.clob_book``'s ``except ValueError`` —
    for any value needing more integer digits than the ambient precision, so
    a 214-byte body containing ``"tick_size": "1E+29"`` escaped
    ``normalize_clob_book`` entirely and produced **no rejection ledger
    entry**: the silent drop core invariant 14 exists to prevent.

    Both are closed by pinning :data:`CANONICAL_DECIMAL_CONTEXT` explicitly
    and refusing, as a counted ``ValueError``, any value outside an explicit
    magnitude budget. The budget is checked *before* any context-sensitive
    operation runs, so no accepted value ever depends on the trap firing.
    Widening the context instead would be a fresh resource-exhaustion vector
    rather than a fix: ``"1E+1000000"`` is 11 wire bytes that would render as
    a one-megabyte canonical integer. The budget is far above anything this
    source can legitimately produce — real observed sizes reach ~4.2e5 with
    at most 10 significant digits, against a budget of
    ``MAX_SIGNIFICANT_DIGITS`` digits — so no real value is refused; a
    regression test pins that the real recorded fixtures still parse
    unchanged.
    """
    digits = len(value.as_tuple().digits)
    if digits > MAX_SIGNIFICANT_DIGITS:
        raise ValueError(
            f"decimal value carries {digits} significant digits, above the "
            f"{MAX_SIGNIFICANT_DIGITS}-digit canonical budget; refused rather than "
            "silently rounded to the ambient decimal precision"
        )
    # Every spelling of zero collapses onto one canonical zero and is returned
    # before the exponent bounds below, which describe *non-zero* magnitudes:
    # ``Decimal("0E-100000")`` is still exactly zero, and refusing it for its
    # exponent would refuse a value that carries no magnitude at all.
    if value.is_zero():
        return Decimal(0)

    adjusted = value.adjusted()
    if adjusted > MAX_DECIMAL_EXPONENT:
        raise ValueError(
            f"decimal value has adjusted exponent {adjusted}, above the "
            f"+{MAX_DECIMAL_EXPONENT} canonical budget; refused rather than rendered "
            "as an unboundedly long canonical integer text"
        )
    if adjusted < MIN_DECIMAL_EXPONENT:
        # See MIN_DECIMAL_EXPONENT: without this, a non-zero value this small
        # underflows to zero inside the pinned context and becomes
        # indistinguishable from a genuine zero.
        raise ValueError(
            f"decimal value has adjusted exponent {adjusted}, below the "
            f"{MIN_DECIMAL_EXPONENT} canonical budget; refused rather than allowed to "
            "underflow to zero and collide with a genuine zero's identity"
        )

    with localcontext(CANONICAL_DECIMAL_CONTEXT):
        normalized = value.normalize()
        exponent = normalized.as_tuple().exponent
        if isinstance(exponent, int) and exponent > 0:
            normalized = normalized.quantize(Decimal(1))

    # Fail-closed postcondition. The bounds above are a fast, legible refusal;
    # this is what guarantees the property they are only *believed* to imply —
    # canonicalization must never silently change a finite non-zero numeric
    # value. It does not depend on anyone having reasoned correctly about
    # ``Emin``, ``prec``, or which branch expands which exponent, which is
    # exactly how the underflow described in MIN_DECIMAL_EXPONENT survived
    # review: the reasoning was about text length and the defect was
    # arithmetic. Note ``Decimal("-0") == Decimal("0")`` is ``True``, so the
    # deliberate negative-zero collapse above passes this check rather than
    # being caught by it.
    if normalized != value:
        raise ValueError(
            f"canonicalization changed the numeric value: {value} became {normalized}; "
            "refused rather than silently storing a value the source never sent"
        )
    return normalized
