"""The `price_change` delta payload: ARGOS's typed view of one CLOB market-channel delta group.

This is the second typed canonical payload named by
:attr:`argos.domain.observation.ObservationEnvelopeV1.payload_schema_version`
(``"price_change.v1"``), after ``order_book_snapshot.v1``
(:mod:`argos.domain.orderbook`). It is a normalized *payload*, not the
envelope: event time and source hash live on
:class:`argos.domain.observation.ObservationEnvelopeV1` (``event_time`` and
``source_hash``), per :func:`argos.domain.observation.build_observation_envelope`.
Nothing here reads a clock, performs I/O, or knows about HTTP or WebSockets;
the WebSocket adapter that receives the raw frame and calls
:func:`parse_price_change_group` is a later M2 slice, not this one.

Every decision below is measured against
``docs/research/m2-clob-websocket.md``, not assumed:

1. **Decimal identity, reused not reimplemented.** ADR-0010 hashes this
   model's canonical JSON to derive ``observation_id``. ``docs/STATUS.md``
   records that the negative-zero and trailing-zero decimal-identity class
   has already recurred twice independently across the observation envelope
   and ``OrderBookSnapshotV1``. This module reuses
   :func:`argos.domain.orderbook.parse_wire_decimal` /
   :func:`argos.domain.orderbook.normalize_decimal` rather than reimplementing
   decimal normalization — the third guard against that class, not a fourth
   independent instance of the bug.
2. **No float on the path.** Inherited from :func:`parse_wire_decimal`: every
   price/size field refuses a Python ``float`` or ``bool`` outright, even if
   the caller's own JSON decoding produced one.
3. **Zero-size means removal, not an anomaly.** Live traffic confirms
   directly (``docs/research/m2-clob-websocket.md``, "Priority question 3")
   that a ``price_change`` entry with ``"size": "0"`` represents *removal* of
   that price level — this is normal, documented-and-observed delta-stream
   semantics, not a defect. This is deliberately unlike
   :mod:`argos.domain.orderbook`, where a zero-size level inside a *full REST
   snapshot* is recorded as a counted :class:`~argos.domain.orderbook.OrderBookAnomaly`
   (``ZERO_SIZE_LEVEL_DROPPED``): a snapshot describing a resting order of
   size zero is a source defect, while a delta stream *removing* a level via
   size zero is the source's only vocabulary for removal, confirmed live, not
   inferred from documentation text alone. A directly-constructed or replayed
   :class:`PriceLevelChangeV1` claiming ``SET`` with size 0, or ``REMOVE``
   with nonzero size, is refused rather than silently re-derived — the same
   "recomputable, never drifting" discipline
   :meth:`argos.domain.orderbook.OrderBookSnapshotV1._validate_anomalies_are_recomputable`
   applies to structural anomalies. A *negative* size has no honest
   interpretation and refuses the whole group, matching
   :mod:`argos.domain.orderbook`.
4. **No sequence number; grouping is by ``(asset_id, timestamp, hash)``.**
   ``docs/research/m2-clob-websocket.md`` confirms, by direct observation
   across two live captures, that this channel has no sequence field of any
   kind. A ``(timestamp, hash)`` pair names a *post-state*, not a wire
   message, and the same pair for one token has been observed spread across
   up to three separate WebSocket frames, each carrying one distinct price
   level change (see :func:`parse_price_change_group`'s docstring and
   ``tests/test_price_change.py``'s identity-hazard test). ``timestamp`` and
   ``hash`` are therefore not stored as model fields — like
   ``OrderBookSnapshotV1``, they name the observation's ``event_time`` and
   ``source_hash`` and belong on the envelope — but every *level change* the
   selected entries carry is preserved in :attr:`PriceChangeV1.changes`,
   because dropping it would let ADR-0010 identity (which hashes only the
   canonical payload) collapse three genuinely different level changes onto
   one ``observation_id`` and silently lose two of them.
5. **Cross-token frames are real, not a defect.** The research note found
   that subscribing to one token delivers ``price_change`` entries for its
   unsubscribed binary sibling in the same frame. :func:`parse_price_change_group`
   filters by the caller-supplied ``asset_id`` rather than trusting "this
   frame is about my token" — the same "never trust wire scope" discipline
   :mod:`argos.domain.orderbook` applies to wire order.
6. **``source_best_bid``/``source_best_ask`` are named, not ``best_bid``/``best_ask``.**
   Core invariant 2 requires bid, ask, spread, and depth to be preserved, and
   forbids treating a midpoint as executable — the same discipline extends
   here: these are the *source's own asserted* top-of-book at the moment of
   this delta, not a value ARGOS derives, so the field names make that
   impossible to mistake. If the selected entries within one group disagree
   on either value, the whole group is refused rather than one disagreeing
   value being picked arbitrarily — measured: every group in the real capture
   agreed.

Not decided here, left for the adapter/ingestion slice, exactly like
:mod:`argos.domain.orderbook`: how a raised :class:`ValueError` becomes a
:class:`argos.errors.RejectionReason`/``RejectedObservationV1`` entry, and how
``ingest_sequence`` is assigned. This module does not know about the
rejection ledger, the event store, or the WebSocket connection it will
eventually sit behind.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from argos.domain.market import CONDITION_ID_PATTERN, TOKEN_ID_PATTERN
from argos.domain.orderbook import (
    MAX_PRICE,
    MIN_PRICE,
    BookSide,
    parse_wire_decimal,
)
from argos.domain.versioning import VersionedModel

EXPECTED_EVENT_KEYS = frozenset({"event_type", "market", "timestamp", "price_changes"})
"""The top-level keys a real ``price_change`` frame carries, per
``docs/research/m2-clob-websocket.md`` and verified against the recorded live
capture. Checked for the same reason :data:`EXPECTED_ENTRY_KEYS` is, one level
up: independent adversarial testing found that an unrecognized *top-level* key
was silently ignored while an unrecognized *entry* key was refused — so a
future field, including one spelled exactly like the sequence number this
channel is currently confirmed to lack, would have arrived, been discarded,
and never been counted or reasoned about (core invariant 14). Refusing means a
source schema change halts ingestion loudly with a ledger entry rather than
being absorbed in silence; that is the same trade already made at entry level,
and the visible failure is the point."""

EXPECTED_ENTRY_KEYS = frozenset(
    {"asset_id", "price", "size", "side", "hash", "best_bid", "best_ask"}
)
"""The wire keys a real ``price_changes`` array entry carries, per
``docs/research/m2-clob-websocket.md``. An entry with any other key is refused
rather than silently accepted with the unknown field dropped."""


class NoEntriesForToken(ValueError):
    """The frame carried no ``price_changes`` entry for the requested token.

    A distinct type rather than a plain ``ValueError`` with a recognisable
    message, because this is the one outcome of
    :func:`parse_price_change_group` that is **not** a malformation: the
    research note established by direct observation that a frame legitimately
    carries entries for an unsubscribed binary sibling token and may name
    nothing for the token a given call was made on behalf of
    (``docs/research/m2-clob-websocket.md``, "Priority question 4"). An
    ingestion layer must be able to tell that apart from a genuine defect, or
    it would write a rejection-ledger row for every healthy frame about a
    sibling and drown the ledger's signal.

    It subclasses ``ValueError`` so that a caller which does not care about
    the distinction — every other consumer of this module — keeps working
    unchanged with a single ``except ValueError``.

    This type exists because the alternative was worse. The ingestion layer
    originally recognised this case by comparing the exception's *message
    text* against a literal copied from this module's source: safe in its
    failure direction (a text change would produce a noisy false rejection
    rather than a silent drop) but a coupling that rots invisibly. Raising a
    named type moves the contract into the type system, where a rename breaks
    the import instead of quietly changing behaviour.
    """


class PriceLevelChangeKind(StrEnum):
    """Whether a level change sets a resting size or removes the level entirely.

    See the module docstring, point 3: derived from ``size``, never asserted
    independently of it — :meth:`PriceLevelChangeV1._validate_kind_matches_size`
    enforces the two can never disagree.
    """

    SET = "set"
    REMOVE = "remove"


class PriceLevelChangeV1(BaseModel):
    """One level change from a ``price_changes`` entry.

    ``side`` is already the ARGOS :class:`BookSide` (``BID``/``ASK``), not the
    wire spelling (``"BUY"``/``"SELL"``) — the mapping happens once, in
    :func:`_parse_side`, the same place :mod:`argos.domain.orderbook` maps
    wire concerns onto stored fields rather than teaching the model itself
    the source's vocabulary.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    side: BookSide
    price: Decimal = Field(ge=MIN_PRICE, le=MAX_PRICE)
    size: Decimal = Field(ge=0)
    kind: PriceLevelChangeKind

    @field_validator("price", "size", mode="before")
    @classmethod
    def _parse(cls, value: Any) -> Decimal:
        return parse_wire_decimal(value)

    @model_validator(mode="after")
    def _validate_kind_matches_size(self) -> PriceLevelChangeV1:
        """``kind`` must never disagree with ``size`` — never re-derived silently.

        Mirrors ``OrderBookSnapshotV1._validate_anomalies_are_recomputable``:
        a directly-constructed or replayed record claiming ``SET`` at size 0,
        or ``REMOVE`` at a nonzero size, is refused rather than corrected.
        """
        is_zero = self.size == 0
        if is_zero and self.kind is not PriceLevelChangeKind.REMOVE:
            raise ValueError(
                f"a zero-size level change must have kind={PriceLevelChangeKind.REMOVE!r}, "
                f"got kind={self.kind!r} at size 0"
            )
        if not is_zero and self.kind is not PriceLevelChangeKind.SET:
            raise ValueError(
                f"a nonzero-size level change must have kind={PriceLevelChangeKind.SET!r}, "
                f"got kind={self.kind!r} at size {self.size}"
            )
        return self


