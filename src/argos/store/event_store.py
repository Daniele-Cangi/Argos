"""The idempotent, append-only SQLite event store (ADR-0011).

``docs/adr/0011-sqlite-event-store-and-delivery-record.md`` is the
specification this module implements; read it before changing anything here.
Two decisions it left for this slice, and this module's own resolution of
each:

- **What a delivery is** (ADR-0010 left this open): one row per *arrival*,
  keyed ``(capture_run_id, ingest_sequence)``, distinct from the one
  immutable ``observation`` row per distinct ``observation_id``. A duplicate
  arrival gets its own :class:`DeliveryRecord` with
  ``disposition=DUPLICATE``; the observation row it points at is never
  touched again. This is what makes "idempotent insert" also "observable"
  (``docs/02_ARCHITECTURE.md``, "Persistence model").
- **How ``capture_run`` stays append-only.** ADR-0011 describes
  ``capture_run`` as "the manifest row, with ``ended_at`` NULL meaning not
  closed" (section 5) — which reads as one row, mutated at close time — while
  its own Consequences section separately requires "no UPDATE, DELETE, or
  ALTER literal may appear in ``argos.store``". Those two clauses conflict
  for exactly this table: recording a close by setting a column on an
  existing row *is* an in-place mutation. This module resolves it the same
  way ADR-0011 resolves aggregate counters (section 5, "derived, never
  incremented"): ``capture_run`` is append-only, one row per lifecycle event.
  Opening inserts a row with ``ended_at`` unset; closing inserts a *second*
  row for the same ``capture_run_id`` with ``ended_at`` set. "Not yet closed"
  is therefore a derived read — no row with ``ended_at`` set exists yet — not
  a column two different writes touch. Two partial unique indexes make "opened
  twice" and "closed twice" impossible at the schema level rather than by
  convention. This is a deliberate deviation from a literal single-row
  reading of section 5, recorded here rather than silently picked.

Everything else follows section 5 directly: ``observation`` stays opaque to
payload types (section 3) — the canonical JSON of ``to_record()`` in one
``record`` column, plus a handful of columns duplicated out of it for keys,
filtering, and migration, never a column per contract field. ``rejection``
keys on ``(capture_run_id, ingest_sequence)`` because ``rejection_id`` is a
grouping key, not a unique key (section 7): two different malformed entries
in one frame, refused for the same reason against the same archived frame
hash, share one ``rejection_id`` by construction, and both rows must
survive. ``recompute_observation_id``/``recompute_rejection_id`` are checked
on write (a mismatch is the caller's fault: :class:`ContractViolationError`)
and independently on read (a mismatch means the stored row itself disagrees
with its own identity: :class:`StorageError`) — section 8.

No wall clock: every timestamp column is filled from a value the caller
passed in (an ``ObservationEnvelopeV1``/``RejectedObservationV1`` field, or an
explicit ``started_at``/``ended_at`` argument), never from
``CURRENT_TIMESTAMP`` or any Python call to the real clock.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from argos.clock import ensure_utc
from argos.domain.observation import (
    MAX_IDENTIFIER_LENGTH,
    ObservationEnvelopeV1,
    RejectedObservationV1,
    recompute_observation_id,
    recompute_rejection_id,
)
from argos.domain.text import is_clean_identifier
from argos.errors import ContractViolationError, ImmutabilityViolationError, StorageError

_SYNCHRONOUS_FULL = 2
"""``PRAGMA synchronous`` reports an integer; FULL is 2."""

__all__ = [
    "CaptureRunCounts",
    "CaptureRunRecord",
    "CompletionStatus",
    "DeliveryRecord",
    "Disposition",
    "EventStore",
    "RejectionRecord",
    "SQLiteEventStore",
    "open_sqlite_event_store",
]


class Disposition(StrEnum):
    """What an arrival was, relative to the observation it names."""

    ACCEPTED_NEW = "accepted_new"
    DUPLICATE = "duplicate"


class CompletionStatus(StrEnum):
    """How a capture run ended.

    Assigned once, by whichever call closes the run. A run a reader later
    finds with no closing row at all (``iter_open_capture_runs``) was never
    closed by anyone — the crashed process never got to write one — so there
    is deliberately no ``INTERRUPTED`` value written *by* the crashed run
    itself; interruption is the *absence* of a completion record, discovered
    by a later reader, not a status the dying process assigns itself.
    """

    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    """One row of ``delivery``: one arrival, whether new or a duplicate.

    ``source_frame_sha256``/``source_frame_offset`` make the residual
    identity collision ADR-0010 documents and ADR-0011 section 6 quantifies
    countable rather than solved: two deliveries sharing an
    ``observation_id`` are distinguishable as the same frame carrying the
    same entry twice (same hash, same offset), the same frame carrying an
    entry twice (same hash, different offset), or a genuine two-frame resend
    (different hash) purely from what is already stored, without re-taking a
    capture.
    """

    capture_run_id: str
    ingest_sequence: int
    observation_id: str
    received_time: datetime
    disposition: Disposition
    source_frame_sha256: str
    source_frame_offset: int


@dataclass(frozen=True, slots=True)
class RejectionRecord:
    """One row of ``rejection``: one rejected arrival.

    Not part of the five operations ADR-0011 names explicitly, but added
    because two of this slice's required test scenarios — "both rows survive
    a shared rejection_id" and "a rejection ledger with reason and raw hash"
    — cannot be verified from outside ``argos.store`` without a way to read
    the ledger back. It mirrors :func:`SQLiteEventStore.iter_deliveries`:
    same shape of operation, same read-only, same-run scope.
    """

    capture_run_id: str
    ingest_sequence: int
    duplicate_of_observation_id: str | None
    rejection: RejectedObservationV1


@dataclass(frozen=True, slots=True)
class CaptureRunRecord:
    """The state of one capture run, merged from its append-only rows.

    ``started_at`` is always present once a run has been opened: the opening
    row is never touched again, closed or not. ``ended_at``/
    ``completion_status`` are both ``None`` until a closing row exists.
    """

    capture_run_id: str
    started_at: datetime
    ended_at: datetime | None
    completion_status: CompletionStatus | None

    @property
    def is_open(self) -> bool:
        """``True`` until a closing row exists — the mechanical, queryable
        "interrupted capture" signal ADR-0011 section 2 names."""
        return self.ended_at is None


@dataclass(frozen=True, slots=True)
class CaptureRunCounts:
    """Aggregate counters for one capture run, derived by query.

    Never stored as an incrementing integer (ADR-0011 section 5): a summary
    computed this way cannot silently disagree with the rows it summarizes.
    """

    accepted: int
    duplicate: int
    rejected: int


class EventStore(Protocol):
    """The port a capture loop, and later M3 replay, read and write through.

    Narrow by design: open/close a capture run, append an observation,
    append a rejection, get an observation by id, and iterate one run's
    deliveries/rejections in arrival order. No query DSL, no migrations, no
    retention/compaction, no second backend — those are explicitly out of
    this slice (ADR-0011, Consequences).
    """

    def open_capture_run(self, capture_run_id: str, *, started_at: datetime) -> None:
        """Record that a capture run has begun."""
        ...

    def close_capture_run(
        self,
        capture_run_id: str,
        *,
        ended_at: datetime,
        completion_status: CompletionStatus,
    ) -> None:
        """Record that a capture run has ended. Refuses to close twice."""
        ...

    def get_capture_run(self, capture_run_id: str) -> CaptureRunRecord | None:
        """Return the merged state of a capture run, or ``None`` if unknown."""
        ...

    def iter_open_capture_runs(self) -> Iterator[CaptureRunRecord]:
        """Runs with no closing row — interrupted, unless still genuinely live."""
        ...

    def append_observation(
        self,
        envelope: ObservationEnvelopeV1,
        *,
        source_frame_offset: int = 0,
    ) -> DeliveryRecord:
        """Insert ``envelope`` idempotently and record its own arrival.

        Inserts a new ``observation`` row only if ``envelope.observation_id``
        has never been seen; always inserts a new ``delivery`` row. Both
        writes are one transaction, whichever branch is taken.
        """
        ...

    def append_rejection(
        self,
        rejection: RejectedObservationV1,
        *,
        ingest_sequence: int,
        duplicate_of_observation_id: str | None = None,
    ) -> None:
        """Insert one rejection-ledger row.

        ``rejection.rejection_id`` is a grouping key, not a unique key
        (ADR-0011 section 7): two different malformed entries in one frame
        refused for the same reason can share one, and both still get a row
        because uniqueness lives on ``(capture_run_id, ingest_sequence)``.
        """
        ...

    def get_observation(self, observation_id: str) -> ObservationEnvelopeV1 | None:
        """Return the stored envelope for ``observation_id``, or ``None``."""
        ...

    def iter_deliveries(self, capture_run_id: str) -> Iterator[DeliveryRecord]:
        """Iterate one run's deliveries in ``ingest_sequence`` order."""
        ...

    def iter_rejections(self, capture_run_id: str) -> Iterator[RejectionRecord]:
        """Iterate one run's rejections in ``ingest_sequence`` order."""
        ...

    def counts_for_capture_run(self, capture_run_id: str) -> CaptureRunCounts:
        """Return accepted/duplicate/rejected counts, derived by query."""
        ...

    def close(self) -> None:
        """Release the underlying connection."""
        ...


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS capture_run (
    id INTEGER PRIMARY KEY,
    capture_run_id TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    completion_status TEXT CHECK (
        completion_status IS NULL OR completion_status IN ('completed', 'failed')
    )
);

