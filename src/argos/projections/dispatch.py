"""The one object a live capture and a replay both drive (ADR-0012 section 8).

Core invariant 5 — "live and replay use the same domain handlers; only the clock
and event source may differ" — has been true in this repository only because
there was one path: the capture loop wrote to the store and stopped, and every
projection driven so far was driven by a test. This module is that shared path.
:func:`argos.ingestion.capture.run_capture` calls it live, and
``argos.replay`` calls it over stored records, and it is the *same object*
rather than two implementations of one protocol — a protocol would let the two
drift and be satisfied by a test that never compared them.

It is a pure state machine: no network, no filesystem, no database, no clock.
Given the same sequence of envelopes it produces the same state and the same
:meth:`ObservationDispatcher.state_hash`, which is the property M3's exit
criterion rests on.

Three decisions from ADR-0012 live here rather than in a caller, deliberately,
because a decision duplicated in two callers is a decision that will eventually
differ between them:

1. **A duplicate is skipped, not re-applied** (section 3). Re-applying a
   ``price_change`` group is not idempotent: a second ``REMOVE`` of a level
   names a price the projection no longer holds, and the projection correctly
   records ``REMOVE_OF_ABSENT_LEVEL`` — an anomaly whose whole job is to signal
   a missed delta. Replaying duplicates manufactures that signal out of the
   deduplication mechanism, and makes the anomaly count a function of how often
   the source resent.
2. **The watermark marks; it never reorders, buffers or drops** (section 4).
   ADR-0003 forbids reordering late data into the past outside a separately
   labelled experiment, so a late observation is applied in arrival order and
   counted as late. Nothing else happens to it.
3. **An unhandled payload kind is a counted outcome, not a skip** (consequences).
   When ``last_trade_price`` gains a model at M4, this counter is what shows it
   arriving.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from argos.clock import ensure_utc
from argos.domain.observation import ObservationEnvelopeV1, read_declared_payload
from argos.domain.orderbook import OrderBookSnapshotV1
from argos.domain.pricechange import PriceChangeV1
from argos.domain.versioning import VersionedModel
from argos.domain.wsbook import WsBookSnapshotV1
from argos.projections.book import BookProjectionAnomaly, BookState, OrderBookProjection

_Handler = Callable[
    [OrderBookProjection, ObservationEnvelopeV1, VersionedModel],
    tuple["DispatchOutcomeKind", tuple[BookProjectionAnomaly, ...]],
]

__all__ = [
    "STATE_HASH_VERSION",
    "DispatchCounts",
    "DispatchOutcome",
    "DispatchOutcomeKind",
    "Lateness",
    "ObservationDispatcher",
    "Watermark",
]

STATE_HASH_VERSION: Final = "state_hash.v1"
"""The state-hash encoding's own version, hashed into every value it produces.

