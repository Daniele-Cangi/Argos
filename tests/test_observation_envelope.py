"""Tests for `ObservationEnvelopeV1` and `RejectedObservationV1`.

M2 exit criteria this file serves: "duplicate source event does not create a
second accepted observation" (identity stability/collision tests below) and
"invalid messages enter a rejection ledger with reason and raw hash".
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from argos.domain.observation import (
    MAX_IDENTIFIER_LENGTH,
    MAX_PAYLOAD_CANONICAL_BYTES,
    EventTimeStatus,
    ObservationEnvelopeV1,
    ObservationQualityFlag,
    ObservationSource,
    RejectedObservationV1,
    build_observation_envelope,
    build_rejected_observation,
    read_payload,
    recompute_observation_id,
    recompute_rejection_id,
)
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.domain.versioning import VersionedModel
from argos.errors import (
    ContractViolationError,
    NaiveDatetimeError,
    RejectionReason,
    SchemaVersionError,
)


class _Nested(VersionedModel):
    """Carries an arbitrarily deep tree, to exercise recursion limits."""

    schema_version: ClassVar[str] = "test_nested_depth.v1"

    tree: dict[str, Any]


class _Payload(VersionedModel):
    """A stand-in for the typed per-event-type payloads the adapter slices add."""

    schema_version: ClassVar[str] = "test_payload.v1"

    x: int = 0
    price: Decimal | None = None
    bids: tuple[Decimal, ...] = ()
    label: str = ""


START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
RAW_BYTES = b'{"bids": [{"price": "0.5", "size": "10"}]}'


def _at(hour: int, minute: int, second: int) -> datetime:
    return datetime(2026, 1, 1, hour, minute, second, tzinfo=UTC)


def _provenance(**overrides: Any) -> SourceProvenanceV1:
    fields: dict[str, Any] = {
        "source": "clob_rest",
        "endpoint": "/book",
        "http_status": 200,
        "retrieved_at": START,
        "raw_sha256": sha256_hex(RAW_BYTES),
        "byte_length": len(RAW_BYTES),
    }
    fields.update(overrides)
    return SourceProvenanceV1(**fields)


def _envelope(**overrides: Any) -> ObservationEnvelopeV1:
    provenance = overrides.pop("provenance", None) or _provenance()
    fields: dict[str, Any] = {
        "source": ObservationSource.CLOB_REST,
        "source_event_type": "book",
        "market_id": "market-1",
        "condition_id": None,
        "token_id": "123",
        "event_time": START,
        "received_time": START,
        "ingest_sequence": 1,
        "payload": _Payload(bids=(Decimal("0.5"),)),
        "provenance": provenance,
        "parser_version": "parser-1",
        "capture_run_id": "run-1",
    }
    fields.update(overrides)
    return build_observation_envelope(**fields)


def _rejection(**overrides: Any) -> RejectedObservationV1:
    provenance = overrides.pop("provenance", None) or _provenance(source="gamma")
    fields: dict[str, Any] = {
        "reason": RejectionReason.MALFORMED_PAYLOAD,
        "detail": "outcome_token_map length mismatch",
        "source": ObservationSource.GAMMA,
        "provenance": provenance,
        "received_time": START,
        "rejected_at": START,
        "capture_run_id": "run-1",
    }
    fields.update(overrides)
    return build_rejected_observation(**fields)


# --- identity: stability, change, and independence --------------------------------


def test_identity_is_stable_across_redelivery() -> None:
    """The M2 exit criterion depends on this: the same source event delivered
    twice, with different ingest_sequence/received_time/capture_run_id, must
    collide onto one observation_id."""
    first = _envelope(ingest_sequence=1, received_time=START, capture_run_id="run-1")
    second = _envelope(
        ingest_sequence=42,
        received_time=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
        capture_run_id="run-2",
    )
    assert first.observation_id == second.observation_id


def test_source_hash_and_source_sequence_both_discriminate() -> None:
    """They are combined with the content hash, not ranked above it, so each one
    still distinguishes two otherwise-identical messages."""
    with_hash_a = _envelope(source_hash="hash-a", payload=_Payload(x=1))
    with_hash_b = _envelope(source_hash="hash-b", payload=_Payload(x=1))
    assert with_hash_a.observation_id != with_hash_b.observation_id

    seq_1 = _envelope(source_sequence="1", payload=_Payload(x=1))
    seq_2 = _envelope(source_sequence="2", payload=_Payload(x=1))
    assert seq_1.observation_id != seq_2.observation_id

    assert with_hash_a.observation_id != seq_1.observation_id


def test_a_reverted_book_state_is_not_mistaken_for_a_duplicate() -> None:
    """Regression, reproduced against the first draft of `_observation_identity`.

    A book that moves A -> B -> A carries the source's own content hash for A on
    both the first and the third poll. While the source hash outranked payload
    content, the third observation collided with the first and the store would
    have refused it as a duplicate — erasing the real observation that the book
    was back at A at 12:00:09. Core invariant 14: that is a silent loss."""
    book_a = _Payload(bids=(Decimal("0.51"),))
    book_b = _Payload(bids=(Decimal("0.60"),))
    poll_1 = _envelope(source_hash="hash-a", payload=book_a, event_time=_at(12, 0, 0))
    poll_2 = _envelope(source_hash="hash-b", payload=book_b, event_time=_at(12, 0, 5))
    poll_3 = _envelope(source_hash="hash-a", payload=book_a, event_time=_at(12, 0, 9))
    ids = {poll_1.observation_id, poll_2.observation_id, poll_3.observation_id}
    assert len(ids) == 3, "a reverted state is a new observation, not a duplicate"


def test_a_source_sequence_reset_does_not_collide_two_different_events() -> None:
    """Regression, reproduced against the first draft of `_observation_identity`.

    A source sequence that restarts at 1 after a reconnect — which the M2 exit
    criteria explicitly anticipate — made two genuinely different events share
    one identity while the sequence outranked payload content, so one was
    silently dropped."""
    before = _envelope(source_sequence="1", payload=_Payload(label="A"), event_time=_at(12, 0, 0))
    after = _envelope(source_sequence="1", payload=_Payload(label="B"), event_time=_at(12, 0, 5))
    assert before.observation_id != after.observation_id


def test_an_identical_redelivery_still_collides_when_the_source_supplies_a_hash() -> None:
    """The control for the two regressions above: combining discriminators must
    not defeat the M2 criterion itself. Every stable field is identical here."""
    first = _envelope(source_hash="hash-a", payload=_Payload(x=1), ingest_sequence=1)
    second = _envelope(source_hash="hash-a", payload=_Payload(x=1), ingest_sequence=2)
    assert first.observation_id == second.observation_id


def test_identity_changes_when_rule_bearing_content_changes_via_content_hash() -> None:
    """No source_sequence/source_hash: the content hash must still distinguish
    genuinely different events."""
    base = _envelope(payload=_Payload(price=Decimal("0.5")))
    changed_payload = _envelope(payload=_Payload(price=Decimal("0.6")))
    changed_market = _envelope(payload=_Payload(price=Decimal("0.5")), market_id="market-2")
    changed_token = _envelope(payload=_Payload(price=Decimal("0.5")), token_id="456")
    changed_event_type = _envelope(
        payload=_Payload(price=Decimal("0.5")), source_event_type="price_change"
    )
    changed_time = _envelope(
        payload=_Payload(price=Decimal("0.5")), event_time=datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    )
    ids = {
        base.observation_id,
        changed_payload.observation_id,
        changed_market.observation_id,
        changed_token.observation_id,
        changed_event_type.observation_id,
        changed_time.observation_id,
    }
    assert len(ids) == 6


def test_identity_is_independent_of_ingest_sequence_and_received_time() -> None:
    a = _envelope(ingest_sequence=1, received_time=START)
    b = _envelope(ingest_sequence=2, received_time=datetime(2026, 6, 1, tzinfo=UTC))
    assert a.observation_id == b.observation_id


def test_identity_is_independent_of_capture_run_id() -> None:
    """A second capture session observing the same live event is the same
    event, not a new one; see the module docstring in observation.py."""
    a = _envelope(capture_run_id="run-a")
    b = _envelope(capture_run_id="run-b")
    assert a.observation_id == b.observation_id


def test_identity_distinguishes_missing_from_unparseable_event_time() -> None:
    missing = _envelope(event_time=None, payload=_Payload(x=1))
    unparseable = _envelope(
        event_time=None, event_time_raw="not-a-timestamp", payload=_Payload(x=1)
    )
    present = _envelope(event_time=START, payload=_Payload(x=1))
    ids = {missing.observation_id, unparseable.observation_id, present.observation_id}
    assert len(ids) == 3


def test_two_unparseable_event_times_with_different_raw_text_get_different_identity() -> None:
    a = _envelope(event_time=None, event_time_raw="garbage-1", payload=_Payload(x=1))
    b = _envelope(event_time=None, event_time_raw="garbage-2", payload=_Payload(x=1))
    assert a.observation_id != b.observation_id


def test_observation_id_is_deterministic_given_identical_inputs() -> None:
    a = _envelope()
    b = _envelope()
    assert a.observation_id == b.observation_id


# --- event_time: absence and invalidity are explicit, never substituted -----------


def test_missing_event_time_is_represented_not_substituted() -> None:
    envelope = _envelope(event_time=None)
    assert envelope.event_time_status is EventTimeStatus.MISSING
    assert envelope.event_time is None
    assert envelope.event_time_raw is None
    # received_time must never leak into event_time.
    assert envelope.received_time == START


def test_unparseable_event_time_keeps_the_raw_source_text() -> None:
    envelope = _envelope(event_time=None, event_time_raw="not-a-real-timestamp")
    assert envelope.event_time_status is EventTimeStatus.UNPARSEABLE
    assert envelope.event_time is None
    assert envelope.event_time_raw == "not-a-real-timestamp"


def test_present_status_requires_event_time_and_forbids_raw_text() -> None:
    with pytest.raises(ValidationError):
        ObservationEnvelopeV1(
            observation_id="observation-x",
            source=ObservationSource.CLOB_REST,
            source_event_type="book",
            event_time_status=EventTimeStatus.PRESENT,
            event_time=None,
            received_time=START,
            ingest_sequence=1,
            payload_schema_version="test_payload.v1",
            payload=_Payload().to_record(),
            raw_payload_sha256=sha256_hex(RAW_BYTES),
            provenance=_provenance(),
            parser_version="p1",
            capture_run_id="run-1",
        )


def test_missing_status_forbids_event_time_raw() -> None:
    with pytest.raises(ValidationError):
        ObservationEnvelopeV1(
            observation_id="observation-x",
            source=ObservationSource.CLOB_REST,
            source_event_type="book",
            event_time_status=EventTimeStatus.MISSING,
            event_time=None,
            event_time_raw="something",
            received_time=START,
            ingest_sequence=1,
            payload_schema_version="test_payload.v1",
            payload=_Payload().to_record(),
            raw_payload_sha256=sha256_hex(RAW_BYTES),
            provenance=_provenance(),
            parser_version="p1",
            capture_run_id="run-1",
        )


def test_unparseable_status_requires_raw_text() -> None:
    with pytest.raises(ValidationError):
        ObservationEnvelopeV1(
            observation_id="observation-x",
            source=ObservationSource.CLOB_REST,
            source_event_type="book",
            event_time_status=EventTimeStatus.UNPARSEABLE,
            event_time=None,
            event_time_raw=None,
            received_time=START,
            ingest_sequence=1,
            payload_schema_version="test_payload.v1",
            payload=_Payload().to_record(),
            raw_payload_sha256=sha256_hex(RAW_BYTES),
            provenance=_provenance(),
            parser_version="p1",
            capture_run_id="run-1",
        )


# --- naive / non-UTC datetimes -----------------------------------------------------


def test_naive_received_time_is_rejected() -> None:
    with pytest.raises(NaiveDatetimeError):
        _envelope(received_time=datetime(2026, 1, 1, 12, 0))


def test_naive_event_time_is_rejected() -> None:
    with pytest.raises(NaiveDatetimeError):
        _envelope(event_time=datetime(2026, 1, 1, 12, 0))


def test_a_naive_event_time_is_rejected_on_direct_model_construction_too() -> None:
    """The model itself must anchor the timestamp, not only the builder — a
    record read back from storage has to be as trustworthy as one just built."""
    with pytest.raises(NaiveDatetimeError):
        ObservationEnvelopeV1(
            observation_id="observation-x",
            source=ObservationSource.CLOB_REST,
            source_event_type="book",
            event_time_status=EventTimeStatus.PRESENT,
            event_time=datetime(2026, 1, 1, 12, 0),
            received_time=START,
            ingest_sequence=1,
            payload_schema_version="test_payload.v1",
            payload=_Payload().to_record(),
            raw_payload_sha256=sha256_hex(RAW_BYTES),
            provenance=_provenance(),
            parser_version="p1",
            capture_run_id="run-1",
        )


def test_non_utc_timestamps_are_anchored_to_utc() -> None:
    from datetime import timedelta, timezone

    offset = timezone(timedelta(hours=5))
    local = datetime(2026, 1, 1, 17, 0, tzinfo=offset)
    envelope = _envelope(event_time=local, received_time=local)
    assert envelope.event_time is not None
    assert envelope.event_time.tzinfo == UTC
    assert envelope.event_time == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


# --- ingest_sequence and raw hash consistency --------------------------------------


def test_ingest_sequence_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        _envelope(ingest_sequence=0)


def test_raw_payload_sha256_must_match_provenance() -> None:
    provenance = _provenance()
    with pytest.raises(ValidationError):
        ObservationEnvelopeV1(
            observation_id="observation-x",
            source=ObservationSource.CLOB_REST,
            source_event_type="book",
            event_time_status=EventTimeStatus.MISSING,
            received_time=START,
            ingest_sequence=1,
            payload_schema_version="test_payload.v1",
            payload=_Payload().to_record(),
            raw_payload_sha256="0" * 64,
            provenance=provenance,
            parser_version="p1",
            capture_run_id="run-1",
        )


# --- payload container immutability -------------------------------------------------


def test_the_caller_cannot_reach_the_stored_payload_after_construction() -> None:
    """The builder takes a frozen typed model and stores its canonical mapping,
    so there is no aliased container left for a caller to mutate. The weaker
    version of this guarantee — freezing a caller-supplied dict — was the M0
    architecture blocker's shape (`RunManifest.settings_snapshot`)."""
    payload = _Payload(bids=(Decimal("0.5"),))
    envelope = _envelope(payload=payload)
    with pytest.raises(ValidationError):
        payload.bids = (Decimal("999"),)  # type: ignore[misc]
    assert envelope.to_record()["payload"] == {"x": 0, "price": None, "bids": ["0.5"], "label": ""}


