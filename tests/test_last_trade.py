"""Real-fixture tests for standalone CLOB last_trade_price evidence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import orjson
import pytest

from argos.clock import ReplayClock
from argos.domain.lasttrade import LastTradePriceV1, TradeSide, parse_last_trade_price
from argos.domain.observation import ObservationEnvelopeV1, RejectedObservationV1, read_payload
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.errors import RejectionReason
from argos.ingestion.capture import run_capture
from argos.ingestion.clob_book import MAX_NORMALIZABLE_BYTES
from argos.ingestion.clob_last_trade import normalize_clob_last_trade
from argos.projections.dispatch import DispatchOutcomeKind, ObservationDispatcher
from argos.sources.clob_ws import MarketFrame
from argos.store.event_store import Disposition, open_sqlite_event_store
from argos.store.raw_archive import read_raw_payload

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "clob"
RAW_PATH = FIXTURE_DIR / "ws_last_trade_price.raw.json"
META_PATH = FIXTURE_DIR / "ws_last_trade_price.meta.json"
EXPECTED_SHA256 = "9a6d43ed15d780707b4e47694337cd7ef4e4c4f4deb9e0d52a562810f9666601"
TOKEN = "72524354305074802179776970471253785890775894791734656584623668204695639445292"
OTHER_TOKEN = "83002214334081634686977291419563962458246456620357515715713481762414608708134"
CONDITION = "0x4b520df237d5ea575233cebef0c23895afb1f69b7436b4cf1d412e6ea4b88775"
RECEIVED = datetime(2026, 8, 19, 22, 20, 44, 59730, tzinfo=UTC)


def _raw() -> bytes:
    return RAW_PATH.read_bytes()


def _event() -> dict[str, Any]:
    value = orjson.loads(_raw())
    assert isinstance(value, dict)
    return value


def _provenance(**updates: Any) -> SourceProvenanceV1:
    record = orjson.loads(META_PATH.read_bytes())
    for fixture_metadata_key in (
        "note",
        "parser_version",
        "payload_kind",
        "redaction",
        "source_endpoint",
    ):
        record.pop(fixture_metadata_key)
    record.pop("schema_version")
    record.update(updates)
    return SourceProvenanceV1(**record)


def _normalize(
    event: Any | None = None,
    *,
    token: str = TOKEN,
    provenance: SourceProvenanceV1 | None = None,
) -> ObservationEnvelopeV1 | RejectedObservationV1 | None:
    return normalize_clob_last_trade(
        event=_event() if event is None else event,
        provenance=provenance or _provenance(),
        requested_token_id=token,
        received_time=RECEIVED,
        rejected_at=RECEIVED,
        ingest_sequence=1,
        capture_run_id="real-last-trade-fixture",
        raw_payload_location=f"clob_market_ws/{EXPECTED_SHA256}.raw.json",
    )


def test_fixture_is_the_exact_archived_real_payload() -> None:
    raw = _raw()
    assert len(raw) == 392
    assert sha256_hex(raw) == EXPECTED_SHA256
    assert _provenance().reconstructed is False


def test_real_fixture_parses_and_round_trips_as_a_versioned_contract() -> None:
    trade = parse_last_trade_price(_event())
    assert trade.condition_id == CONDITION
    assert trade.asset_id == TOKEN
    assert trade.price == Decimal("0.54")
    assert trade.size == Decimal("18.518517")
    assert trade.fee_rate_bps == 0
    assert trade.side is TradeSide.BUY
    assert trade.transaction_hash == (
        "0xfe173cc9e6bd81abd2df62e3ccd199930dbe4d3c4013d1336e6ec5e54a38d3a4"
    )
    assert LastTradePriceV1.from_record(trade.to_record()) == trade


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_type", "trade"),
        ("market", "not-a-condition"),
        ("asset_id", "not-a-token"),
        ("price", 0.54),
        ("size", "0"),
        ("fee_rate_bps", "-1"),
        ("side", "HOLD"),
        ("transaction_hash", "0x1234"),
    ],
)
def test_real_schema_refuses_malformed_fields(field: str, value: Any) -> None:
    event = _event()
    event[field] = value
    with pytest.raises(ValueError):
        parse_last_trade_price(event)


def test_real_schema_refuses_missing_unknown_and_non_object_shapes() -> None:
    missing = _event()
    missing.pop("size")
    with pytest.raises(ValueError, match="missing fields"):
        parse_last_trade_price(missing)

    expanded = _event()
    expanded["sequence"] = "invented"
    with pytest.raises(ValueError, match="unexpected top-level"):
        parse_last_trade_price(expanded)

    with pytest.raises(ValueError, match="JSON object"):
        parse_last_trade_price([])  # type: ignore[arg-type]


def test_normalizer_builds_a_typed_envelope_with_event_and_receipt_time_separate() -> None:
    result = _normalize()
    assert isinstance(result, ObservationEnvelopeV1)
    assert result.source_event_type == "last_trade_price"
    assert result.payload_schema_version == "last_trade_price.v1"
    assert result.event_time == datetime(2026, 8, 19, 22, 20, 44, 130000, tzinfo=UTC)
    assert result.received_time == RECEIVED
    assert result.raw_payload_sha256 == EXPECTED_SHA256
    assert read_payload(result, LastTradePriceV1).price == Decimal("0.54")


def test_normalizer_counts_a_sibling_as_not_applicable() -> None:
    assert _normalize(token=OTHER_TOKEN) is None


def test_normalizer_turns_malformed_and_oversized_input_into_evidence() -> None:
    malformed = _event()
    malformed["price"] = "2"
    rejected = _normalize(malformed)
    assert isinstance(rejected, RejectedObservationV1)
    assert rejected.reason is RejectionReason.MALFORMED_PAYLOAD
    assert rejected.condition_id == CONDITION

    oversized = _normalize(provenance=_provenance(byte_length=MAX_NORMALIZABLE_BYTES + 1))
    assert isinstance(oversized, RejectedObservationV1)
    assert "normalization budget" in oversized.detail

    non_object = _normalize([])
    assert isinstance(non_object, RejectedObservationV1)
    assert non_object.condition_id is None


def test_dispatcher_applies_trade_only_to_auxiliary_state_and_not_book_hash() -> None:
    envelope = _normalize()
    assert isinstance(envelope, ObservationEnvelopeV1)
    dispatcher = ObservationDispatcher()
    before = dispatcher.state_hash()
    outcome = dispatcher.dispatch(envelope)
    assert outcome.kind is DispatchOutcomeKind.APPLIED_AUXILIARY
    assert dispatcher.state_hash() == before
    assert dispatcher.projections == {}
    assert dispatcher.last_trades[(CONDITION, TOKEN)].price == Decimal("0.54")
    assert dispatcher.counts.applied_auxiliary == 1
    assert dispatcher.counts.as_record()["applied_auxiliary"] == 1

    duplicate = dispatcher.dispatch(envelope)
    assert duplicate.kind is DispatchOutcomeKind.SKIPPED_DUPLICATE
    assert dispatcher.counts.skipped_duplicates == 1


def test_dispatcher_refuses_to_apply_unscoped_trade_evidence() -> None:
    envelope = _normalize()
    assert isinstance(envelope, ObservationEnvelopeV1)
    unscoped = envelope.model_copy(update={"token_id": None})
    dispatcher = ObservationDispatcher()
    outcome = dispatcher.dispatch(unscoped)
    assert outcome.kind is DispatchOutcomeKind.UNSCOPED
    assert dispatcher.last_trades == {}


@dataclass
class _OneFrameSource:
    frame: MarketFrame

    async def frames(self) -> AsyncIterator[MarketFrame]:
        yield self.frame


async def test_capture_accepts_and_archives_the_real_frame_end_to_end(tmp_path: Path) -> None:
    raw = _raw()
    provenance = _provenance()
    frame = MarketFrame(text=raw.decode(), received_time=RECEIVED, provenance=provenance)
    store = open_sqlite_event_store(":memory:")
    dispatcher = ObservationDispatcher()
    health = await run_capture(
        frame_source=_OneFrameSource(frame),
        store=store,
        clock=ReplayClock(RECEIVED),
        capture_run_id="real-last-trade-capture",
        subscribed_token_ids=[TOKEN, OTHER_TOKEN],
        raw_archive_dir=tmp_path,
        dispatcher=dispatcher,
    )

    assert health.accepted == 1
    assert health.not_applicable == 1
    assert health.rejected == 0
    assert health.unknown_event_type == 0
    deliveries = list(store.iter_deliveries("real-last-trade-capture"))
    assert len(deliveries) == 1
    assert deliveries[0].disposition is Disposition.ACCEPTED_NEW
    envelope = store.get_observation(deliveries[0].observation_id)
    assert envelope is not None
    assert envelope.payload_schema_version == "last_trade_price.v1"
    assert read_raw_payload(tmp_path, EXPECTED_SHA256)[0] == raw
    assert dispatcher.projections == {}
    assert dispatcher.last_trades[(CONDITION, TOKEN)].price == Decimal("0.54")