-- At most one still-open row and at most one closing row per capture_run_id.
-- This is what keeps capture_run append-only: closing inserts a second row
-- instead of touching the first, so "not yet closed" is a derived read (no
-- row with ended_at set exists yet), never a column two writes both reach.
CREATE UNIQUE INDEX IF NOT EXISTS capture_run_open_once
    ON capture_run (capture_run_id) WHERE ended_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS capture_run_closed_once
    ON capture_run (capture_run_id) WHERE ended_at IS NOT NULL;

-- One immutable row per distinct observation_id. capture_run_id/ingest_sequence
-- are deliberately *not* a foreign key here: capture_run_id is not unique in
-- capture_run by the append-only design above (it names up to two rows), so a
-- plain SQL foreign key cannot target it. "The named run was actually opened"
-- is instead checked in the same transaction as every insert, in Python.
CREATE TABLE IF NOT EXISTS observation (
    id INTEGER PRIMARY KEY,
    observation_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    payload_schema_version TEXT NOT NULL,
    source TEXT NOT NULL,
    market_id TEXT,
    condition_id TEXT,
    token_id TEXT,
    event_time TEXT,
    raw_payload_sha256 TEXT NOT NULL,
    first_seen_capture_run_id TEXT NOT NULL,
    first_seen_ingest_sequence INTEGER NOT NULL,
    first_seen_received_time TEXT NOT NULL,
    record TEXT NOT NULL,
    UNIQUE (observation_id)
);