def test_payload_cannot_be_mutated_through_the_frozen_attribute() -> None:
    envelope = _envelope(payload=_Payload(x=1))
    with pytest.raises((TypeError, AttributeError)):
        envelope.payload["a"] = 2  # type: ignore[index]


# --- round trip and foreign schema version refusal ----------------------------------


def test_envelope_round_trips_through_a_record() -> None:
    envelope = _envelope()
    record = envelope.to_record()
    restored = ObservationEnvelopeV1.from_record(record)
    assert restored.observation_id == envelope.observation_id
    assert restored.to_record() == record


def test_a_replayed_envelope_equals_the_live_one_it_was_stored_from() -> None:
    """Regression for a reproduced invariant-5 break.

    While the envelope held live Python objects, `to_record()` dumped in JSON
    mode and `Decimal('0.5')` reloaded as `'0.5'`. The replayed envelope then
    differed from the live one *while carrying the same observation_id* — an
    identity asserting "same event" over two unequal objects. M3's "identical
    input + code + config produces identical output hash" runs through exactly
    this object, so the divergence would have surfaced as a replay hash
    mismatch with no obvious cause.

    Storing the canonical form makes the two identical by construction, and
    typed access restores the real types on the way out."""
    live = _envelope(payload=_Payload(price=Decimal("0.5"), bids=(Decimal("0.25"),)))
    replayed = ObservationEnvelopeV1.from_record(live.to_record())
    assert replayed == live
    assert replayed.payload == live.payload
    assert read_payload(replayed, _Payload) == read_payload(live, _Payload)
    assert read_payload(replayed, _Payload).price == Decimal("0.5")


