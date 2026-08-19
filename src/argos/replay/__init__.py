"""Capture readers, replay scheduler, watermarks, and run hashes (M3).

ADR-0012 is the specification. In short: a replay is scoped to one capture run,
reads both ledgers merged on `ingest_sequence`, hands every accepted and
duplicate arrival to the *same* `ObservationDispatcher` a live capture drives,
advances a `ReplayClock` to each arrival's `received_time` and never backwards,
marks late events without reordering them, and produces a state hash that
covers projected state and nothing else.
"""

from argos.replay.manifest import (
    LATE_EVENT_POLICY_KIND,
    ReplayManifestV1,
    ReplayResultStatus,
    late_event_policy_record,
)
from argos.replay.pacing import RealTimePacer, ReplayPacer, VirtualPacer
from argos.replay.reader import ArrivalKind, ReplayArrival, read_capture_arrivals
from argos.replay.run import ReplayResult, replay_capture
from argos.replay.session import ReplayCounts, ReplayMode, ReplaySession, StepResult

__all__ = [
    "LATE_EVENT_POLICY_KIND",
    "ArrivalKind",
    "RealTimePacer",
    "ReplayArrival",
    "ReplayCounts",
    "ReplayManifestV1",
    "ReplayMode",
    "ReplayPacer",
    "ReplayResult",
    "ReplayResultStatus",
    "ReplaySession",
    "StepResult",
    "VirtualPacer",
    "late_event_policy_record",
    "read_capture_arrivals",
    "replay_capture",
]