A golden replay test pins a literal hash. If the encoding below ever changes,
every pinned value must change too — visibly, as a failing golden test with a
legible cause — rather than two ARGOS revisions quietly agreeing or quietly
disagreeing for reasons nobody can locate. It sits *inside* the hashed material
rather than beside it so it cannot be forgotten, the same discipline
:data:`argos.projections.book.BOOK_STATE_DIGEST_VERSION` already applies one
level down.
"""


class Lateness(StrEnum):
    """Where an observation sat relative to the watermark when it arrived."""

    ON_TIME = "on_time"
    LATE = "late"
    """Its ``event_time`` was strictly earlier than the watermark. Applied
    anyway, in arrival order, and counted — see the module docstring, point 2."""

    UNDATABLE = "undatable"
    """The source sent no usable event time, so the question does not arise.

    A third value rather than folding into ``ON_TIME``: "the source sent no
    timestamp" and "the source sent one and it was not late" are different facts
    about the source, and an ``UNPARSEABLE`` status is evidence of a parser or
    source-schema defect that a summary must not launder into "fine"."""


class DispatchOutcomeKind(StrEnum):
    """What the dispatcher did with one observation."""

    APPLIED_SNAPSHOT = "applied_snapshot"
    APPLIED_DELTA = "applied_delta"

    SKIPPED_DUPLICATE = "skipped_duplicate"
    """The same ``observation_id`` had already been applied in this session."""

    UNHANDLED_PAYLOAD = "unhandled_payload"
    """No handler is wired for this payload's schema version. Counted, never
    silently ignored: this is the counter that will show ``last_trade_price``
    arriving once M4 gives it a model."""

    UNSCOPED = "unscoped"
    """The envelope names no ``condition_id``/``token_id`` pair, so there is no
    projection it could belong to. Real rather than defensive: the envelope
    contract makes all three scope ids optional, because some source messages
    are lifecycle-wide."""


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """What happened to one observation, returned so a caller can react per event."""

    kind: DispatchOutcomeKind
    observation_id: str
    lateness: Lateness
    anomalies: tuple[BookProjectionAnomaly, ...] = ()


@dataclass(frozen=True, slots=True)
class DispatchCounts:
    """Every outcome, counted. Zeros are always present (core invariant 14).

    An absent counter and a zero counter read identically in a report, which is
    why every field exists on every result rather than appearing only when
    non-zero.
    """

    applied_snapshots: int = 0
    applied_deltas: int = 0
    skipped_duplicates: int = 0
    unhandled_payloads: int = 0
    unscoped: int = 0
    on_time: int = 0
    late: int = 0
    undatable: int = 0

    def as_record(self) -> dict[str, int]:
        """A stable mapping for the replay manifest's ``output_record_counts``."""
        return {
            "applied_snapshots": self.applied_snapshots,
            "applied_deltas": self.applied_deltas,
            "skipped_duplicates": self.skipped_duplicates,
            "unhandled_payloads": self.unhandled_payloads,
            "unscoped": self.unscoped,
            "on_time": self.on_time,
            "late": self.late,
            "undatable": self.undatable,
        }


@dataclass(slots=True)
class Watermark:
    """The maximum event time seen, minus a configured tolerance (ADR-0012 §4).

    Observational only. It classifies; it does not hold anything back. A
    watermark that buffered events to release them in event-time order would be
    the reordering ADR-0003 forbids, performed silently.

    One watermark for the whole stream rather than one per token: on this source
    the ``timestamp`` belongs to the market and is shared by every entry in a
    frame, including the ones for the sibling token, so per-token watermarks
    would track the same number twice.
    """

    allowed_lateness: timedelta = timedelta(0)
    """Zero by default, and that is evidence rather than caution: no capture in
    this repository — recorded or live — contains a single out-of-order arrival,
    so zero flags nothing that has ever been observed while flagging any genuine
    regression the first time it happens. A non-zero default would be a guess
    about a phenomenon this project has never seen."""

    _high: datetime | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.allowed_lateness < timedelta(0):
            raise ValueError(
                "allowed_lateness must not be negative; a negative tolerance would "
                "push the watermark ahead of the newest event seen and mark "
                "on-time arrivals late"
            )

    @property
    def value(self) -> datetime | None:
        """The current watermark, or ``None`` before any datable observation."""
        if self._high is None:
            return None
        return self._high - self.allowed_lateness

    def classify(self, event_time: datetime | None) -> Lateness:
        """Judge ``event_time`` against the watermark **as it stands now**.

        Called before :meth:`observe`, always: an observation cannot be late
        relative to a watermark it advanced itself.

        Strictly earlier, never earlier-or-equal. The WebSocket research
        observed one logical book transition spanning several frames with an
        identical ``(timestamp, hash)``, so equal event times are normal traffic
        on this source and flagging them would bury the real signal.
        """
        if event_time is None:
            return Lateness.UNDATABLE
        current = self.value
        if current is not None and ensure_utc(event_time) < current:
            return Lateness.LATE
        return Lateness.ON_TIME

    def observe(self, event_time: datetime | None) -> None:
        """Advance the high-water mark. Never moves it backwards."""
        if event_time is None:
            return
        moment = ensure_utc(event_time)
        if self._high is None or moment > self._high:
            self._high = moment