def test_rejection_round_trips_through_a_record() -> None:
    rejection = _rejection()
    record = rejection.to_record()
    restored = RejectedObservationV1.from_record(record)
    assert restored.rejection_id == rejection.rejection_id
    assert restored.to_record() == record


def test_envelope_refuses_a_foreign_schema_version() -> None:
    record = _envelope().to_record()
    record["schema_version"] = "observation_envelope.v2"
    with pytest.raises(SchemaVersionError):
        ObservationEnvelopeV1.from_record(record)


def test_rejection_refuses_a_foreign_schema_version() -> None:
    record = _rejection().to_record()
    record["schema_version"] = "rejected_observation.v2"
    with pytest.raises(SchemaVersionError):
        RejectedObservationV1.from_record(record)


def test_envelope_refuses_unknown_fields() -> None:
    record = _envelope().to_record()
    record["unexpected_field"] = "surprise"
    with pytest.raises(ValidationError):
        ObservationEnvelopeV1.from_record(record)


# --- rejection ledger: reason, raw hash, untrusted text --------------------------


def test_rejection_carries_reason_and_raw_hash() -> None:
    rejection = _rejection(reason=RejectionReason.INVALID_TIMESTAMP)
    assert rejection.reason is RejectionReason.INVALID_TIMESTAMP
    assert rejection.raw_payload_sha256 == sha256_hex(RAW_BYTES)


