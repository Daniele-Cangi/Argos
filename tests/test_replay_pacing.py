"""Replay scheduler pacing (ADR-0009's `argos.replay` half, ADR-0012 section 7).

Written because the branch-coverage gate caught this module at 78% against its
90% floor, and because the backlog entry claiming it was "deliberately
untested -- waiting out real gaps is the flaky timing test docs/13 forbids" was
an overstatement. Waiting out a capture's real inter-arrival gaps would indeed
be flaky. Asserting that `wait(0)` does not sleep, and that a millisecond wait
returns, is neither: nothing here asserts *how long* anything took.
"""

from __future__ import annotations

import time

from argos.replay.pacing import RealTimePacer, ReplayPacer, VirtualPacer


def test_a_virtual_pacer_records_what_it_would_have_waited() -> None:
    """The point of the default pacer: a test that simply skipped pacing would
    prove nothing about whether the scheduler asked for the right pauses."""
    pacer = VirtualPacer()
    pacer.wait(0.25)
    pacer.wait(1.5)
    assert pacer.waits == [0.25, 1.5]
    assert pacer.total_seconds == 1.75


def test_a_virtual_pacer_starts_empty_and_totals_zero() -> None:
    """An accelerated replay makes no waits at all, and "no waits" must be a
    value rather than an absence a caller has to special-case."""
    pacer = VirtualPacer()
    assert pacer.waits == []
    assert pacer.total_seconds == 0


def test_two_virtual_pacers_do_not_share_their_recorded_waits() -> None:
    """A mutable default would make one replay's pacing show up in the next
    one's -- the hidden shared state CLAUDE.md prohibits, in miniature."""
    first, second = VirtualPacer(), VirtualPacer()
    first.wait(1.0)
    assert second.waits == []


def test_a_real_pacer_does_not_sleep_for_a_zero_or_negative_wait() -> None:
    """`ReplaySession` computes gaps by subtracting timestamps, so a zero gap is
    ordinary rather than exceptional. Asserted as "returns promptly", with a
    bound loose enough (100 ms) that only an actual `sleep` of the requested
    duration could break it."""
    pacer = RealTimePacer()
    started = time.perf_counter()
    pacer.wait(0)
    pacer.wait(-5)
    assert time.perf_counter() - started < 0.1


def test_a_real_pacer_actually_waits_when_asked() -> None:
    """The one behaviour it has. A millisecond, so the assertion is about *that
    it slept* rather than about how accurately -- an accuracy assertion is what
    would make this flaky."""
    pacer = RealTimePacer()
    started = time.perf_counter()
    pacer.wait(0.001)
    assert time.perf_counter() - started >= 0.0005


def test_both_pacers_satisfy_the_port() -> None:
    """`ReplayPacer` is a plain `Protocol`, so nothing checks this at runtime
    unless something asks. A replay handed an object with the wrong shape would
    otherwise fail deep inside a scheduler loop."""
    for pacer in (VirtualPacer(), RealTimePacer()):
        accepted: ReplayPacer = pacer
        accepted.wait(0)