-- One row per arrival, whether it turned out to be new or a duplicate.
CREATE TABLE IF NOT EXISTS delivery (
    id INTEGER PRIMARY KEY,
    capture_run_id TEXT NOT NULL,
    ingest_sequence INTEGER NOT NULL,
    observation_id TEXT NOT NULL REFERENCES observation (observation_id),
    received_time TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('accepted_new', 'duplicate')),
    source_frame_sha256 TEXT NOT NULL,
    source_frame_offset INTEGER NOT NULL,
    UNIQUE (capture_run_id, ingest_sequence)
);

-- rejection_id is a grouping key, not a unique key (ADR-0011 section 7): two
-- different malformed entries in one frame, refused for the same reason
-- against the same archived frame hash, land on one rejection_id because
-- there is no within-payload discriminator in _rejection_identity.
-- Uniqueness lives on (capture_run_id, ingest_sequence) instead, so both
-- rows survive.
CREATE TABLE IF NOT EXISTS rejection (
    id INTEGER PRIMARY KEY,
    capture_run_id TEXT NOT NULL,
    ingest_sequence INTEGER NOT NULL,
    rejection_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    source TEXT NOT NULL,
    raw_payload_sha256 TEXT NOT NULL,
    received_time TEXT NOT NULL,
    rejected_at TEXT NOT NULL,
    duplicate_of_observation_id TEXT REFERENCES observation (observation_id),
    record TEXT NOT NULL,
    UNIQUE (capture_run_id, ingest_sequence)
);
"""

_INSERT_OBSERVATION_SQL = """
INSERT INTO observation (
    observation_id, schema_version, payload_schema_version, source,
    market_id, condition_id, token_id, event_time, raw_payload_sha256,
    first_seen_capture_run_id, first_seen_ingest_sequence, first_seen_received_time,
    record
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_DELIVERY_SQL = """
INSERT INTO delivery (
    capture_run_id, ingest_sequence, observation_id, received_time,
    disposition, source_frame_sha256, source_frame_offset
) VALUES (?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_REJECTION_SQL = """
INSERT INTO rejection (
    capture_run_id, ingest_sequence, rejection_id, reason, source,
    raw_payload_sha256, received_time, rejected_at, duplicate_of_observation_id,
    record
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_CAPTURE_RUN_SQL = """
INSERT INTO capture_run (capture_run_id, started_at, ended_at, completion_status)
VALUES (?, ?, ?, ?)
"""


def open_sqlite_event_store(path: str | Path) -> SQLiteEventStore:
    """Open (or create) a SQLite event store at ``path``.

    ``path`` may be ``":memory:"``. Every pragma ADR-0011 requires is set
    explicitly here rather than assumed: ``journal_mode=WAL`` (recovers to
    the last committed transaction after an interrupted capture, which is
    what makes an open ``capture_run`` row a genuine crash signal, not a
    torn write); ``synchronous=FULL`` (``docs/02_ARCHITECTURE.md``: "fail
    loudly when it cannot prove the integrity of a capture"); and
    ``foreign_keys=ON`` (SQLite defaults this off per connection — without
    it, ``delivery.observation_id`` and ``rejection.duplicate_of_observation_id``
    would silently accept a reference to nothing).

    The connection is created fresh by this call and owned by the returned
    store; nothing about it is cached at import time or module scope (the
    ADR-0009 hidden-global-state defect, in a new place, is exactly what
    that would reproduce).
    """
    connection = sqlite3.connect(str(path), isolation_level=None)
    journal_mode = _pragma_result(connection, "PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    synchronous = _pragma_result(connection, "PRAGMA synchronous")
    foreign_keys = _pragma_result(connection, "PRAGMA foreign_keys")

    # SQLite does not error on a refused journal-mode change; it returns the
    # mode actually in effect. Security review measured ":memory:" silently
    # staying on "memory" while this function's docstring claimed WAL, and the
    # same silent decline happens on filesystems without shared-memory support.
    # ADR-0011 hangs the M2 "an interrupted capture closes or marks its manifest
    # incomplete" criterion on WAL recovery, so an unverified pragma is a
    # durability claim in prose rather than a property.
    expected_journal_mode = "memory" if str(path) == ":memory:" else "wal"
    if journal_mode != expected_journal_mode:
        connection.close()
        raise StorageError(
            "sqlite refused the required journal mode",
            requested=expected_journal_mode,
            in_effect=str(journal_mode),
        )
    if synchronous != _SYNCHRONOUS_FULL:
        connection.close()
        raise StorageError("sqlite refused synchronous=FULL", in_effect=str(synchronous))
    if foreign_keys != 1:
        connection.close()
        raise StorageError("sqlite refused foreign_keys=ON", in_effect=str(foreign_keys))
    return SQLiteEventStore(connection)


@contextmanager
def _decoding(what: str, **context: object) -> Iterator[None]:
    """Turn any failure to decode a stored row into a :class:`StorageError`.

    Security review measured six corruption shapes escaping the taxonomy as
    foreign exceptions — ``json.JSONDecodeError`` on a non-JSON ``record``,
    ``ValueError`` on a corrupt ``received_time`` or an unknown enum value,
    ``TypeError`` on a NULL ``started_at`` — while the module docstring
    claimed identity mismatch on read raises ``StorageError``. Only that one
    case was translated.

    This is the M1 finding in this same package reopened: ``read_raw_payload``
    leaked ``FileNotFoundError``/``JSONDecodeError`` until M1 fixed it, so a
    caller could count and explain the failure (core invariant 14).

    A row that cannot be decoded is a storage-integrity failure, not a caller
    error, so even a ``ContractViolationError`` from ``from_record`` becomes
    ``StorageError`` here — the stored bytes disagree with their own contract,
    and the caller did nothing wrong by reading them.
    """
    try:
        yield
    except StorageError:
        raise
    except Exception as error:
        raise StorageError(
            "stored row could not be decoded",
            what=what,
            error_type=type(error).__name__,
            error=str(error),
            **context,
        ) from error


def _validate_capture_run_id(capture_run_id: str) -> None:
    """Hold ``capture_run_id`` to the same contract the envelope holds it to.

    Security review measured this boundary storing, unbounded and
    unneutralized, a value ``ObservationEnvelopeV1._validate_identifier``
    refuses for the same field name: an OSC 52 clipboard write and an RLO
    override survived verbatim into ``iter_open_capture_runs()``, which is
    precisely the "interrupted capture" report a future renderer will print,
    and a 20,000,000-character id was accepted in 0.168 s. That is the M2
    HIGH finding on ``market_id``/``condition_id``/``token_id`` relocated to
    a new boundary, the same way the M1 gzip bomb relocated downstream of the
    byte cap that fixed it.

    A secondary consequence made the gap self-evident: a run opened under a
    hostile id could never receive an observation at all, because the envelope
    validator rejects the matching ``capture_run_id``. The store was accepting
    run ids that were structurally unusable.

    Refusal rather than neutralization, matching the accepted-observation path:
    neutralization is lossy, and two different hostile ids would collapse onto
    one stored run.
    """
    if not capture_run_id:
        raise ContractViolationError("capture_run_id must not be empty")
    if len(capture_run_id) > MAX_IDENTIFIER_LENGTH:
        raise ContractViolationError(
            "capture_run_id exceeds the identifier length limit",
            limit=MAX_IDENTIFIER_LENGTH,
            supplied=len(capture_run_id),
        )
    if not is_clean_identifier(capture_run_id):
        raise ContractViolationError(
            "capture_run_id contains a display-control character; refused rather "
            "than neutralized because neutralization is lossy and this value "
            "names the run every delivery row points at"
        )


def _pragma_result(connection: sqlite3.Connection, statement: str) -> object:
    """Return what a pragma actually left in effect, not what was asked for."""
    row = connection.execute(statement).fetchone()
    return None if row is None else row[0]


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Run one block as an atomic unit.

    A raw ``sqlite3.Error`` is translated into :class:`StorageError` so
    nothing escapes the ARGOS taxonomy (core invariant 14); an error this
    module raised deliberately (:class:`ContractViolationError`,
    :class:`StorageError`, :class:`ImmutabilityViolationError`) is rolled
    back and re-raised unchanged. Both paths roll back before propagating —
    "detecting the duplicate and recording its arrival must both land or
    neither" (ADR-0011 section 2) applies to every write in this module, not
    only the observation/delivery pair.
    """
    # BEGIN IMMEDIATE is inside the try, not before it. Both reviews of this
    # slice independently found it outside: taking the write lock is the single
    # statement most likely to fail in a real capture ("database is locked"
    # against a concurrent writer, "Cannot operate on a closed database" after
    # close()), and both are sqlite3.Error subclasses that escaped as foreign
    # exceptions. A capture loop cannot count what it cannot classify (core
    # invariant 14).
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
    except sqlite3.Error as error:
        connection.rollback()
        raise StorageError("sqlite operation failed", error=str(error)) from error
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


class SQLiteEventStore:
    """SQLite/WAL adapter behind the :class:`EventStore` port (ADR-0011)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        connection.executescript(_SCHEMA_SQL)

    def close(self) -> None:
        self._connection.close()

    # --- capture_run ---------------------------------------------------------

    def open_capture_run(self, capture_run_id: str, *, started_at: datetime) -> None:
        _validate_capture_run_id(capture_run_id)
        connection = self._connection
        with _transaction(connection):
            status = _capture_run_status(connection, capture_run_id)
            if status != "missing":
                raise StorageError(
                    "capture_run_id has already been opened",
                    capture_run_id=capture_run_id,
                    status=status,
                )
            connection.execute(
                _INSERT_CAPTURE_RUN_SQL,
                (capture_run_id, ensure_utc(started_at).isoformat(), None, None),
            )

    def close_capture_run(
        self,
        capture_run_id: str,
        *,
        ended_at: datetime,
        completion_status: CompletionStatus,
    ) -> None:
        connection = self._connection
        with _transaction(connection):
            status = _capture_run_status(connection, capture_run_id)
            if status == "missing":
                raise StorageError("no such capture_run", capture_run_id=capture_run_id)
            if status == "closed":
                raise ImmutabilityViolationError(
                    "capture_run is already closed; its completion cannot be "
                    "recorded a second time",
                    capture_run_id=capture_run_id,
                )
            connection.execute(
                _INSERT_CAPTURE_RUN_SQL,
                (capture_run_id, None, ensure_utc(ended_at).isoformat(), completion_status.value),
            )

    def get_capture_run(self, capture_run_id: str) -> CaptureRunRecord | None:
        connection = self._connection
        open_row = connection.execute(
            "SELECT started_at FROM capture_run WHERE capture_run_id = ? AND ended_at IS NULL",
            (capture_run_id,),
        ).fetchone()
        closed_row = connection.execute(
            "SELECT ended_at, completion_status FROM capture_run "
            "WHERE capture_run_id = ? AND ended_at IS NOT NULL",
            (capture_run_id,),
        ).fetchone()
        if open_row is None and closed_row is None:
            return None
        if open_row is None:
            # Closing always requires an already-open row (_capture_run_status
            # is checked in the same transaction as every close), so this
            # means the opening row itself is gone -- storage corruption, not
            # a state this store can ever legitimately produce.
            raise StorageError(
                "capture_run has a closing row but no opening row",
                capture_run_id=capture_run_id,
            )
        started_raw: str = open_row[0]
        ended_at: datetime | None = None
        completion_status: CompletionStatus | None = None
        with _decoding("capture_run", capture_run_id=capture_run_id):
            if closed_row is not None:
                ended_raw: str = closed_row[0]
                status_raw: str = closed_row[1]
                ended_at = ensure_utc(datetime.fromisoformat(ended_raw))
                completion_status = CompletionStatus(status_raw)
            return CaptureRunRecord(
                capture_run_id=capture_run_id,
                started_at=ensure_utc(datetime.fromisoformat(started_raw)),
                ended_at=ended_at,
                completion_status=completion_status,
            )

    def iter_open_capture_runs(self) -> Iterator[CaptureRunRecord]:
        connection = self._connection
        rows = connection.execute(
            "SELECT capture_run_id, started_at FROM capture_run WHERE ended_at IS NULL "
            "AND capture_run_id NOT IN "
            "(SELECT capture_run_id FROM capture_run WHERE ended_at IS NOT NULL)"
        ).fetchall()
        for capture_run_id, started_at in rows:
            run_id: str = capture_run_id
            started_raw: str = started_at
            with _decoding("capture_run", capture_run_id=run_id):
                record = CaptureRunRecord(
                    capture_run_id=run_id,
                    started_at=ensure_utc(datetime.fromisoformat(started_raw)),
                    ended_at=None,
                    completion_status=None,
                )
            yield record

    # --- observation / delivery -----------------------------------------------

    def append_observation(
        self,
        envelope: ObservationEnvelopeV1,
        *,
        source_frame_offset: int = 0,
    ) -> DeliveryRecord:
        _verify_observation_identity(envelope)
        record_text = _dump_record(envelope.to_record())
        connection = self._connection
        result: DeliveryRecord
        with _transaction(connection):
            _require_open_capture_run(connection, envelope.capture_run_id)
            _reserve_ingest_sequence(connection, envelope.capture_run_id, envelope.ingest_sequence)
            existing = connection.execute(
                "SELECT 1 FROM observation WHERE observation_id = ?", (envelope.observation_id,)
            ).fetchone()
            disposition = (
                Disposition.DUPLICATE if existing is not None else Disposition.ACCEPTED_NEW
            )
            if existing is None:
                connection.execute(
                    _INSERT_OBSERVATION_SQL,
                    (
                        envelope.observation_id,
                        ObservationEnvelopeV1.schema_version,
                        envelope.payload_schema_version,
                        envelope.source.value,
                        envelope.market_id,
                        envelope.condition_id,
                        envelope.token_id,
                        envelope.event_time.isoformat() if envelope.event_time else None,
                        envelope.raw_payload_sha256,
                        envelope.capture_run_id,
                        envelope.ingest_sequence,
                        envelope.received_time.isoformat(),
                        record_text,
                    ),
                )
            connection.execute(
                _INSERT_DELIVERY_SQL,
                (
                    envelope.capture_run_id,
                    envelope.ingest_sequence,
                    envelope.observation_id,
                    envelope.received_time.isoformat(),
                    disposition.value,
                    envelope.raw_payload_sha256,
                    source_frame_offset,
                ),
            )
            result = DeliveryRecord(
                capture_run_id=envelope.capture_run_id,
                ingest_sequence=envelope.ingest_sequence,
                observation_id=envelope.observation_id,
                received_time=envelope.received_time,
                disposition=disposition,
                source_frame_sha256=envelope.raw_payload_sha256,
                source_frame_offset=source_frame_offset,
            )
        return result

    def get_observation(self, observation_id: str) -> ObservationEnvelopeV1 | None:
        row = self._connection.execute(
            "SELECT record FROM observation WHERE observation_id = ?", (observation_id,)
        ).fetchone()
        if row is None:
            return None
        record_text: str = row[0]
        with _decoding("observation", observation_id=observation_id):
            record: dict[str, Any] = json.loads(record_text)
            envelope = ObservationEnvelopeV1.from_record(record)
        recomputed = recompute_observation_id(envelope)
        if recomputed != envelope.observation_id:
            raise StorageError(
                "stored observation failed identity verification on read; the "
                "record disagrees with its own recomputed identity",
                observation_id=envelope.observation_id,
                recomputed=recomputed,
            )
        return envelope

    def iter_deliveries(self, capture_run_id: str) -> Iterator[DeliveryRecord]:
        cursor = self._connection.execute(
            "SELECT capture_run_id, ingest_sequence, observation_id, received_time, "
            "disposition, source_frame_sha256, source_frame_offset FROM delivery "
            "WHERE capture_run_id = ? ORDER BY ingest_sequence ASC",
            (capture_run_id,),
        )
        for row in cursor:
            run_id: str = row[0]
            ingest_sequence: int = row[1]
            observation_id: str = row[2]
            received_raw: str = row[3]
            disposition_raw: str = row[4]
            source_frame_sha256: str = row[5]
            source_frame_offset: int = row[6]
            with _decoding("delivery", capture_run_id=run_id, ingest_sequence=ingest_sequence):
                delivery = DeliveryRecord(
                    capture_run_id=run_id,
                    ingest_sequence=ingest_sequence,
                    observation_id=observation_id,
                    received_time=ensure_utc(datetime.fromisoformat(received_raw)),
                    disposition=Disposition(disposition_raw),
                    source_frame_sha256=source_frame_sha256,
                    source_frame_offset=source_frame_offset,
                )
            yield delivery

    # --- rejection -------------------------------------------------------------

    def append_rejection(
        self,
        rejection: RejectedObservationV1,
        *,
        ingest_sequence: int,
        duplicate_of_observation_id: str | None = None,
    ) -> None:
        if ingest_sequence <= 0:
            raise ContractViolationError(
                "ingest_sequence must be positive", ingest_sequence=ingest_sequence
            )
        _verify_rejection_identity(rejection)
        record_text = _dump_record(rejection.to_record())
        connection = self._connection
        with _transaction(connection):
            _require_open_capture_run(connection, rejection.capture_run_id)
            _reserve_ingest_sequence(connection, rejection.capture_run_id, ingest_sequence)
            connection.execute(
                _INSERT_REJECTION_SQL,
                (
                    rejection.capture_run_id,
                    ingest_sequence,
                    rejection.rejection_id,
                    rejection.reason.value,
                    rejection.source.value,
                    rejection.raw_payload_sha256,
                    rejection.received_time.isoformat(),
                    rejection.rejected_at.isoformat(),
                    duplicate_of_observation_id,
                    record_text,
                ),
            )

    def iter_rejections(self, capture_run_id: str) -> Iterator[RejectionRecord]:
        cursor = self._connection.execute(
            "SELECT capture_run_id, ingest_sequence, duplicate_of_observation_id, record "
            "FROM rejection WHERE capture_run_id = ? ORDER BY ingest_sequence ASC",
            (capture_run_id,),
        )
        for row in cursor:
            run_id: str = row[0]
            ingest_sequence: int = row[1]
            duplicate_of: str | None = row[2]
            record_text: str = row[3]
            with _decoding("rejection", capture_run_id=run_id, ingest_sequence=ingest_sequence):
                record: dict[str, Any] = json.loads(record_text)
                rejection = RejectedObservationV1.from_record(record)
            recomputed = recompute_rejection_id(rejection)
            if recomputed != rejection.rejection_id:
                raise StorageError(
                    "stored rejection failed identity verification on read; the "
                    "record disagrees with its own recomputed identity",
                    rejection_id=rejection.rejection_id,
                    recomputed=recomputed,
                )
            yield RejectionRecord(
                capture_run_id=run_id,
                ingest_sequence=ingest_sequence,
                duplicate_of_observation_id=duplicate_of,
                rejection=rejection,
            )

    # --- derived aggregates ------------------------------------------------------

    def counts_for_capture_run(self, capture_run_id: str) -> CaptureRunCounts:
        connection = self._connection
        accepted_row = connection.execute(
            "SELECT COUNT(*) FROM delivery WHERE capture_run_id = ? AND disposition = ?",
            (capture_run_id, Disposition.ACCEPTED_NEW.value),
        ).fetchone()
        duplicate_row = connection.execute(
            "SELECT COUNT(*) FROM delivery WHERE capture_run_id = ? AND disposition = ?",
            (capture_run_id, Disposition.DUPLICATE.value),
        ).fetchone()
        rejected_row = connection.execute(
            "SELECT COUNT(*) FROM rejection WHERE capture_run_id = ?", (capture_run_id,)
        ).fetchone()
        accepted: int = accepted_row[0]
        duplicate: int = duplicate_row[0]
        rejected: int = rejected_row[0]
        return CaptureRunCounts(accepted=accepted, duplicate=duplicate, rejected=rejected)


def _capture_run_status(connection: sqlite3.Connection, capture_run_id: str) -> str:
    """Return ``"missing"``, ``"open"``, or ``"closed"`` for ``capture_run_id``."""
    has_open = (
        connection.execute(
            "SELECT 1 FROM capture_run WHERE capture_run_id = ? AND ended_at IS NULL",
            (capture_run_id,),
        ).fetchone()
        is not None
    )
    has_closed = (
        connection.execute(
            "SELECT 1 FROM capture_run WHERE capture_run_id = ? AND ended_at IS NOT NULL",
            (capture_run_id,),
        ).fetchone()
        is not None
    )
    if has_closed:
        return "closed"
    if has_open:
        return "open"
    return "missing"


def _require_open_capture_run(connection: sqlite3.Connection, capture_run_id: str) -> None:
    status = _capture_run_status(connection, capture_run_id)
    if status == "missing":
        raise StorageError(
            "no such capture_run; open_capture_run must be called first",
            capture_run_id=capture_run_id,
        )
    if status == "closed":
        raise StorageError(
            "capture_run is already closed; it cannot accept a new arrival",
            capture_run_id=capture_run_id,
        )


def _reserve_ingest_sequence(
    connection: sqlite3.Connection, capture_run_id: str, ingest_sequence: int
) -> None:
    """Refuse a reused ``ingest_sequence``.

    ``delivery`` and ``rejection`` draw from one counter per capture run
    (ADR-0011 section 5); each has its own ``UNIQUE(capture_run_id,
    ingest_sequence)`` index, but that alone cannot see across the two
    tables. This check does, inside the same transaction as the insert it
    guards.
    """
    row = connection.execute(
        "SELECT 1 FROM delivery WHERE capture_run_id = ? AND ingest_sequence = ? "
        "UNION SELECT 1 FROM rejection WHERE capture_run_id = ? AND ingest_sequence = ?",
        (capture_run_id, ingest_sequence, capture_run_id, ingest_sequence),
    ).fetchone()
    if row is not None:
        raise StorageError(
            "ingest_sequence is already used by another arrival in this capture run",
            capture_run_id=capture_run_id,
            ingest_sequence=ingest_sequence,
        )


def _verify_observation_identity(envelope: ObservationEnvelopeV1) -> None:
    recomputed = recompute_observation_id(envelope)
    if recomputed != envelope.observation_id:
        raise ContractViolationError(
            "envelope.observation_id does not match its own recomputed identity; "
            "refusing to store a forged or corrupted identity",
            claimed=envelope.observation_id,
            recomputed=recomputed,
        )


def _verify_rejection_identity(rejection: RejectedObservationV1) -> None:
    recomputed = recompute_rejection_id(rejection)
    if recomputed != rejection.rejection_id:
        raise ContractViolationError(
            "rejection.rejection_id does not match its own recomputed identity; "
            "refusing to store a forged or corrupted identity",
            claimed=rejection.rejection_id,
            recomputed=recomputed,
        )


def _dump_record(record: dict[str, Any]) -> str:
    """Deterministic JSON text for a stored ``record`` column.

    ``record`` always comes from ``VersionedModel.to_record()``, which dumps
    in JSON mode, so every value is already JSON-native; this only fixes key
    order and whitespace so the same record serializes identically regardless
    of dict insertion order.
    """
    return json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