def test_rejection_neutralizes_control_characters_in_detail() -> None:
    hostile = "line one\x1b]52;c;AAAA\x07 line two \x9bhidden"
    rejection = _rejection(detail=hostile)
    assert "\x1b" not in rejection.detail
    assert "\x9b" not in rejection.detail
    assert "\N{REPLACEMENT CHARACTER}" in rejection.detail


def test_rejection_neutralizes_bidirectional_overrides_in_detail() -> None:
    hostile = f"safe {chr(0x202E)}reversed{chr(0x202C)} safe"
    rejection = _rejection(detail=hostile)
    assert chr(0x202E) not in rejection.detail
    assert "\N{REPLACEMENT CHARACTER}" in rejection.detail


def test_rejection_detail_newlines_and_tabs_survive_sanitization() -> None:
    rejection = _rejection(detail="line one\nline two\tindented")
    assert rejection.detail == "line one\nline two\tindented"


def test_rejection_detail_is_bounded_in_length() -> None:
    rejection = _rejection(detail="x" * 10_000)
    assert len(rejection.detail) < 10_000
    assert "truncated" in rejection.detail


def test_rejection_holds_no_parsed_payload_field() -> None:
    assert "payload" not in RejectedObservationV1.model_fields


def test_rejection_raw_payload_sha256_must_match_provenance() -> None:
    with pytest.raises(ValidationError):
        RejectedObservationV1(
            rejection_id="rejection-x",
            reason=RejectionReason.MALFORMED_PAYLOAD,
            detail="bad",
            source=ObservationSource.GAMMA,
            raw_payload_sha256="0" * 64,
            provenance=_provenance(),
            received_time=START,
            rejected_at=START,
            capture_run_id="run-1",
        )


