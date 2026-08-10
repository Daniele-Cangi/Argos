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
    read_payload,
    recompute_observation_id,
)
from argos.domain.orderbook import (
    BookSide,
    OrderBookAnomaly,
    OrderBookAnomalyKind,
    OrderBookLevel,
    OrderBookSnapshotV1,
    parse_order_book_snapshot,
)
from argos.domain.text import neutralize_and_bound, neutralize_untrusted_text
from argos.domain.versioning import VersionedModel, ensure_supported_version, freeze, thaw

__all__ = [
    "BookSide",
    "EventTimeStatus",
    "ObservationEnvelopeV1",
    "ObservationQualityFlag",
    "ObservationSource",
    "OrderBookAnomaly",
    "OrderBookAnomalyKind",
    "OrderBookLevel",
    "OrderBookSnapshotV1",
    "RejectedObservationV1",
    "VersionedModel",
    "build_observation_envelope",
    "build_rejected_observation",
    "ensure_supported_version",
    "freeze",
    "neutralize_and_bound",
    "neutralize_untrusted_text",
    "parse_order_book_snapshot",
    "read_payload",
    "recompute_observation_id",
    "thaw",
]
