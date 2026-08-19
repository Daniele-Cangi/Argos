"""The replay scheduler: the only thing that moves a :class:`ReplayClock`.

ADR-0003's deliverable is "``ReplayClock`` controlled only by the replay
scheduler". ADR-0009 made that a type-level fact by removing every mutator from
the ``Clock`` protocol; this module is the owner that remains.

What it does per arrival, in order:

1. pace, if the mode asks for it (never touching the clock, never the hash);
2. advance the replay clock to the arrival's ``received_time``;
3. hand accepted and duplicate arrivals to the **shared**
   :class:`~argos.projections.dispatch.ObservationDispatcher` — the same object
   a live capture drives (ADR-0012 section 8) — and count rejections.

The clock is advanced to ``received_time``, not ``event_time``. A replay
answers "what did ARGOS know, and when did it know it"; advancing to event time
would hand a replayed forecast information at the moment the market moved
rather than at the moment ARGOS learned of it, which is the leakage ADR-0003
exists to prevent.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum

from argos.clock import ReplayClock
from argos.errors import RejectionReason
from argos.projections.dispatch import DispatchOutcome, ObservationDispatcher
from argos.replay.pacing import ReplayPacer, VirtualPacer
from argos.replay.reader import ArrivalKind, ReplayArrival

__all__ = ["ReplayCounts", "ReplayMode", "ReplaySession", "StepResult"]


class ReplayMode(StrEnum):
    """How fast the scheduler walks the capture.

    Pacing only. All three drive the identical :meth:`ReplaySession.step`, and
    ADR-0009 requires that none of them can reach the output hash — a property
    this repository tests directly rather than asserts.
    """

    ACCELERATED = "accelerated"
    """As fast as the machine allows. The default, and what every test uses."""

    ORIGINAL_ARRIVAL = "original_arrival"
    """Paced to the real gaps between ``received_time`` values, for a replay
    somebody is watching."""

    STEPWISE = "stepwise"
    """One arrival per :meth:`ReplaySession.step` call, with the caller
    deciding when the next one happens. Named as a deliverable by
    ``docs/07_MILESTONES.md`` in the same breath as accelerated; recorded as an
    amendment to ``docs/04_DATA_CONTRACTS.md``, which lists only two modes."""


@dataclass(frozen=True, slots=True)
class ReplayCounts:
    """Arrival-level counters, distinct from the dispatcher's outcome counters.

    Two levels, deliberately not merged: these count what the *capture*
    recorded (how many arrivals, of which kinds), while
    :class:`~argos.projections.dispatch.DispatchCounts` counts what the
    *dispatcher* did with them. A single flat set would make "the capture
    rejected 4 events" and "the dispatcher had no handler for 4 payloads"
    indistinguishable, and those are different problems with different fixes.
    """

    arrivals: int = 0
    accepted: int = 0
    duplicate: int = 0
    rejected: int = 0
    sequence_gaps: int = 0
    """Missing ``ingest_sequence`` values between consecutive arrivals.

    The capture loop allocates a sequence only when a record is actually
    written ("peek, then commit"), so a healthy capture is contiguous and this
    is zero. A gap therefore means a write was lost — a crashed process between
    allocation and insert, or a foreign writer — and it is counted rather than
    ignored because a replay of an incomplete capture must not look identical to
    a replay of a complete one.
    """

    received_time_regressions: int = 0
    """Arrivals whose ``received_time`` was earlier than the one before.

    Reachable on real data: ``received_time`` comes from ``LiveClock.now()``,
    and a system clock can step backwards under NTP. The clock is left where it
    is and the replay continues (ADR-0012 section 5) — killing a replay of an
    already-recorded capture is not a policy, it is a crash.
    """

    rejection_reasons: tuple[tuple[str, int], ...] = ()
    """Rejected arrivals tallied by reason, sorted, so a summary names *which*
    kind of invalid input a capture contained rather than only how much."""

    def as_record(self) -> dict[str, object]:
        return {
            "arrivals": self.arrivals,
            "accepted": self.accepted,
            "duplicate": self.duplicate,
            "rejected": self.rejected,
            "sequence_gaps": self.sequence_gaps,
            "received_time_regressions": self.received_time_regressions,
            "rejection_reasons": dict(self.rejection_reasons),
        }


@dataclass(frozen=True, slots=True)
class StepResult:
    """What one arrival did, for a caller stepping through a replay by hand."""

    arrival: ReplayArrival
    outcome: DispatchOutcome | None
    """``None`` for a rejected arrival: it reaches no projection by design."""


@dataclass(slots=True)
class ReplaySession:
    """One replay in progress. Not reusable: a session is a run.

    Constructed with everything it needs and nothing it does not — the arrival
    stream, the dispatcher, the clock it owns, and a pacer. There is no event
    store here on purpose: reading is
    :func:`argos.replay.reader.read_capture_arrivals`'s job, and a scheduler
    that could re-read its own input would be able to produce a different run
    from the same arguments.
    """

    arrivals: Iterator[ReplayArrival]
    dispatcher: ObservationDispatcher
    clock: ReplayClock
    mode: ReplayMode = ReplayMode.ACCELERATED
    pacer: ReplayPacer = field(default_factory=VirtualPacer)

    _counts: ReplayCounts = field(default_factory=ReplayCounts, init=False, repr=False)
    _reasons: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _last_sequence: int | None = field(default=None, init=False, repr=False)
    _last_received: datetime | None = field(default=None, init=False, repr=False)

    _first_sequence: int | None = field(default=None, init=False, repr=False)

    @property
    def counts(self) -> ReplayCounts:
        return replace(self._counts, rejection_reasons=tuple(sorted(self._reasons.items())))

    @property
    def first_sequence(self) -> int | None:
        """The first ``ingest_sequence`` this session replayed, or ``None``.

        Recorded because the manifest states the *span* of input a run covered:
        an empty capture and a capture whose first record is sequence 5 are
        different facts, and neither should read as "started at the beginning".
        """
        return self._first_sequence

    @property
    def last_sequence(self) -> int | None:
        return self._last_sequence

    def step(self) -> StepResult | None:
        """Replay exactly one arrival, or return ``None`` when the capture ends."""
        arrival = next(self.arrivals, None)
        if arrival is None:
            return None

        self._pace(arrival)
        self._advance_clock(arrival)
        self._note_sequence(arrival)

        self._bump(arrivals=1)
        if arrival.kind is ArrivalKind.REJECTED:
            assert arrival.rejection is not None
            self._bump(rejected=1)
            reason = arrival.rejection.reason
            self._reasons[_reason_key(reason)] = self._reasons.get(_reason_key(reason), 0) + 1
            return StepResult(arrival=arrival, outcome=None)

        assert arrival.envelope is not None
        if arrival.kind is ArrivalKind.DUPLICATE:
            self._bump(duplicate=1)
        else:
            self._bump(accepted=1)
        # Both kinds are handed to the dispatcher, duplicates included: refusing
        # to re-apply one is the dispatcher's guarantee (ADR-0012 section 3), and
        # filtering here would move that decision into a caller, where a live
        # capture would then need its own copy of it.
        outcome = self.dispatcher.dispatch(arrival.envelope)
        return StepResult(arrival=arrival, outcome=outcome)

    def run(self) -> ReplayCounts:
        """Replay every remaining arrival.

        ``STEPWISE`` is a caller-driven mode, so running one to completion here
        is legal and simply means the caller chose not to step: the mode changes
        who decides when the next arrival happens, never what happens to it.
        """
        while self.step() is not None:
            pass
        return self.counts

    def _pace(self, arrival: ReplayArrival) -> None:
        if self.mode is not ReplayMode.ORIGINAL_ARRIVAL:
            return
        if self._last_received is None:
            return
        gap = (arrival.received_time - self._last_received).total_seconds()
        if gap > 0:
            self.pacer.wait(gap)

    def _advance_clock(self, arrival: ReplayArrival) -> None:
        """Move the replay clock to this arrival, never backwards.

        ``ReplayClock.advance_to`` raises on a backwards move, and a stored
        ``received_time`` can genuinely regress. Counting it and leaving the
        clock where it is keeps the failure visible (core invariant 14) without
        making a recorded capture unreplayable.
        """
        previous = self._last_received
        if previous is not None and arrival.received_time < previous:
            self._bump(received_time_regressions=1)
            return
        self.clock.advance_to(arrival.received_time)
        self._last_received = arrival.received_time

    def _note_sequence(self, arrival: ReplayArrival) -> None:
        if self._first_sequence is None:
            self._first_sequence = arrival.ingest_sequence
        previous = self._last_sequence
        if previous is not None and arrival.ingest_sequence > previous + 1:
            self._bump(sequence_gaps=arrival.ingest_sequence - previous - 1)
        self._last_sequence = arrival.ingest_sequence

    def _bump(self, **deltas: int) -> None:
        self._counts = replace(
            self._counts,
            **{name: getattr(self._counts, name) + value for name, value in deltas.items()},
        )


def _reason_key(reason: RejectionReason) -> str:
    return reason.value