def test_rejection_identity_is_stable_across_redelivery() -> None:
    a = _rejection(capture_run_id="run-1")
    b = _rejection(capture_run_id="run-2")
    assert a.rejection_id == b.rejection_id


def test_rejection_identity_changes_with_reason() -> None:
    a = _rejection(reason=RejectionReason.MALFORMED_PAYLOAD)
    b = _rejection(reason=RejectionReason.INVALID_TIMESTAMP)
    assert a.rejection_id != b.rejection_id


# --- typed payload access -----------------------------------------------------------


class _OrderBookSnapshotStub(VersionedModel):
    schema_version: ClassVar[str] = "order_book_snapshot.v1"

    bids: list[dict[str, Any]]
    asks: list[dict[str, Any]]


def test_read_payload_validates_into_the_requested_model() -> None:
    envelope = _envelope(
        payload=_OrderBookSnapshotStub(bids=[{"price": "0.5", "size": "10"}], asks=[])
    )
    typed = read_payload(envelope, _OrderBookSnapshotStub)
    assert typed.bids == [{"price": "0.5", "size": "10"}]


def test_read_payload_refuses_a_mismatched_schema_version() -> None:
    envelope = _envelope(payload=_Payload())
    with pytest.raises(SchemaVersionError):
        read_payload(envelope, _OrderBookSnapshotStub)


