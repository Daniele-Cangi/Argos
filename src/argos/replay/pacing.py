"""Scheduler pacing for replay, which must never reach the output hash.

ADR-0009 split :class:`argos.clock.Clock` from
:class:`argos.clock.pacing.Pacer` and said outright where this belongs:
"Scheduler pacing for M3's accelerated and stepwise modes is a different
concern, will live in ``argos.replay``, and must never influence the output
hash." This module is that concern, kept separate from the live ``Pacer`` for
the same reason the two ports were separated in the first place — a live
adapter's retry backoff and a replay scheduler's playback speed are different
things that happen to both involve waiting.

Deliberately synchronous. A replay issues no request and awaits no peer; the
only reason to wait at all is to let a human watch a capture unfold at the
speed it originally arrived. Making the whole replay path async to express that
would buy nothing and would put a cancel scope around code whose determinism is
the entire product.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol, final

__all__ = ["RealTimePacer", "ReplayPacer", "VirtualPacer"]


class ReplayPacer(Protocol):
    """How long a replay pauses between arrivals."""

    def wait(self, seconds: float) -> None:
        """Pause for ``seconds`` of playback time, however this pacer defines it."""
        ...


@final
@dataclass(slots=True)
class VirtualPacer:
    """Records the waits it would have made, and makes none. The default.

    Every test uses this, which is what keeps the suite both fast and honest:
    a test that simply skipped pacing would prove nothing about whether the
    scheduler asked for the right pauses, while this one can be asserted
    against. `docs/13_TEST_STRATEGY.md` asks for "no flaky timing sleeps in
    deterministic tests" — a recording pacer is how that is satisfied without
    dropping the behaviour from the tests entirely.
    """

    waits: list[float] = field(default_factory=list)

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)

    @property
    def total_seconds(self) -> float:
        """How long this replay *would* have taken, had it paced for real."""
        return sum(self.waits)


@final
class RealTimePacer:
    """Actually sleeps, for a replay somebody is watching.

    Uses ``time.sleep`` rather than the injected :class:`argos.clock.Clock`,
    and that is correct rather than a shortcut: the clock a replay injects is a
    :class:`argos.clock.ReplayClock`, whose whole purpose is to move only when
    the scheduler says so. Sleeping "on" it would be a no-op, which is the
    category error ADR-0009 removed from the ``Clock`` protocol. Real elapsed
    time is not timekeeping.

    A negative or non-finite duration is refused by the caller before it gets
    here; this class deliberately holds no policy of its own.
    """

    def wait(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)
