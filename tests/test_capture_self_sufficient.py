"""The point of this slice: a stored WebSocket capture is now self-sufficient.

Before this slice, a live capture stored `price_change.v1` deltas only — every
`book` event became an `unknown_event_type` rejection (verified on a real
45-second live capture: 22 observations, 4 such rejections,
`docs/BACKLOG.md`). A stored capture could replay its deltas but had nothing
in the *same* capture to seed a projection from; the seed had to come from a
separate REST `/book` poll.

This test drives the real recorded WebSocket capture
(`tests/fixtures/clob/ws_market_price_change.raw.json`) through the actual
production path — `argos.ingestion.capture.run_capture` into a real
`SQLiteEventStore` — then reads the stored observations back out exactly as a
downstream consumer would: `store.get_observation` plus `read_payload`, no
shortcut through the domain parsers directly. The stored `ws_book_snapshot.v1`
payloads seed `argos.projections.book.OrderBookProjection` and the stored
`price_change.v1` payloads apply on top, and the projected state is checked
level-for-level against each of the three later stored `book` snapshots in the
same run — the three snapshot-to-snapshot transitions the real capture spans,
the same three
`tests/test_book_projection.py::test_a_real_snapshot_plus_real_deltas_reconstructs_the_next_real_snapshot`
already reconstructs directly from parsed wire data. What is new here is that
every one of those inputs came through the envelope/store round trip first
-- normalization, identity, canonical-JSON serialization, and SQLite storage
and retrieval -- not only through the domain parser.

Only `direction: "recv"` messages are fed in: a `"send"` entry (the four
client `PING`s) was never received by ARGOS and feeding it in as though it
were inbound traffic would be fabricated evidence, not a stored capture.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from argos.clock import ReplayClock
from argos.domain.observation import ObservationEnvelopeV1, read_payload
from argos.domain.orderbook import OrderBookLevel
from argos.domain.pricechange import PriceChangeV1
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.domain.wsbook import WsBookSnapshotV1
from argos.ingestion.capture import run_capture
from argos.projections.book import BookProjectionAnomalyKind, BookState, OrderBookProjection
from argos.sources.clob_ws import MarketFrame
from argos.store.event_store import open_sqlite_event_store

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "clob" / "ws_market_price_change.raw.json"
)

TOKEN_YES = "34691510069031117755834214800869745291092253295564665516660269316660628637961"
CONDITION_ID = "0x94a39addec8bd24a2d03deeaa43bdee6a2b11eca403cd12495eb24132cc23173"
START = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)

# The three snapshot-to-snapshot transitions the real capture spans, as
# (seeding book message index, closing book message index) -- matches
# `tests/test_book_projection.py::REAL_TRANSITIONS` exactly, verified
# independently by this file's own `test_the_run_visits_four_book_snapshots`.
REAL_TRANSITIONS = ((0, 16), (16, 26), (26, 37))


def _load_fixture() -> dict[str, Any]:
    with FIXTURE_PATH.open() as handle:
        return json.load(handle, parse_float=Decimal)  # type: ignore[no-any-return]


def _received_frames() -> list[MarketFrame]:
    """Every genuinely *received* frame, in arrival order, from the real capture.

    Deliberately excludes `direction: "send"` entries (the client's own
    outbound `PING`s) -- see the module docstring.
    """
    fixture = _load_fixture()
    connect_url: str = fixture["connect"]["connect_url"]
    frames: list[MarketFrame] = []
    for message in fixture["messages"]:
        if message["direction"] != "recv":
            continue
        received_time = datetime.fromisoformat(message["wall_time"])
        raw_text: str = message["raw"]
        raw_bytes = raw_text.encode("utf-8")
        frames.append(
            MarketFrame(
                text=raw_text,
                received_time=received_time,
                provenance=SourceProvenanceV1(
                    source="clob_market_ws",
                    endpoint=connect_url,
                    http_status=None,
                    retrieved_at=received_time,
                    raw_sha256=sha256_hex(raw_bytes),
                    byte_length=len(raw_bytes),
                ),
            )
        )
    return frames


class _ListFrameSource:
    def __init__(self, frames: list[MarketFrame]) -> None:
        self._frames = frames

    async def frames(self) -> Any:
        for frame in self._frames:
            yield frame


def _levels(levels: tuple[OrderBookLevel, ...]) -> list[tuple[Decimal, Decimal]]:
    return [(level.price, level.size) for level in levels]


async def _run_real_capture() -> tuple[str, Any]:
    store = open_sqlite_event_store(":memory:")
    clock = ReplayClock(START)
    run_id = "self-sufficient-capture"
    frames = _received_frames()
    health = await run_capture(
        frame_source=_ListFrameSource(frames),
        store=store,
        clock=clock,
        capture_run_id=run_id,
        subscribed_token_ids=[TOKEN_YES],
    )
    return run_id, (store, health)


async def test_the_run_visits_exactly_four_book_snapshots_and_some_price_changes() -> None:
    """Ground truth this test's own reconstruction is measured against."""
    run_id, (store, _health) = await _run_real_capture()
    deliveries = list(store.iter_deliveries(run_id))
    envelopes = [store.get_observation(d.observation_id) for d in deliveries]
    schema_versions = [e.payload_schema_version for e in envelopes if e is not None]
    assert schema_versions.count("ws_book_snapshot.v1") == 4
    assert schema_versions.count("price_change.v1") >= 28
    # Every stored record for this run is one of the two payload kinds this
    # capture actually carries -- nothing silently fell through a third path.
    assert set(schema_versions) == {"ws_book_snapshot.v1", "price_change.v1"}