def test_the_payload_label_cannot_disagree_with_the_payload() -> None:
    """The builder derives `payload_schema_version` from the payload model, so a
    caller cannot label one payload type as another. While the label was a free
    string, the mismatch stayed invisible until `read_payload` ran during
    *replay* — against a capture that can no longer be re-taken."""
    envelope = _envelope(payload=_OrderBookSnapshotStub(bids=[], asks=[]))
    assert envelope.payload_schema_version == "order_book_snapshot.v1"
    assert read_payload(envelope, _OrderBookSnapshotStub).bids == []


# --- property tests -----------------------------------------------------------------


@given(
    ingest_sequence_a=st.integers(min_value=1, max_value=10_000),
    ingest_sequence_b=st.integers(min_value=1, max_value=10_000),
)
def test_identity_never_depends_on_ingest_sequence(
    ingest_sequence_a: int, ingest_sequence_b: int
) -> None:
    a = _envelope(ingest_sequence=ingest_sequence_a)
    b = _envelope(ingest_sequence=ingest_sequence_b)
    assert a.observation_id == b.observation_id


@given(price=st.decimals(min_value="0", max_value="1", places=4, allow_nan=False))
def test_changing_the_payload_changes_the_content_hash_identity(price: Decimal) -> None:
    base = _envelope(payload=_Payload(price=Decimal("0.4242")))
    varied = _envelope(payload=_Payload(price=price))
    if price == Decimal("0.4242"):
        assert base.observation_id == varied.observation_id
    else:
        assert base.observation_id != varied.observation_id


@given(detail=st.text(min_size=1, max_size=200))
def test_rejection_detail_never_contains_a_c0_control_character_after_sanitization(
    detail: str,
) -> None:
    rejection = _rejection(detail=detail)
    assert all(character in "\n\t" or ord(character) >= 0x20 for character in rejection.detail)
    assert all(ord(character) < 0x7F or ord(character) > 0x9F for character in rejection.detail)


# --- identity encoding: injectivity and auditability --------------------------------


