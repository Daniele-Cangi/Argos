"""Tests for `argos.ingestion.clob_price_change.normalize_clob_price_change`.

Builds against the real recorded CLOB WebSocket market-channel fixture
(`tests/fixtures/clob/ws_market_price_change.raw.json`, a verbatim copy of
`docs/research/fixtures/clob-ws-market-2026-08-10T184742Z.json`), not only
constructed frames -- the same fixture-first convention
`tests/test_price_change.py` and `tests/test_clob_book_ingestion.py` already
follow. This file does not import from either: "test modules are not a
shared library in this repository" (`tests/test_clob_client.py`'s own stated
convention), so the fixture-reading and event-lookup helpers below are a
deliberate, small duplication of `tests/test_price_change.py`'s own.

Closes the M2 exit criteria this normalizer sits directly under, extended
from REST to the WebSocket source: "invalid messages enter a rejection
ledger with reason and raw hash" and "zero-size level update is represented
as removal" (now with end-to-end store evidence, not only payload-level
evidence).
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from argos.domain.observation import (
    EventTimeStatus,
    ObservationEnvelopeV1,
    ObservationSource,
    RejectedObservationV1,
    read_payload,
)
from argos.domain.orderbook import BookSide
from argos.domain.pricechange import PriceChangeV1, PriceLevelChangeKind
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.errors import RejectionReason
from argos.ingestion.clob_book import MAX_NORMALIZABLE_BYTES
from argos.ingestion.clob_price_change import (
    CLOB_WS_PRICE_CHANGE_EVENT_TYPE,
    normalize_clob_price_change,
)
from argos.store.event_store import Disposition, open_sqlite_event_store

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "clob" / "ws_market_price_change.raw.json"
)

TOKEN_YES = "34691510069031117755834214800869745291092253295564665516660269316660628637961"
TOKEN_NO = "95561057794427123541889915407555646439882912350845258651794843110787555977699"
UNRELATED_TOKEN = "11111111111111111111111111111111111111111111111111111111111111111111111111"
CONDITION_ID = "0x94a39addec8bd24a2d03deeaa43bdee6a2b11eca403cd12495eb24132cc23173"

RUN = "capture-run-1"
RECEIVED = datetime(2026, 8, 10, 18, 47, 46, tzinfo=UTC)
REJECTED_AT = RECEIVED + timedelta(milliseconds=5)


# --- fixture reading, deliberately duplicated from tests/test_price_change.py ----------


def _load_fixture() -> dict[str, Any]:
    with FIXTURE_PATH.open() as handle:
        return json.load(handle, parse_float=Decimal)  # type: ignore[no-any-return]


def _price_change_events() -> list[tuple[int, dict[str, Any]]]:
    """Every real `price_change` event in the fixture, tagged with its message index."""
    fixture = _load_fixture()
    events: list[tuple[int, dict[str, Any]]] = []
    for index, message in enumerate(fixture["messages"]):
        raw = message["raw"]
        if raw in ("PING", "PONG"):
            continue
        parsed = json.loads(raw, parse_float=Decimal)
        candidates = parsed if isinstance(parsed, list) else [parsed]
        for event in candidates:
            if event.get("event_type") == "price_change":
                events.append((index, event))
    return events


def _event_at(message_index: int) -> dict[str, Any]:
    for index, event in _price_change_events():
        if index == message_index:
            return event
    raise AssertionError(f"no price_change event at message index {message_index}")


def _event_with_zero_size_entry_for(token: str) -> dict[str, Any]:
    """The one real frame carrying a zero-size entry for `token`.

    Confirmed by `tests/test_price_change.py`'s own
    `test_the_real_fixtures_two_zero_size_entries_are_represented_as_removal`:
    exactly two real zero-size entries exist in this capture, one per token.
    """
    for _index, event in _price_change_events():
        for entry in event["price_changes"]:
            if entry["asset_id"] == token and entry["size"] == "0":
                return event
    raise AssertionError(f"no zero-size price_changes entry found for token {token!r}")


# --- helpers -----------------------------------------------------------------------------


def _provenance(raw: bytes, **overrides: Any) -> SourceProvenanceV1:
    fields: dict[str, Any] = {
        "source": "clob_market_ws",
        "endpoint": "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        "http_status": None,
        "retrieved_at": RECEIVED,
        "raw_sha256": sha256_hex(raw),
        "byte_length": len(raw),
    }
    fields.update(overrides)
    return SourceProvenanceV1(**fields)


def _normalize(
    event: Any,
    *,
    raw: bytes = b"raw-bytes-for-hashing",
    requested_token_id: str = TOKEN_YES,
    received_time: datetime = RECEIVED,
    rejected_at: datetime = REJECTED_AT,
    ingest_sequence: int = 1,
    capture_run_id: str = RUN,
    byte_length: int | None = None,
    **overrides: Any,
) -> ObservationEnvelopeV1 | RejectedObservationV1 | None:
    provenance_overrides: dict[str, Any] = {}
    if byte_length is not None:
        provenance_overrides["byte_length"] = byte_length
    return normalize_clob_price_change(
        event=event,
        provenance=_provenance(raw, **provenance_overrides),
        requested_token_id=requested_token_id,
        received_time=received_time,
        rejected_at=rejected_at,
        ingest_sequence=ingest_sequence,
        capture_run_id=capture_run_id,
        **overrides,
    )


def _minimal_event(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_type": "price_change",
        "market": CONDITION_ID,
        "timestamp": "1786387666174",
        "price_changes": [
            {
                "asset_id": TOKEN_YES,
                "price": "0.49",
                "size": "636",
                "side": "SELL",
                "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                "best_bid": "0.28",
                "best_ask": "0.29",
            }
        ],
    }
    payload.update(overrides)
    return payload


def _large_price_change_event(count: int, *, token: str = TOKEN_YES) -> dict[str, Any]:
    """A well-formed frame carrying `count` distinct level changes for `token`.

    Distinct, finely-spaced decimal prices avoid the duplicate-(side, price)
    refusal `PriceChangeV1` enforces, so every entry is legal and the parser
    genuinely does the full amount of work the byte cap exists to avoid.
    """
    entries = [
        {
            "asset_id": token,
            "price": "0." + str(100_000 + i)[1:],
            "size": "1",
            "side": "BUY" if i % 2 == 0 else "SELL",
            "hash": "a" * 40,
            "best_bid": "0.5",
            "best_ask": "0.6",
        }
        for i in range(count)
    ]
    return {
        "event_type": "price_change",
        "market": CONDITION_ID,
        "timestamp": "1786387666174",
        "price_changes": entries,
    }


# --- the happy path ------------------------------------------------------------------


def test_a_real_frame_normalizes_into_an_accepted_envelope() -> None:
    event = _event_at(1)
    result = _normalize(event)

    assert isinstance(result, ObservationEnvelopeV1)
    assert result.source is ObservationSource.CLOB_MARKET_WS
    assert result.source_event_type == CLOB_WS_PRICE_CHANGE_EVENT_TYPE
    assert result.condition_id == CONDITION_ID.lower()
    assert result.token_id == TOKEN_YES
    assert result.source_hash == "5ce704dea0a2123a388f1d8b432aad0058f5c479"
    assert result.event_time_status is EventTimeStatus.PRESENT
    assert result.event_time == datetime.fromtimestamp(1786387666174 / 1000, tz=UTC)
    assert result.ingest_sequence == 1
    assert result.capture_run_id == RUN


def test_the_accepted_envelope_carries_a_typed_price_change_payload() -> None:
    event = _event_at(1)
    result = _normalize(event)
    assert isinstance(result, ObservationEnvelopeV1)

    payload = read_payload(result, PriceChangeV1)
    assert len(payload.changes) == 1
    change = payload.changes[0]
    assert change.side is BookSide.ASK
    assert change.price == Decimal("0.49")
    assert change.kind is PriceLevelChangeKind.SET


# --- the `None` return: a frame not about this token, not a malformed frame ----------


def test_a_frame_with_no_entry_for_the_requested_token_returns_none_not_a_rejection() -> None:
    """A real, well-formed frame -- just not about the token this call asked
    about. Must be distinguishable from a rejection: `None`, no ledger entry
    minted by this function, and it is the caller's job to count it."""
    event = _event_at(1)
    result = _normalize(event, requested_token_id=UNRELATED_TOKEN)
    assert result is None