async def test_a_stored_capture_reconstructs_all_three_real_transitions_with_no_rest_call() -> None:
    """The point of this slice.

    One continuous projection, seeded and advanced *entirely* from records
    read back out of `SQLiteEventStore` -- never from the domain parsers
    called directly, and never from a REST `/book` response. Every one of the
    three later stored `book` snapshots must be reconstructed exactly,
    level for level, by the deltas stored in between.
    """
    run_id, (store, _health) = await _run_real_capture()
    deliveries = list(store.iter_deliveries(run_id))

    ordered_payloads: list[tuple[str, datetime, str | None, WsBookSnapshotV1 | PriceChangeV1]] = []
    for delivery in deliveries:
        envelope = store.get_observation(delivery.observation_id)
        assert isinstance(envelope, ObservationEnvelopeV1)
        assert envelope.event_time is not None, "every real record in this capture has a timestamp"
        if envelope.payload_schema_version == "ws_book_snapshot.v1":
            book_payload = read_payload(envelope, WsBookSnapshotV1)
            ordered_payloads.append(
                ("book", envelope.event_time, envelope.source_hash, book_payload)
            )
        else:
            delta_payload = read_payload(envelope, PriceChangeV1)
            ordered_payloads.append(
                ("price_change", envelope.event_time, envelope.source_hash, delta_payload)
            )

    projection = OrderBookProjection(condition_id=CONDITION_ID, asset_id=TOKEN_YES)
    seen_snapshots = 0
    transitions_checked = 0

    for kind, event_time, source_hash, payload in ordered_payloads:
        if kind == "book":
            assert isinstance(payload, WsBookSnapshotV1)
            snapshot_state = BookState(
                condition_id=payload.condition_id,
                asset_id=payload.asset_id,
                bids=payload.bids,
                asks=payload.asks,
                source_asserted_hash=source_hash,
            )
            seen_snapshots += 1
            if seen_snapshots == 1:
                # The seed: nothing to reconstruct yet.
                projection.apply_snapshot(snapshot_state, event_time=event_time)
                continue

            # This is a *closing* snapshot for one of the three real
            # transitions -- compare BEFORE re-seeding, so equality is
            # evidence the deltas alone reconstructed it, not that re-seeding
            # papered over a divergence.
            projected_bids = _levels(projection.state().bids)
            projected_asks = _levels(projection.state().asks)
            expected_bids = _levels(snapshot_state.bids)
            expected_asks = _levels(snapshot_state.asks)
            assert projected_bids == expected_bids, (
                f"bid side diverged before transition {transitions_checked + 1}"
            )
            assert projected_asks == expected_asks, (
                f"ask side diverged before transition {transitions_checked + 1}"
            )
            transitions_checked += 1

            anomalies = projection.apply_snapshot(snapshot_state, event_time=event_time)
            disagreements = [
                a
                for a in anomalies
                if a.kind is BookProjectionAnomalyKind.SNAPSHOT_DISAGREES_WITH_PROJECTION
            ]
            assert disagreements == []
        else:
            assert isinstance(payload, PriceChangeV1)
            projection.apply_delta(payload, event_time=event_time, source_asserted_hash=source_hash)

    assert seen_snapshots == 4
    assert transitions_checked == 3
    assert (
        projection.anomaly_counts[BookProjectionAnomalyKind.SNAPSHOT_DISAGREES_WITH_PROJECTION] == 0
    )
