"""``replay_capture``: one capture run in, one manifest and one state hash out.

This is the composition root for a replay, and the only place that knows about
all of the event store, the reader, the scheduler, the shared dispatcher and
the manifest at once. Everything it composes is separately testable, which is
why this module is short.

The replay clock starts at the capture run's own recorded ``started_at``, read
from the store. Deliberately not "now", and not a caller-supplied default:
starting a replay at a moment taken from outside the capture would make the
virtual clock's initial value a property of when the replay happened, and every
timestamp a downstream forecast recorded would inherit it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from itertools import chain

from argos.clock import Clock, ReplayClock
from argos.config.manifest import WorkingTreeStatus
from argos.config.settings import Settings
from argos.errors import ReplayError
from argos.projections.dispatch import ObservationDispatcher, Watermark
from argos.replay.manifest import (
    ReplayManifestV1,
    ReplayResultStatus,
    late_event_policy_record,
)
from argos.replay.pacing import ReplayPacer, VirtualPacer
from argos.replay.reader import read_capture_arrivals
from argos.replay.session import ReplayCounts, ReplayMode, ReplaySession
from argos.store.event_store import EventStore

__all__ = ["ReplayResult", "replay_capture"]


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Everything one replay produced.

    The dispatcher is handed back rather than only its hash, because a caller
    that wants to *look* at the reconstructed books — an M4 baseline, an
    operator inspecting a market — should not have to re-run the replay to get
    them.
    """

    manifest: ReplayManifestV1
    dispatcher: ObservationDispatcher
    counts: ReplayCounts

    @property
    def state_hash(self) -> str:
        return self.manifest.output_state_hash


def replay_capture(
    *,
    store: EventStore,
    capture_run_id: str,
    settings: Settings,
    wall_clock: Clock,
    replay_run_id: str,
    mode: ReplayMode = ReplayMode.ACCELERATED,
    allowed_lateness: timedelta = timedelta(0),
    pacer: ReplayPacer | None = None,
    code_revision: str | None = None,
    working_tree: WorkingTreeStatus = WorkingTreeStatus.UNKNOWN,
) -> ReplayResult:
    """Replay one capture run and return its manifest, state and counts.

    ``wall_clock`` stamps the manifest's own ``started_at``/``finished_at`` and
    is the *only* real-time reading in a replay. It is separate from the
    :class:`~argos.clock.ReplayClock` the scheduler advances, because they
    answer different questions: one is "when did this replay happen", the other
    is "what time was it inside the capture". Conflating them is how a replay
    starts leaking the present into the past.

    The manifest's timestamps therefore differ between two runs of the same
    capture, and that is correct — the reproducibility claim is about
    ``output_state_hash`` and ``output_record_counts``, which do not.
    """
    run = store.get_capture_run(capture_run_id)
    if run is None:
        raise ReplayError("no such capture_run", capture_run_id=capture_run_id)

    started_at = wall_clock.now()
    dispatcher = ObservationDispatcher(watermark=Watermark(allowed_lateness=allowed_lateness))

    # The replay clock is seeded from the *first arrival*, not from the capture
    # run's own `started_at`, and the difference is load-bearing rather than
    # cosmetic. Those two timestamps come from two different clocks -- the run
    # row from the capture loop's, the arrival from the transport's -- and
    # nothing in the store obliges them to agree. Seeding from the run row and
    # then advancing to an earlier arrival raises `ClockRegressionError` and
    # kills the replay; seeding from the run row and *skipping* the advance
    # would leave the virtual clock ahead of the capture, which is the leakage
    # this whole milestone exists to prevent. Seeding from the arrivals makes
    # the clock a function of the data being replayed, which is what it should
    # have been. Reproduced against the recorded capture, whose frames predate
    # the injected run start by four days.
    arrivals = read_capture_arrivals(store, capture_run_id)
    first = next(arrivals, None)
    session = ReplaySession(
        arrivals=arrivals if first is None else chain([first], arrivals),
        dispatcher=dispatcher,
        clock=ReplayClock(run.started_at if first is None else first.received_time),
        mode=mode,
        pacer=pacer if pacer is not None else VirtualPacer(),
    )

    status = ReplayResultStatus.COMPLETED
    try:
        counts = session.run()
    except Exception as error:
        # A replay that died still produced a manifest describing how far it
        # got, for the same reason a capture that died still closes its run:
        # "it failed" is a result, and a missing manifest is indistinguishable
        # from a replay nobody started.
        counts = session.counts
        manifest = _manifest(
            replay_run_id=replay_run_id,
            capture_run_id=capture_run_id,
            settings=settings,
            started_at=started_at,
            finished_at=wall_clock.now(),
            mode=mode,
            allowed_lateness=allowed_lateness,
            session=session,
            dispatcher=dispatcher,
            counts=counts,
            status=ReplayResultStatus.FAILED,
            code_revision=code_revision,
            working_tree=working_tree,
            completion_status=run.completion_status.value if run.completion_status else None,
        )
        raise ReplayError(
            "replay failed part-way through",
            replay_run_id=replay_run_id,
            capture_run_id=capture_run_id,
            arrivals_replayed=counts.arrivals,
            cause_type=type(error).__name__,
            cause=str(error),
            partial_manifest=manifest.to_record(),
        ) from error

    manifest = _manifest(
        replay_run_id=replay_run_id,
        capture_run_id=capture_run_id,
        settings=settings,
        started_at=started_at,
        finished_at=wall_clock.now(),
        mode=mode,
        allowed_lateness=allowed_lateness,
        session=session,
        dispatcher=dispatcher,
        counts=counts,
        status=status,
        code_revision=code_revision,
        working_tree=working_tree,
        completion_status=run.completion_status.value if run.completion_status else None,
    )
    return ReplayResult(manifest=manifest, dispatcher=dispatcher, counts=counts)


def _manifest(
    *,
    replay_run_id: str,
    capture_run_id: str,
    settings: Settings,
    started_at: object,
    finished_at: object,
    mode: ReplayMode,
    allowed_lateness: timedelta,
    session: ReplaySession,
    dispatcher: ObservationDispatcher,
    counts: ReplayCounts,
    status: ReplayResultStatus,
    code_revision: str | None,
    working_tree: WorkingTreeStatus,
    completion_status: str | None,
) -> ReplayManifestV1:
    return ReplayManifestV1(
        replay_run_id=replay_run_id,
        source_capture_run_id=capture_run_id,
        code_revision=code_revision,
        working_tree=working_tree,
        config_fingerprint=settings.fingerprint(),
        started_at=started_at,  # type: ignore[arg-type]
        finished_at=finished_at,  # type: ignore[arg-type]
        replay_mode=mode,
        input_first_sequence=session.first_sequence,
        input_last_sequence=session.last_sequence,
        input_arrival_count=counts.arrivals,
        output_state_hash=dispatcher.state_hash(),
        # Both count sets, side by side and never merged: one describes what the
        # capture recorded, the other what the dispatcher did with it, and a
        # single flat set would make "the capture rejected 4 events" and "the
        # dispatcher had no handler for 4 payloads" indistinguishable.
        output_record_counts={
            "arrivals": counts.as_record(),
            "dispatch": dispatcher.counts.as_record(),
        },
        late_event_policy=late_event_policy_record(allowed_lateness),
        result_status=status,
        source_completion_status=completion_status,
    )
