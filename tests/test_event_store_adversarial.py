"""Independent adversarial coverage for the SQLite/WAL event store (ADR-0011).

``tests/test_event_store.py`` already closes the exit-criteria-facing cases.
This file's job is different: attack the properties that file's own author
could not see from inside the design -- concurrency between two real
connections to the same file, a process dying mid-transaction, the
append-only claim holding through the *public API* rather than only through
the source-text scan in ``tests/test_boundaries.py``, the ``capture_run``
partial-unique-index lifecycle, cross-table ``ingest_sequence`` integrity
under a real race, identity verification on the rejection path (the
observation path already has a whitebox test; the rejection path does not),
and round-trip fidelity for shapes the existing fixtures do not exercise
(a *present* event time, fully absent optional fields, non-ASCII payload
text). It also independently attacks ``argos.store.raw_archive``'s new
fsync behaviour (ADR-0011 section 4).

Every finding that could not be fixed here (this file owns no source changes)
is reproduced as a passing test that pins down and demonstrates the current
behaviour, so it can be judged on evidence rather than description.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest

import argos.store.raw_archive as raw_archive_module
from argos.domain.observation import (
    ObservationEnvelopeV1,
    ObservationSource,
    RejectedObservationV1,
    build_observation_envelope,
    build_rejected_observation,
)
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.domain.versioning import VersionedModel
from argos.errors import RejectionReason, StorageError
from argos.store.event_store import (
    CompletionStatus,
    DeliveryRecord,
    Disposition,
    SQLiteEventStore,
    open_sqlite_event_store,
)
from argos.store.raw_archive import write_raw_payload

START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
RUN = "capture-run-1"

WS_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "research"
    / "fixtures"
    / "clob-ws-market-2026-08-10T184742Z.json"
)


class _Payload(VersionedModel):
    schema_version: ClassVar[str] = "test_event_store_adversarial_payload.v1"

    label: str = "x"


class _UnicodePayload(VersionedModel):
    schema_version: ClassVar[str] = "test_event_store_adversarial_unicode_payload.v1"

    note: str


def _provenance(
    raw: bytes, *, source: str = "clob_market_ws", **overrides: Any
) -> SourceProvenanceV1:
    fields: dict[str, Any] = {
        "source": source,
        "endpoint": "https://clob.polymarket.com/ws",
        "http_status": None,
        "retrieved_at": START,
        "raw_sha256": sha256_hex(raw),
        "byte_length": len(raw),
    }
    fields.update(overrides)
    return SourceProvenanceV1(**fields)


def _envelope(
    *,
    ingest_sequence: int,
    capture_run_id: str = RUN,
    label: str = "x",
    provenance: SourceProvenanceV1 | None = None,
    **overrides: Any,
) -> ObservationEnvelopeV1:
    raw = f"raw-{label}".encode()
    fields: dict[str, Any] = {
        "source": ObservationSource.CLOB_MARKET_WS,
        "source_event_type": "price_change",
        "market_id": None,
        "condition_id": None,
        "token_id": "token-1",
        "event_time": None,
        "received_time": START,
        "ingest_sequence": ingest_sequence,
        "payload": _Payload(label=label),
        "provenance": provenance or _provenance(raw),
        "parser_version": "test-parser.v1",
        "capture_run_id": capture_run_id,
    }
    fields.update(overrides)
    return build_observation_envelope(**fields)


def _rejection(
    *,
    detail: str = "malformed",
    capture_run_id: str = RUN,
    raw: bytes = b"bad-bytes",
    reason: RejectionReason = RejectionReason.MALFORMED_PAYLOAD,
    **overrides: Any,
) -> RejectedObservationV1:
    fields: dict[str, Any] = {
        "reason": reason,
        "detail": detail,
        "source": ObservationSource.CLOB_MARKET_WS,
        "source_event_type": "price_change",
        "market_id": None,
        "condition_id": None,
        "token_id": None,
        "provenance": _provenance(raw),
        "received_time": START,
        "rejected_at": START,
        "capture_run_id": capture_run_id,
    }
    fields.update(overrides)
    return build_rejected_observation(**fields)


def _connect(path: Path, *, timeout: float = 5.0) -> sqlite3.Connection:
    """Build a connection with exactly the pragmas ``open_sqlite_event_store`` sets.

    Needed for the whitebox/concurrency tests below, which must open more than
    one connection to the same on-disk file -- something the public
    ``open_sqlite_event_store`` factory (one connection per call, no path
    reuse contract) does not forbid but is never exercised by in the existing
    suite, which uses ``:memory:`` throughout.
    """
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=timeout)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@pytest.fixture
def store() -> Iterator[SQLiteEventStore]:
    event_store = open_sqlite_event_store(":memory:")
    event_store.open_capture_run(RUN, started_at=START)
    yield event_store
    event_store.close()


# =====================================================================================
# 1. Idempotency under concurrency and failure
# =====================================================================================


def test_two_real_connections_racing_to_insert_the_same_observation_yield_exactly_one_row(
    tmp_path: Path,
) -> None:
    """Two separate OS-level connections, two OS threads, one file. Not a
    simulation: SQLite's own write lock is what has to make this safe, and
    this test lets it actually contend rather than asserting it would."""
    path = tmp_path / "race.db"
    setup = open_sqlite_event_store(path)
    setup.open_capture_run(RUN, started_at=START)
    setup.close()

    shared_provenance = _provenance(b"same-frame-raced")
    envelope_a = _envelope(ingest_sequence=1, provenance=shared_provenance)
    envelope_b = _envelope(ingest_sequence=2, provenance=shared_provenance)
    assert envelope_a.observation_id == envelope_b.observation_id

    barrier = threading.Barrier(2)
    out_a: list[object] = []
    out_b: list[object] = []

    def worker(envelope: ObservationEnvelopeV1, out: list[object]) -> None:
        connection = _connect(path)
        thread_store = SQLiteEventStore(connection)
        barrier.wait()
        try:
            out.append(thread_store.append_observation(envelope))
        except Exception as error:
            out.append(error)
        finally:
            connection.close()

    t1 = threading.Thread(target=worker, args=(envelope_a, out_a))
    t2 = threading.Thread(target=worker, args=(envelope_b, out_b))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert not t1.is_alive() and not t2.is_alive(), "a worker thread never finished"
    results = out_a + out_b
    assert len(results) == 2, f"a worker raised nothing and appended nothing: {results!r}"
    dispositions = [r.disposition.value for r in results if isinstance(r, DeliveryRecord)]
    assert sorted(dispositions) == ["accepted_new", "duplicate"], results

    check = sqlite3.connect(str(path))
    try:
        observation_rows = check.execute("SELECT COUNT(*) FROM observation").fetchone()[0]
        delivery_rows = check.execute("SELECT COUNT(*) FROM delivery").fetchone()[0]
    finally:
        check.close()
    assert observation_rows == 1
    assert delivery_rows == 2


def test_a_second_writer_blocked_by_the_first_never_observes_a_partial_row(
    tmp_path: Path,
) -> None:
    """While connection A holds the ``BEGIN IMMEDIATE`` write lock mid-insert,
    connection B must see either nothing (if it looks before A commits) or the
    complete post-commit state -- never an observation row with no delivery
    row, which is the concrete shape core invariant 14 forbids for this store.

    That part always held. Reproducing it also surfaced a defect, **since
    fixed**: ``_transaction`` issued ``BEGIN IMMEDIATE`` *before* its own
    ``try:`` block, so the one statement most likely to fail in a real capture
    -- "database is locked" against a second writer -- escaped as a bare
    ``sqlite3.OperationalError`` through every public method built on it,
    rather than the documented ``StorageError``. Security review found the
    same defect independently, plus a second instance of it ("Cannot operate
    on a closed database"). This test now pins the taxonomy, because a capture
    loop cannot count what it cannot classify (core invariant 14)."""
    path = tmp_path / "blocked.db"
    setup = open_sqlite_event_store(path)
    setup.open_capture_run(RUN, started_at=START)
    setup.close()

    holder = _connect(path)
    holder.execute("BEGIN IMMEDIATE")
    # Hold the write lock without committing -- this is connection A "mid-insert".

    reader = sqlite3.connect(str(path), timeout=0.2)
    # A plain read against the main db file under WAL does not need the write
    # lock, so this must succeed and see the pre-transaction state.
    assert reader.execute("SELECT COUNT(*) FROM observation").fetchone()[0] == 0
    reader.close()

    blocked_writer = _connect(path, timeout=0.2)
    blocked_store = SQLiteEventStore(blocked_writer)
    with pytest.raises(StorageError) as blocked:
        blocked_store.append_observation(_envelope(ingest_sequence=1))
    # The underlying sqlite message is preserved in the error's context, not
    # flattened away -- a capture loop has to be able to tell lock contention
    # (retryable) from a schema mismatch (not) without parsing prose.
    assert "database is locked" in str(blocked.value.context["error"])
    blocked_writer.close()

    holder.rollback()
    holder.close()

    # No partial row was left behind -- the underlying atomicity claim this
    # test set out to check holds, and held even while the taxonomy did not.
    verify = open_sqlite_event_store(path)
    assert verify.counts_for_capture_run(RUN).accepted == 0
    result = verify.append_observation(_envelope(ingest_sequence=1))
    assert result.disposition is Disposition.ACCEPTED_NEW
    verify.close()


class _RaisingConnection(sqlite3.Connection):
    """A connection that raises once a chosen SQL substring is executed.

    Simulates "the process died mid-transaction": the interpreter would not
    get to catch anything in a real crash, but the store's transaction
    boundary (``BEGIN IMMEDIATE`` ... commit-or-rollback) is what is under
    test, and an uncaught exception inside that boundary is the faithful
    reproduction of a crash for a rollback-on-failure design -- SQLite itself
    guarantees the same recovery for a real process kill via the WAL.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fail_on_substring: str | None = None
        self.triggered = False

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        should_fail = (
            self.fail_on_substring is not None
            and self.fail_on_substring in sql
            and not self.triggered
        )
        if should_fail:
            self.triggered = True
            raise RuntimeError("simulated process crash mid-transaction")
        return super().execute(sql, parameters)


def _raising_connect(path: Path) -> _RaisingConnection:
    connection = sqlite3.connect(
        str(path), isolation_level=None, timeout=5.0, factory=_RaisingConnection
    )
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@pytest.mark.parametrize("failing_statement", ["INSERT INTO observation", "INSERT INTO delivery"])
def test_a_crash_during_the_first_arrivals_own_insert_leaves_no_row_at_all(
    tmp_path: Path, failing_statement: str
) -> None:
    """Whichever half of the accepted-new write fails, reopening the database
    file from scratch (a fresh connection, as a restarted process would) must
    show zero observation rows and zero delivery rows -- never one without
    the other."""
    path = tmp_path / "crash_new.db"
    connection = _raising_connect(path)
    store_under_test = SQLiteEventStore(connection)
    store_under_test.open_capture_run(RUN, started_at=START)

    envelope = _envelope(ingest_sequence=1)
    connection.fail_on_substring = failing_statement
    with pytest.raises(RuntimeError):
        store_under_test.append_observation(envelope)
    connection.close()

    reopened = open_sqlite_event_store(path)
    assert reopened.get_observation(envelope.observation_id) is None
    counts = reopened.counts_for_capture_run(RUN)
    assert counts.accepted == 0
    assert counts.duplicate == 0
    # The run-open row is from an earlier, already-committed transaction and
    # must survive untouched.
    run = reopened.get_capture_run(RUN)
    assert run is not None
    assert run.is_open

    # The crash is recoverable: retrying with the same ingest_sequence (a
    # real capture loop retries the same arrival after a restart) now
    # succeeds cleanly, because nothing was left behind to collide with.
    retried = reopened.append_observation(_envelope(ingest_sequence=1))
    assert retried.disposition is Disposition.ACCEPTED_NEW
    reopened.close()


def test_a_crash_during_a_duplicates_own_delivery_insert_leaves_the_first_arrival_untouched(
    tmp_path: Path,
) -> None:
    """This is the exact case ADR-0011 section 2 names: 'detecting the
    duplicate and recording its arrival must both land or neither.' The first
    arrival is already committed in an earlier transaction; only the second
    (duplicate) arrival's delivery insert is made to fail."""
    path = tmp_path / "crash_dup.db"
    connection = _raising_connect(path)
    store_under_test = SQLiteEventStore(connection)
    store_under_test.open_capture_run(RUN, started_at=START)

    first = _envelope(ingest_sequence=1)
    store_under_test.append_observation(first)

    second = _envelope(ingest_sequence=2, received_time=START + timedelta(minutes=1))
    assert first.observation_id == second.observation_id
    connection.fail_on_substring = "INSERT INTO delivery"
    with pytest.raises(RuntimeError):
        store_under_test.append_observation(second)
    connection.close()

    reopened = open_sqlite_event_store(path)
    counts = reopened.counts_for_capture_run(RUN)
    assert counts.accepted == 1
    assert counts.duplicate == 0

    check = sqlite3.connect(str(path))
    try:
        assert check.execute("SELECT COUNT(*) FROM observation").fetchone()[0] == 1
        assert check.execute("SELECT COUNT(*) FROM delivery").fetchone()[0] == 1
    finally:
        check.close()
    reopened.close()


def test_a_crash_during_append_rejection_leaves_no_rejection_row(tmp_path: Path) -> None:
    path = tmp_path / "crash_rejection.db"
    connection = _raising_connect(path)
    store_under_test = SQLiteEventStore(connection)
    store_under_test.open_capture_run(RUN, started_at=START)

    connection.fail_on_substring = "INSERT INTO rejection"
    with pytest.raises(RuntimeError):
        store_under_test.append_rejection(_rejection(), ingest_sequence=1)
    connection.close()

    reopened = open_sqlite_event_store(path)
    assert reopened.counts_for_capture_run(RUN).rejected == 0
    # ingest_sequence=1 was never actually reserved (its transaction rolled
    # back), so it is free to use again.
    reopened.append_rejection(_rejection(), ingest_sequence=1)
    assert reopened.counts_for_capture_run(RUN).rejected == 1
    reopened.close()


# =====================================================================================
# 2. The append-only claim, attacked behaviourally rather than by source-text scan
# =====================================================================================


def test_a_refused_second_open_never_changes_the_first_opens_started_at(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reopen.db"
    event_store = open_sqlite_event_store(path)
    event_store.open_capture_run(RUN, started_at=START)
    with pytest.raises(StorageError):
        event_store.open_capture_run(RUN, started_at=START + timedelta(days=1))

    run = event_store.get_capture_run(RUN)
    assert run is not None
    assert run.started_at == START  # not the rejected second value

    check = sqlite3.connect(str(path))
    try:
        rows = check.execute(
            "SELECT started_at, ended_at FROM capture_run WHERE capture_run_id = ?", (RUN,)
        ).fetchall()
    finally:
        check.close()
    assert rows == [(START.isoformat(), None)]
    event_store.close()


def test_the_observation_record_column_is_byte_identical_before_and_after_a_duplicate_arrival(
    store: SQLiteEventStore,
) -> None:
    """No ``UPDATE``, ``INSERT OR REPLACE``, or ``REPLACE INTO`` can be
    hiding behind the append-only source-text scan and still be exercised
    from the public API: the stored ``record`` text for the first arrival's
    row must be the literal same bytes after a duplicate lands."""
    first = _envelope(ingest_sequence=1)
    store.append_observation(first)

    reloaded_before = store.get_observation(first.observation_id)
    assert reloaded_before is not None
    text_before = reloaded_before.to_record()

    second = _envelope(ingest_sequence=2, received_time=START + timedelta(hours=1))
    store.append_observation(second)

    reloaded_after = store.get_observation(first.observation_id)
    assert reloaded_after is not None
    assert reloaded_after.to_record() == text_before


def test_a_duplicate_arriving_under_different_raw_bytes_never_overwrites_the_raw_hash(
    store: SQLiteEventStore,
) -> None:
    """``observation_id`` excludes ``raw_payload_sha256`` by design (ADR-0010:
    identity is a hash of *normalized* payload content, not of the raw
    bytes). Two envelopes with identical normalized content but genuinely
    different raw bytes therefore collide as the 'same' observation. Confirm
    the stored observation pins the *first* arrival's raw hash and the second
    arrival's own raw hash only ever reaches its ``delivery`` row, never the
    ``observation`` row."""
    first = _envelope(ingest_sequence=1, provenance=_provenance(b"frame-A-bytes"))
    second = _envelope(
        ingest_sequence=2,
        received_time=START + timedelta(microseconds=196),
        provenance=_provenance(b"frame-B-different-bytes"),
    )
    assert first.observation_id == second.observation_id
    assert first.raw_payload_sha256 != second.raw_payload_sha256

    store.append_observation(first)
    delivery_of_second = store.append_observation(second)

    stored = store.get_observation(first.observation_id)
    assert stored is not None
    assert stored.raw_payload_sha256 == first.raw_payload_sha256
    assert stored.raw_payload_sha256 != second.raw_payload_sha256
    # The second arrival's own frame hash is recorded on its delivery row,
    # not lost and not smuggled into the observation row.
    assert delivery_of_second.source_frame_sha256 == second.raw_payload_sha256


def test_observation_filter_columns_are_never_cross_checked_against_the_record_on_read(
    tmp_path: Path,
) -> None:
    """Blind spot, not a contract violation: ``observation.market_id`` (and
    its siblings) are documented as denormalized 'for keys, filtering, and
    migration' (ADR-0011 section 3), and no ``EventStore`` method queries by
    them today. This test proves that fact rather than assuming it: tampering
    the ``market_id`` *column* alone (leaving ``record`` untouched) is
    invisible to ``get_observation``, which reads only ``record``. If a
    future slice adds a query keyed on these columns, it would query state
    that nothing here keeps consistent with the record it is supposed to
    describe."""
    path = tmp_path / "column_drift.db"
    event_store = open_sqlite_event_store(path)
    event_store.open_capture_run(RUN, started_at=START)
    envelope = _envelope(ingest_sequence=1, market_id="market-real")
    event_store.append_observation(envelope)
    event_store.close()

    connection = sqlite3.connect(str(path))
    connection.execute(
        "UPDATE observation SET market_id = 'market-FORGED' WHERE observation_id = ?",
        (envelope.observation_id,),
    )
    connection.commit()
    connection.close()

    reread = open_sqlite_event_store(path)
    reloaded = reread.get_observation(envelope.observation_id)
    assert reloaded is not None
    # The record (the actual source of truth for every read path) still says
    # what it always said -- the tampered column was never consulted.
    assert reloaded.market_id == "market-real"
    reread.close()


# =====================================================================================
# 3. The capture_run lifecycle
# =====================================================================================


def test_a_closed_run_cannot_be_reopened() -> None:
    event_store = open_sqlite_event_store(":memory:")
    event_store.open_capture_run(RUN, started_at=START)
    event_store.close_capture_run(
        RUN, ended_at=START + timedelta(minutes=1), completion_status=CompletionStatus.COMPLETED
    )
    with pytest.raises(StorageError) as caught:
        event_store.open_capture_run(RUN, started_at=START + timedelta(hours=1))
    assert caught.value.context.get("status") == "closed"
    event_store.close()


def test_iter_open_capture_runs_agrees_with_get_capture_run_across_a_mixed_batch() -> None:
    event_store = open_sqlite_event_store(":memory:")
    run_ids = [f"run-{i}" for i in range(6)]
    for run_id in run_ids:
        event_store.open_capture_run(run_id, started_at=START)
    # Close every other run.
    for run_id in run_ids[::2]:
        event_store.close_capture_run(
            run_id,
            ended_at=START + timedelta(minutes=1),
            completion_status=CompletionStatus.COMPLETED,
        )

    open_from_iterator = {record.capture_run_id for record in event_store.iter_open_capture_runs()}
    open_from_get = {run_id for run_id in run_ids if not _require_run(event_store, run_id).is_open}
    still_open_from_get = {
        run_id for run_id in run_ids if _require_run(event_store, run_id).is_open
    }
    assert open_from_iterator == still_open_from_get
    assert open_from_iterator.isdisjoint(open_from_get)
    assert open_from_iterator == set(run_ids[1::2])
    event_store.close()


def _require_run(event_store: SQLiteEventStore, run_id: str) -> Any:
    run = event_store.get_capture_run(run_id)
    assert run is not None
    return run


def test_closing_races_never_produce_two_closing_rows(tmp_path: Path) -> None:
    """The append-only design relies on a partial unique index
    (``capture_run_closed_once``) as the backstop under the Python status
    check ADR-0011 records as weaker than a database constraint. Race two
    real close attempts and confirm the index, not luck, decides it: exactly
    one succeeds and the table never holds two closing rows."""
    path = tmp_path / "close_race.db"
    setup = open_sqlite_event_store(path)
    setup.open_capture_run(RUN, started_at=START)
    setup.close()

    barrier = threading.Barrier(2)
    outcomes: list[object] = []
    lock = threading.Lock()

    def closer(status: CompletionStatus) -> None:
        connection = _connect(path)
        thread_store = SQLiteEventStore(connection)
        barrier.wait()
        try:
            thread_store.close_capture_run(
                RUN, ended_at=START + timedelta(minutes=1), completion_status=status
            )
            with lock:
                outcomes.append("ok")
        except Exception as error:
            with lock:
                outcomes.append(error)
        finally:
            connection.close()

    t1 = threading.Thread(target=closer, args=(CompletionStatus.COMPLETED,))
    t2 = threading.Thread(target=closer, args=(CompletionStatus.FAILED,))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert outcomes.count("ok") == 1
    assert len(outcomes) == 2

    check = sqlite3.connect(str(path))
    try:
        closed_rows = check.execute(
            "SELECT COUNT(*) FROM capture_run WHERE capture_run_id = ? AND ended_at IS NOT NULL",
            (RUN,),
        ).fetchone()[0]
    finally:
        check.close()
    assert closed_rows == 1


def test_a_delivery_racing_a_close_never_lands_against_an_already_closed_run(
    tmp_path: Path,
) -> None:
    """Whichever of {append, close} commits first fully determines what the
    other sees, because both run inside ``BEGIN IMMEDIATE``. There is no
    interleaving in which an accepted delivery row exists for a run whose
    close committed strictly before the append's own transaction began."""
    path = tmp_path / "append_close_race.db"
    setup = open_sqlite_event_store(path)
    setup.open_capture_run(RUN, started_at=START)
    setup.close()

    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}

    def appender() -> None:
        connection = _connect(path)
        thread_store = SQLiteEventStore(connection)
        barrier.wait()
        try:
            outcomes["append"] = thread_store.append_observation(_envelope(ingest_sequence=1))
        except Exception as error:
            outcomes["append"] = error
        finally:
            connection.close()

    def closer() -> None:
        connection = _connect(path)
        thread_store = SQLiteEventStore(connection)
        barrier.wait()
        try:
            thread_store.close_capture_run(
                RUN,
                ended_at=START + timedelta(minutes=1),
                completion_status=CompletionStatus.COMPLETED,
            )
            outcomes["close"] = "ok"
        except Exception as error:
            outcomes["close"] = error
        finally:
            connection.close()

    t1 = threading.Thread(target=appender)
    t2 = threading.Thread(target=closer)
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    verify = open_sqlite_event_store(path)
    run = verify.get_capture_run(RUN)
    assert run is not None
    accepted = verify.counts_for_capture_run(RUN).accepted
    if isinstance(outcomes.get("append"), DeliveryRecord):
        # The append committed. Whether or not the run is now closed, the
        # accepted row is real and countable -- it cannot have vanished.
        assert accepted == 1
    else:
        # The append was refused (the run closed first) -- no row of any
        # kind exists for it.
        assert accepted == 0
    # The close is unconditional in this race (nothing contends for the
    # closing-row slot) and must always have landed.
    assert outcomes.get("close") == "ok"
    assert not run.is_open
    verify.close()


# =====================================================================================
# 4. ingest_sequence integrity across delivery and rejection under a real race
# =====================================================================================


def test_a_delivery_and_a_rejection_racing_for_the_same_ingest_sequence_yield_exactly_one_winner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sequence_race.db"
    setup = open_sqlite_event_store(path)
    setup.open_capture_run(RUN, started_at=START)
    setup.close()

    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}

    def deliver() -> None:
        connection = _connect(path)
        thread_store = SQLiteEventStore(connection)
        barrier.wait()
        try:
            outcomes["delivery"] = thread_store.append_observation(_envelope(ingest_sequence=7))
        except Exception as error:
            outcomes["delivery"] = error
        finally:
            connection.close()

    def reject() -> None:
        connection = _connect(path)
        thread_store = SQLiteEventStore(connection)
        barrier.wait()
        try:
            thread_store.append_rejection(_rejection(), ingest_sequence=7)
            outcomes["rejection"] = "ok"
        except Exception as error:
            outcomes["rejection"] = error
        finally:
            connection.close()

    t1 = threading.Thread(target=deliver)
    t2 = threading.Thread(target=reject)
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    delivery_won = isinstance(outcomes.get("delivery"), DeliveryRecord)
    rejection_won = outcomes.get("rejection") == "ok"
    # Exactly one side claimed ingest_sequence=7; the loser must have been
    # refused rather than silently overwriting the winner.
    assert delivery_won != rejection_won, outcomes

    verify = open_sqlite_event_store(path)
    delivery_rows_at_7 = list(verify.iter_deliveries(RUN))
    rejection_rows_at_7 = list(verify.iter_rejections(RUN))
    total_rows_at_sequence_7 = sum(
        1 for row in delivery_rows_at_7 if row.ingest_sequence == 7
    ) + sum(1 for row in rejection_rows_at_7 if row.ingest_sequence == 7)
    assert total_rows_at_sequence_7 == 1
    verify.close()


