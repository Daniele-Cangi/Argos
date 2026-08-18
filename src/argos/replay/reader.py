"""Reading one capture run back in its original arrival order (ADR-0012 §1-2).

ADR-0003 requires replay to reproduce the arrival sequence ARGOS actually saw.
The event store already holds that sequence: ``delivery`` and ``rejection`` draw
from **one** ``ingest_sequence`` counter per capture run, and the store refuses a
reused value across both tables inside the insert's own transaction. So the
arrival order is recovered by merging the two ledgers on that counter — no new
query on the :class:`~argos.store.event_store.EventStore` port, which keeps
ADR-0011's "SQL stays inside ``argos.store``" intact rather than widening the
port for one consumer.

Scoped to exactly one capture run, deliberately. ``ingest_sequence`` means
nothing between runs, and a cross-run total order would have to be invented —
by ``started_at``, which two concurrent captures can share or interleave, or by
run id, which is alphabetical rather than temporal. ADR-0003's "reproducibility
requires stable tie-breaking" is precisely a warning against inventing one.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from argos.domain.observation import ObservationEnvelopeV1, RejectedObservationV1
from argos.errors import ReplayError
from argos.store.event_store import DeliveryRecord, Disposition, EventStore, RejectionRecord

__all__ = ["ArrivalKind", "ReplayArrival", "read_capture_arrivals"]


class ArrivalKind(StrEnum):
    """What one arrival was, as the capture recorded it."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    """A redelivery of an observation already accepted in this run. Replayed as
    an arrival and counted; the dispatcher refuses to let it move state twice
    (ADR-0012 section 3)."""

    REJECTED = "rejected"
    """Input the capture refused. Replayed as an arrival, its reason tallied,
    never applied to state — a rejection carries no payload by construction, and
    a ledger whose records could reach a projection would defeat its own
    purpose."""


@dataclass(frozen=True, slots=True)
class ReplayArrival:
    """One arrival, in the position the capture recorded it.

    ``envelope`` is present for :attr:`ArrivalKind.ACCEPTED` and
    :attr:`ArrivalKind.DUPLICATE` — both name a stored observation — and
    ``rejection`` for :attr:`ArrivalKind.REJECTED`. Exactly one of the two is
    ever set; the invariant is checked on construction rather than trusted,
    because a replay that dispatched a half-built arrival would corrupt state
    silently.
    """

    ingest_sequence: int
    kind: ArrivalKind
    received_time: datetime
    envelope: ObservationEnvelopeV1 | None = None
    rejection: RejectedObservationV1 | None = None

    def __post_init__(self) -> None:
        has_envelope = self.envelope is not None
        has_rejection = self.rejection is not None
        if has_envelope == has_rejection:
            raise ReplayError(
                "an arrival must carry exactly one of an envelope or a rejection",
                ingest_sequence=self.ingest_sequence,
                kind=self.kind.value,
            )
        if (self.kind is ArrivalKind.REJECTED) != has_rejection:
            raise ReplayError(
                "arrival kind disagrees with what it carries",
                ingest_sequence=self.ingest_sequence,
                kind=self.kind.value,
            )


def read_capture_arrivals(store: EventStore, capture_run_id: str) -> Iterator[ReplayArrival]:
    """Yield one capture run's arrivals in ascending ``ingest_sequence``.

    Refuses a run the store has never opened: replaying an unknown run would
    otherwise yield an empty stream, and "this capture is empty" and "this
    capture does not exist" are different answers that must not look identical
    in a manifest.

    Refuses a delivery whose observation is missing rather than skipping it. The
    store enforces that reference with a SQL foreign key, so its absence means
    the database is damaged; continuing would produce a state hash for a capture
    ARGOS could not actually read, which is worse than stopping.
    """
    if store.get_capture_run(capture_run_id) is None:
        raise ReplayError("no such capture_run", capture_run_id=capture_run_id)

    # Both ledgers already come back ordered by ingest_sequence, and the store
    # refuses a value used twice across the two tables, so a straight merge on
    # that key reconstructs the arrival order exactly -- and lazily, so a large
    # capture is never materialized.
    for record in _merge(
        store.iter_deliveries(capture_run_id), store.iter_rejections(capture_run_id)
    ):
        yield _to_arrival(store, record)


def _merge(
    deliveries: Iterator[DeliveryRecord],
    rejections: Iterator[RejectionRecord],
) -> Iterator[DeliveryRecord | RejectionRecord]:
    delivery = next(deliveries, None)
    rejection = next(rejections, None)
    while delivery is not None or rejection is not None:
        take_delivery = rejection is None or (
            delivery is not None and delivery.ingest_sequence < rejection.ingest_sequence
        )
        if take_delivery:
            assert delivery is not None
            yield delivery
            delivery = next(deliveries, None)
        else:
            assert rejection is not None
            yield rejection
            rejection = next(rejections, None)


def _to_arrival(store: EventStore, record: DeliveryRecord | RejectionRecord) -> ReplayArrival:
    if isinstance(record, RejectionRecord):
        return ReplayArrival(
            ingest_sequence=record.ingest_sequence,
            kind=ArrivalKind.REJECTED,
            received_time=record.rejection.received_time,
            rejection=record.rejection,
        )

    envelope = store.get_observation(record.observation_id)
    if envelope is None:
        raise ReplayError(
            "a delivery names an observation the store does not hold; the "
            "database is damaged, and a state hash computed over what remains "
            "would describe a capture ARGOS could not read",
            capture_run_id=record.capture_run_id,
            ingest_sequence=record.ingest_sequence,
            observation_id=record.observation_id,
        )
    return ReplayArrival(
        ingest_sequence=record.ingest_sequence,
        kind=(
            ArrivalKind.DUPLICATE
            if record.disposition is Disposition.DUPLICATE
            else ArrivalKind.ACCEPTED
        ),
        # The *delivery's* received time, not the envelope's. They differ for a
        # duplicate by construction: the envelope pins the first arrival's value
        # (ADR-0011 section 5), and this arrival is a later one. Replaying the
        # envelope's copy would move the replay clock to a moment that already
        # passed.
        received_time=record.received_time,
        envelope=envelope,
    )