def test_every_real_frame_returns_none_for_a_token_the_frame_never_mentions() -> None:
    """Not a single real captured frame in ~85 seconds of live traffic ever
    names a third token -- every one of the 34 real price_change frames must
    take the None branch for a token nobody subscribed to."""
    for _index, event in _price_change_events():
        assert _normalize(event, requested_token_id=UNRELATED_TOKEN) is None


# --- malformed frame -> rejection, never an exception --------------------------------


def test_a_malformed_real_shaped_frame_produces_a_rejection_not_an_exception() -> None:
    event = _event_at(1)
    yes_entry = next(e for e in event["price_changes"] if e["asset_id"] == TOKEN_YES)
    event = {**event, "price_changes": [{**yes_entry, "side": "SIDEWAYS"}]}
    result = _normalize(event)

    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD
    assert result.token_id == TOKEN_YES
    assert result.raw_payload_sha256 == sha256_hex(b"raw-bytes-for-hashing")


def test_a_non_object_event_is_rejected() -> None:
    result = _normalize([1, 2, 3])
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD


def test_price_changes_not_a_list_is_rejected() -> None:
    result = _normalize(_minimal_event(price_changes="not-a-list"))
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD


# --- event time: never substitute the wall clock --------------------------------------


def test_an_unparseable_timestamp_is_preserved_not_substituted() -> None:
    result = _normalize(_minimal_event(timestamp="not-a-number"))
    assert isinstance(result, ObservationEnvelopeV1)
    assert result.event_time_status is EventTimeStatus.UNPARSEABLE
    assert result.event_time is None
    assert result.event_time_raw == "not-a-number"
    assert result.received_time == RECEIVED


