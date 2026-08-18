"""Deterministic state built from observations (M2-M3).

``book`` is the per-token order-book state machine; ``dispatch`` is the object
a live capture and a replay both drive, which is what makes core invariant 5
("live and replay use the same domain handlers") a fact about one object rather
than a claim about two paths (ADR-0012 section 8).
"""

from argos.projections.book import (
    BOOK_STATE_DIGEST_VERSION,
    BookProjectionAnomaly,
    BookProjectionAnomalyKind,
    BookState,
    OrderBookProjection,
)
from argos.projections.dispatch import (
    STATE_HASH_VERSION,
    DispatchCounts,
    DispatchOutcome,
    DispatchOutcomeKind,
    Lateness,
    ObservationDispatcher,
    Watermark,
)

__all__ = [
    "BOOK_STATE_DIGEST_VERSION",
    "STATE_HASH_VERSION",
    "BookProjectionAnomaly",
    "BookProjectionAnomalyKind",
    "BookState",
    "DispatchCounts",
    "DispatchOutcome",
    "DispatchOutcomeKind",
    "Lateness",
    "ObservationDispatcher",
    "OrderBookProjection",
    "Watermark",
]
