"""Versioned domain contracts and invariants.

Domain code is pure. It must not import HTTP clients, database implementations,
Typer, environment variables, or wall-clock functions; time enters through the
injected :mod:`argos.clock` protocol. ``tests/test_boundaries.py`` enforces this
mechanically.
"""

from argos.domain.observation import (
    EventTimeStatus,
    ObservationEnvelopeV1,
    ObservationQualityFlag,
    ObservationSource,
    RejectedObservationV1,
    build_observation_envelope,
    build_rejected_observation,
    read_declared_payload,
    read_payload,
    recompute_observation_id,
)
from argos.domain.orderbook import (
    BookSide,
    OrderBookAnomaly,
    OrderBookAnomalyKind,
    OrderBookLevel,
    OrderBookSnapshotV1,
    normalize_decimal,
    parse_order_book_snapshot,
    parse_wire_decimal,
)
from argos.domain.pricechange import (
    NoEntriesForToken,
    PriceChangeGroup,
    PriceChangeV1,
    PriceLevelChangeKind,
    PriceLevelChangeV1,
    parse_price_change_group,
)
from argos.domain.text import neutralize_and_bound, neutralize_untrusted_text
from argos.domain.versioning import (
    VersionedModel,
    ensure_supported_version,
    freeze,
    registered_schemas,
    resolve_schema,
    thaw,
)
from argos.domain.wsbook import WsBookSnapshotV1, parse_ws_book_snapshot

__all__ = [
    "BookSide",
    "EventTimeStatus",
    "NoEntriesForToken",
    "ObservationEnvelopeV1",
    "ObservationQualityFlag",
    "ObservationSource",
    "OrderBookAnomaly",
    "OrderBookAnomalyKind",
    "OrderBookLevel",
    "OrderBookSnapshotV1",
    "PriceChangeGroup",
    "PriceChangeV1",
    "PriceLevelChangeKind",
    "PriceLevelChangeV1",
    "RejectedObservationV1",
    "VersionedModel",
    "WsBookSnapshotV1",
    "build_observation_envelope",
    "build_rejected_observation",
    "ensure_supported_version",
    "freeze",
    "neutralize_and_bound",
    "neutralize_untrusted_text",
    "normalize_decimal",
    "parse_order_book_snapshot",
    "parse_price_change_group",
    "parse_wire_decimal",
    "parse_ws_book_snapshot",
    "read_declared_payload",
    "read_payload",
    "recompute_observation_id",
    "registered_schemas",
    "resolve_schema",
    "thaw",
]
