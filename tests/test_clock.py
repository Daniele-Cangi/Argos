from datetime import UTC, datetime, timedelta, timezone

import pytest

from argos.clock import Clock, LiveClock, ReplayClock, ensure_utc
from argos.errors import ClockRegressionError, NaiveDatetimeError

START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_both_clocks_satisfy_the_protocol() -> None:
    assert isinstance(LiveClock(), Clock)
    assert isinstance(ReplayClock(START), Clock)


def test_live_clock_returns_aware_utc() -> None:
    now = LiveClock().now()
    assert now.tzinfo is UTC


async def test_live_clock_rejects_negative_sleep() -> None:
    with pytest.raises(ValueError):
        await LiveClock().sleep(-0.1)


def test_ensure_utc_converts_offset_timestamps() -> None:
    stockholm = datetime(2026, 1, 1, 13, 0, tzinfo=timezone(timedelta(hours=1)))
    assert ensure_utc(stockholm) == START


def test_ensure_utc_rejects_naive_timestamps() -> None:
    with pytest.raises(NaiveDatetimeError) as caught:
        ensure_utc(datetime(2026, 1, 1, 12, 0))
    assert caught.value.code == "argos.naive_datetime"


def test_replay_clock_does_not_move_on_its_own() -> None:
    clock = ReplayClock(START)
    assert clock.now() == START
    assert clock.now() == START


def test_replay_clock_advances_only_when_told() -> None:
    clock = ReplayClock(START)
    clock.advance_by(30)
    assert clock.now() == START + timedelta(seconds=30)
    clock.advance_to(START + timedelta(minutes=5))
    assert clock.now() == START + timedelta(minutes=5)


def test_replay_clock_allows_repeated_event_time() -> None:
    clock = ReplayClock(START)
    clock.advance_to(START)
    assert clock.now() == START


def test_replay_clock_rejects_backwards_moves() -> None:
    clock = ReplayClock(START)
    clock.advance_by(10)
    with pytest.raises(ClockRegressionError):
        clock.advance_to(START)
    with pytest.raises(ClockRegressionError):
        clock.advance_by(-1)


def test_replay_clock_rejects_naive_start() -> None:
    with pytest.raises(NaiveDatetimeError):
        ReplayClock(datetime(2026, 1, 1, 12, 0))


async def test_replay_sleep_advances_virtual_time_without_waiting() -> None:
    clock = ReplayClock(START)
    await clock.sleep(3600)
    assert clock.now() == START + timedelta(hours=1)


def test_replay_clock_is_reproducible() -> None:
    def run() -> list[datetime]:
        clock = ReplayClock(START)
        stamps = []
        for step in (1, 2, 3):
            clock.advance_by(step)
            stamps.append(clock.now())
        return stamps

    assert run() == run()
