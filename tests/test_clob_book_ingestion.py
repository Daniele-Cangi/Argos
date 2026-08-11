"""Tests for `argos.ingestion.clob_book.normalize_clob_book`.

Builds against the real recorded CLOB `/book` fixture
(`tests/fixtures/clob/book_yes.raw.json`, a verbatim copy of
`docs/research/fixtures/clob-book-yes-2026-08-10T181007Z.json`), not only
constructed payloads, per the project's fixture-first testing convention —
and closes the M2 exit criteria this normalizer sits directly under:
"duplicate source event does not create a second accepted observation" and
"invalid messages enter a rejection ledger with reason and raw hash".
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
from argos.domain.orderbook import OrderBookSnapshotV1
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.errors import RejectionReason
from argos.ingestion.clob_book import CLOB_REST_BOOK_EVENT_TYPE, normalize_clob_book
from argos.store.event_store import Disposition, open_sqlite_event_store

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "clob" / "book_yes.raw.json"
TOKEN_ID = "63842529068710005716169325380315470359047749786610778647370693404952498013178"
CONDITION_ID = "0x876506d8b2bd7a0d3fa4fe18c024eee6e1dd81ee24c26795dadd6cfe4a7b5d0d"
RUN = "capture-run-1"
RECEIVED = datetime(2026, 8, 10, 18, 10, 8, tzinfo=UTC)
REJECTED_AT = datetime(2026, 8, 10, 18, 10, 8, 5, tzinfo=UTC)


def _load_payload(**overrides: Any) -> dict[str, Any]:
    with FIXTURE.open() as handle:
        payload: dict[str, Any] = json.load(handle, parse_float=Decimal)
    payload.update(overrides)
    return payload


def _provenance(raw: bytes, **overrides: Any) -> SourceProvenanceV1:
    fields: dict[str, Any] = {
        "source": "clob_rest",
        "endpoint": f"https://clob.polymarket.com/book?token_id={TOKEN_ID}",
        "http_status": 200,
        "retrieved_at": RECEIVED,
        "raw_sha256": sha256_hex(raw),
        "byte_length": len(raw),
    }
    fields.update(overrides)
    return SourceProvenanceV1(**fields)


def _normalize(
    payload: Any,
    *,
    raw: bytes = b"raw-bytes-for-hashing",
    requested_token_id: str = TOKEN_ID,
    received_time: datetime = RECEIVED,
    rejected_at: datetime = REJECTED_AT,
    ingest_sequence: int = 1,
    capture_run_id: str = RUN,
    **overrides: Any,
) -> ObservationEnvelopeV1 | RejectedObservationV1:
    return normalize_clob_book(
        payload=payload,
        provenance=_provenance(raw),
        requested_token_id=requested_token_id,
        received_time=received_time,
        rejected_at=rejected_at,
        ingest_sequence=ingest_sequence,
        capture_run_id=capture_run_id,
        **overrides,
    )


# --- the happy path -------------------------------------------------------------------


def test_the_real_fixture_normalizes_into_an_accepted_envelope() -> None:
    payload = _load_payload()
    result = _normalize(payload)

    assert isinstance(result, ObservationEnvelopeV1)
    assert result.source is ObservationSource.CLOB_REST
    assert result.source_event_type == CLOB_REST_BOOK_EVENT_TYPE
    assert result.condition_id == CONDITION_ID
    assert result.token_id == TOKEN_ID
    assert result.source_hash == "4f5acf63ca0bba3aad4d9b888c6a05614ce9cf7a"
    assert result.event_time_status is EventTimeStatus.PRESENT
    assert result.event_time == datetime.fromtimestamp(1786385407185 / 1000, tz=UTC)
    assert result.ingest_sequence == 1
    assert result.capture_run_id == RUN


def test_the_accepted_envelope_carries_a_typed_order_book_snapshot() -> None:
    payload = _load_payload()
    result = _normalize(payload)
    assert isinstance(result, ObservationEnvelopeV1)

    snapshot = read_payload(result, OrderBookSnapshotV1)
    # The counter-intuitive wire order (docs/research/m2-clob-rest-book.md):
    # this must reflect the *sorted* "best level first" convention, not the
    # wire's own bids-ascending/asks-descending shape.
    assert snapshot.best_bid == Decimal("0.42")
    assert snapshot.best_ask == Decimal("0.43")


# --- malformed body -> rejection, never an exception -----------------------------------


@pytest.mark.parametrize(
    ("mutation", "description"),
    [
        ({"neg_risk": None}, "neg_risk is missing/null"),
        ({"bids": "not-a-list"}, "bids is not an array"),
        ({"tick_size": "0.00"}, "tick_size is not positive"),
    ],
)
def test_a_malformed_body_produces_a_rejection_not_an_exception(
    mutation: dict[str, Any], description: str
) -> None:
    payload = _load_payload(**mutation)
    result = _normalize(payload)

    assert isinstance(result, RejectedObservationV1), description
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD
    assert result.token_id == TOKEN_ID
    assert result.raw_payload_sha256 == sha256_hex(b"raw-bytes-for-hashing")


def test_a_non_object_body_is_rejected() -> None:
    result = _normalize([1, 2, 3])
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD


def test_an_asset_id_mismatch_is_rejected() -> None:
    """A response for the wrong token must not be silently accepted under the
    id this adapter asked for."""
    payload = _load_payload()
    result = _normalize(payload, requested_token_id="1" * 60)
    assert isinstance(result, RejectedObservationV1)
    assert "does not match" in result.detail


def test_a_non_string_hash_is_rejected_but_a_missing_hash_is_not() -> None:
    """See the module docstring: absence is tolerated, wrong type is not."""
    hostile = _load_payload(hash=12345)
    rejected = _normalize(hostile)
    assert isinstance(rejected, RejectedObservationV1)
    assert rejected.reason is RejectionReason.MALFORMED_PAYLOAD

    payload = _load_payload()
    del payload["hash"]
    accepted = _normalize(payload)
    assert isinstance(accepted, ObservationEnvelopeV1)
    assert accepted.source_hash is None


# --- event time: never substitute the wall clock ---------------------------------------


def test_an_unparseable_timestamp_is_preserved_not_substituted() -> None:
    payload = _load_payload(timestamp="not-a-number")
    result = _normalize(payload)

    assert isinstance(result, ObservationEnvelopeV1)
    assert result.event_time_status is EventTimeStatus.UNPARSEABLE
    assert result.event_time is None
    assert result.event_time_raw == "not-a-number"
    assert result.received_time == RECEIVED
    assert result.event_time_raw != result.received_time.isoformat()


def test_a_missing_timestamp_is_missing_not_substituted() -> None:
    payload = _load_payload()
    del payload["timestamp"]
    result = _normalize(payload)

    assert isinstance(result, ObservationEnvelopeV1)
    assert result.event_time_status is EventTimeStatus.MISSING
    assert result.event_time is None
    assert result.event_time_raw is None


def test_a_non_string_timestamp_is_unparseable_with_diagnostic_text_preserved() -> None:
    payload = _load_payload(timestamp=1786385407185)
    result = _normalize(payload)

    assert isinstance(result, ObservationEnvelopeV1)
    assert result.event_time_status is EventTimeStatus.UNPARSEABLE
    assert result.event_time_raw == "1786385407185"


def test_an_empty_timestamp_string_is_treated_as_missing() -> None:
    payload = _load_payload(timestamp="")
    result = _normalize(payload)

    assert isinstance(result, ObservationEnvelopeV1)
    assert result.event_time_status is EventTimeStatus.MISSING


# --- determinism -----------------------------------------------------------------------


def test_normalization_of_the_same_bytes_is_deterministic() -> None:
    payload = _load_payload()
    first = _normalize(payload)
    second = _normalize(payload)

    assert isinstance(first, ObservationEnvelopeV1)
    assert isinstance(second, ObservationEnvelopeV1)
    assert first.observation_id == second.observation_id
    assert first.to_record() == second.to_record()


def test_a_cosmetically_different_but_equal_payload_collapses_identity() -> None:
    """A dict built with different key order must still normalize identically —
    canonicalization, not incidental JSON ordering, drives identity (ADR-0010)."""
    payload = _load_payload()
    reordered = dict(reversed(list(payload.items())))
    first = _normalize(payload)
    second = _normalize(reordered)

    assert isinstance(first, ObservationEnvelopeV1)
    assert isinstance(second, ObservationEnvelopeV1)
    assert first.observation_id == second.observation_id


def test_raw_payload_location_is_forwarded_when_supplied() -> None:
    payload = _load_payload()
    result = _normalize(payload, raw_payload_location="archive/clob_rest/deadbeef.raw.json")
    assert isinstance(result, ObservationEnvelopeV1)
    assert result.raw_payload_location == "archive/clob_rest/deadbeef.raw.json"


def test_raw_payload_location_defaults_to_none() -> None:
    payload = _load_payload()
    result = _normalize(payload)
    assert isinstance(result, ObservationEnvelopeV1)
    assert result.raw_payload_location is None


# --- end-to-end through the event store: the M2 duplicate criterion --------------------


def test_a_refetched_unchanged_book_collapses_onto_one_observation_with_two_deliveries() -> None:
    """The M2 exit criterion, end to end: a re-fetched, byte-identical book
    produces a second *delivery* row (an observed arrival) without a second
    *observation* row (core invariant 14 -- the duplicate is observable, not
    silently dropped and not double-counted as new evidence)."""
    payload = _load_payload()
    raw = FIXTURE.read_bytes()

    first = _normalize(payload, raw=raw, ingest_sequence=1)
    second = _normalize(
        payload, raw=raw, ingest_sequence=2, received_time=RECEIVED + timedelta(minutes=5)
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

        stored = store.get_observation(first.observation_id)
        assert stored is not None
        assert stored.observation_id == first.observation_id
    finally:
        store.close()


def test_a_rejection_can_be_appended_to_the_ledger_with_reason_and_raw_hash() -> None:
    payload = _load_payload(bids="not-a-list")
    raw = b"malformed-clob-body"
    rejection = _normalize(payload, raw=raw, ingest_sequence=1)
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


# --- regressions from the M2 CLOB adapter security review ---------------------------


def test_a_response_too_large_to_normalize_is_rejected_cheaply_not_accepted() -> None:
    """HIGH. Security review measured the gap between the client's 32 MiB
    response cap and the envelope's 4 MiB canonical-payload cap: a 31 MiB
    `timestamp` string cost 21.18 s of CPU and was **accepted** as an
    observation; a 31 MiB `market` string on a rejected payload cost 32.37 s
    and 571 MiB RSS; 900,000 book levels peaked at 1,266 MiB. `Pacer` cannot
    bound any of it — a cancel scope cannot interrupt synchronous CPU work,
    re-measured at 14.21 s elapsed against a 0.50 s deadline with
    `cancelled_caught` False.

    This is the same class the earlier M2 review closed at
    `build_observation_envelope`, relocated one layer upstream where no cap
    applied. Reproduced here at 8 MiB (5.48 s CPU before the fix, 0.02 s
    after) to keep the test fast while exercising the same path.

    A rejection rather than a raise: the bytes really did arrive, and a refusal
    that leaves no ledger entry is the silent drop invariant 14 forbids.
    """
    payload = _load_payload(timestamp="9" * 8_000_000)
    raw = json.dumps(payload).encode()

    result = _normalize(payload, raw=raw)

    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD
    assert "normalization budget" in result.detail
    assert result.raw_payload_sha256 == sha256_hex(raw)


def test_an_oversized_market_field_is_refused_before_it_is_neutralized() -> None:
    """The same budget also covers the rejection path, which was the more
    expensive of the two: `neutralize_and_bound` bounds the *stored* value but
    neutralizes the *whole* input first, and the rejection path ran it twice."""
    payload = _load_payload(market="x" * 8_000_000)
    result = _normalize(payload, raw=json.dumps(payload).encode())

    assert isinstance(result, RejectedObservationV1)
    assert "normalization budget" in result.detail


@pytest.mark.parametrize(
    ("hostile_hash", "label"),
    [
        ("a" * 257, "one character past the identifier limit"),
        ("ab\ncd", "an embedded newline"),
        ("h\x1b]52;c;cHdu\x07", "an OSC 52 clipboard write"),
        ("h‮reversed", "a right-to-left override"),
    ],
)
def test_a_hostile_hash_becomes_a_rejection_rather_than_raising(
    hostile_hash: str, label: str
) -> None:
    """MEDIUM. `_extract_source_hash` checked only "non-empty string", so the
    value then reached `ObservationEnvelopeV1._validate_identifier`, which
    refuses by design — but that call sits *outside* this module's try, so a
    raw pydantic `ValidationError` propagated out of `normalize_clob_book`,
    contradicting its own docstring ("never raises for a malformed payload")
    and producing **no ledger entry at all**. Reproduced on a 3.7 KB payload,
    so no resource cost is needed to reach it.

    The 256-character boundary is exact: 256 is accepted, 257 refused.
    """
    payload = _load_payload(hash=hostile_hash)

    result = _normalize(payload)

    assert isinstance(result, RejectedObservationV1), f"{label} must be rejected, not raised"
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD
    assert "hash" in result.detail


def test_a_hash_at_exactly_the_identifier_limit_is_still_accepted() -> None:
    """The guard must be exact, not merely strict — a 256-character hash is
    legal and must not be refused by an off-by-one."""
    result = _normalize(_load_payload(hash="a" * 256))

    assert isinstance(result, ObservationEnvelopeV1)
    assert result.source_hash == "a" * 256