def test_a_separator_inside_a_source_field_cannot_forge_another_field() -> None:
    """Regression for a reproduced collision (architecture review, blocking).

    The identity material was joined on `\x1f`. Because `source_event_type`,
    `market_id`, `condition_id`, `token_id`, `source_sequence` and `source_hash`
    are all free-form source-controlled strings, a value carrying the separator
    could shift a field boundary and produce byte-identical material for two
    genuinely different observations — so an idempotent insert would drop one.
    Two defences now stand between a source and that collision, and both are
    checked here. The separator can no longer reach an identifier at all —
    security review found these fields stored verbatim, ESC and all, so they are
    refused outright rather than neutralized, because neutralizing is lossy and
    the value is inside the identity. And the encoding is length-prefixed, so
    even legal values cannot shift a field boundary."""
    with pytest.raises(ValidationError):
        _envelope(source_event_type="book\x1fmarket-1", market_id=None)
    with pytest.raises(ValidationError):
        _envelope(source_event_type="book", market_id="market-1\x1f")

    shifted = _envelope(source_event_type="bookmarket-1", market_id=None)
    honest = _envelope(source_event_type="book", market_id="market-1")
    assert shifted.observation_id != honest.observation_id


def test_an_absent_field_cannot_be_forged_by_a_sentinel_value() -> None:
    """`source_sequence="absent"` produced the identity of `source_sequence=None`
    while absence was encoded as the literal text `absent`."""
    absent = _envelope(source_sequence=None, source_hash=None)
    forged = _envelope(source_sequence="absent", source_hash=None)
    assert absent.observation_id != forged.observation_id


def test_the_stored_identity_can_be_recomputed_from_the_stored_record() -> None:
    """An identity nobody can recompute is an identity nobody can audit.

    The derivation runs on validated field values — sanitized, bounded, UTC
    anchored — so it must reproduce from what the record actually holds, not
    from what the builder was handed. While it ran on the builder's raw
    arguments, two envelopes storing byte-identical text carried different ids."""
    envelope = _envelope()
    assert recompute_observation_id(envelope) == envelope.observation_id
    restored = ObservationEnvelopeV1.from_record(envelope.to_record())
    assert recompute_observation_id(restored) == envelope.observation_id


def test_truncated_text_stays_distinguishable_and_recomputable() -> None:
    """Two payloads differing only past the truncation cap must not collapse.

    Security review reproduced exactly that: truncation is a lossy projection,
    identity is derived from the truncated value, so two different raw payloads
    shared one observation_id while their raw hashes differed — an idempotent
    store keeps one and the other's raw payload loses its observation, under
    source control. The bounding suffix now carries a digest of the full text,
    which restores injectivity without giving up recomputability."""
    a = _envelope(event_time=None, event_time_raw="x" * 600 + "A")
    b = _envelope(event_time=None, event_time_raw="x" * 600 + "B")
    assert a.event_time_raw != b.event_time_raw
    assert a.observation_id != b.observation_id
    assert recompute_observation_id(a) == a.observation_id


# --- clock skew quality flag --------------------------------------------------------


def test_an_event_time_far_ahead_of_receipt_is_flagged_but_still_accepted() -> None:
    """`docs/04_DATA_CONTRACTS.md`: an event time leading received_time beyond the
    configured tolerance must emit a quality flag. Accepting it is deliberate —
    a source clock running fast is a fact about the source — but an event-time
    window must be able to see that the timestamp was never corroborated."""
    envelope = _envelope(event_time=datetime(2999, 1, 1, tzinfo=UTC), received_time=START)
    assert envelope.event_time_status is EventTimeStatus.PRESENT
    assert ObservationQualityFlag.EVENT_TIME_AHEAD_OF_RECEIPT in envelope.quality_flags


def test_a_normal_observation_carries_no_quality_flags() -> None:
    """The guard must not be degenerate: an empty tuple has to mean "checked and
    clean", not "flagged everything"."""
    assert _envelope().quality_flags == ()


def test_small_clock_skew_within_tolerance_is_not_flagged() -> None:
    """Source and local clocks are never exactly aligned; flagging every
    sub-second lead would make the flag meaningless."""
    envelope = _envelope(event_time=_at(12, 0, 1), received_time=_at(12, 0, 0))
    assert envelope.quality_flags == ()


# --- provenance cross-checks ---------------------------------------------------------