def test_a_missing_timestamp_is_missing_not_substituted() -> None:
    event = _minimal_event()
    del event["timestamp"]
    result = _normalize(event)
    assert isinstance(result, ObservationEnvelopeV1)
    assert result.event_time_status is EventTimeStatus.MISSING
    assert result.event_time is None


# --- Task 3.2: entry_hash validated inside this module's own try ----------------------


@pytest.mark.parametrize(
    ("hostile_hash", "label"),
    [
        ("a" * 257, "one character past the identifier limit"),
        ("ab\ncd", "an embedded newline"),
        ("h\x1b]52;c;cHdu\x07", "an OSC 52 clipboard write"),
        ("h" * 20_000_000, "20,000,000 characters"),
    ],
)
def test_a_hostile_entry_hash_becomes_a_rejection_rather_than_raising(
    hostile_hash: str, label: str
) -> None:
    """MEDIUM, third appearance (`docs/BACKLOG.md`). `parse_price_change_group`
    deliberately leaves `entry_hash` unsanitized -- it then reaches
    `ObservationEnvelopeV1._validate_identifier`, which refuses by design, but
    that call sits *outside* this module's own try, so a raw pydantic
    `ValidationError` would otherwise propagate out of
    `normalize_clob_price_change`, contradicting its own docstring and
    producing **no ledger entry at all**."""
    event = _minimal_event(
        price_changes=[{**_minimal_event()["price_changes"][0], "hash": hostile_hash}]
    )
    result = _normalize(event)

    assert isinstance(result, RejectedObservationV1), f"{label} must be rejected, not raised"
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD
    assert "hash" in result.detail


def test_a_hash_at_exactly_the_identifier_limit_is_still_accepted() -> None:
    event = _minimal_event(
        price_changes=[{**_minimal_event()["price_changes"][0], "hash": "a" * 256}]
    )
    result = _normalize(event)
    assert isinstance(result, ObservationEnvelopeV1)
    assert result.source_hash == "a" * 256


# --- Task 3.1: a byte cap checked before any parsing -----------------------------------


def test_an_oversized_frame_is_rejected_cheaply_without_paying_the_parse_cost() -> None:
    """`docs/BACKLOG.md`: `parse_price_change_group` bounds neither array
    length nor field size, so a large `price_changes` array is expensive to
    parse (measured: 100,000 entries cost 16.15 s CPU / 81.2 MiB upstream).

    Reproduced here with 20,000 well-formed entries so the *baseline* cost is
    measured directly by this test rather than only asserted from a prior
    measurement, while staying fast enough to run in CI: parsing all 20,000
    (`provenance.byte_length` under the cap, so the full parse runs) is timed
    against refusing the identical event when `provenance.byte_length`
    exceeds `MAX_NORMALIZABLE_BYTES` (checked before a single key is read out
    of `event`, so the cost is independent of how large `event` actually is).
    """
    event = _large_price_change_event(20_000)

    start = time.perf_counter()
    accepted = _normalize(event, byte_length=1_000)
    parse_elapsed = time.perf_counter() - start
    assert isinstance(accepted, ObservationEnvelopeV1), (
        "the baseline must actually accept -- otherwise the timing comparison is vacuous"
    )

    start = time.perf_counter()
    rejected = _normalize(event, byte_length=MAX_NORMALIZABLE_BYTES + 1)
    reject_elapsed = time.perf_counter() - start
    assert isinstance(rejected, RejectedObservationV1)
    assert rejected.reason is RejectionReason.MALFORMED_PAYLOAD
    assert "normalization budget" in rejected.detail

    assert parse_elapsed > 0.1, (
        "the baseline parse must be measurably expensive, or this test proves nothing "
        f"(measured {parse_elapsed:.4f}s)"
    )
    assert reject_elapsed < parse_elapsed / 5, (
        f"refusal ({reject_elapsed:.4f}s) must be a small fraction of the parse it avoided "
        f"({parse_elapsed:.4f}s)"
    )