class PriceChangeV1(VersionedModel):
    """A normalized group of same-post-state price level changes for one token.

    One instance corresponds to one ``(asset_id, timestamp, hash)`` group —
    every ``price_changes`` entry for a given token that shares one content
    hash within a single wire frame, per :func:`parse_price_change_group`.
    ``timestamp`` and ``hash`` are deliberately not fields here; see the
    module docstring, point 4.

    ``market`` in the wire event is Polymarket's condition id, named
    ``condition_id`` here to match :class:`argos.domain.market.MarketDefinitionV1`
    and :class:`argos.domain.orderbook.OrderBookSnapshotV1`'s terminology
    rather than perpetuating the source's overloaded ``market`` field name.
    """

    schema_version: ClassVar[str] = "price_change.v1"

    condition_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)

    changes: tuple[PriceLevelChangeV1, ...] = Field(min_length=1)
    """Every level change the group carries. Sorted deterministically by
    ``(side, price)`` on every construction path — never trust wire order,
    the same rule :mod:`argos.domain.orderbook` enforces for book levels — and
    a duplicate ``(side, price)`` within one group is refused rather than
    resolved, because there is no non-arbitrary way to pick between two
    conflicting changes to the same level in the same post-state, the same
    reasoning :mod:`argos.domain.orderbook` applies to a duplicate price
    level in a snapshot."""

    source_best_bid: Decimal | None = Field(default=None, ge=MIN_PRICE, le=MAX_PRICE)
    source_best_ask: Decimal | None = Field(default=None, ge=MIN_PRICE, le=MAX_PRICE)
    """The source's own asserted top-of-book, carried on every entry in the
    group. Named ``source_*`` so no reader mistakes these for ARGOS-derived
    values — see the module docstring, point 6. ``None`` only if the source
    omitted both fields on every selected entry; a *mix* of present and
    absent, or disagreeing present values, is refused by
    :func:`parse_price_change_group` before this model is ever constructed."""

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

    @field_validator("source_best_bid", "source_best_ask", mode="before")
    @classmethod
    def _parse_source_top_of_book(cls, value: Any) -> Any:
        return value if value is None else parse_wire_decimal(value)

    @field_validator("changes")
    @classmethod
    def _validate_changes_are_canonically_ordered(
        cls, value: tuple[PriceLevelChangeV1, ...]
    ) -> tuple[PriceLevelChangeV1, ...]:
        keys = [(change.side.value, change.price) for change in value]
        if len(set(keys)) != len(keys):
            raise ValueError(f"duplicate (side, price) level change(s) in one group: {keys}")
        if keys != sorted(keys):
            raise ValueError(
                "changes must be sorted deterministically by (side, price); "
                f"got {keys}, expected {sorted(keys)}"
            )
        return value


