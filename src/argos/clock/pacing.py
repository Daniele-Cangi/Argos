"""The :class:`Pacer` protocol: real elapsed time, kept apart from timekeeping.

ADR-0009 splits ``Clock`` (a pure reader of "what time is it") from ``Pacer``
(the two operations that only make sense against *real* elapsed time: waiting
and bounding a deadline). A retry backoff or a request deadline is an
interaction with a live network peer — it has no meaning in replay, where the
outcome of every attempt is already fixed by the capture. Pacing therefore
lives only where live adapters run (``argos.sources``, later ``argos.ingestion``)
and is never handed to, or satisfied by, a replay clock.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol, final

import anyio
from anyio import CancelScope

from argos.clock.base import _require_duration


class Pacer(Protocol):
    """Owns real elapsed time: waiting and bounding a deadline.

    Adapters take a ``Pacer`` alongside a ``Clock``. Only :class:`RealPacer`
    exists in live mode; there is no replay pacer, because replay issues no
    request and therefore has nothing to retry or bound (ADR-0009).
    """

    async def wait(self, seconds: float) -> None:
        """Wait ``seconds`` of real elapsed time."""
        ...

    def move_on_after(self, seconds: float) -> AbstractContextManager[CancelScope, bool]:
        """Return a context manager that cancels its body after ``seconds``.

        The ``bool`` exit type is load-bearing, not decoration: it tells the
        type checker this context manager may *swallow* the cancellation it
        raised. Annotated without it, the exit type narrows to ``None``, mypy
        concludes the body always propagates, and every ``if
        scope.cancelled_caught:`` deadline branch downstream is reported as
        unreachable — silently typing the timeout path as dead code.
        """
        ...


@final
class RealPacer:
    """Wall-clock pacing adapter used in live mode, delegating to ``anyio``."""

    async def wait(self, seconds: float) -> None:
        await anyio.sleep(_require_duration(seconds))

    def move_on_after(self, seconds: float) -> AbstractContextManager[CancelScope, bool]:
        return anyio.move_on_after(_require_duration(seconds))