def test_an_envelope_cannot_claim_a_different_source_than_its_provenance() -> None:
    """`provenance.source` also builds the raw archive path, so a mismatch points
    the envelope at the wrong archive directory and the raw payload it claims to
    link to is not where it says it is."""
    with pytest.raises(ValidationError):
        _envelope(source=ObservationSource.CLOB_MARKET_WS, provenance=_provenance(source="gamma"))


def test_provenance_allows_a_null_http_status_for_a_non_http_transport() -> None:
    """A WebSocket frame has no HTTP status. Requiring one would force the
    adapter to invent a value inside the contract whose job is provenance."""
    websocket = _provenance(source="clob_market_ws", http_status=None, endpoint="wss://example/ws")
    assert websocket.http_status is None


# --- security review regressions ----------------------------------------------------


def test_a_hostile_identifier_is_refused_from_an_accepted_observation() -> None:
    """Security review found `market_id`, `condition_id`, `token_id`,
    `source_sequence` and `source_hash` stored verbatim: ESC, OSC 52, RLO and
    newlines all survived, while `detail` beside them was sanitized. This is the
    M1 finding recurring — `compiler/audit.py` states in its own comment that
    `market_id` is validated for presence, not content, and that rendering it raw
    put a working clipboard write in the report title."""
    for field in ("market_id", "condition_id", "token_id", "source_sequence", "source_hash"):
        with pytest.raises(ValidationError):
            _envelope(**{field: "m\x1b]52;c;AAAA\x07"})
        with pytest.raises(ValidationError):
            _envelope(**{field: "m‮noitcerid"})


def test_an_unbounded_identifier_is_refused() -> None:
    """A 20,000,000-character `market_id` was accepted on a record whose `detail`
    was capped at 4,043 — the bound the module claimed applied to one field
    beside five that had none. A condition id is 66 characters; a uint256 token
    id is 78."""
    with pytest.raises(ValidationError):
        _envelope(market_id="x" * (MAX_IDENTIFIER_LENGTH + 1))
    assert _envelope(market_id="x" * MAX_IDENTIFIER_LENGTH).market_id is not None


def test_the_rejection_ledger_bounds_a_hostile_identifier_instead_of_refusing_it() -> None:
    """The opposite choice from the envelope's, deliberately. This record exists
    to hold input ARGOS could not validate, so refusing here would mean the
    rejection could not be written and the input would vanish with no ledger
    entry — the silent drop invariant 14 exists to prevent."""
    rejection = _rejection(market_id="m\x1b]52;c;AAAA\x07" + "x" * 10_000)
    assert rejection.market_id is not None
    assert "\x1b" not in rejection.market_id
    assert len(rejection.market_id) <= MAX_IDENTIFIER_LENGTH + 100
    assert recompute_rejection_id(rejection) == rejection.rejection_id


def test_an_oversized_payload_is_refused_inside_the_taxonomy() -> None:
    """Security review measured a 31.8 MiB payload — legal under the source
    client's own 32 MiB response cap — costing 2.84 s of CPU and 750 MiB of RSS
    to build one envelope, and showed the `Pacer` deadline cannot bound it
    because a cancel scope cannot interrupt synchronous CPU work. This is the M1
    gzip-bomb class relocated downstream of the byte cap that fixed it."""
    huge = _Payload(label="x" * (MAX_PAYLOAD_CANONICAL_BYTES + 1))
    with pytest.raises(ContractViolationError):
        _envelope(payload=huge)


def test_a_pathologically_nested_payload_fails_inside_the_taxonomy() -> None:
    """pydantic raises a bare `ValueError` past nesting depth 254 and freeze/thaw
    raise `RecursionError`; both are trivially reachable from hostile JSON and
    both escaped the taxonomy, so a capture loop would die on one instead of
    counting it and writing a ledger entry."""
    nested: Any = {"leaf": 1}
    for _ in range(600):
        nested = {"n": nested}
    with pytest.raises(ContractViolationError):
        _envelope(payload=_Nested(tree=nested))