@dataclass(slots=True)
class ObservationDispatcher:
    """Applies observations to per-token projections, live or replayed.

    Projections are created on demand, keyed by ``(condition_id, token_id)``.
    On demand rather than from a configured token list because the dispatcher's
    input is the envelope stream, and an envelope already names its own scope —
    requiring a second, separately-configured list would create two places that
    can disagree about which tokens a run covers.
    """

    watermark: Watermark = field(default_factory=Watermark)

    _projections: dict[tuple[str, str], OrderBookProjection] = field(
        default_factory=dict, init=False, repr=False
    )
    _applied: set[str] = field(default_factory=set, init=False, repr=False)
    """Every ``observation_id`` already applied in this session.

    Owned here rather than read off the store's ``Disposition``, so that "a
    duplicate never moves state twice" is a property of the dispatcher and not
    of whichever caller happens to be driving it. Cost, stated rather than
    discovered: one id per distinct observation, roughly 45 bytes, so about
    4 MB per day per token at the volume ADR-0011 extrapolates. Acceptable at
    M3 scale; a session that outgrows it needs a bounded structure, not a
    caller-side check.
    """

    _counts: DispatchCounts = field(default_factory=DispatchCounts, init=False, repr=False)

    @property
    def counts(self) -> DispatchCounts:
        return self._counts

    @property
    def projections(self) -> Mapping[tuple[str, str], OrderBookProjection]:
        """The live projections, keyed by ``(condition_id, token_id)``."""
        return dict(self._projections)

    def dispatch(self, envelope: ObservationEnvelopeV1) -> DispatchOutcome:
        """Apply one observation, or record precisely why it was not applied."""
        lateness = self.watermark.classify(envelope.event_time)

        if envelope.observation_id in self._applied:
            # Classified before this check so a duplicate is still described
            # honestly, but the watermark is not advanced by it: a duplicate
            # carries the event time its original already contributed.
            self._bump(skipped_duplicates=1)
            return DispatchOutcome(
                kind=DispatchOutcomeKind.SKIPPED_DUPLICATE,
                observation_id=envelope.observation_id,
                lateness=lateness,
            )

        payload = read_declared_payload(envelope)
        handler = _HANDLER_FOR.get(type(payload))
        if handler is None:
            self._bump(unhandled_payloads=1)
            return DispatchOutcome(
                kind=DispatchOutcomeKind.UNHANDLED_PAYLOAD,
                observation_id=envelope.observation_id,
                lateness=lateness,
            )

        if envelope.condition_id is None or envelope.token_id is None:
            self._bump(unscoped=1)
            return DispatchOutcome(
                kind=DispatchOutcomeKind.UNSCOPED,
                observation_id=envelope.observation_id,
                lateness=lateness,
            )

        projection = self._projection_for(envelope.condition_id, envelope.token_id)
        kind, anomalies = handler(projection, envelope, payload)

        self._applied.add(envelope.observation_id)
        self.watermark.observe(envelope.event_time)
        self._bump(**{_APPLIED_COUNTER[kind]: 1, _LATENESS_COUNTER[lateness]: 1})
        return DispatchOutcome(
            kind=kind,
            observation_id=envelope.observation_id,
            lateness=lateness,
            anomalies=anomalies,
        )

    def state_hash(self) -> str:
        """A deterministic hash of every projected book state, and nothing else.

        Covers each projection's own ``digest()`` under its
        ``(condition_id, token_id)`` key, in sorted key order. It deliberately
        excludes run ids, timestamps, wall-clock duration, the replay mode,
        ``ingest_sequence`` values, anomaly history and the counts — ADR-0012
        section 6. Two replays of one capture in three different pacing modes
        must produce one hash, and a hash carrying any of those would instead
        prove they were three different runs, which they were and which is not
        the property under test.

        Length-prefixed, and therefore injective, for the reason
        ``argos.domain.observation._digest`` records: joining source-controlled
        strings on any separator lets one field's content forge a field
        boundary, which was a reproduced collision in this repository rather
        than a hypothetical. A ``condition_id`` and a ``token_id`` are both
        source-controlled.
        """
        parts: list[str] = [STATE_HASH_VERSION]
        for (condition_id, token_id), projection in sorted(self._projections.items()):
            parts.extend((condition_id, token_id, projection.digest()))
        encoded = "|".join(f"{len(part)}:{part}" for part in parts)
        return hashlib.sha256(encoded.encode()).hexdigest()

    def _projection_for(self, condition_id: str, token_id: str) -> OrderBookProjection:
        key = (condition_id, token_id)
        projection = self._projections.get(key)
        if projection is None:
            projection = OrderBookProjection(condition_id=condition_id, asset_id=token_id)
            self._projections[key] = projection
        return projection

    def _bump(self, **deltas: int) -> None:
        self._counts = replace(
            self._counts,
            **{name: getattr(self._counts, name) + value for name, value in deltas.items()},
        )