def test_out_of_order_arrival_of_ingest_sequences_is_still_replayed_in_sequence_order(
    store: SQLiteEventStore,
) -> None:
    """Core invariant 6: arrival order and event/sequence order are distinct.
    Insert sequence 5, then 1, then 3 -- an out-of-order arrival pattern a
    reconnect or retried batch could plausibly produce -- and confirm
    ``iter_deliveries`` still replays by ``ingest_sequence``, not insertion
    order."""
    store.append_observation(_envelope(ingest_sequence=5, label="e"))
    store.append_observation(_envelope(ingest_sequence=1, label="a"))
    store.append_observation(_envelope(ingest_sequence=3, label="c"))

    sequences = [d.ingest_sequence for d in store.iter_deliveries(RUN)]
    assert sequences == [1, 3, 5]


# =====================================================================================
# 5. ADR-0011 section 7: rejection_id as a non-unique grouping key, with real data
# =====================================================================================


def test_three_malformed_entries_sharing_one_frame_and_one_rejection_id_all_survive(
    store: SQLiteEventStore,
) -> None:
    """The M2 research note observed *three* removals batched into a single
    WebSocket update; this mirrors that batch size for the rejection ledger
    rather than the two-entry case the existing suite already covers, and
    uses the real captured WebSocket frame bytes rather than a constructed
    placeholder string."""
    real_frame = WS_FIXTURE_PATH.read_bytes()
    entries = [
        _rejection(detail=f"entry #{i} could not be parsed", raw=real_frame) for i in range(3)
    ]
    rejection_ids = {entry.rejection_id for entry in entries}
    assert len(rejection_ids) == 1, "all three must collapse onto one grouping key"

    for sequence, entry in enumerate(entries, start=1):
        store.append_rejection(entry, ingest_sequence=sequence)

    stored = list(store.iter_rejections(RUN))
    assert len(stored) == 3
    assert {row.rejection.detail for row in stored} == {e.detail for e in entries}
    for row in stored:
        assert row.rejection.reason is RejectionReason.MALFORMED_PAYLOAD
        assert row.rejection.raw_payload_sha256 == sha256_hex(real_frame)
    assert store.counts_for_capture_run(RUN).rejected == 3


