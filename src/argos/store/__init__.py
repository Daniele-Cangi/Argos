"""Event-store and ledger protocols plus local adapters.

The durable, replayable event store is an M2 deliverable. What exists today is
the append-only raw payload archive that market discovery writes to.
"""

from argos.store.raw_archive import read_raw_payload, write_raw_payload

__all__ = ["read_raw_payload", "write_raw_payload"]
