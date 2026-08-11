"""Event-store and ledger protocols plus local adapters.

``argos.store.event_store`` (ADR-0011) is the durable, idempotent, append-only
event store: the ``EventStore`` port, the ``SQLiteEventStore`` adapter behind
it, and the delivery/rejection/capture-run record shapes. ``raw_archive`` is
the separate, deliberately minimal content-addressed archive for raw source
bytes that the event store references by hash rather than absorbs (ADR-0011
section 4).
"""

from argos.store.event_store import (
    CaptureRunCounts,
    CaptureRunRecord,
    CompletionStatus,
    DeliveryRecord,
    Disposition,
    EventStore,
    RejectionRecord,
    SQLiteEventStore,
    open_sqlite_event_store,
)
from argos.store.raw_archive import read_raw_payload, write_raw_payload

__all__ = [
    "CaptureRunCounts",
    "CaptureRunRecord",
    "CompletionStatus",
    "DeliveryRecord",
    "Disposition",
    "EventStore",
    "RejectionRecord",
    "SQLiteEventStore",
    "open_sqlite_event_store",
    "read_raw_payload",
    "write_raw_payload",
]
