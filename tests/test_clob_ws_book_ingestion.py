"""Tests for `argos.ingestion.clob_ws_book.normalize_clob_ws_book`.

Builds against the real recorded CLOB WebSocket market-channel fixture
(`tests/fixtures/clob/ws_market_price_change.raw.json`), the same fixture-first
convention `tests/test_clob_price_change_ingestion.py` and
`tests/test_ws_book.py` already follow. This file does not import from
either: test modules are not a shared library in this repository.
"""

from __future__ import annotations

import json
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
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.domain.wsbook import WsBookSnapshotV1
from argos.errors import RejectionReason
from argos.ingestion.clob_book import MAX_NORMALIZABLE_BYTES
from argos.ingestion.clob_ws_book import CLOB_WS_BOOK_EVENT_TYPE, normalize_clob_ws_book

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

BOOK_MESSAGE_INDICES = (0, 16, 26, 37)


# --- fixture reading, deliberately duplicated -------------------------------------------


def _load_messages() -> list[dict[str, Any]]:
    with FIXTURE_PATH.open() as handle:
        fixture: dict[str, Any] = json.load(handle, parse_float=Decimal)
    return fixture["messages"]  # type: ignore[no-any-return]


def _decode(raw: str) -> Any:
    try:
        return json.loads(raw, parse_float=Decimal)
    except json.JSONDecodeError:
        return None


def _book_event_at(index: int) -> dict[str, Any]:
    decoded = _decode(_load_messages()[index]["raw"])
    event = decoded[0] if isinstance(decoded, list) else decoded
    assert isinstance(event, dict)
    assert event.get("event_type") == "book"
    return event


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
    return normalize_clob_ws_book(
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
        "event_type": "book",
        "market": CONDITION_ID,
        "asset_id": TOKEN_YES,
        "timestamp": "1786387645775",
        "hash": "57ccf97f291b9bcf0df6f674afa348f2652bd05f",
        "bids": [{"price": "0.40", "size": "10"}],
        "asks": [{"price": "0.60", "size": "8"}],
    }
    payload.update(overrides)
    return payload


# --- the happy path ------------------------------------------------------------------


@pytest.mark.parametrize("index", BOOK_MESSAGE_INDICES)
def test_every_real_book_event_normalizes_into_an_accepted_envelope(index: int) -> None:
    event = _book_event_at(index)
    result = _normalize(event)

    assert isinstance(result, ObservationEnvelopeV1)
    assert result.source is ObservationSource.CLOB_MARKET_WS
    assert result.source_event_type == CLOB_WS_BOOK_EVENT_TYPE
    assert result.condition_id == CONDITION_ID.lower()
    assert result.token_id == TOKEN_YES
    assert result.source_hash == event["hash"]
    assert result.event_time_status is EventTimeStatus.PRESENT
    assert result.event_time == datetime.fromtimestamp(int(event["timestamp"]) / 1000, tz=UTC)
    assert result.ingest_sequence == 1
    assert result.capture_run_id == RUN


def test_the_accepted_envelope_carries_a_typed_ws_book_payload() -> None:
    result = _normalize(_book_event_at(0))
    assert isinstance(result, ObservationEnvelopeV1)
    payload = read_payload(result, WsBookSnapshotV1)
    assert len(payload.bids) == 27
    assert len(payload.asks) == 55
    assert payload.tick_size == Decimal("0.01")


def test_an_in_stream_snapshot_has_no_tick_size_or_last_trade_price() -> None:
    result = _normalize(_book_event_at(16))
    assert isinstance(result, ObservationEnvelopeV1)
    payload = read_payload(result, WsBookSnapshotV1)
    assert payload.tick_size is None
    assert payload.last_trade_price is None


# --- the `None` return: a book event not about this token, not a malformed one -------


def test_a_book_event_for_a_different_token_returns_none_not_a_rejection() -> None:
    event = _book_event_at(0)
    result = _normalize(event, requested_token_id=UNRELATED_TOKEN)
    assert result is None


def test_every_real_book_event_returns_none_for_a_token_it_never_names() -> None:
    for index in BOOK_MESSAGE_INDICES:
        assert _normalize(_book_event_at(index), requested_token_id=TOKEN_NO) is None