@dataclass(frozen=True, slots=True)
class PriceChangeGroup:
    """A parsed :class:`PriceChangeV1` plus the source content hash it groups on.

    ``hash`` (and ``timestamp``) are excluded from :class:`PriceChangeV1`
    itself — see the module docstring, point 4 — because they name the
    observation's ``event_time``/``source_hash`` on
    :class:`argos.domain.observation.ObservationEnvelopeV1`, not a payload
    field. A caller building an envelope still needs the hash to populate
    ``source_hash``, so it travels back alongside the payload here instead of
    forcing a second, redundant parse of the same frame. A bare
    ``tuple[PriceChangeV1, str]`` would work identically; a small named,
    frozen dataclass is chosen only so a caller cannot swap the two positions
    by reading ``[0]``/``[1]`` from memory, and so this module has no reason
    to depend on :mod:`argos.domain.observation` at all — a domain payload
    model must not need to know about the envelope to be tested or reused
    (the same boundary :mod:`argos.domain.orderbook` keeps).
    """

    payload: PriceChangeV1
    entry_hash: str


def parse_price_change_group(event: Mapping[str, Any], *, asset_id: str) -> PriceChangeGroup:
    """Build a canonical price-change group from one already-JSON-decoded wire frame.

    ``event`` is one already-decoded ``price_change`` message (a plain
    mapping, ideally though not required decoded with ``parse_float=Decimal``
    — this function defends against a float slipping through either way, see
    the module docstring). Only entries whose ``asset_id`` equals the
    caller-supplied ``asset_id`` are selected; entries for any other token
    (including an unsubscribed binary sibling — see the module docstring,
    point 5) are ignored, not merged in.

    All selected entries must share exactly one ``hash``. **A single frame
    naming more than one distinct hash for the requested token's entries was
    never observed in either live capture behind
    ``docs/research/m2-clob-websocket.md``.** This function refuses that shape
    outright rather than inventing a grouping policy for it: a policy invented
    without evidence (first hash wins? most common hash wins? split into two
    envelopes?) is exactly the kind of decision M3 replay would then have to
    reproduce byte-for-byte forever, for a shape nobody has ever actually
    observed on the wire. Refusing an unobserved shape and revisiting this
    function if it is ever observed is preferred to guessing now.

    No network, filesystem, or clock access happens here. Raises
    :class:`ValueError` (via a nested Pydantic ``ValidationError`` for the
    final construction step) for anything this function judges cannot be
    honestly represented: a non-object event or entry, a missing or malformed
    required field, a negative-size change, a duplicate ``(side, price))``
    change, entries disagreeing on ``best_bid``/``best_ask``, or a field this
    module cannot parse as a finite, non-float decimal. It never returns a
    partially-valid group.
    """
    if not isinstance(event, Mapping):
        raise ValueError(f"price_change event must be a JSON object, got {type(event).__name__}")

    event_type = event.get("event_type")
    if event_type != "price_change":
        raise ValueError(f"expected event_type 'price_change', got {event_type!r}")

    unexpected_event_keys = set(event.keys()) - EXPECTED_EVENT_KEYS
    if unexpected_event_keys:
        raise ValueError(
            f"price_change event has unexpected top-level fields: {sorted(unexpected_event_keys)}"
        )

    raw_changes = event.get("price_changes")
    if not isinstance(raw_changes, list):
        raise ValueError(f"'price_changes' must be an array, got {type(raw_changes).__name__}")

    # Every entry is inspected, not only the ones belonging to `asset_id`. A
    # malformed entry (not an object, or carrying no string `asset_id`) cannot
    # be attributed to any token, so skipping it here would discard it for
    # *every* token the caller later asks about -- uncounted and unreasoned,
    # which is the silent drop core invariant 14 and
    # `.claude/rules/data-integrity.md` forbid. Refusing the frame instead
    # costs one counted rejection per token in the ledger, which is exactly the
    # visibility the invariant asks for, and matches
    # `argos.domain.orderbook._build_level`, which refuses a non-object book
    # level rather than skipping it.
    for entry in raw_changes:
        if not isinstance(entry, Mapping):
            raise ValueError(
                f"price_changes entry must be a JSON object, got {type(entry).__name__}"
            )
        entry_asset_id = entry.get("asset_id")
        if not isinstance(entry_asset_id, str) or not entry_asset_id:
            raise ValueError(
                f"price_changes entry has no usable 'asset_id', got {entry_asset_id!r}; "
                "it cannot be attributed to any token and is refused rather than skipped"
            )

    selected = [entry for entry in raw_changes if entry["asset_id"] == asset_id]
    if not selected:
        raise NoEntriesForToken(
            f"no price_changes entries found for asset_id {asset_id!r} in this frame"
        )

    for entry in selected:
        extra_keys = set(entry.keys()) - EXPECTED_ENTRY_KEYS
        if extra_keys:
            raise ValueError(f"price_changes entry has unexpected fields: {sorted(extra_keys)}")

    # Every hash is type-checked *before* the set is built, and before it is
    # sorted. Both of those operations are hostile-input-reachable failures
    # that escape this function's documented `ValueError` contract entirely:
    # an unhashable value (`{"hash": []}` or `{"hash": {}}`) raises
    # `TypeError: unhashable type` from the set comprehension, and a frame
    # mixing types (`"a"` beside `3`) raises `TypeError: '<' not supported`
    # from `sorted` while building the *error message* for a different
    # refusal. A `TypeError` is outside the ARGOS taxonomy, so it flies past
    # `argos.ingestion.clob_price_change`'s `except ValueError` and leaves no
    # rejection-ledger entry at all — the silent drop core invariant 14
    # forbids, and the same shape as the `decimal.InvalidOperation` escape
    # closed one module over. Both were reproduced before this check existed.
    for entry in selected:
        candidate = entry.get("hash")
        if not isinstance(candidate, str) or not candidate:
            raise ValueError(
                f"'hash' must be a non-empty string, got {type(candidate).__name__} "
                f"{_bounded_repr(candidate)}"
            )

    hashes = {entry["hash"] for entry in selected}
    if len(hashes) != 1:
        raise ValueError(
            f"price_changes entries for asset_id {asset_id!r} in one frame carry more than "
            f"one distinct hash ({sorted(hashes)!r}); never "
            "observed in live capture (docs/research/m2-clob-websocket.md) — refusing rather "
            "than inventing a grouping policy that M3 replay would have to inherit"
        )
    entry_hash = next(iter(hashes))

    best_bids = {_parse_optional_top_of_book(entry.get("best_bid")) for entry in selected}
    best_asks = {_parse_optional_top_of_book(entry.get("best_ask")) for entry in selected}
    if len(best_bids) != 1:
        raise ValueError(
            f"price_changes entries for asset_id {asset_id!r} disagree on best_bid: {best_bids}"
        )
    if len(best_asks) != 1:
        raise ValueError(
            f"price_changes entries for asset_id {asset_id!r} disagree on best_ask: {best_asks}"
        )

    changes = tuple(
        sorted(
            (_build_change(entry) for entry in selected),
            key=lambda change: (change.side.value, change.price),
        )
    )

    payload = PriceChangeV1(
        condition_id=event.get("market", ""),
        asset_id=asset_id,
        changes=changes,
        source_best_bid=next(iter(best_bids)),
        source_best_ask=next(iter(best_asks)),
    )
    return PriceChangeGroup(payload=payload, entry_hash=entry_hash)


