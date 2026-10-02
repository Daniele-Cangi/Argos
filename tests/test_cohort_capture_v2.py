"""Bounded V2 capture over the shared ingestion loop, without network I/O."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractContextManager
from datetime import timedelta
from pathlib import Path
from typing import NoReturn

import pytest
from anyio import CancelScope
from test_capture_loop import (
    START,
    TOKEN_NO,
    TOKEN_YES,
    ListFrameSource,
    _frame,
    _unknown_event,
)

from argos.clock import Pacer, RealPacer, ReplayClock
from argos.ingestion import BoundedCaptureStopReasonV2, run_bounded_cohort_capture_v2
from argos.sources.clob_ws import MarketFrame
from argos.store.event_store import SQLiteEventStore, open_sqlite_event_store


class _ClosableFrameSource:
    def __init__(self, frames: Sequence[MarketFrame]) -> None:
        self._frames = frames
        self.closed = False

    async def frames(self) -> AsyncIterator[MarketFrame]:
        try:
            for frame in self._frames:
                yield frame
        finally:
            self.closed = True


class _ReplayScheduledFrameSource:
    """Synthetic scheduler that advances replay time before each frame arrives."""

    def __init__(self, source: object, clock: ReplayClock) -> None:
        self._source = source
        self._clock = clock

    async def frames(self) -> AsyncIterator[MarketFrame]:
        iterator = self._source.frames().__aiter__()
        try:
            async for frame in iterator:
                self._clock.advance_to(max(self._clock.now(), frame.received_time))
                yield frame
        finally:
            close = getattr(iterator, "aclose", None)
            if callable(close):
                await close()


class _ForbiddenReplayPacer:
    async def wait(self, seconds: float) -> NoReturn:
        raise AssertionError("replay capture must not consult wall-clock pacing")

    def move_on_after(self, seconds: float) -> AbstractContextManager[CancelScope, bool]:
        raise AssertionError("replay capture must not consult wall-clock pacing")


@pytest.fixture
def store() -> SQLiteEventStore:
    return open_sqlite_event_store(":memory:")


def _frame_at(seconds: int) -> MarketFrame:
    return _frame(
        _unknown_event(),
        received_time=START + timedelta(seconds=seconds),
    )


async def _capture(
    *,
    store: SQLiteEventStore,
    archive: Path,
    frames: Sequence[MarketFrame],
    run_id: str,
    max_seconds: int = 30,
    max_frames: int = 10,
    max_bytes: int = 10_000,
    frame_source: object | None = None,
    pacer: Pacer | None = None,
    after_frame=None,
    advance_replay_clock: bool = True,
):
    clock = ReplayClock(START)
    source = frame_source or ListFrameSource(frames)
    if advance_replay_clock:
        source = _ReplayScheduledFrameSource(source, clock)
    return await run_bounded_cohort_capture_v2(
        target_id="target-1",
        frame_source=source,
        store=store,
        clock=clock,
        pacer=pacer or RealPacer(),
        capture_run_id=run_id,
        subscribed_token_ids=(TOKEN_YES, TOKEN_NO),
        max_seconds=max_seconds,
        max_frames=max_frames,
        max_bytes=max_bytes,
        raw_archive_dir=archive,
        after_frame=after_frame,
    )


async def test_bounded_capture_uses_shared_loop_and_stops_before_frame_cap_overrun(
    store: SQLiteEventStore, tmp_path: Path
) -> None:
    frames = [_frame_at(1), _frame_at(2), _frame_at(3)]
    observed_counts: list[tuple[int, int]] = []

    def after_frame(frame: MarketFrame, ordinal: int) -> None:
        counts = store.counts_for_capture_run("frame-cap")
        observed_counts.append((ordinal, counts.rejected))

    summary = await _capture(
        store=store,
        archive=tmp_path,
        frames=frames,
        run_id="frame-cap",
        max_frames=2,
        after_frame=after_frame,
    )

    assert summary.stop_reason is BoundedCaptureStopReasonV2.FRAME_CAP
    assert summary.frames_archived == summary.health.frames_consumed == 2
    assert summary.raw_bytes_archived == sum(len(frame.text.encode()) for frame in frames[:2])
    assert summary.boundary_frame_bytes is None
    assert [count for _, count in observed_counts] == [1, 2]
    assert summary.finished_at == frames[1].received_time
    assert store.get_capture_run("frame-cap").ended_at == summary.finished_at
    assert list(store.iter_open_capture_runs()) == []


async def test_byte_cap_excludes_and_accounts_for_the_boundary_frame(
    store: SQLiteEventStore, tmp_path: Path
) -> None:
    first, boundary = _frame_at(1), _frame_at(2)
    byte_limit = len(first.text.encode())

    summary = await _capture(
        store=store,
        archive=tmp_path,
        frames=[first, boundary],
        run_id="byte-cap",
        max_bytes=byte_limit,
    )

    assert summary.stop_reason is BoundedCaptureStopReasonV2.BYTE_CAP
    assert summary.frames_archived == 1
    assert summary.raw_bytes_archived == byte_limit
    assert summary.boundary_frame_bytes == len(boundary.text.encode())
    assert summary.boundary_frame_sha256 == boundary.provenance.raw_sha256
    assert summary.health.frames_consumed == 1


async def test_duration_cap_excludes_a_frame_received_after_the_deadline(
    store: SQLiteEventStore, tmp_path: Path
) -> None:
    first, late = _frame_at(1), _frame_at(6)

    summary = await _capture(
        store=store,
        archive=tmp_path,
        frames=[first, late],
        run_id="duration-cap",
        max_seconds=5,
    )

    assert summary.stop_reason is BoundedCaptureStopReasonV2.DURATION_CAP
    assert summary.frames_archived == 1
    assert summary.boundary_frame_sha256 == late.provenance.raw_sha256
    assert summary.health.frames_consumed == 1


async def test_source_exhaustion_is_reported_and_source_is_closed(
    store: SQLiteEventStore, tmp_path: Path
) -> None:
    source = _ClosableFrameSource([_frame_at(1)])

    summary = await _capture(
        store=store,
        archive=tmp_path,
        frames=[],
        run_id="source-exhausted",
        frame_source=source,
    )

    assert summary.stop_reason is BoundedCaptureStopReasonV2.SOURCE_EXHAUSTED
    assert summary.frames_archived == 1
    assert source.closed


async def test_v2_capture_rejects_invalid_pair_and_nonpositive_caps(
    store: SQLiteEventStore, tmp_path: Path
) -> None:
    common = {
        "target_id": "target-1",
        "frame_source": ListFrameSource([_frame_at(1)]),
        "store": store,
        "clock": ReplayClock(START),
        "pacer": RealPacer(),
        "capture_run_id": "invalid-config",
        "max_seconds": 30,
        "max_frames": 10,
        "max_bytes": 10_000,
        "raw_archive_dir": tmp_path,
    }

    with pytest.raises(ValueError, match="exactly its two distinct tokens"):
        await run_bounded_cohort_capture_v2(**common, subscribed_token_ids=(TOKEN_YES, TOKEN_YES))
    with pytest.raises(ValueError, match="max_frames must be a positive integer"):
        invalid_caps = {**common, "max_frames": 0}
        await run_bounded_cohort_capture_v2(
            **invalid_caps,
            subscribed_token_ids=(TOKEN_YES, TOKEN_NO),
        )


async def test_v2_capture_rejects_tampered_source_provenance(
    store: SQLiteEventStore, tmp_path: Path
) -> None:
    original = _frame_at(1)
    tampered = MarketFrame(
        text=original.text + " ",
        received_time=original.received_time,
        provenance=original.provenance,
    )

    with pytest.raises(ValueError, match="disagree with source provenance"):
        await _capture(
            store=store,
            archive=tmp_path,
            frames=[tampered],
            run_id="tampered-provenance",
        )

    run = store.get_capture_run("tampered-provenance")
    assert run is not None and run.ended_at is not None
    assert run.completion_status.value == "failed"


async def test_v2_capture_rejects_receive_time_not_bound_to_source_retrieval(
    store: SQLiteEventStore, tmp_path: Path
) -> None:
    original = _frame_at(1)
    mismatched = MarketFrame(
        text=original.text,
        received_time=original.received_time + timedelta(seconds=1),
        provenance=original.provenance,
    )

    with pytest.raises(ValueError, match="receive time disagrees with source provenance"):
        await _capture(
            store=store,
            archive=tmp_path,
            frames=[mismatched],
            run_id="mismatched-receipt-time",
        )

    run = store.get_capture_run("mismatched-receipt-time")
    assert run is not None and run.completion_status.value == "failed"


async def test_v2_replay_duration_is_driven_only_by_recorded_timestamps(
    store: SQLiteEventStore, tmp_path: Path
) -> None:
    summary = await _capture(
        store=store,
        archive=tmp_path,
        frames=[_frame_at(1)],
        run_id="replay-does-not-use-wall-clock-timeout",
        max_seconds=2,
        pacer=_ForbiddenReplayPacer(),
    )

    assert summary.stop_reason is BoundedCaptureStopReasonV2.SOURCE_EXHAUSTED
    assert summary.frames_archived == 1
    assert summary.finished_at == _frame_at(1).received_time


async def test_v2_capture_rejects_replay_frame_ahead_of_clock(
    store: SQLiteEventStore, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="advance its replay clock"):
        await _capture(
            store=store,
            archive=tmp_path,
            frames=[_frame_at(1)],
            run_id="stale-replay-clock",
            advance_replay_clock=False,
        )

    run = store.get_capture_run("stale-replay-clock")
    assert run is not None and run.completion_status.value == "failed"