def test_an_oversized_frame_is_rejected_not_accepted_at_the_exact_boundary() -> None:
    """The guard must be exact: one byte over the cap refuses; one byte under accepts.

    Uses a tiny, cheap event -- this test is about the boundary, not about cost.
    """
    event = _minimal_event()

    under = _normalize(event, byte_length=MAX_NORMALIZABLE_BYTES)
    assert isinstance(under, ObservationEnvelopeV1)

    over = _normalize(event, byte_length=MAX_NORMALIZABLE_BYTES + 1)
    assert isinstance(over, RejectedObservationV1)
    assert over.reason is RejectionReason.MALFORMED_PAYLOAD


# --- Task 4: end-to-end evidence through the event store -------------------------------


def test_a_real_zero_size_entry_lands_in_the_store_as_a_remove() -> None:
    """The M2 exit criterion, end to end, on the WebSocket delta stream: the
    two real zero-size entries in this capture (`0.17 BUY` on `TOKEN_YES`,
    `0.83 SELL` on `TOKEN_NO`) must reach the store as an observation whose
    payload represents removal, not merely be recognized at the payload
    level (`tests/test_price_change.py` already proves that)."""
    store = open_sqlite_event_store(":memory:")
    try:
        store.open_capture_run(RUN, started_at=RECEIVED)
        sequence = 1
        for token, expected_side, expected_price in (
            (TOKEN_YES, BookSide.BID, Decimal("0.17")),
            (TOKEN_NO, BookSide.ASK, Decimal("0.83")),
        ):
            event = _event_with_zero_size_entry_for(token)
            raw = json.dumps(event, default=str).encode()
            result = _normalize(event, raw=raw, requested_token_id=token, ingest_sequence=sequence)
            assert isinstance(result, ObservationEnvelopeV1)

            store.append_observation(result)
            stored = store.get_observation(result.observation_id)
            assert stored is not None
            payload = read_payload(stored, PriceChangeV1)
            assert len(payload.changes) == 1
            change = payload.changes[0]
            assert change.kind is PriceLevelChangeKind.REMOVE
            assert change.side is expected_side
            assert change.price == expected_price
            assert change.size == Decimal("0")
            sequence += 1

        counts = store.counts_for_capture_run(RUN)
        assert counts.accepted == 2
        assert counts.rejected == 0
    finally:
        store.close()


def test_redelivering_the_identical_frame_collapses_onto_one_observation_two_deliveries() -> None:
    """The M2 exit criterion, end to end, extended from REST to the WebSocket
    source: a redelivered frame (a websocket resend after reconnect, for
    example) produces a second *delivery* row without a second *observation*
    row."""
    event = _event_at(1)
    raw = json.dumps(event, default=str).encode()

    first = _normalize(event, raw=raw, ingest_sequence=1)
    second = _normalize(
        event, raw=raw, ingest_sequence=2, received_time=RECEIVED + timedelta(minutes=5)
    )
    assert isinstance(first, ObservationEnvelopeV1)
    assert isinstance(second, ObservationEnvelopeV1)
    assert first.observation_id == second.observation_id

    store = open_sqlite_event_store(":memory:")
    try:
        store.open_capture_run(RUN, started_at=RECEIVED)
        first_delivery = store.append_observation(first)
        second_delivery = store.append_observation(second)

        assert first_delivery.disposition is Disposition.ACCEPTED_NEW
        assert second_delivery.disposition is Disposition.DUPLICATE

        counts = store.counts_for_capture_run(RUN)
        assert counts.accepted == 1
        assert counts.duplicate == 1
        assert counts.rejected == 0

        deliveries = list(store.iter_deliveries(RUN))
        assert [d.ingest_sequence for d in deliveries] == [1, 2]
    finally:
        store.close()


def test_a_malformed_real_shaped_frame_reaches_the_rejection_ledger_with_reason_and_raw_hash() -> (
    None
):
    event = _event_at(1)
    yes_entry = next(e for e in event["price_changes"] if e["asset_id"] == TOKEN_YES)
    event = {**event, "price_changes": [{**yes_entry, "side": "SIDEWAYS"}]}
    raw = b"malformed-price-change-frame"
    rejection = _normalize(event, raw=raw, ingest_sequence=1)
    assert isinstance(rejection, RejectedObservationV1)

    store = open_sqlite_event_store(":memory:")
    try:
        store.open_capture_run(RUN, started_at=RECEIVED)
        store.append_rejection(rejection, ingest_sequence=1)

        stored = list(store.iter_rejections(RUN))
        assert len(stored) == 1
        assert stored[0].rejection.reason is RejectionReason.MALFORMED_PAYLOAD
        assert stored[0].rejection.raw_payload_sha256 == sha256_hex(raw)
    finally:
        store.close()