# =====================================================================================
# 6. Identity verification on read -- the rejection path, which the existing
#    whitebox test does not cover (only the observation path is tested there)
# =====================================================================================


def test_a_tampered_stored_rejection_is_refused_on_read() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    event_store = SQLiteEventStore(connection)
    event_store.open_capture_run(RUN, started_at=START)
    rejection = _rejection()
    event_store.append_rejection(rejection, ingest_sequence=1)

    record = rejection.to_record()
    record["rejection_id"] = "rejection-" + "f" * 32
    connection.execute(
        "UPDATE rejection SET record = ? WHERE capture_run_id = ? AND ingest_sequence = ?",
        (__import__("json").dumps(record), RUN, 1),
    )

    with pytest.raises(StorageError):
        list(event_store.iter_rejections(RUN))
    connection.close()


def test_the_rejection_id_column_itself_is_not_re_verified_on_read(tmp_path: Path) -> None:
    """``iter_rejections`` never selects the ``rejection_id`` column -- only
    ``record``. Confirm this directly: forging the column (leaving ``record``
    untouched) has no effect on what is read back, so the column is
    documentation/indexing metadata only and carries no independent
    guarantee. Relevant to anyone tempted to query or join on it later."""
    path = tmp_path / "rejection_column_drift.db"
    event_store = open_sqlite_event_store(path)
    event_store.open_capture_run(RUN, started_at=START)
    rejection = _rejection(detail="original detail")
    event_store.append_rejection(rejection, ingest_sequence=1)
    event_store.close()

    connection = sqlite3.connect(str(path))
    connection.execute(
        "UPDATE rejection SET rejection_id = 'rejection-FORGED' "
        "WHERE capture_run_id = ? AND ingest_sequence = 1",
        (RUN,),
    )
    connection.commit()
    connection.close()

    reread = open_sqlite_event_store(path)
    stored = list(reread.iter_rejections(RUN))
    assert len(stored) == 1
    # The forged column value is silently never consulted.
    assert stored[0].rejection.rejection_id == rejection.rejection_id
    reread.close()


