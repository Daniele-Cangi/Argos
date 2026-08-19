"""Tests for the dispatcher live and replay both drive (ADR-0012 section 8).

Every envelope here is built by `build_observation_envelope` from a real
payload model, not hand-assembled, so what is exercised is the object an
adapter actually produces. The order-book payloads reuse the recorded live
capture's own token, condition id and price levels
(`tests/fixtures/clob/ws_market_price_change.raw.json`), so the shapes are
observed rather than invented.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

import pytest

from argos.domain.observation import (
    ObservationEnvelopeV1,
    ObservationSource,
    build_observation_envelope,
)
from argos.domain.orderbook import BookSide, OrderBookLevel, OrderBookSnapshotV1
from argos.domain.pricechange import (
    PriceChangeV1,
    PriceLevelChangeKind,
    PriceLevelChangeV1,
)
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.domain.versioning import VersionedModel
from argos.domain.wsbook import WsBookSnapshotV1
from argos.errors import SchemaVersionError
from argos.projections.book import BookProjectionAnomalyKind
from argos.projections.dispatch import (
    STATE_HASH_VERSION,
    DispatchOutcomeKind,
    Lateness,
    ObservationDispatcher,
    Watermark,
)

TOKEN = "34691510069031117755834214800869745291092253295564665516660269316660628637961"
SIBLING = "95561057794427123541889915407555646439882912350845258651794843110787555977699"
CONDITION = "0x94a39addec8bd24a2d03deeaa43bdee6a2b11eca403cd12495eb24132cc23173"
START = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)
RAW = b'{"event_type": "book"}'


def _provenance(**overrides: Any) -> SourceProvenanceV1:
    fields: dict[str, Any] = {
        "source": "clob_market_ws",
        "endpoint": "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        "http_status": None,
        "retrieved_at": START,
        "raw_sha256": sha256_hex(RAW),
        "byte_length": len(RAW),
    }
    fields.update(overrides)
    return SourceProvenanceV1(**fields)


def _envelope(payload: VersionedModel, **overrides: Any) -> ObservationEnvelopeV1:
    fields: dict[str, Any] = {
        "source": ObservationSource.CLOB_MARKET_WS,
        "source_event_type": "book",
        "condition_id": CONDITION,
        "token_id": TOKEN,
        "event_time": START,
        "received_time": START,
        "ingest_sequence": 1,
        "payload": payload,
        "provenance": _provenance(),
        "parser_version": "test/1",
        "capture_run_id": "run-1",
    }
    fields.update(overrides)
    return build_observation_envelope(**fields)


def _book(*, bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> WsBookSnapshotV1:
    return WsBookSnapshotV1(
        condition_id=CONDITION,
        asset_id=TOKEN,
        bids=tuple(
            OrderBookLevel(price=Decimal(p), size=Decimal(s))
            for p, s in sorted(bids, key=lambda level: Decimal(level[0]), reverse=True)
        ),
        asks=tuple(
            OrderBookLevel(price=Decimal(p), size=Decimal(s))
            for p, s in sorted(asks, key=lambda level: Decimal(level[0]))
        ),
    )


def _delta(*, price: str, size: str, side: BookSide = BookSide.BID) -> PriceChangeV1:
    return PriceChangeV1(
        condition_id=CONDITION,
        asset_id=TOKEN,
        changes=(
            PriceLevelChangeV1(
                side=side,
                price=Decimal(price),
                size=Decimal(size),
                kind=(
                    PriceLevelChangeKind.REMOVE if Decimal(size) == 0 else PriceLevelChangeKind.SET
                ),
            ),
        ),
    )


# --- applying ---------------------------------------------------------------------


def test_a_snapshot_then_a_delta_build_the_state_the_projection_would() -> None:
    dispatcher = ObservationDispatcher()
    seed = dispatcher.dispatch(_envelope(_book(bids=[("0.48", "100")], asks=[("0.52", "80")])))
    moved = dispatcher.dispatch(
        _envelope(
            _delta(price="0.49", size="42"),
            source_event_type="price_change",
            event_time=START + timedelta(seconds=1),
            ingest_sequence=2,
        )
    )
    assert seed.kind is DispatchOutcomeKind.APPLIED_SNAPSHOT
    assert moved.kind is DispatchOutcomeKind.APPLIED_DELTA

    projection = dispatcher.projections[(CONDITION, TOKEN)]
    assert [(level.price, level.size) for level in projection.state().bids] == [
        (Decimal("0.49"), Decimal("42")),
        (Decimal("0.48"), Decimal("100")),
    ]
    assert dispatcher.counts.applied_snapshots == 1
    assert dispatcher.counts.applied_deltas == 1


def test_two_tokens_get_two_projections_and_never_blend() -> None:
    """The research found one frame carrying entries for an unsubscribed sibling,
    so "the events on this stream belong to my token" is measurably false."""
    dispatcher = ObservationDispatcher()
    dispatcher.dispatch(_envelope(_book(bids=[("0.48", "100")], asks=[])))
    sibling_book = WsBookSnapshotV1(
        condition_id=CONDITION,
        asset_id=SIBLING,
        bids=(OrderBookLevel(price=Decimal("0.51"), size=Decimal("7")),),
        asks=(),
    )
    dispatcher.dispatch(_envelope(sibling_book, token_id=SIBLING, ingest_sequence=2))

    assert set(dispatcher.projections) == {(CONDITION, TOKEN), (CONDITION, SIBLING)}
    assert dispatcher.projections[(CONDITION, TOKEN)].state().bids[0].size == Decimal("100")
    assert dispatcher.projections[(CONDITION, SIBLING)].state().bids[0].size == Decimal("7")


def test_a_rest_snapshot_seeds_the_same_projection_as_a_websocket_one() -> None:
    """Both payload models are wired, and they reach one projection: a capture
    seeded from a REST poll and continued on the WebSocket stream is the
    original reason `BookState` exists rather than one model being used
    directly."""
    dispatcher = ObservationDispatcher()
    rest = OrderBookSnapshotV1(
        condition_id=CONDITION,
        asset_id=TOKEN,
        bids=(OrderBookLevel(price=Decimal("0.48"), size=Decimal("100")),),
        asks=(OrderBookLevel(price=Decimal("0.52"), size=Decimal("80")),),
        tick_size=Decimal("0.001"),
        min_order_size=Decimal("5"),
        neg_risk=False,
        last_trade_price=Decimal("0.5"),
    )
    outcome = dispatcher.dispatch(
        _envelope(
            rest, source=ObservationSource.CLOB_REST, provenance=_provenance(source="clob_rest")
        )
    )
    assert outcome.kind is DispatchOutcomeKind.APPLIED_SNAPSHOT
    assert dispatcher.projections[(CONDITION, TOKEN)].is_seeded


# --- duplicates (ADR-0012 section 3) ----------------------------------------------


def test_a_duplicate_is_skipped_and_cannot_move_state_twice() -> None:
    """The decision most likely to be got wrong by accident.

    Re-applying a `price_change` group is not idempotent in general: a second
    `REMOVE` names a level the projection no longer holds, and the projection
    records `REMOVE_OF_ABSENT_LEVEL` -- an anomaly whose whole job is to signal
    a missed delta. Replaying duplicates would manufacture that signal out of
    the deduplication mechanism itself.
    """
    dispatcher = ObservationDispatcher()
    dispatcher.dispatch(_envelope(_book(bids=[("0.48", "100")], asks=[])))
    removal = _envelope(
        _delta(price="0.48", size="0"),
        source_event_type="price_change",
        event_time=START + timedelta(seconds=1),
        ingest_sequence=2,
    )
    first = dispatcher.dispatch(removal)
    # A genuine redelivery: identical bytes, so an identical observation_id --
    # only `ingest_sequence` and `received_time` differ, and identity excludes
    # both by design (ADR-0010 section 2).
    again = dispatcher.dispatch(
        _envelope(
            _delta(price="0.48", size="0"),
            source_event_type="price_change",
            event_time=START + timedelta(seconds=1),
            ingest_sequence=3,
            received_time=START + timedelta(seconds=2),
        )
    )
    assert first.observation_id == again.observation_id
    assert first.kind is DispatchOutcomeKind.APPLIED_DELTA
    assert again.kind is DispatchOutcomeKind.SKIPPED_DUPLICATE
    assert not again.anomalies
    assert dispatcher.counts.skipped_duplicates == 1

    projection = dispatcher.projections[(CONDITION, TOKEN)]
    assert projection.anomaly_counts[BookProjectionAnomalyKind.REMOVE_OF_ABSENT_LEVEL] == 0


def test_a_duplicate_does_not_change_the_state_hash() -> None:
    dispatcher = ObservationDispatcher()
    dispatcher.dispatch(_envelope(_book(bids=[("0.48", "100")], asks=[])))
    before = dispatcher.state_hash()
    dispatcher.dispatch(_envelope(_book(bids=[("0.48", "100")], asks=[]), ingest_sequence=9))
    assert dispatcher.state_hash() == before


# --- the watermark (ADR-0012 section 4) -------------------------------------------


def test_an_event_never_marks_itself_late() -> None:
    """Classified before the watermark is advanced: an observation cannot be
    late relative to a watermark it moved itself."""
    watermark = Watermark()
    assert watermark.classify(START) is Lateness.ON_TIME
    watermark.observe(START)
    assert watermark.classify(START) is Lateness.ON_TIME


def test_equal_event_times_are_not_late() -> None:
    """The research observed one logical book transition spanning several frames
    with an identical `(timestamp, hash)`, so equality is normal traffic here and
    flagging it would bury the real signal."""
    watermark = Watermark()
    watermark.observe(START)
    assert watermark.classify(START) is Lateness.ON_TIME


def test_a_strictly_earlier_event_is_late_and_is_still_applied() -> None:
    """ADR-0003 forbids reordering late data into the past outside a separately
    labelled experiment, so the watermark marks and nothing else happens."""
    dispatcher = ObservationDispatcher()
    dispatcher.dispatch(
        _envelope(_book(bids=[("0.48", "100")], asks=[]), event_time=START + timedelta(seconds=10))
    )
    late = dispatcher.dispatch(
        _envelope(
            _delta(price="0.47", size="5"),
            source_event_type="price_change",
            event_time=START,
            ingest_sequence=2,
        )
    )
    assert late.lateness is Lateness.LATE
    assert late.kind is DispatchOutcomeKind.APPLIED_DELTA
    assert dispatcher.counts.late == 1
    # Applied, not held back or dropped.
    prices = [level.price for level in dispatcher.projections[(CONDITION, TOKEN)].state().bids]
    assert Decimal("0.47") in prices


def test_allowed_lateness_widens_the_tolerance_without_changing_the_policy() -> None:
    dispatcher = ObservationDispatcher(watermark=Watermark(allowed_lateness=timedelta(seconds=30)))
    dispatcher.dispatch(
        _envelope(_book(bids=[("0.48", "100")], asks=[]), event_time=START + timedelta(seconds=10))
    )
    outcome = dispatcher.dispatch(
        _envelope(
            _delta(price="0.47", size="5"),
            source_event_type="price_change",
            event_time=START,
            ingest_sequence=2,
        )
    )
    assert outcome.lateness is Lateness.ON_TIME
    assert dispatcher.counts.late == 0


def test_a_negative_tolerance_is_refused() -> None:
    """It would push the watermark ahead of the newest event seen and mark
    on-time arrivals late, which is not a stricter policy but a wrong one."""
    with pytest.raises(ValueError):
        Watermark(allowed_lateness=timedelta(seconds=-1))


def test_an_observation_with_no_event_time_is_undatable_and_still_applied() -> None:
    """`.claude/rules/data-integrity.md`: never replace an invalid source
    timestamp silently. Refusing would lose a real book state; substituting
    `received_time` would invent one. It is applied and counted as neither on
    time nor late."""
    dispatcher = ObservationDispatcher()
    outcome = dispatcher.dispatch(
        _envelope(
            _book(bids=[("0.48", "100")], asks=[]),
            event_time=None,
            event_time_raw="not-a-timestamp",
        )
    )
    assert outcome.lateness is Lateness.UNDATABLE
    assert outcome.kind is DispatchOutcomeKind.APPLIED_SNAPSHOT
    assert dispatcher.counts.undatable == 1
    assert dispatcher.counts.on_time == 0
    assert dispatcher.projections[(CONDITION, TOKEN)].is_seeded


def test_an_undatable_observation_does_not_erase_the_watermark() -> None:
    """Assigning `None` through would silently disable lateness detection for
    every event after an undatable one."""
    dispatcher = ObservationDispatcher()
    dispatcher.dispatch(
        _envelope(_book(bids=[("0.48", "100")], asks=[]), event_time=START + timedelta(seconds=10))
    )
    dispatcher.dispatch(
        _envelope(
            _book(bids=[("0.48", "101")], asks=[]),
            event_time=None,
            event_time_raw="junk",
            ingest_sequence=2,
        )
    )
    assert dispatcher.watermark.value == START + timedelta(seconds=10)
    outcome = dispatcher.dispatch(
        _envelope(
            _delta(price="0.47", size="5"),
            source_event_type="price_change",
            event_time=START,
            ingest_sequence=3,
        )
    )
    assert outcome.lateness is Lateness.LATE


# --- what is not applied ----------------------------------------------------------


class _UnwiredPayload(VersionedModel):
    """A payload with no handler -- the position `last_trade_price` is in today."""

    schema_version: ClassVar[str] = "test_unwired_payload.v1"

    value: int = 1


def test_a_payload_with_no_handler_is_counted_not_skipped() -> None:
    dispatcher = ObservationDispatcher()
    outcome = dispatcher.dispatch(_envelope(_UnwiredPayload()))
    assert outcome.kind is DispatchOutcomeKind.UNHANDLED_PAYLOAD
    assert dispatcher.counts.unhandled_payloads == 1
    assert dispatcher.projections == {}


def test_an_envelope_naming_no_token_is_counted_as_unscoped() -> None:
    dispatcher = ObservationDispatcher()
    outcome = dispatcher.dispatch(_envelope(_book(bids=[("0.48", "1")], asks=[]), token_id=None))
    assert outcome.kind is DispatchOutcomeKind.UNSCOPED
    assert dispatcher.counts.unscoped == 1


def test_an_unregistered_payload_version_fails_inside_the_taxonomy() -> None:
    """A stored record naming a schema no model declares is a storage-integrity
    problem, not a dispatch decision -- it must not be silently treated as
    unhandled, which would make a corrupt record look like an ordinary
    unsupported event type."""
    dispatcher = ObservationDispatcher()
    envelope = _envelope(_book(bids=[("0.48", "1")], asks=[]))
    forged = envelope.model_copy(update={"payload_schema_version": "no_such_schema.v1"})
    with pytest.raises(SchemaVersionError):
        dispatcher.dispatch(forged)


# --- the state hash (ADR-0012 section 6) ------------------------------------------


def test_the_state_hash_depends_only_on_the_projected_state() -> None:
    """Two dispatchers reaching one book state by different routes agree.

    One is seeded directly at the final state; the other is seeded earlier and
    walked there by a delta. Ingest sequences, received times and event times
    all differ.
    """
    direct = ObservationDispatcher()
    direct.dispatch(_envelope(_book(bids=[("0.49", "42"), ("0.48", "100")], asks=[])))

    walked = ObservationDispatcher()
    walked.dispatch(_envelope(_book(bids=[("0.48", "100")], asks=[])))
    walked.dispatch(
        _envelope(
            _delta(price="0.49", size="42"),
            source_event_type="price_change",
            event_time=START + timedelta(minutes=5),
            received_time=START + timedelta(minutes=5),
            ingest_sequence=77,
        )
    )
    assert direct.state_hash() == walked.state_hash()


def test_a_different_book_produces_a_different_hash() -> None:
    a = ObservationDispatcher()
    a.dispatch(_envelope(_book(bids=[("0.48", "100")], asks=[])))
    b = ObservationDispatcher()
    b.dispatch(_envelope(_book(bids=[("0.48", "101")], asks=[])))
    assert a.state_hash() != b.state_hash()


def test_an_empty_dispatcher_still_hashes_its_version() -> None:
    """So that "no projections" is a value rather than an absent one, and so a
    golden test can pin the empty case."""
    assert len(ObservationDispatcher().state_hash()) == 64
    assert STATE_HASH_VERSION == "state_hash.v1"


def test_the_hash_separates_two_tokens_that_could_forge_one_key() -> None:
    """Length-prefixed, and therefore injective: joining source-controlled
    strings on any separator lets one field's content forge a field boundary,
    which was a reproduced collision in this repository rather than a
    hypothetical. `condition_id` and `token_id` are both source-controlled."""
    dispatcher = ObservationDispatcher()
    dispatcher.dispatch(_envelope(_book(bids=[("0.48", "100")], asks=[])))
    one_token = dispatcher.state_hash()

    two = ObservationDispatcher()
    two.dispatch(_envelope(_book(bids=[("0.48", "100")], asks=[])))
    sibling_book = WsBookSnapshotV1(
        condition_id=CONDITION,
        asset_id=SIBLING,
        bids=(OrderBookLevel(price=Decimal("0.48"), size=Decimal("100")),),
        asks=(),
    )
    two.dispatch(_envelope(sibling_book, token_id=SIBLING, ingest_sequence=2))
    assert two.state_hash() != one_token
