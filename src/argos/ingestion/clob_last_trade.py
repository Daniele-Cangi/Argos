"""Normalize a real standalone CLOB last_trade_price event."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Final

from argos.domain.lasttrade import parse_last_trade_price
from argos.domain.observation import (
    DEFAULT_CLOCK_SKEW_TOLERANCE,
    ObservationEnvelopeV1,
    ObservationSource,
    RejectedObservationV1,
    build_observation_envelope,
    build_rejected_observation,
)
from argos.domain.provenance import SourceProvenanceV1
from argos.errors import RejectionReason
from argos.ingestion.clob_book import MAX_NORMALIZABLE_BYTES
from argos.ingestion.wire import parse_event_time

CLOB_WS_LAST_TRADE_EVENT_TYPE: Final = "last_trade_price"
CLOB_WS_LAST_TRADE_NORMALIZER_VERSION: Final = "clob-ws-last-trade-normalizer/1"


def normalize_clob_last_trade(
    *,
    event: Any,
    provenance: SourceProvenanceV1,
    requested_token_id: str,
    received_time: datetime,
    rejected_at: datetime,
    ingest_sequence: int,
    capture_run_id: str,
    raw_payload_location: str | None = None,
    clock_skew_tolerance: timedelta = DEFAULT_CLOCK_SKEW_TOLERANCE,
) -> ObservationEnvelopeV1 | RejectedObservationV1 | None:
    """Return an accepted envelope, a durable rejection, or a counted non-event."""
    if provenance.byte_length > MAX_NORMALIZABLE_BYTES:
        return build_rejected_observation(
            reason=RejectionReason.MALFORMED_PAYLOAD,
            detail=(
                f"event of {provenance.byte_length} bytes exceeds the "
                f"{MAX_NORMALIZABLE_BYTES}-byte normalization budget"
            ),
            source=ObservationSource.CLOB_MARKET_WS,
            source_event_type=CLOB_WS_LAST_TRADE_EVENT_TYPE,
            token_id=requested_token_id,
            provenance=provenance,
            received_time=received_time,
            rejected_at=rejected_at,
            capture_run_id=capture_run_id,
        )

    condition_id_hint = _best_effort_text(event, "market")
    try:
        trade = parse_last_trade_price(event)
        if trade.asset_id != requested_token_id:
            return None
    except ValueError as error:
        return build_rejected_observation(
            reason=RejectionReason.MALFORMED_PAYLOAD,
            detail=str(error),
            source=ObservationSource.CLOB_MARKET_WS,
            source_event_type=CLOB_WS_LAST_TRADE_EVENT_TYPE,
            condition_id=condition_id_hint,
            token_id=requested_token_id,
            provenance=provenance,
            received_time=received_time,
            rejected_at=rejected_at,
            capture_run_id=capture_run_id,
        )

    event_time, event_time_raw = parse_event_time(event.get("timestamp"))
    return build_observation_envelope(
        source=ObservationSource.CLOB_MARKET_WS,
        source_event_type=CLOB_WS_LAST_TRADE_EVENT_TYPE,
        condition_id=trade.condition_id,
        token_id=trade.asset_id,
        event_time=event_time,
        event_time_raw=event_time_raw,
        received_time=received_time,
        ingest_sequence=ingest_sequence,
        payload=trade,
        provenance=provenance,
        raw_payload_location=raw_payload_location,
        parser_version=CLOB_WS_LAST_TRADE_NORMALIZER_VERSION,
        capture_run_id=capture_run_id,
        clock_skew_tolerance=clock_skew_tolerance,
    )


def _best_effort_text(event: Any, key: str) -> str | None:
    if not isinstance(event, Mapping):
        return None
    value = event.get(key)
    return value if isinstance(value, str) and value else None
