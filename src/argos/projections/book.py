"""The order-book projection: a full snapshot plus deltas reconstructed into one book state.

This closes the M2 exit criterion "book snapshot plus deltas reconstruct a
tested projection" (``docs/07_MILESTONES.md``). It is a **pure domain state
machine**: no network, no filesystem, no database, no clock. Nothing is
injected because nothing needs to be — the same sequence of inputs always
produces the same state and the same :meth:`OrderBookProjection.digest`,
whether it is driven live by the capture loop or by an M3 replay, which is
core invariant 5 stated as a property of this module rather than as an
aspiration.

Every decision below is measured against ``docs/research/m2-clob-websocket.md``
and against the recorded live capture
``tests/fixtures/clob/ws_market_price_change.raw.json``, not assumed.

1. **Seeded from a book *state*, not from a payload model.** The public
   WebSocket ``book`` event and the REST ``/book`` response are **not the same
   schema**, verified by direct inspection of the recorded capture: the REST
   response and the initial WebSocket ``book`` delivered on subscribe carry
   ``tick_size`` and ``last_trade_price``, but *neither* WebSocket ``book``
   event carries ``min_order_size`` or ``neg_risk``, and the three later
   in-stream ``book`` events (capture messages 16, 26, 37) carry only
   ``market``, ``asset_id``, ``timestamp``, ``hash``, ``bids`` and ``asks`` —
   dropping ``tick_size`` and ``last_trade_price`` as well. A WebSocket
   ``book`` event therefore cannot be parsed into
   :class:`~argos.domain.orderbook.OrderBookSnapshotV1`, which requires all
   four fields. Weakening that model to fit would degrade a shipped REST
   contract to accommodate a WebSocket quirk, and inventing a second payload
   model is a separate slice. So this module seeds from :class:`BookState` —
   the intersection both sources really do provide (ids, bids, asks) — with
   :meth:`BookState.from_order_book_snapshot` as the convenience path for the
   REST model.
2. **Removal is read off the typed field, never re-derived from size.**
   :class:`~argos.domain.pricechange.PriceLevelChangeKind` is already
   validated as ``REMOVE`` if and only if ``size == 0``, by
   :meth:`~argos.domain.pricechange.PriceLevelChangeV1._validate_kind_matches_size`,
   which refuses a record where the two disagree rather than correcting it.
   Re-deriving the convention here would create a second place that decides
   what removal means, and two places that decide one thing eventually
   disagree; this module trusts the typed field and would rather fail loudly
   at the payload boundary than silently diverge from it.
3. **Decimal handling is reused, never reimplemented.** Every price and size
   arrives already canonicalized through
   :func:`argos.domain.orderbook.parse_wire_decimal`, and the digest renders
   through :func:`argos.domain.orderbook.normalize_decimal`. ``docs/STATUS.md``
   records the negative-zero/trailing-zero decimal-identity class recurring
   three times independently across this repository; this module is not the
   fourth.
4. **Everything unusual is counted and reasoned** (core invariant 14,
   ``.claude/rules/data-integrity.md``). A delta for another token, a delta
   arriving before any snapshot, a removal of a level that is not present, a
   regressing event time, and a snapshot that disagrees with the projected
   state all produce a :class:`BookProjectionAnomaly`. None is ever silently
   absorbed, and none is ever silently repaired.
5. **Out-of-order handling is deliberately shallow — see
   :meth:`OrderBookProjection.apply_delta`.** M2 applies in arrival order and
   *counts* an event-time regression. That is **not a late-event policy**.
   Watermarks and a principled late/out-of-order policy are M3 deliverables
   (``docs/07_MILESTONES.md``); this module must not pre-empt them with an
   invented one that M3 replay would then have to reproduce forever.
6. **ARGOS cannot verify the source's book hash — see
   :attr:`BookState.source_asserted_hash`.** The research established that a
   delta's ``hash`` is the hash of the *resulting* book state and reconciles
   exactly with REST for the same state, but the algorithm producing it is not
   published, so ARGOS cannot compute it. This projection therefore **cannot
   detect a missed delta from the delta stream alone**; divergence is only
   detectable when a full snapshot arrives and disagrees with the projected
   state (:attr:`BookProjectionAnomalyKind.SNAPSHOT_DISAGREES_WITH_PROJECTION`).
   The source's asserted hash is carried alongside the state so an operator can
   compare it against another observation of the same state, and every name
   here says ``source_asserted`` precisely so no reader mistakes carrying it
   for checking it.

Not decided here, left to the slices that own them: persistence of a projected
state (there is no schema version on :class:`BookState` because nothing writes
one — the store deals in observations, not projections), fan-out across many
tokens, and how a :class:`BookProjectionAnomaly` becomes a rejection-ledger row
or a manifest counter. This module keeps the same boundary
:mod:`argos.domain.orderbook` and :mod:`argos.domain.pricechange` keep: it does
not know about the event store, the envelope, or the transport it will sit
behind.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from argos.clock import ensure_utc
from argos.domain.market import CONDITION_ID_PATTERN, TOKEN_ID_PATTERN
from argos.domain.orderbook import (
    BookSide,
    OrderBookLevel,
    OrderBookSnapshotV1,
    normalize_decimal,
    parse_wire_decimal,
)
from argos.domain.pricechange import PriceChangeV1, PriceLevelChangeKind

BOOK_STATE_DIGEST_VERSION: Final = "book_state_digest.v1"
"""The digest algorithm's own version, hashed into every digest it produces.

