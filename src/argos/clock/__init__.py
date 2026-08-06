"""Clock protocol and implementations.

Core invariant 5: live and replay use the same domain handlers; only the clock
and the event source may differ. Domain code therefore never calls
``datetime.now()`` — it receives a :class:`Clock`.
"""

from argos.clock.base import Clock, LiveClock, ReplayClock, ensure_utc

__all__ = ["Clock", "LiveClock", "ReplayClock", "ensure_utc"]
