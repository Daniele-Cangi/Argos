"""Budget enforcement for one synthetic or live V2 cohort target capture.

This adapter wraps the shared ``run_capture`` path. It never opens a socket or
chooses a target; callers must pass the two admitted token IDs and a separate
per-target store/archive. A fetched frame that would cross the byte/time cap is
not normalized or written into the bounded capture, but its digest and size are
returned so the caller can account for the stopping boundary.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from argos.clock import Clock, Pacer, ReplayClock, ensure_utc
from argos.ingestion.capture import CaptureHealth, FrameSource, run_capture
from argos.sources.clob_ws import MarketFrame
from argos.store.event_store import EventStore

__all__ = [
    "BoundedCaptureStopReasonV2",
    "BoundedCaptureSummaryV2",
    "run_bounded_cohort_capture_v2",
]


class BoundedCaptureStopReasonV2(StrEnum):
    """Why a bounded V2 target capture stopped consuming source frames."""

    SOURCE_EXHAUSTED = "source_exhausted"
    DURATION_CAP = "duration_cap"
    FRAME_CAP = "frame_cap"
    BYTE_CAP = "byte_cap"


@dataclass(frozen=True, slots=True)
class BoundedCaptureSummaryV2:
    """Non-persistent run result; persistent close evidence binds this summary."""

    target_id: str
    capture_run_id: str
    started_at: datetime
    finished_at: datetime
    stop_reason: BoundedCaptureStopReasonV2
    frames_archived: int
    raw_bytes_archived: int
    boundary_frame_bytes: int | None
    boundary_frame_sha256: str | None
    health: CaptureHealth


@dataclass(frozen=True, slots=True)
class _BoundaryFrame:
    byte_length: int
    sha256: str


class _BoundedFrameSource:
    def __init__(
        self,
        *,
        source: FrameSource,
        clock: Clock,
        pacer: Pacer,
        max_seconds: int,
        max_frames: int,
        max_bytes: int,
        after_frame: Callable[[MarketFrame, int], None] | None,
    ) -> None:
        self._source = source
        self._clock = clock
        self._pacer = pacer
        self._max_seconds = max_seconds
        self._max_frames = max_frames
        self._max_bytes = max_bytes
        self._after_frame = after_frame
        self.started_at: datetime | None = ensure_utc(clock.now())
        self.stop_reason: BoundedCaptureStopReasonV2 | None = None
        self.frames_archived = 0
        self.raw_bytes_archived = 0
        self.boundary_frame: _BoundaryFrame | None = None

    async def frames(self) -> AsyncIterator[MarketFrame]:
        iterator = self._source.frames().__aiter__()
        assert self.started_at is not None
        deadline = self.started_at + timedelta(seconds=self._max_seconds)
        try:
            while True:
                if self.frames_archived >= self._max_frames:
                    self.stop_reason = BoundedCaptureStopReasonV2.FRAME_CAP
                    return
                remaining = (deadline - ensure_utc(self._clock.now())).total_seconds()
                if remaining <= 0:
                    self.stop_reason = BoundedCaptureStopReasonV2.DURATION_CAP
                    return

                frame: MarketFrame | None = None
                if isinstance(self._clock, ReplayClock):
                    try:
                        frame = await anext(iterator)
                    except StopAsyncIteration:
                        self.stop_reason = BoundedCaptureStopReasonV2.SOURCE_EXHAUSTED
                        return
                else:
                    with self._pacer.move_on_after(remaining) as scope:
                        try:
                            frame = await anext(iterator)
                        except StopAsyncIteration:
                            self.stop_reason = BoundedCaptureStopReasonV2.SOURCE_EXHAUSTED
                            return
                    if scope.cancelled_caught:
                        self.stop_reason = BoundedCaptureStopReasonV2.DURATION_CAP
                        return
                assert frame is not None
                raw = frame.text.encode("utf-8")
                if not frame.provenance.matches(raw):
                    raise ValueError("V2 capture frame bytes disagree with source provenance")

                # Replay sources advance by their recorded receive timestamps while
                # a live clock advances independently. Use the later instant so a
                # replay cannot smuggle post-deadline data into the bounded run.
                received_at = ensure_utc(frame.received_time)
                retrieved_at = ensure_utc(frame.provenance.retrieved_at)
                if received_at != retrieved_at:
                    raise ValueError(
                        "V2 frame receive time disagrees with source provenance retrieval time"
                    )
                clock_now = ensure_utc(self._clock.now())
                effective_now = max(clock_now, received_at)
                if effective_now > deadline:
                    self.stop_reason = BoundedCaptureStopReasonV2.DURATION_CAP
                    self.boundary_frame = _BoundaryFrame(len(raw), frame.provenance.raw_sha256)
                    return
                if received_at > clock_now:
                    raise ValueError(
                        "V2 frame source must advance its replay clock to accepted receipt times"
                    )
                if self.raw_bytes_archived + len(raw) > self._max_bytes:
                    self.stop_reason = BoundedCaptureStopReasonV2.BYTE_CAP
                    self.boundary_frame = _BoundaryFrame(len(raw), frame.provenance.raw_sha256)
                    return

                self.frames_archived += 1
                self.raw_bytes_archived += len(raw)
                yield frame
                if self._after_frame is not None:
                    self._after_frame(frame, self.frames_archived)
        finally:
            close = getattr(iterator, "aclose", None)
            if callable(close):
                await close()


async def run_bounded_cohort_capture_v2(
    *,
    target_id: str,
    frame_source: FrameSource,
    store: EventStore,
    clock: Clock,
    pacer: Pacer,
    capture_run_id: str,
    subscribed_token_ids: Iterable[str],
    max_seconds: int,
    max_frames: int,
    max_bytes: int,
    raw_archive_dir: Path,
    after_frame: Callable[[MarketFrame, int], None] | None = None,
) -> BoundedCaptureSummaryV2:
    """Run the existing ingestion handlers under one V2 target's hard limits.

    The source must receive exactly one admitted binary pair. The function is
    deliberately target-scoped and requires raw archival; a caller creates a
    distinct store and archive for each target. A limit stop is a clean end of
    the bounded window, not an ingestion exception; source/store failures still
    flow through ``run_capture`` and close the store run as failed. For replay,
    the frame-source scheduler owns the supplied ``ReplayClock`` and must
    advance it to each accepted frame's receive time before yielding that frame;
    a frame ahead of the clock is rejected rather than recorded in a run that
    ends before the archived evidence.
    """
    if not isinstance(target_id, str) or not target_id.strip():
        raise ValueError("V2 capture requires one admitted target ID")
    token_ids = tuple(subscribed_token_ids)
    if (
        len(token_ids) != 2
        or any(not isinstance(token_id, str) or not token_id.strip() for token_id in token_ids)
        or len(set(token_ids)) != 2
    ):
        raise ValueError("V2 target capture must subscribe to exactly its two distinct tokens")
    for name, value in (
        ("max_seconds", max_seconds),
        ("max_frames", max_frames),
        ("max_bytes", max_bytes),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"V2 capture {name} must be a positive integer")

    bounded_source = _BoundedFrameSource(
        source=frame_source,
        clock=clock,
        pacer=pacer,
        max_seconds=max_seconds,
        max_frames=max_frames,
        max_bytes=max_bytes,
        after_frame=after_frame,
    )
    health = await run_capture(
        frame_source=bounded_source,
        store=store,
        clock=clock,
        capture_run_id=capture_run_id,
        subscribed_token_ids=token_ids,
        raw_archive_dir=raw_archive_dir,
    )
    run = store.get_capture_run(capture_run_id)
    if run is None or run.ended_at is None or bounded_source.started_at is None:
        raise RuntimeError("V2 bounded capture did not produce a closed capture-run record")
    if bounded_source.stop_reason is None:
        raise RuntimeError("V2 bounded capture ended without a stopping reason")
    return BoundedCaptureSummaryV2(
        target_id=target_id,
        capture_run_id=capture_run_id,
        started_at=run.started_at,
        finished_at=run.ended_at,
        stop_reason=bounded_source.stop_reason,
        frames_archived=bounded_source.frames_archived,
        raw_bytes_archived=bounded_source.raw_bytes_archived,
        boundary_frame_bytes=(
            bounded_source.boundary_frame.byte_length
            if bounded_source.boundary_frame is not None
            else None
        ),
        boundary_frame_sha256=(
            bounded_source.boundary_frame.sha256
            if bounded_source.boundary_frame is not None
            else None
        ),
        health=health,
    )