# --- malformed event -> rejection, never an exception ---------------------------------


def test_a_malformed_real_shaped_event_produces_a_rejection_not_an_exception() -> None:
    event = {**_book_event_at(0), "bids": [{"price": "0.4", "size": "0"}]}
    result = _normalize(event)

    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD
    assert result.token_id == TOKEN_YES
    assert result.raw_payload_sha256 == sha256_hex(b"raw-bytes-for-hashing")


def test_a_non_object_event_is_rejected() -> None:
    result = _normalize([1, 2, 3])
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD


def test_bids_not_a_list_is_rejected() -> None:
    result = _normalize(_minimal_event(bids="not-a-list"))
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD


def test_an_unexpected_top_level_field_is_rejected() -> None:
    result = _normalize(_minimal_event(neg_risk=True))
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


# --- hash: optional, tolerant of absence, strict about type ---------------------------


def test_hash_absent_is_accepted_with_source_hash_none() -> None:
    event = _minimal_event()
    del event["hash"]
    result = _normalize(event)
    assert isinstance(result, ObservationEnvelopeV1)
    assert result.source_hash is None


def test_a_non_string_hash_is_rejected() -> None:
    result = _normalize(_minimal_event(hash=12345))
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD
    assert "hash" in result.detail


@pytest.mark.parametrize(
    ("hostile_hash", "label"),
    [
        ("a" * 257, "one character past the identifier limit"),
        ("ab\ncd", "an embedded newline"),
        ("h\x1b]52;c;cHdu\x07", "an OSC 52 clipboard write"),
        ("h" * 20_000_000, "20,000,000 characters"),
        ("\ud800", "a lone UTF-16 surrogate"),
    ],
)
def test_a_hostile_hash_becomes_a_rejection_rather_than_raising(
    hostile_hash: str, label: str
) -> None:
    result = _normalize(_minimal_event(hash=hostile_hash))
    assert isinstance(result, RejectedObservationV1), f"{label} must be rejected, not raised"
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD
    assert "hash" in result.detail


def test_a_hash_at_exactly_the_identifier_limit_is_still_accepted() -> None:
    result = _normalize(_minimal_event(hash="a" * 256))
    assert isinstance(result, ObservationEnvelopeV1)
    assert result.source_hash == "a" * 256


# --- a byte cap checked before any parsing ---------------------------------------------


def test_a_response_over_the_byte_cap_is_rejected_before_parsing() -> None:
    event = _minimal_event()
    result = _normalize(event, byte_length=MAX_NORMALIZABLE_BYTES + 1)
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD
    assert str(MAX_NORMALIZABLE_BYTES + 1) in result.detail


def test_a_response_at_exactly_the_byte_cap_is_not_rejected_for_size() -> None:
    event = _minimal_event()
    result = _normalize(event, byte_length=MAX_NORMALIZABLE_BYTES)
    assert isinstance(result, ObservationEnvelopeV1)


def test_the_byte_cap_check_precedes_any_parsing_even_for_unparsable_content() -> None:
    """A body too large to normalize is refused on size alone, even if its
    content is also independently malformed -- the same discipline
    `clob_price_change`'s own byte-cap test pins."""
    result = _normalize({"not": "a book event"}, byte_length=MAX_NORMALIZABLE_BYTES + 1)
    assert isinstance(result, RejectedObservationV1)
    assert "normalization budget" in result.detail


# --- duplicate redelivery: same identity, not tested here at the store layer ----------


def test_two_deliveries_of_the_same_event_mint_the_same_observation_id() -> None:
    """Store-level dedupe is exercised end to end in the capture-loop tests;
    this pins the identity precondition it depends on: the same event, event
    time, and provenance normalize to the same `observation_id`."""
    event = _book_event_at(0)
    first = _normalize(event, ingest_sequence=1)
    second = _normalize(event, ingest_sequence=2)
    assert isinstance(first, ObservationEnvelopeV1)
    assert isinstance(second, ObservationEnvelopeV1)
    assert first.observation_id == second.observation_id