An M3 golden replay test pins a literal digest string. If the encoding below
ever changes, every pinned digest must change too — visibly, as a failing
golden test with a legible cause — rather than two runs of different ARGOS
revisions quietly agreeing or quietly disagreeing for reasons nobody can
locate. Bumping this constant is the mechanism for that, and it is inside the
hashed material rather than beside it so it cannot be forgotten.
"""


class BookProjectionAnomalyKind(StrEnum):
    """A specific, named irregularity observed while projecting a book.

    Deliberately a third vocabulary, kept separate from
    :class:`argos.domain.orderbook.OrderBookAnomalyKind` (defects *inside* one
    snapshot payload) and
    :class:`argos.domain.observation.ObservationQualityFlag` (defects in the
    *delivery* of one observation). These describe defects in a *sequence* of
    observations, which neither of the other two can express: no single payload
    and no single delivery is wrong when a removal names a level that is not
    there, only the relationship between them is.
    """

    DELTA_BEFORE_SNAPSHOT = "delta_before_snapshot"
    """A delta arrived while the projection had no book state to apply it to.

    Real, not defensive: ``docs/research/m2-clob-websocket.md`` (priority
    question 4) found that subscribing to one token delivers ``price_change``
    entries for its unsubscribed binary sibling, for which **no ``book`` event
    is ever sent**. A projection driven for such a token receives deltas and
    never a snapshot. The delta is not applied — applying it would fabricate a
    book out of the fragment of it that happened to change."""

    ASSET_ID_MISMATCH = "asset_id_mismatch"
    """An input named a different ``asset_id`` than this projection's token.

    Refused, never applied: projecting one token's deltas onto another token's
    book would silently manufacture a book state that no source ever
    asserted."""

    CONDITION_ID_MISMATCH = "condition_id_mismatch"
    """An input named a different ``condition_id`` than this projection's market.

    Refused for the same reason as :attr:`ASSET_ID_MISMATCH`. Checked
    separately rather than folded into it because the two failures mean
    different things operationally: a token mismatch is a fan-out bug in the
    adapter, while a condition mismatch on a matching token id means the source
    is re-attributing a token to a different market — the token-to-condition
    binding ``docs/STATUS.md`` already records ARGOS stores on the source's word
    alone."""

    REMOVE_OF_ABSENT_LEVEL = "remove_of_absent_level"
    """A ``REMOVE`` change named a price level the projected book did not hold.

    Counted rather than refused, and the resulting state is unchanged, which is
    the state the source is describing either way. It is a genuine signal
    though, and the reason it must be counted rather than shrugged off: since
    ARGOS cannot verify the source's book hash (see the module docstring, point
    6), a removal of a level ARGOS never saw is one of the few *local* hints
    that a delta was missed or that the projection was seeded from a stale
    snapshot."""

    EVENT_TIME_REGRESSION = "event_time_regression"
    """An input's event time was strictly earlier than the last applied one.

    Still applied, in arrival order — see :meth:`OrderBookProjection.apply_delta`.
    This is a counter, not a policy."""

    SNAPSHOT_DISAGREES_WITH_PROJECTION = "snapshot_disagrees_with_projection"
    """An arriving full snapshot did not match the state projected from deltas.

    The **only** divergence detector this module has, and the reason it exists
    at all — see the module docstring, point 6. The snapshot wins: it is a
    complete statement of the book from the source, while the projected state
    is ARGOS's reconstruction, and a reconstruction never overrides a direct
    observation."""


@dataclass(frozen=True, slots=True)
class BookProjectionAnomaly:
    """One recorded irregularity. Never silent — core invariant 14.

    A frozen dataclass rather than a Pydantic model, and deliberately carrying
    no ``schema_version``: nothing persists these. They are in-memory counters
    for a run, and giving them a schema version would advertise a durability
    contract this slice does not implement (the engineering rule is that every
    *persistent* record is versioned, and the honest way to satisfy it here is
    not to persist).
    """

    kind: BookProjectionAnomalyKind
    detail: str
    event_time: datetime | None = None
    side: BookSide | None = None
    price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class BookState:
    """A complete book for one token: the seed both sources can actually supply.

    See the module docstring, point 1, for why this exists rather than
    :class:`argos.domain.orderbook.OrderBookSnapshotV1` being used directly.
    This carries only what a REST ``/book`` response and *every* observed
    WebSocket ``book`` event both provide. ``tick_size``,
    ``last_trade_price``, ``min_order_size`` and ``neg_risk`` are absent
    because the three in-stream WebSocket ``book`` events in the recorded
    capture do not carry them; a projection that required them could not be
    re-seeded from the live stream at all.
    """

    condition_id: str
    asset_id: str

    bids: tuple[OrderBookLevel, ...]
    """Sorted descending by price: ``bids[0]`` is the best bid, or empty."""

    asks: tuple[OrderBookLevel, ...]
    """Sorted ascending by price: ``asks[0]`` is the best ask, or empty."""

    source_asserted_hash: str | None = None
    """The content hash the *source* attached to this book state, verbatim.

    **ARGOS does not and cannot verify this value.**
    ``docs/research/m2-clob-websocket.md`` (priority question 2) established by
    live capture that it is the hash of the resulting book state and that it
    reconciles exactly between a WebSocket delta, a WebSocket ``book`` event,
    and a REST ``/book`` response for the same state — but the algorithm that
    produces it is not published, so ARGOS cannot recompute it from the levels
    it holds. It is preserved so an operator (or a later slice, if the
    algorithm is ever established) can compare two independent observations of
    the same state by hash. The name says ``source_asserted`` for the same
    reason :attr:`argos.domain.pricechange.PriceChangeV1.source_best_bid` does:
    so no reader can mistake a value ARGOS carries for a value ARGOS checked.
    """

    def __post_init__(self) -> None:
        _validate_condition_id(self.condition_id)
        _validate_asset_id(self.asset_id)
        _validate_side_order(self.bids, side=BookSide.BID, descending=True)
        _validate_side_order(self.asks, side=BookSide.ASK, descending=False)

    @classmethod
    def from_order_book_snapshot(
        cls, snapshot: OrderBookSnapshotV1, *, source_asserted_hash: str | None = None
    ) -> BookState:
        """Seed from the REST payload model. Lossless for everything a book state is.

        ``tick_size``, ``min_order_size``, ``neg_risk`` and
        ``last_trade_price`` are dropped, not lost: they remain on the
        ``OrderBookSnapshotV1`` the caller still holds, and on the observation
        envelope that carries it. They are simply not part of a *book state*,
        and the WebSocket stream that re-seeds this projection does not supply
        them (module docstring, point 1).

        ``source_asserted_hash`` is a parameter rather than being read off the
        snapshot because ``OrderBookSnapshotV1`` deliberately does not store
        the source ``hash`` — it names the observation's ``source_hash`` on
        :class:`argos.domain.observation.ObservationEnvelopeV1`, and duplicating
        it onto the payload would create two copies that can disagree.
        """
        return cls(
            condition_id=snapshot.condition_id,
            asset_id=snapshot.asset_id,
            bids=snapshot.bids,
            asks=snapshot.asks,
            source_asserted_hash=source_asserted_hash,
        )

    @classmethod
    def from_wire_levels(
        cls,
        *,
        condition_id: str,
        asset_id: str,
        bids: Iterable[Mapping[str, Any]],
        asks: Iterable[Mapping[str, Any]],
        source_asserted_hash: str | None = None,
    ) -> BookState:
        """Seed from raw ``[{"price": ..., "size": ...}]`` arrays, in either wire order.

        This is the path a WebSocket ``book`` event takes, since it cannot
        become an ``OrderBookSnapshotV1`` (module docstring, point 1). Wire
        order is never trusted or preserved: the measured order is bids
        ascending / asks descending, both ending at top of book, which is
        exactly backwards from the naive ``[0]`` reading, so both sides are
        sorted explicitly into best-level-first form here — the same discipline
        :func:`argos.domain.orderbook.parse_order_book_snapshot` applies, and
        for the same measured reason.

        Raises :class:`ValueError` for anything that cannot be honestly
        represented as a book state, including a **zero-size level**. A
        resting order of size zero is not a resting order; the REST parser
        drops it and records a counted
        :attr:`argos.domain.orderbook.OrderBookAnomalyKind.ZERO_SIZE_LEVEL_DROPPED`
        anomaly, and this function has no anomaly sink to record one into
        before the projection it will seed exists. No zero-size level appears
        in any recorded *snapshot*, REST or WebSocket — all four ``book``
        events in the live capture were checked directly, and the REST research
        note found none either; zero size was only ever observed on the *delta*
        stream, where it means removal. Refusing loudly is therefore the
        correct handling of an unobserved shape: if one is ever seen, the fix
        belongs in a snapshot-parsing slice that can count it, not in a silent
        drop here.
        """
        return cls(
            condition_id=condition_id,
            asset_id=asset_id,
            bids=tuple(
                sorted(
                    _parse_wire_levels(bids, side=BookSide.BID),
                    key=lambda level: level.price,
                    reverse=True,
                )
            ),
            asks=tuple(
                sorted(_parse_wire_levels(asks, side=BookSide.ASK), key=lambda level: level.price)
            ),
            source_asserted_hash=source_asserted_hash,
        )

    @property
    def best_bid(self) -> Decimal | None:
        """The highest resting bid price, or ``None`` if the book has no bids."""
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        """The lowest resting ask price, or ``None`` if the book has no asks."""
        return self.asks[0].price if self.asks else None

    def digest(self) -> str:
        """See :meth:`OrderBookProjection.digest`."""
        return _digest_levels(
            condition_id=self.condition_id,
            asset_id=self.asset_id,
            bids=self.bids,
            asks=self.asks,
        )


@dataclass(slots=True)
class OrderBookProjection:
    """One token's book, reconstructed from a snapshot plus a stream of deltas.

    Mutable by design — this is a state machine, and a projection that returned
    a new object per delta would make "the same handler drives live and replay"
    (core invariant 5) harder to hold, not easier. Everything *observable* from
    it is immutable: :meth:`state` returns a frozen :class:`BookState` and
    :meth:`anomalies` returns a tuple.

    Scoped to exactly one ``(condition_id, asset_id)`` at construction. That is
    not bookkeeping: it is what makes
    :attr:`BookProjectionAnomalyKind.ASSET_ID_MISMATCH` a refusal rather than a
    convention. The research note found a single WebSocket frame carrying
    entries for an unsubscribed sibling token, so "the events on this
    connection belong to my token" is measurably false, and a projection that
    took the token from whatever arrived would silently blend two books.
    """

    condition_id: str
    asset_id: str

    _bids: dict[Decimal, Decimal] = field(default_factory=dict, init=False, repr=False)
    _asks: dict[Decimal, Decimal] = field(default_factory=dict, init=False, repr=False)
    _seeded: bool = field(default=False, init=False, repr=False)
    _source_asserted_hash: str | None = field(default=None, init=False, repr=False)
    _last_event_time: datetime | None = field(default=None, init=False, repr=False)
    _anomalies: list[BookProjectionAnomaly] = field(default_factory=list, init=False, repr=False)
    _applied_snapshots: int = field(default=0, init=False, repr=False)
    _applied_deltas: int = field(default=0, init=False, repr=False)
    _applied_level_changes: int = field(default=0, init=False, repr=False)
    _refused_inputs: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        _validate_condition_id(self.condition_id)
        _validate_asset_id(self.asset_id)

    # --- reading --------------------------------------------------------------

    @property
    def is_seeded(self) -> bool:
        """Whether any snapshot has been applied. Deltas are refused until one has."""
        return self._seeded

    @property
    def last_event_time(self) -> datetime | None:
        """The event time of the most recently *applied* input.

        The maximum is not tracked separately, and deliberately so: this is the
        last applied event time in **arrival order**, which is the only
        ordering M2 claims to reproduce. A watermark — the thing that would
        legitimately be a running maximum — is M3 work, and naming a maximum
        here would look like one without behaving like one.
        """
        return self._last_event_time

    @property
    def source_asserted_hash(self) -> str | None:
        """The most recent hash the source asserted for a state it described.

        Carried, never checked — see :attr:`BookState.source_asserted_hash`.
        Set from the seeding snapshot and updated by every applied delta that
        supplies one, so it names the source's claim about the *current* state
        if and only if no delta has been missed, which is precisely what ARGOS
        cannot determine (module docstring, point 6).
        """
        return self._source_asserted_hash

    @property
    def anomalies(self) -> tuple[BookProjectionAnomaly, ...]:
        """Every irregularity recorded so far, in the order it was observed."""
        return tuple(self._anomalies)

    @property
    def anomaly_counts(self) -> Mapping[BookProjectionAnomalyKind, int]:
        """How many of each kind were recorded. Every kind is present, including zeros.

        Zeros are included so a caller writing a run summary reports "0 deltas
        arrived before a snapshot" rather than omitting the line entirely — an
        absent counter and a zero counter read identically in a report, and
        core invariant 14 is about the count being *visible*.
        """
        counts = dict.fromkeys(BookProjectionAnomalyKind, 0)
        for anomaly in self._anomalies:
            counts[anomaly.kind] += 1
        return counts

    @property
    def applied_snapshot_count(self) -> int:
        """How many snapshots were applied (refused ones are not counted here)."""
        return self._applied_snapshots

    @property
    def applied_delta_count(self) -> int:
        """How many delta groups were applied (refused ones are not counted here)."""
        return self._applied_deltas

    @property
    def applied_level_change_count(self) -> int:
        """How many individual level changes were applied across all deltas.

        Distinct from :attr:`applied_delta_count` because one
        :class:`~argos.domain.pricechange.PriceChangeV1` group carries every
        level change sharing one post-state hash, which the research note
        observed reaching six entries.
        """
        return self._applied_level_changes

    @property
    def refused_input_count(self) -> int:
        """How many inputs were refused outright and changed nothing.

        A refusal is always accompanied by an anomaly, so this is derivable
        from :attr:`anomaly_counts`; it is exposed separately because the
        derivation is not obvious (``REMOVE_OF_ABSENT_LEVEL`` and
        ``EVENT_TIME_REGRESSION`` are recorded on inputs that *were* applied)
        and a caller writing a manifest should not have to know which kinds
        imply refusal.
        """
        return self._refused_inputs

    def state(self) -> BookState:
        """The current projected book, as an immutable, canonically ordered state.

        Rebuilt on each call from the internal mapping rather than maintained
        alongside it: one source of truth, and no way for a cached state to
        drift from the levels it claims to describe.
        """
        return BookState(
            condition_id=self.condition_id,
            asset_id=self.asset_id,
            bids=tuple(
                OrderBookLevel(price=price, size=self._bids[price])
                for price in sorted(self._bids, reverse=True)
            ),
            asks=tuple(
                OrderBookLevel(price=price, size=self._asks[price]) for price in sorted(self._asks)
            ),
            source_asserted_hash=self._source_asserted_hash,
        )

    def digest(self) -> str:
        """A deterministic hash of the projected book state, for M3 golden replay.

        Covers exactly the identity of the book: ``condition_id``, ``asset_id``,
        and every level on both sides. It covers **nothing else** — not event
        times, not anomaly history, not how many deltas were applied, not the
        source's asserted hash — because the property M3 needs is that two
        projections which reached the same book state by different routes
        produce the same digest. A snapshot at state B and a snapshot at state
        A followed by the deltas leading to B must agree, and they do.

        Order-independent of insertion by construction: levels are sorted by
        price before hashing, so the digest is a function of the book, not of
        the sequence that built it.

        The encoding is length-prefixed and therefore injective, for the reason
        ``argos.domain.observation._digest`` records: joining source-controlled
        fields on any separator lets one field's content forge a field
        boundary, which was a reproduced collision in this repository, not a
        hypothetical. That helper is private to its module, so the discipline
        is reproduced here rather than the import; the *decimal* rendering
        underneath is reused, not reproduced (module docstring, point 3).

        This is **ARGOS's own digest and is not the source's book hash**. The
        two are unrelated values over the same state and must never be
        compared; see :attr:`BookState.source_asserted_hash`.
        """
        return self.state().digest()

    # --- applying -------------------------------------------------------------

    def apply_snapshot(
        self, snapshot: BookState, *, event_time: datetime
    ) -> tuple[BookProjectionAnomaly, ...]:
        """Replace the projected state wholesale with ``snapshot``.

        Wholesale, never merged: a full book is a complete statement about
        every resting level, so a price the projection holds and the snapshot
        omits has been removed at the source. Merging would keep it alive as a
        phantom level forever, and no later delta would ever remove it, since
        the source has no reason to send a removal for a level it already
        considers gone.

        If the projection was already seeded and the arriving snapshot
        disagrees with the projected state, that is recorded as
        :attr:`BookProjectionAnomalyKind.SNAPSHOT_DISAGREES_WITH_PROJECTION`
        *before* the replacement — the only divergence signal available (module
        docstring, point 6). The snapshot still wins.

        Returns the anomalies this call recorded, and records them on the
        projection as well, so a caller can react per-event without walking
        :attr:`anomalies` looking for what is new.
        """
        recorded: list[BookProjectionAnomaly] = []
        moment = ensure_utc(event_time)

        if not self._matches_scope(
            condition_id=snapshot.condition_id,
            asset_id=snapshot.asset_id,
            event_time=moment,
            what="snapshot",
            recorded=recorded,
        ):
            self._refused_inputs += 1
            return self._record(recorded)

        self._note_event_time_regression(moment, what="snapshot", recorded=recorded)

        incoming = _levels_as_mapping(snapshot)
        if self._seeded and (self._bids, self._asks) != incoming:
            recorded.append(
                BookProjectionAnomaly(
                    kind=BookProjectionAnomalyKind.SNAPSHOT_DISAGREES_WITH_PROJECTION,
                    detail=(
                        "an arriving full snapshot does not match the state projected from "
                        f"deltas: {_describe_divergence(self._bids, incoming[0], BookSide.BID)}; "
                        f"{_describe_divergence(self._asks, incoming[1], BookSide.ASK)}. "
                        "The snapshot replaces the projected state. ARGOS cannot attribute "
                        "this to a missed delta rather than a misapplied one: the source's "
                        "book hash algorithm is not published, so no per-delta verification "
                        "is possible (docs/research/m2-clob-websocket.md)"
                    ),
                    event_time=moment,
                )
            )

        self._bids, self._asks = incoming
        self._seeded = True
        self._source_asserted_hash = snapshot.source_asserted_hash
        self._last_event_time = moment
        self._applied_snapshots += 1
        return self._record(recorded)

    def apply_delta(
        self,
        delta: PriceChangeV1,
        *,
        event_time: datetime,
        source_asserted_hash: str | None = None,
    ) -> tuple[BookProjectionAnomaly, ...]:
        """Apply one ``price_change`` group to the projected state.

        ``REMOVE`` deletes the level; ``SET`` sets its size. The kind is read
        off :attr:`~argos.domain.pricechange.PriceLevelChangeV1.kind` and
        **not** re-derived from ``size``, because
        ``PriceLevelChangeV1._validate_kind_matches_size`` already refuses any
        record where the two disagree — see the module docstring, point 2. The
        convention itself ("size 0 means removal") is the M2 exit criterion's,
        confirmed on live traffic in ``docs/research/m2-clob-websocket.md``,
        priority question 3.

        **Ordering is arrival order, and that is all it is.** The wire delivers
        an order; this method applies that order, records the event time it
        last applied, and counts a regression as
        :attr:`BookProjectionAnomalyKind.EVENT_TIME_REGRESSION` — it does not
        reorder, buffer, delay, or drop. A regressed delta is still applied.
        **This is not a late-event policy.** ARGOS has no watermark yet, and
        inventing one here would mean M3 inherits a policy chosen without the
        analysis M3 exists to do; watermarks and the late-event policy are M3
        deliverables (``docs/07_MILESTONES.md``). Arrival order is also what
        core invariant 6 requires replay to reproduce, so applying it is the
        M2-correct behaviour rather than merely the simple one. No out-of-order
        delivery was observed in either live capture, over a sample far too
        small to claim the property holds.

        A delta arriving before any snapshot is **counted and not applied**
        (:attr:`BookProjectionAnomalyKind.DELTA_BEFORE_SNAPSHOT`); so is a
        delta naming a different token or market. Returns the anomalies this
        call recorded.
        """
        recorded: list[BookProjectionAnomaly] = []
        moment = ensure_utc(event_time)

        if not self._matches_scope(
            condition_id=delta.condition_id,
            asset_id=delta.asset_id,
            event_time=moment,
            what="delta",
            recorded=recorded,
        ):
            self._refused_inputs += 1
            return self._record(recorded)

        if not self._seeded:
            recorded.append(
                BookProjectionAnomaly(
                    kind=BookProjectionAnomalyKind.DELTA_BEFORE_SNAPSHOT,
                    detail=(
                        f"a delta carrying {len(delta.changes)} level change(s) arrived for "
                        f"asset_id {self.asset_id!r} before any snapshot seeded this "
                        "projection; not applied, because a book cannot be fabricated from "
                        "the fragment of it that changed. Expected for a token whose deltas "
                        "arrive on a connection subscribed to its binary sibling, which "
                        "receives no book event of its own "
                        "(docs/research/m2-clob-websocket.md)"
                    ),
                    event_time=moment,
                )
            )
            self._refused_inputs += 1
            return self._record(recorded)

        self._note_event_time_regression(moment, what="delta", recorded=recorded)

        for change in delta.changes:
            side = self._bids if change.side is BookSide.BID else self._asks
            if change.kind is PriceLevelChangeKind.REMOVE:
                if change.price not in side:
                    recorded.append(
                        BookProjectionAnomaly(
                            kind=BookProjectionAnomalyKind.REMOVE_OF_ABSENT_LEVEL,
                            detail=(
                                f"a removal named {change.side.value} price {change.price}, "
                                "which the projected book does not hold; the resulting state "
                                "is the same either way, but a removal of a level ARGOS never "
                                "saw is one of the few local hints that a delta was missed or "
                                "that the seeding snapshot was stale"
                            ),
                            event_time=moment,
                            side=change.side,
                            price=change.price,
                        )
                    )
                else:
                    del side[change.price]
            else:
                side[change.price] = change.size
            self._applied_level_changes += 1

        if source_asserted_hash is not None:
            self._source_asserted_hash = source_asserted_hash
        self._last_event_time = moment
        self._applied_deltas += 1
        return self._record(recorded)

    # --- internals ------------------------------------------------------------

    def _record(
        self, recorded: Sequence[BookProjectionAnomaly]
    ) -> tuple[BookProjectionAnomaly, ...]:
        self._anomalies.extend(recorded)
        return tuple(recorded)

    def _matches_scope(
        self,
        *,
        condition_id: str,
        asset_id: str,
        event_time: datetime,
        what: str,
        recorded: list[BookProjectionAnomaly],
    ) -> bool:
        """Whether an input belongs to this projection's token and market.

        Both are checked, and both are refusals rather than warnings: see
        :attr:`BookProjectionAnomalyKind.ASSET_ID_MISMATCH`.
        """
        if asset_id != self.asset_id:
            recorded.append(
                BookProjectionAnomaly(
                    kind=BookProjectionAnomalyKind.ASSET_ID_MISMATCH,
                    detail=(
                        f"a {what} for asset_id {asset_id!r} reached the projection for "
                        f"asset_id {self.asset_id!r}; refused, never applied — one token's "
                        "changes projected onto another token's book would manufacture a "
                        "state no source ever asserted"
                    ),
                    event_time=event_time,
                )
            )
            return False
        if condition_id != self.condition_id:
            recorded.append(
                BookProjectionAnomaly(
                    kind=BookProjectionAnomalyKind.CONDITION_ID_MISMATCH,
                    detail=(
                        f"a {what} for condition_id {condition_id!r} reached the projection "
                        f"for condition_id {self.condition_id!r} on the same asset_id "
                        f"{asset_id!r}; refused, never applied — the source is re-attributing "
                        "a token to a different market"
                    ),
                    event_time=event_time,
                )
            )
            return False
        return True

    def _note_event_time_regression(
        self, event_time: datetime, *, what: str, recorded: list[BookProjectionAnomaly]
    ) -> None:
        """Count a backwards step in event time. Counting only — see :meth:`apply_delta`.

        Strictly earlier, not earlier-or-equal: the research note observed one
        logical book transition spread across separate frames carrying an
        identical ``(timestamp, hash)`` pair, so equal event times are normal
        traffic on this source and flagging them would bury the real signal.
        """
        previous = self._last_event_time
        if previous is not None and event_time < previous:
            recorded.append(
                BookProjectionAnomaly(
                    kind=BookProjectionAnomalyKind.EVENT_TIME_REGRESSION,
                    detail=(
                        f"a {what} carries event time {event_time.isoformat()}, earlier than "
                        f"the last applied {previous.isoformat()}; applied anyway, in arrival "
                        "order, and counted. This is not a late-event policy: watermarks and "
                        "the late/out-of-order policy are M3 deliverables"
                    ),
                    event_time=event_time,
                )
            )


def _levels_as_mapping(state: BookState) -> tuple[dict[Decimal, Decimal], dict[Decimal, Decimal]]:
    return (
        {level.price: level.size for level in state.bids},
        {level.price: level.size for level in state.asks},
    )


def _describe_divergence(
    projected: Mapping[Decimal, Decimal], incoming: Mapping[Decimal, Decimal], side: BookSide
) -> str:
    """Summarize how two sides differ, bounded so a wide book cannot flood a log line."""
    differing = sorted(
        price
        for price in set(projected) | set(incoming)
        if projected.get(price) != incoming.get(price)
    )
    if not differing:
        return f"{side.value}s agree"
    shown = ", ".join(
        f"{price}: projected {projected.get(price, 'absent')} vs snapshot "
        f"{incoming.get(price, 'absent')}"
        for price in differing[:5]
    )
    suffix = "" if len(differing) <= 5 else f" (and {len(differing) - 5} more)"
    return f"{len(differing)} differing {side.value} level(s): {shown}{suffix}"


def _parse_wire_levels(
    entries: Iterable[Mapping[str, Any]], *, side: BookSide
) -> list[OrderBookLevel]:
    """Parse raw ``{"price": ..., "size": ...}`` entries. See :meth:`BookState.from_wire_levels`."""
    levels: list[OrderBookLevel] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError(
                f"{side.value} level must be a JSON object, got {type(entry).__name__}"
            )
        extra_keys = set(entry.keys()) - {"price", "size"}
        if extra_keys:
            raise ValueError(f"{side.value} level has unexpected fields: {sorted(extra_keys)}")
        if "price" not in entry or "size" not in entry:
            raise ValueError(f"{side.value} level is missing 'price' or 'size': {dict(entry)!r}")
        size = parse_wire_decimal(entry["size"])
        if size <= 0:
            raise ValueError(
                f"{side.value} level at price {entry['price']!r} has size {size}, which is not a "
                "resting order; a book state cannot represent it and this function has no "
                "anomaly sink to count it into. Never observed in any recorded snapshot, REST "
                "or WebSocket — see BookState.from_wire_levels"
            )
        # price is not pre-parsed here: OrderBookLevel's own validator calls
        # parse_wire_decimal and enforces the [0, 1] range, so parsing it twice
        # would be a second place that decides what a price is.
        levels.append(OrderBookLevel(price=entry["price"], size=size))
    return levels


def _validate_side_order(
    levels: Sequence[OrderBookLevel], *, side: BookSide, descending: bool
) -> None:
    """Refuse an unordered or duplicated side rather than silently re-sorting it.

    The same rule, and the same refusal-over-coercion reasoning, as
    ``argos.domain.orderbook._validate_side_ordering``: a caller holding
    unsorted wire data should go through :meth:`BookState.from_wire_levels`,
    which sorts explicitly.
    """
    prices = [level.price for level in levels]
    if len(set(prices)) != len(prices):
        raise ValueError(f"duplicate {side.value} price level(s) in {prices}")
    if prices != sorted(prices, reverse=descending):
        direction = "descending" if descending else "ascending"
        raise ValueError(
            f"{side.value} levels must be sorted {direction} by price (best level first), "
            f"got {prices}"
        )


def _validate_condition_id(value: str) -> None:
    if not CONDITION_ID_PATTERN.fullmatch(value.lower()):
        raise ValueError(f"condition_id must be a 0x-prefixed 32-byte hash, got {value!r}")


def _validate_asset_id(value: str) -> None:
    if not TOKEN_ID_PATTERN.fullmatch(value):
        raise ValueError(f"asset_id must be a decimal token id string, got {value!r}")


def _digest_levels(
    *,
    condition_id: str,
    asset_id: str,
    bids: Sequence[OrderBookLevel],
    asks: Sequence[OrderBookLevel],
) -> str:
    """The digest encoding. See :meth:`OrderBookProjection.digest` for the reasoning."""
    parts: list[str] = [BOOK_STATE_DIGEST_VERSION, condition_id, asset_id]
    for side_name, levels in (("bid", bids), ("ask", asks)):
        ordered = sorted(levels, key=lambda level: level.price)
        parts.append(f"{side_name}:{len(ordered)}")
        for level in ordered:
            # normalize_decimal is idempotent on an already-canonical value and
            # is reused rather than reimplemented (module docstring, point 3).
            parts.append(str(normalize_decimal(level.price)))
            parts.append(str(normalize_decimal(level.size)))
    encoded = "|".join(f"{len(part)}:{part}" for part in parts)
    return hashlib.sha256(encoded.encode()).hexdigest()