_LATENESS_COUNTER: Final[Mapping[Lateness, str]] = {
    Lateness.ON_TIME: "on_time",
    Lateness.LATE: "late",
    Lateness.UNDATABLE: "undatable",
}

_APPLIED_COUNTER: Final[Mapping[DispatchOutcomeKind, str]] = {
    DispatchOutcomeKind.APPLIED_SNAPSHOT: "applied_snapshots",
    DispatchOutcomeKind.APPLIED_DELTA: "applied_deltas",
}


def _apply_ws_book(
    projection: OrderBookProjection,
    envelope: ObservationEnvelopeV1,
    payload: VersionedModel,
) -> tuple[DispatchOutcomeKind, tuple[BookProjectionAnomaly, ...]]:
    assert isinstance(payload, WsBookSnapshotV1)  # guaranteed by _HANDLER_FOR's key
    state = BookState(
        condition_id=payload.condition_id,
        asset_id=payload.asset_id,
        bids=payload.bids,
        asks=payload.asks,
        # The source's own hash lives on the envelope, not on the payload:
        # `OrderBookSnapshotV1` deliberately does not carry it, because
        # duplicating it onto the payload would create two copies that can
        # disagree. Carried, never checked — ARGOS cannot compute it.
        source_asserted_hash=envelope.source_hash,
    )
    anomalies = projection.apply_snapshot(state, event_time=envelope.event_time)
    return DispatchOutcomeKind.APPLIED_SNAPSHOT, anomalies


def _apply_rest_book(
    projection: OrderBookProjection,
    envelope: ObservationEnvelopeV1,
    payload: VersionedModel,
) -> tuple[DispatchOutcomeKind, tuple[BookProjectionAnomaly, ...]]:
    assert isinstance(payload, OrderBookSnapshotV1)
    state = BookState.from_order_book_snapshot(payload, source_asserted_hash=envelope.source_hash)
    anomalies = projection.apply_snapshot(state, event_time=envelope.event_time)
    return DispatchOutcomeKind.APPLIED_SNAPSHOT, anomalies


def _apply_price_change(
    projection: OrderBookProjection,
    envelope: ObservationEnvelopeV1,
    payload: VersionedModel,
) -> tuple[DispatchOutcomeKind, tuple[BookProjectionAnomaly, ...]]:
    assert isinstance(payload, PriceChangeV1)
    anomalies = projection.apply_delta(
        payload,
        event_time=envelope.event_time,
        source_asserted_hash=envelope.source_hash,
    )
    return DispatchOutcomeKind.APPLIED_DELTA, anomalies


_HANDLER_FOR: Final[dict[type[VersionedModel], _Handler]] = {
    WsBookSnapshotV1: _apply_ws_book,
    OrderBookSnapshotV1: _apply_rest_book,
    PriceChangeV1: _apply_price_change,
}
"""Keyed by payload *type*, not by version string.

The schema registry (`argos.domain.versioning.resolve_schema`) already turned
the stored version into a class, so dispatching on the class is one lookup
rather than a chain of string comparisons that can silently fall through. A
payload kind absent from this table is an explicit, counted
``UNHANDLED_PAYLOAD`` — which is the honest state of ``last_trade_price`` and
``tick_size_change`` today.
"""