# =====================================================================================
# 7. Round-trip fidelity for shapes the existing fixture-based tests do not exercise
# =====================================================================================


def test_round_trip_preserves_a_present_event_time_absent_optional_fields_and_non_ascii_text(
    store: SQLiteEventStore,
) -> None:
    """The existing byte-identical test (``test_event_store.py``) always
    builds from the REST book fixture, whose helper passes ``event_time=None``
    -- so ``EventTimeStatus.PRESENT`` is never exercised through the store at
    all. Also exercises every optional identifier left unset at once, and
    non-ASCII (including combining/CJK/emoji) text inside the payload, which
    the envelope does not sanitize."""
    payload = _UnicodePayload(note="book·喵星人·δ — déjà vu — İstanbul — Москва — 🐈")
    envelope = build_observation_envelope(
        source=ObservationSource.CLOB_MARKET_WS,
        source_event_type="book·喵星人",
        market_id=None,
        condition_id=None,
        token_id="token-unicode-1",
        event_time=datetime(2026, 3, 4, 5, 6, 7, 123456, tzinfo=UTC),
        received_time=START,
        ingest_sequence=1,
        source_sequence=None,
        source_hash=None,
        payload=payload,
        provenance=_provenance(b"unicode-frame-bytes"),
        parser_version="test-parser.v1",
        capture_run_id=RUN,
    )
    store.append_observation(envelope)

    reloaded = store.get_observation(envelope.observation_id)
    assert reloaded is not None
    assert reloaded == envelope
    assert reloaded.to_record() == envelope.to_record()
    assert reloaded.event_time == envelope.event_time
    assert reloaded.market_id is None
    assert reloaded.condition_id is None
    assert reloaded.source_sequence is None
    assert reloaded.source_hash is None


