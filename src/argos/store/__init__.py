"""Event-store and ledger protocols plus local adapters.

``argos.store.event_store`` (ADR-0011) is the durable, idempotent, append-only
event store: the ``EventStore`` port, the ``SQLiteEventStore`` adapter behind
it, and the delivery/rejection/capture-run record shapes. ``raw_archive`` is
the separate, deliberately minimal content-addressed archive for raw source
bytes that the event store references by hash rather than absorbs (ADR-0011
section 4).
"""

from argos.store.event_store import (
    ARGOS_APPLICATION_ID,
    EVENT_STORE_SCHEMA_VERSION,
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
from argos.store.raw_archive import (
    archive_relative_location,
    read_raw_payload,
    write_raw_payload,
)

__all__ = [
    "ARGOS_APPLICATION_ID",
    "EVENT_STORE_SCHEMA_VERSION",
    "CaptureRunCounts",
    "CaptureRunRecord",
    "CompletionStatus",
    "DeliveryRecord",
    "Disposition",
    "EventStore",
    "RejectionRecord",
    "SQLiteEventStore",
    "archive_relative_location",
    "open_sqlite_event_store",
    "read_raw_payload",
    "write_raw_payload",
]