def _build_change(entry: Mapping[str, Any]) -> PriceLevelChangeV1:
    """Parse one wire ``price_changes`` entry into a typed level change."""
    if "price" not in entry or "size" not in entry or "side" not in entry:
        raise ValueError(
            f"price_changes entry is missing 'price', 'size', or 'side': {dict(entry)!r}"
        )

    price = parse_wire_decimal(entry["price"])
    if not (MIN_PRICE <= price <= MAX_PRICE):
        raise ValueError(f"price_changes entry price {price} is outside the valid [0, 1] range")

    size = parse_wire_decimal(entry["size"])
    if size < 0:
        raise ValueError(f"price_changes entry at price {price} has a negative size: {size}")

    side = _parse_side(entry["side"])
    kind = PriceLevelChangeKind.REMOVE if size == 0 else PriceLevelChangeKind.SET
    return PriceLevelChangeV1(side=side, price=price, size=size, kind=kind)


def _bounded_repr(value: Any, *, limit: int = 120) -> str:
    """A ``repr`` that cannot itself become the resource-exhaustion vector.

    A rejection detail is neutralized and bounded downstream, but building the
    string is not free: a 20,000,000-character value or a deeply nested
    container would be fully rendered first, which is the recurring "the bound
    runs downstream of the work it is meant to bound" shape.
    """
    rendered = repr(value)[: limit + 1]
    return rendered if len(rendered) <= limit else rendered[:limit] + "..."


def _parse_side(value: Any) -> BookSide:
    """Map the wire spelling onto :class:`BookSide`. Never coerced, never defaulted."""
    if value == "BUY":
        return BookSide.BID
    if value == "SELL":
        return BookSide.ASK
    raise ValueError(f"'side' must be 'BUY' or 'SELL', got {value!r}")


def _parse_optional_top_of_book(value: Any) -> Decimal | None:
    # parse_wire_decimal already normalizes (see the module docstring, point 1),
    # so this collapses "0.28" and "0.280" from two different entries in one
    # group onto the same set element before the agreement check below runs.
    return None if value is None else parse_wire_decimal(value)
