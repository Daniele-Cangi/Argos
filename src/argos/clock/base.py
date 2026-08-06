"""The :class:`Clock` protocol, a wall-clock adapter, and a virtual replay clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, final, runtime_checkable

import anyio

from argos.errors import ClockRegressionError, NaiveDatetimeError


def ensure_utc(moment: datetime) -> datetime:
    """Return ``moment`` in UTC, refusing naive timestamps.

    A naive timestamp is ambiguous evidence; silently assuming UTC would corrupt
    event-time reasoning, so it is rejected instead.
    """
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise NaiveDatetimeError(
            "timestamps must carry an explicit timezone", value=moment.isoformat()
        )
    return moment.astimezone(UTC)


@runtime_checkable
class Clock(Protocol):
    """The only source of time available to application and domain code."""

    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait ``seconds`` of this clock's time."""
        ...


@final
class LiveClock:
    """Wall-clock adapter used in live mode."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep duration must be non-negative")
        await anyio.sleep(seconds)


@final
class ReplayClock:
    """Virtual clock advanced only by its owner — in M3, the replay scheduler.

    Time never moves on its own and never moves backwards, so a replay run
    produces the same timestamps on every execution.
    """

    def __init__(self, start: datetime) -> None:
        self._now = ensure_utc(start)

    def now(self) -> datetime:
        return self._now

    def advance_to(self, moment: datetime) -> None:
        """Move the clock forward to ``moment``.

        Moving to the same instant is allowed: several observations may share an
        event time. Moving backwards is a determinism bug and raises.
        """
        target = ensure_utc(moment)
        if target < self._now:
            raise ClockRegressionError(
                "replay clock cannot move backwards",
                current=self._now.isoformat(),
                requested=target.isoformat(),
            )
        self._now = target

    def advance_by(self, seconds: float) -> None:
        """Move the clock forward by ``seconds``."""
        if seconds < 0:
            raise ClockRegressionError(
                "replay clock cannot advance by a negative duration", seconds=seconds
            )
        self._now = self._now + timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        """Advance virtual time without waiting in real time."""
        if seconds < 0:
            raise ValueError("sleep duration must be non-negative")
        self.advance_by(seconds)