def test_capture_run_timestamps_are_exactly_what_was_injected_never_the_wall_clock() -> None:
    """No ``datetime.now()``/``CURRENT_TIMESTAMP`` anywhere on this path:
    round-trip a timestamp far outside any plausible 'now' in either
    direction and confirm it comes back byte-identical."""
    event_store = open_sqlite_event_store(":memory:")
    far_past = datetime(1975, 1, 1, tzinfo=UTC)
    far_future = datetime(2099, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
    event_store.open_capture_run(RUN, started_at=far_past)
    event_store.close_capture_run(
        RUN, ended_at=far_future, completion_status=CompletionStatus.COMPLETED
    )
    run = event_store.get_capture_run(RUN)
    assert run is not None
    assert run.started_at == far_past
    assert run.ended_at == far_future
    event_store.close()


# =====================================================================================
# 8. raw_archive._write_atomically -- the new fsync behaviour (ADR-0011 section 4)
# =====================================================================================


def _archive_provenance(raw: bytes, **overrides: Any) -> SourceProvenanceV1:
    fields: dict[str, Any] = {
        "source": "gamma",
        "endpoint": "https://gamma-api.polymarket.com/markets?limit=1",
        "http_status": 200,
        "retrieved_at": START,
        "raw_sha256": sha256_hex(raw),
        "byte_length": len(raw),
    }
    fields.update(overrides)
    return SourceProvenanceV1(**fields)


def test_fsync_is_actually_invoked_for_both_the_file_and_its_directory_on_a_normal_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The module's docstring claims an fsync before rename and one after,
    for the directory. Confirmed by spying on the real syscall rather than
    trusting the docstring: two files (sidecar, payload) each produce one
    file-fsync and one directory-fsync, four calls total, and the normal
    write path is otherwise unaffected."""
    raw = b'[{"id":"1","question":"Will it?"}]'
    real_fsync = os.fsync
    calls: list[int] = []

    def spy(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    path = write_raw_payload(tmp_path, raw=raw, provenance=_archive_provenance(raw))
    assert path.read_bytes() == raw
    assert len(calls) == 4


def test_a_directory_fsync_failure_after_a_successful_rename_is_reported_in_the_taxonomy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_write_atomically`` originally guarded only the write-and-file-fsync
    step; ``os.replace`` and the directory fsync that follows ran outside that
    guard. Forcing ``_fsync_directory`` to fail (a real, if rare, mode --
    ``fsync`` on a directory can fail on some filesystems and mounts) showed
    two problems at once. The exception crossed ``write_raw_payload``'s
    boundary as a bare ``OSError`` with no ARGOS ``code``, unlike the
    mismatched-hash and outside-archive paths in the same function. And, more
    seriously, the rename this fsync exists to make crash-durable had
    *already happened*, so the call reported failure while the file was
    genuinely on disk.

    **Both are fixed.** The failure is now a ``StorageError`` whose message
    states precisely that distinction -- the file is present and readable, but
    its directory entry is not yet durable -- because a caller that cannot
    tell "nothing happened" from "the file landed but may not survive a crash"
    cannot make a correct recovery decision. The state on disk is asserted
    below unchanged: this fix corrects the report, not the (unavoidable)
    fact that the rename already completed."""
    raw = b'[{"id":"1","question":"Will it?"}]'
    digest = sha256_hex(raw)

    def failing_fsync_directory(directory: Path) -> None:
        raise OSError("simulated directory fsync failure (e.g. filesystem does not support it)")

    monkeypatch.setattr(raw_archive_module, "_fsync_directory", failing_fsync_directory)

    with pytest.raises(StorageError) as caught:
        write_raw_payload(tmp_path, raw=raw, provenance=_archive_provenance(raw))
    assert hasattr(caught.value, "code"), "the failure must carry an ARGOS error code"
    assert "durable" in str(caught.value)

    sidecar = tmp_path / "gamma" / f"{digest}.meta.json"
    payload_file = tmp_path / "gamma" / f"{digest}.raw.json"
    # The sidecar's rename already completed before the directory fsync that
    # failed -- it is on disk despite the call having raised. The error message
    # now says so explicitly instead of leaving the caller to guess.
    assert sidecar.exists()
    # The payload write never started; this failure is not "everything or
    # nothing" from the caller's point of view, and the message admits it.
    assert not payload_file.exists()


def test_a_file_fsync_failure_still_rolls_back_the_temp_file_and_taxonomy_status_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contrast case for the finding above: a failure *inside* the
    try/except (the file-level ``os.fsync``, before the rename) does clean up
    its own temp file and does not leave a partially-visible artifact --
    confirming the guarded half of the function behaves as documented. It
    still surfaces as a bare OSError rather than a taxonomy type, which is
    consistent with ``tests/test_raw_archive.py``'s own
    ``test_a_symlink_at_the_temp_path_cannot_be_written_through`` (also a
    bare ``OSError``), so this is pre-existing, not a regression from the
    fsync addition."""
    raw = b'[{"id":"1","question":"Will it?"}]'
    digest = sha256_hex(raw)
    real_fsync = os.fsync
    call_count = 0

    def flaky(fd: int) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise OSError("simulated disk-full during the first (file-level) fsync")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", flaky)

    with pytest.raises(OSError):
        write_raw_payload(tmp_path, raw=raw, provenance=_archive_provenance(raw))

    archive_dir = tmp_path / "gamma"
    leftover_partials = list(archive_dir.glob("*.partial")) if archive_dir.exists() else []
    assert leftover_partials == []
    payload_file = tmp_path / "gamma" / f"{digest}.raw.json"
    sidecar = tmp_path / "gamma" / f"{digest}.meta.json"
    assert not payload_file.exists()
    assert not sidecar.exists()


def test_a_stale_partial_file_from_a_prior_crash_no_longer_wedges_the_archive(
    tmp_path: Path,
) -> None:
    """``_write_atomically`` opens its temp file with ``O_CREAT | O_EXCL``,
    which is exactly right against a symlink attack (see
    ``test_raw_archive.py``) but had a second, unintended consequence: a
    process killed between creating the temp file and renaming it away (the
    try/except only unlinks for exceptions raised by this function's own code,
    not for a kill) left a ``.partial`` that permanently blocked every
    subsequent attempt to archive that exact payload, with a bare
    ``FileExistsError``. It also wedged the documented sidecar-repair path.

    That is precisely the crash the M2 store slice's fsync work exists to
    reason about, so **the archive now heals from it**: a stale *regular* file
    is cleared and the write proceeds. A symlink is still refused --
    ``O_CREAT | O_EXCL`` reports a planted symlink as ``FileExistsError``, not
    ``ELOOP``, so an unconditional unlink here would have silently downgraded
    the M1 symlink guard from "refuse" to "delete and proceed". That
    regression was caught by the existing symlink test and is pinned there."""
    raw = b'[{"id":"1","question":"Will it?"}]'
    digest = sha256_hex(raw)
    archive_dir = tmp_path / "gamma"
    archive_dir.mkdir(parents=True)
    stale_partial = archive_dir / f"{digest}.meta.json.partial"
    stale_partial.write_bytes(b"leftover from a killed process")

    path = write_raw_payload(tmp_path, raw=raw, provenance=_archive_provenance(raw))

    assert path.read_bytes() == raw
    assert not stale_partial.exists(), "the stale temp file must be cleared, not left behind"
    assert (archive_dir / f"{digest}.meta.json").exists()
    # And the repair path documented on write_raw_payload works again.
    (archive_dir / f"{digest}.meta.json").unlink()
    write_raw_payload(tmp_path, raw=raw, provenance=_archive_provenance(raw))
    assert (archive_dir / f"{digest}.meta.json").exists()
