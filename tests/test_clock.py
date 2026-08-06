from datetime import UTC, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

import pytest

from argos.clock import Clock, LiveClock, ReplayClock, ensure_utc
from argos.errors import ClockRegressionError, InvalidDurationError, NaiveDatetimeError

START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
NEW_YORK = ZoneInfo("America/New_York")


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
    """An earlier instant is a regression; a negative duration is an invalid
    argument, since the clock never moved at all."""
    clock = ReplayClock(START)
    clock.advance_by(10)
    with pytest.raises(ClockRegressionError):
        clock.advance_to(START)
    with pytest.raises(InvalidDurationError):
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


# --- timezone normalization ------------------------------------------------------


@pytest.mark.parametrize(
    ("local", "expected"),
    [
        # standard time: New York is UTC-5
        (datetime(2026, 1, 15, 7, 0, tzinfo=NEW_YORK), datetime(2026, 1, 15, 12, 0, tzinfo=UTC)),
        # daylight saving time: the same wall clock is UTC-4
        (datetime(2026, 7, 15, 8, 0, tzinfo=NEW_YORK), datetime(2026, 7, 15, 12, 0, tzinfo=UTC)),
    ],
)
def test_ensure_utc_applies_the_offset_in_force_at_that_moment(
    local: datetime, expected: datetime
) -> None:
    """A fixed-offset conversion would be wrong on one side of the DST boundary."""
    assert ensure_utc(local) == expected


def test_ensure_utc_respects_the_fold_of_an_ambiguous_local_time() -> None:
    """01:30 happens twice on the US fall-back date; the two are distinct instants."""
    ambiguous = datetime(2026, 11, 1, 1, 30, tzinfo=NEW_YORK)
    first_pass = ensure_utc(ambiguous.replace(fold=0))
    second_pass = ensure_utc(ambiguous.replace(fold=1))
    assert first_pass == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert second_pass == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    assert second_pass - first_pass == timedelta(hours=1)


def test_ensure_utc_is_idempotent_and_preserves_sub_second_precision() -> None:
    moment = datetime(2026, 1, 1, 12, 0, 0, 123456, tzinfo=timezone(timedelta(hours=-3)))
    once = ensure_utc(moment)
    assert once.microsecond == 123456
    assert ensure_utc(once) == once
    assert ensure_utc(once).tzinfo is UTC


def test_ensure_utc_rejects_a_timezone_that_declares_no_offset() -> None:
    """A tzinfo present but returning None is as ambiguous as a naive timestamp."""

    class OffsetlessZone(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta | None:
            return None

        def dst(self, dt: datetime | None) -> timedelta | None:
            return None

        def tzname(self, dt: datetime | None) -> str | None:
            return "OFFSETLESS"

    with pytest.raises(NaiveDatetimeError):
        ensure_utc(datetime(2026, 1, 1, 12, 0, tzinfo=OffsetlessZone()))


# --- advance semantics -----------------------------------------------------------


def test_advance_to_compares_instants_not_wall_clock_readings() -> None:
    """13:00+01:00 is *before* 12:30 UTC, so it must be refused as a regression."""
    clock = ReplayClock(datetime(2026, 1, 1, 12, 30, tzinfo=UTC))
    earlier_instant = datetime(2026, 1, 1, 13, 0, tzinfo=timezone(timedelta(hours=1)))
    with pytest.raises(ClockRegressionError):
        clock.advance_to(earlier_instant)

    later_instant = datetime(2026, 1, 1, 14, 0, tzinfo=timezone(timedelta(hours=1)))
    clock.advance_to(later_instant)
    assert clock.now() == datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    assert clock.now().tzinfo is UTC


def test_repeated_advance_to_the_same_instant_is_idempotent() -> None:
    clock = ReplayClock(START)
    for _ in range(5):
        clock.advance_to(START + timedelta(seconds=1))
    assert clock.now() == START + timedelta(seconds=1)


def test_advance_by_zero_is_allowed_and_does_not_move_time() -> None:
    clock = ReplayClock(START)
    clock.advance_by(0)
    assert clock.now() == START


def test_advance_by_does_not_accumulate_floating_point_drift() -> None:
    """Ten tenths of a second must land exactly on one second, not 0.9999999s."""
    clock = ReplayClock(START)
    for _ in range(10):
        clock.advance_by(0.1)
    assert clock.now() == START + timedelta(seconds=1)

    fine = ReplayClock(START)
    for _ in range(1000):
        fine.advance_by(0.001)
    assert fine.now() == START + timedelta(seconds=1)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -0.5])
def test_a_rejected_advance_never_leaves_the_clock_in_a_half_moved_state(bad: float) -> None:
    """Whatever the failure mode, an unusable duration must not corrupt replay time."""
    clock = ReplayClock(START)
    with pytest.raises((ClockRegressionError, ValueError, OverflowError)):
        clock.advance_by(bad)
    assert clock.now() == START


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_duration_is_refused_with_a_countable_code(bad: float) -> None:
    """nan slips past a `seconds < 0` guard: it would hang anyio.sleep under a live
    clock and raise an untyped ValueError from timedelta under a replay clock."""
    with pytest.raises(InvalidDurationError) as caught:
        ReplayClock(START).advance_by(bad)
    assert caught.value.code == "argos.invalid_duration"


async def test_a_non_finite_sleep_is_refused_by_both_clocks() -> None:
    with pytest.raises(InvalidDurationError):
        await ReplayClock(START).sleep(float("nan"))
    with pytest.raises(InvalidDurationError):
        await LiveClock().sleep(float("nan"))


def test_duration_failures_stay_catchable_as_value_errors() -> None:
    """Callers written against the stdlib sleep contract must not break."""
    assert issubclass(InvalidDurationError, ValueError)


def test_a_rejected_advance_to_never_leaves_the_clock_in_a_half_moved_state() -> None:
    clock = ReplayClock(START)
    clock.advance_by(60)
    with pytest.raises(ClockRegressionError):
        clock.advance_to(START)
    with pytest.raises(NaiveDatetimeError):
        clock.advance_to(datetime(2027, 1, 1, 0, 0))
    assert clock.now() == START + timedelta(seconds=60)


async def test_replay_sleep_rejects_negative_durations_without_moving_time() -> None:
    clock = ReplayClock(START)
    with pytest.raises(ValueError):
        await clock.sleep(-1)
    assert clock.now() == START


async def test_replay_sleep_of_zero_is_a_no_op() -> None:
    clock = ReplayClock(START)
    await clock.sleep(0)
    assert clock.now() == START


async def test_live_clock_sleep_of_zero_returns_without_delaying() -> None:
    """Exercises the non-negative branch without introducing a timing dependency."""
    before = LiveClock().now()
    await LiveClock().sleep(0)
    assert LiveClock().now() >= before
