"""Tests for `argos.ingestion.capture`'s `book` event dispatch.

Focused on what changed in this slice: `book` is no longer routed to the
`UNKNOWN_EVENT_TYPE` rejection path, and the sequence/store/health machinery
`tests/test_capture_loop.py` already exercises generically for `price_change`
behaves identically for `book` -- a `book` event is one more record kind
dispatched through the same peek-then-commit machinery, not a new allocation
rule (`argos.ingestion.capture`'s own module docstring, Decision 2). This file
does not import from `tests/test_capture_loop.py`: test modules are not a
shared library in this repository.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import orjson
import pytest

from argos.clock import ReplayClock
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.errors import RejectionReason
from argos.ingestion.capture import run_capture
from argos.sources.clob_ws import MarketFrame
from argos.store.event_store import Disposition, SQLiteEventStore, open_sqlite_event_store

TOKEN_YES = "34691510069031117755834214800869745291092253295564665516660269316660628637961"
TOKEN_NO = "95561057794427123541889915407555646439882912350845258651794843110787555977699"
CONDITION_ID = "0x94a39addec8bd24a2d03deeaa43bdee6a2b11eca403cd12495eb24132cc23173"

START = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)
ENDPOINT = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _book_event(
    *,
    timestamp: str = "1786387645775",
    entry_hash: str = "57ccf97f291b9bcf0df6f674afa348f2652bd05f",
    asset_id: str = TOKEN_YES,
) -> dict[str, Any]:
    return {
        "event_type": "book",
        "market": CONDITION_ID,
        "asset_id": asset_id,
        "timestamp": timestamp,
        "hash": entry_hash,
        "bids": [{"price": "0.40", "size": "10"}],
        "asks": [{"price": "0.60", "size": "8"}],
    }


def _price_change_event(
    *,
    timestamp: str = "1786387666174",
    entry_hash: str = "5ce704dea0a2123a388f1d8b432aad0058f5c479",
    asset_id: str = TOKEN_YES,
) -> dict[str, Any]:
    return {
        "event_type": "price_change",
        "market": CONDITION_ID,
        "timestamp": timestamp,
        "price_changes": [
            {
                "asset_id": asset_id,
                "price": "0.49",
                "size": "636",
                "side": "SELL",
                "hash": entry_hash,
                "best_bid": "0.28",
                "best_ask": "0.29",
            }
        ],
    }


def _frame(payload: Any, *, received_time: datetime = START) -> MarketFrame:
    body = orjson.dumps(payload).decode()
    raw = body.encode("utf-8")
    return MarketFrame(
        text=body,
        received_time=received_time,
        provenance=SourceProvenanceV1(
            source="clob_market_ws",
            endpoint=ENDPOINT,
            http_status=None,
            retrieved_at=received_time,
            raw_sha256=sha256_hex(raw),
            byte_length=len(raw),
        ),
    )


@dataclass
class ListFrameSource:
    frames_to_yield: Sequence[MarketFrame]

    async def frames(self) -> AsyncIterator[MarketFrame]:
        for frame in self.frames_to_yield:
            yield frame


@pytest.fixture
def store() -> SQLiteEventStore:
    return open_sqlite_event_store(":memory:")


@pytest.fixture
def clock() -> ReplayClock:
    return ReplayClock(START)


async def test_a_book_event_is_accepted_not_rejected_as_unknown(
    store: SQLiteEventStore, clock: ReplayClock
) -> None:
    run_id = "run-book-accepted"
    health = await run_capture(
        frame_source=ListFrameSource([_frame(_book_event())]),
        store=store,
        clock=clock,
        capture_run_id=run_id,
        subscribed_token_ids=[TOKEN_YES],
    )
    assert health.accepted == 1
    assert health.unknown_event_type == 0
    assert health.rejected == 0
    deliveries = list(store.iter_deliveries(run_id))
    assert len(deliveries) == 1
    assert deliveries[0].disposition is Disposition.ACCEPTED_NEW
    envelope = store.get_observation(deliveries[0].observation_id)
    assert envelope is not None
    assert envelope.payload_schema_version == "ws_book_snapshot.v1"


async def test_duplicate_book_frames_collapse_to_one_observation_with_two_deliveries(
    store: SQLiteEventStore, clock: ReplayClock
) -> None:
    event = _book_event()
    run_id = "run-book-duplicate"
    health = await run_capture(
        frame_source=ListFrameSource([_frame(event), _frame(event)]),
        store=store,
        clock=clock,
        capture_run_id=run_id,
        subscribed_token_ids=[TOKEN_YES],
    )
    deliveries = list(store.iter_deliveries(run_id))
    assert len(deliveries) == 2
    assert deliveries[0].observation_id == deliveries[1].observation_id
    assert deliveries[0].disposition is Disposition.ACCEPTED_NEW
    assert deliveries[1].disposition is Disposition.DUPLICATE
    assert health.accepted == 1
    assert health.duplicate == 1


async def test_a_book_event_naming_only_the_sibling_is_not_applicable(
    store: SQLiteEventStore, clock: ReplayClock
) -> None:
    run_id = "run-book-sibling"
    health = await run_capture(
        frame_source=ListFrameSource([_frame(_book_event(asset_id=TOKEN_NO))]),
        store=store,
        clock=clock,
        capture_run_id=run_id,
        subscribed_token_ids=[TOKEN_YES],
    )
    assert health.not_applicable == 1
    assert health.accepted == 0
    assert health.rejected == 0
    assert list(store.iter_deliveries(run_id)) == []
    assert list(store.iter_rejections(run_id)) == []


async def test_a_malformed_book_event_is_a_counted_rejection(
    store: SQLiteEventStore, clock: ReplayClock
) -> None:
    event = {**_book_event(), "bids": [{"price": "0.4", "size": "0"}]}
    run_id = "run-book-malformed"
    health = await run_capture(
        frame_source=ListFrameSource([_frame(event)]),
        store=store,
        clock=clock,
        capture_run_id=run_id,
        subscribed_token_ids=[TOKEN_YES],
    )
    assert health.rejected == 1
    rejections = list(store.iter_rejections(run_id))
    assert rejections[0].rejection.reason is RejectionReason.MALFORMED_PAYLOAD


async def test_book_and_price_change_events_share_one_contiguous_sequence(
    store: SQLiteEventStore, clock: ReplayClock
) -> None:
    """A `book` event is one more record kind, not a new allocation rule
    (module docstring, Decision 2) -- the sequence is contiguous across both
    event kinds, in arrival order."""
    frames = [
        _frame(_book_event()),
        _frame(_price_change_event()),
        _frame(_book_event(timestamp="1786387700000", entry_hash="deadbeef01")),
    ]
    run_id = "run-mixed-sequence"
    await run_capture(
        frame_source=ListFrameSource(frames),
        store=store,
        clock=clock,
        capture_run_id=run_id,
        subscribed_token_ids=[TOKEN_YES],
    )
    deliveries = list(store.iter_deliveries(run_id))
    assert [record.ingest_sequence for record in deliveries] == [1, 2, 3]
    schema_versions = [
        store.get_observation(record.observation_id).payload_schema_version  # type: ignore[union-attr]
        for record in deliveries
    ]
    assert schema_versions == ["ws_book_snapshot.v1", "price_change.v1", "ws_book_snapshot.v1"]
