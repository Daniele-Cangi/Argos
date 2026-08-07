"""Clock protocol and implementations.

Core invariant 5: live and replay use the same domain handlers; only the clock
and the event source may differ. Domain code therefore never calls
``datetime.now()`` — it receives a :class:`Clock`.

Real elapsed time — waiting and deadlines — is a distinct concern from
timekeeping and lives in :mod:`argos.clock.pacing` (ADR-0009). It is exported
here for convenience but is meant for live adapters, not domain code.
"""

from argos.clock.base import Clock, LiveClock, ReplayClock, ensure_utc
from argos.clock.pacing import Pacer, RealPacer

__all__ = ["Clock", "LiveClock", "Pacer", "RealPacer", "ReplayClock", "ensure_utc"]