def test_the_shared_timestamp_hash_pair_across_three_real_frames_mints_three_observation_rows() -> (
    None
):
    """The identity hazard `docs/research/m2-clob-websocket.md` records and
    `tests/test_price_change.py` already pins at the payload level: for
    `TOKEN_YES`, `(timestamp=1786387666174,
    hash=5ce704dea0a2123a388f1d8b432aad0058f5c479)` spans three separate real
    frames (`messages[1]`, `[2]`, `[3]`), each carrying a different price
    level change. Proven here through the store, not only through comparing
    `observation_id` values directly."""
    frames = [_event_at(1), _event_at(2), _event_at(3)]

    for event in frames:
        for entry in event["price_changes"]:
            if entry["asset_id"] == TOKEN_YES:
                assert entry["hash"] == "5ce704dea0a2123a388f1d8b432aad0058f5c479"
                assert event["timestamp"] == "1786387666174"

    store = open_sqlite_event_store(":memory:")
    try:
        store.open_capture_run(RUN, started_at=RECEIVED)
        observation_ids = set()
        for sequence, event in enumerate(frames, start=1):
            raw = json.dumps(event, default=str).encode() + str(sequence).encode()
            result = _normalize(event, raw=raw, ingest_sequence=sequence)
            assert isinstance(result, ObservationEnvelopeV1)
            delivery = store.append_observation(result)
            assert delivery.disposition is Disposition.ACCEPTED_NEW, (
                "each of the three genuinely different level changes must be new "
                "evidence, not silently collapsed onto an earlier one"
            )
            observation_ids.add(result.observation_id)

        assert len(observation_ids) == 3

        counts = store.counts_for_capture_run(RUN)
        assert counts.accepted == 3
        assert counts.duplicate == 0
    finally:
        store.close()


# --- a malformed hash reaches the ledger instead of escaping --------------------------
#
# Reported against a9b9802. `parse_price_change_group` built its hash set, and
# sorted it for an error message, before type-checking any hash, so an
# unhashable or mixed-type `hash` raised a bare `TypeError` -- outside the ARGOS
# taxonomy and therefore past this module's `except ValueError`. The frame would
# have left no rejection-ledger entry at all. Fixed in the domain; pinned here
# at the boundary that actually owes the ledger row, because that is the
# property that matters operationally.


@pytest.mark.parametrize(
    ("label", "hash_value"),
    [
        ("unhashable list", []),
        ("unhashable dict", {}),
        ("null", None),
        ("numeric", 3),
        ("empty string", ""),
    ],
)
def test_a_malformed_hash_is_recorded_as_a_rejection_not_raised(
    label: str, hash_value: Any
) -> None:
    event = _minimal_event()
    event["price_changes"][0]["hash"] = hash_value

    result = _normalize(event)

    assert isinstance(result, RejectedObservationV1), f"{label}: got {type(result).__name__}"
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD
    assert result.raw_payload_sha256
    assert "hash" in result.detail


def test_a_frame_mixing_hash_types_across_entries_is_recorded_as_a_rejection() -> None:
    """The second escape: `sorted()` over a mixed-type set, while building the
    error message for a *different* refusal (more than one distinct hash)."""
    event = _minimal_event()
    first = event["price_changes"][0]
    second = dict(first)
    second["price"] = "0.50"
    second["hash"] = 3
    event["price_changes"] = [first, second]

    result = _normalize(event)

    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD


def test_a_malformed_hash_rejection_is_persisted_to_the_ledger() -> None:
    """End to end: the row an operator would actually count is really written."""
    event = _minimal_event()
    event["price_changes"][0]["hash"] = []
    result = _normalize(event)
    assert isinstance(result, RejectedObservationV1)

    store = open_sqlite_event_store(":memory:")
    try:
        store.open_capture_run(RUN, started_at=RECEIVED)
        store.append_rejection(result, ingest_sequence=1)
        rejections = list(store.iter_rejections(RUN))
    finally:
        store.close()

    assert len(rejections) == 1
    stored = rejections[0].rejection
    assert stored.reason is RejectionReason.MALFORMED_PAYLOAD
    assert stored.raw_payload_sha256 == result.raw_payload_sha256
    assert "hash" in stored.detail
