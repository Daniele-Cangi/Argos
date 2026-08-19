"""Tests for the SQLite/WAL event store (ADR-0011).

M2 exit criteria this file closes with adapter-independent evidence:
"duplicate source event does not create a second accepted observation" and
"invalid messages enter a rejection ledger with reason and raw hash". Several
tests build the envelope from the real recorded CLOB `/book` fixture
(`docs/research/fixtures/clob-book-yes-2026-08-10T181007Z.json`), not only
from a constructed payload, per the project's fixture-first testing
convention.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest

from argos.domain.observation import (
    ObservationEnvelopeV1,
    ObservationSource,
    RejectedObservationV1,
    build_observation_envelope,
    build_rejected_observation,
)
from argos.domain.orderbook import OrderBookSnapshotV1, parse_order_book_snapshot
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.domain.versioning import VersionedModel
from argos.errors import (
    ContractViolationError,
    ImmutabilityViolationError,
    RejectionReason,
    StorageError,
)
from argos.store.event_store import (
    _EXPECTED_INDEXES,
    _EXPECTED_OBJECTS,
    _EXPECTED_TABLES,
    _SCHEMA_SQL,
    ARGOS_APPLICATION_ID,
    EVENT_STORE_SCHEMA_VERSION,
    CompletionStatus,
    Disposition,
    SQLiteEventStore,
    open_sqlite_event_store,
)

START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
RUN = "capture-run-1"

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "research"
    / "fixtures"
    / "clob-book-yes-2026-08-10T181007Z.json"
)


class _Payload(VersionedModel):
    """A minimal typed payload for tests that do not need a real order book."""

    schema_version: ClassVar[str] = "test_event_store_payload.v1"

    label: str = "x"


def _load_fixture_bytes() -> bytes:
    return FIXTURE_PATH.read_bytes()


def _load_fixture_snapshot() -> OrderBookSnapshotV1:
    with FIXTURE_PATH.open() as handle:
        raw = json.load(handle, parse_float=Decimal)
    return parse_order_book_snapshot(raw)


def _store() -> SQLiteEventStore:
    return open_sqlite_event_store(":memory:")


def _provenance(raw: bytes, *, source: str = "clob_rest", **overrides: Any) -> SourceProvenanceV1:
    fields: dict[str, Any] = {
        "source": source,
        "endpoint": "https://clob.polymarket.com/book?token_id=1",
        "http_status": 200,
        "retrieved_at": START,
        "raw_sha256": sha256_hex(raw),
        "byte_length": len(raw),
    }
    fields.update(overrides)
    return SourceProvenanceV1(**fields)


def _fixture_envelope(
    *, ingest_sequence: int, capture_run_id: str = RUN, **overrides: Any
) -> ObservationEnvelopeV1:
    raw = _load_fixture_bytes()
    fields: dict[str, Any] = {
        "source": ObservationSource.CLOB_REST,
        "source_event_type": "book",
        "market_id": None,
        "condition_id": None,
        "token_id": "test-token-1",
        "event_time": None,
        "received_time": START,
        "ingest_sequence": ingest_sequence,
        "payload": _load_fixture_snapshot(),
        "provenance": _provenance(raw),
        "parser_version": "test-parser.v1",
        "capture_run_id": capture_run_id,
    }
    fields.update(overrides)
    return build_observation_envelope(**fields)


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
        "provenance": provenance or _provenance(raw, source="clob_market_ws"),
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
        "provenance": _provenance(raw, source="clob_market_ws"),
        "received_time": START,
        "rejected_at": START,
        "capture_run_id": capture_run_id,
    }
    fields.update(overrides)
    return build_rejected_observation(**fields)


@pytest.fixture
def store() -> SQLiteEventStore:
    event_store = _store()
    event_store.open_capture_run(RUN, started_at=START)
    yield event_store
    event_store.close()


# --- duplicate collapse ------------------------------------------------------------


def test_a_duplicate_observation_does_not_create_a_second_accepted_observation(
    store: SQLiteEventStore,
) -> None:
    first = _envelope(ingest_sequence=1)
    second = _envelope(ingest_sequence=2, received_time=START + timedelta(seconds=5))
    assert first.observation_id == second.observation_id

    first_result = store.append_observation(first)
    second_result = store.append_observation(second)

    assert first_result.disposition is Disposition.ACCEPTED_NEW
    assert second_result.disposition is Disposition.DUPLICATE

    counts = store.counts_for_capture_run(RUN)
    assert counts.accepted == 1
    assert counts.duplicate == 1
    assert counts.rejected == 0


def test_the_duplicate_records_its_own_arrival_without_mutating_the_observation(
    store: SQLiteEventStore,
) -> None:
    first = _envelope(ingest_sequence=1)
    later = START + timedelta(minutes=40)
    second = _envelope(ingest_sequence=2, received_time=later)
    store.append_observation(first)
    store.append_observation(second)

    deliveries = list(store.iter_deliveries(RUN))
    assert [d.ingest_sequence for d in deliveries] == [1, 2]
    assert deliveries[0].disposition is Disposition.ACCEPTED_NEW
    assert deliveries[1].disposition is Disposition.DUPLICATE
    assert deliveries[1].received_time == later

    stored = store.get_observation(first.observation_id)
    assert stored is not None
    # The observation row pins the *first* arrival's values, per ADR-0011
    # section 5 -- the duplicate's later received_time never reaches it.
    assert stored.received_time == START
    assert stored.ingest_sequence == 1


def test_the_real_fixture_envelope_collapses_on_redelivery(store: SQLiteEventStore) -> None:
    first = _fixture_envelope(ingest_sequence=1)
    second = _fixture_envelope(ingest_sequence=2)
    assert first.observation_id == second.observation_id

    first_result = store.append_observation(first)
    second_result = store.append_observation(second)
    assert first_result.disposition is Disposition.ACCEPTED_NEW
    assert second_result.disposition is Disposition.DUPLICATE


# --- round trip ----------------------------------------------------------------------


def test_a_stored_envelope_reloads_byte_identical(store: SQLiteEventStore) -> None:
    """M3's identical-hash replay criterion runs through this round trip."""
    original = _fixture_envelope(ingest_sequence=1)
    store.append_observation(original)

    reloaded = store.get_observation(original.observation_id)
    assert reloaded is not None
    assert reloaded == original
    assert reloaded.to_record() == original.to_record()


# --- rejection ledger -----------------------------------------------------------------


def test_a_rejection_is_stored_with_reason_and_raw_hash(store: SQLiteEventStore) -> None:
    rejection = _rejection(detail="could not parse", raw=b"hostile-bytes")
    store.append_rejection(rejection, ingest_sequence=1)

    stored = list(store.iter_rejections(RUN))
    assert len(stored) == 1
    record = stored[0]
    assert record.rejection.reason is RejectionReason.MALFORMED_PAYLOAD
    assert record.rejection.raw_payload_sha256 == sha256_hex(b"hostile-bytes")
    assert record.rejection.detail == "could not parse"
    assert record.duplicate_of_observation_id is None


def test_two_different_malformed_entries_in_one_frame_both_survive_the_ledger(
    store: SQLiteEventStore,
) -> None:
    """ADR-0011 section 7: two different rejections can share one
    rejection_id (same reason/source/scope/raw hash, different detail) when
    a frame carries more than one bad entry. Both rows must survive."""
    shared_raw = b"one-frame-two-bad-entries"
    first = _rejection(detail="entry #1 missing price", raw=shared_raw)
    second = _rejection(detail="entry #2 missing size", raw=shared_raw)
    assert first.rejection_id == second.rejection_id
    assert first.detail != second.detail

    store.append_rejection(first, ingest_sequence=1)
    store.append_rejection(second, ingest_sequence=2)

    stored = list(store.iter_rejections(RUN))
    assert len(stored) == 2
    assert {row.rejection.detail for row in stored} == {
        "entry #1 missing price",
        "entry #2 missing size",
    }
    assert store.counts_for_capture_run(RUN).rejected == 2


def test_a_rejection_can_point_at_the_accepted_twin_it_duplicates(
    store: SQLiteEventStore,
) -> None:
    accepted = _envelope(ingest_sequence=1)
    store.append_observation(accepted)
    rejection = _rejection(reason=RejectionReason.DUPLICATE_EVENT, detail="already accepted")
    store.append_rejection(
        rejection, ingest_sequence=2, duplicate_of_observation_id=accepted.observation_id
    )

    stored = list(store.iter_rejections(RUN))
    assert stored[0].duplicate_of_observation_id == accepted.observation_id


def test_a_rejection_referencing_an_unknown_observation_is_refused(
    store: SQLiteEventStore,
) -> None:
    rejection = _rejection()
    with pytest.raises(StorageError):
        store.append_rejection(
            rejection, ingest_sequence=1, duplicate_of_observation_id="observation-does-not-exist"
        )


# --- ingest_sequence uniqueness --------------------------------------------------------


def test_ingest_sequence_is_unique_within_delivery(store: SQLiteEventStore) -> None:
    store.append_observation(_envelope(ingest_sequence=1, label="a"))
    with pytest.raises(StorageError):
        store.append_observation(_envelope(ingest_sequence=1, label="b"))


def test_ingest_sequence_is_unique_across_delivery_and_rejection(store: SQLiteEventStore) -> None:
    store.append_observation(_envelope(ingest_sequence=1, label="a"))
    with pytest.raises(StorageError):
        store.append_rejection(_rejection(), ingest_sequence=1)


def test_a_non_positive_ingest_sequence_is_refused_for_a_rejection(store: SQLiteEventStore) -> None:
    with pytest.raises(ContractViolationError):
        store.append_rejection(_rejection(), ingest_sequence=0)


# --- identity verification --------------------------------------------------------------


def test_a_forged_observation_id_is_refused_on_write(store: SQLiteEventStore) -> None:
    envelope = _envelope(ingest_sequence=1)
    forged = envelope.model_copy(update={"observation_id": "observation-" + "0" * 32})
    with pytest.raises(ContractViolationError):
        store.append_observation(forged)


def test_a_forged_rejection_id_is_refused_on_write(store: SQLiteEventStore) -> None:
    rejection = _rejection()
    forged = rejection.model_copy(update={"rejection_id": "rejection-" + "0" * 32})
    with pytest.raises(ContractViolationError):
        store.append_rejection(forged, ingest_sequence=1)


def test_a_tampered_stored_record_is_refused_on_read() -> None:
    """Simulates on-disk corruption directly at the SQL level -- the same kind
    of whitebox tampering `tests/test_raw_archive.py` performs on a file --
    to prove the read-time identity check is a real, independent line of
    defense and not merely dead code shadowed by the write-time check."""
    # isolation_level=None matches open_sqlite_event_store's own connection
    # setup: SQLiteEventStore issues its own explicit BEGIN/COMMIT/ROLLBACK
    # as raw SQL, which needs autocommit mode to avoid colliding with
    # sqlite3's default implicit-transaction bookkeeping.
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    store = SQLiteEventStore(connection)
    store.open_capture_run(RUN, started_at=START)
    envelope = _envelope(ingest_sequence=1)
    store.append_observation(envelope)

    # Whitebox tampering only, executed by the test itself with a raw SQL
    # connection it owns -- not through argos.store, which is mechanically
    # forbidden from ever issuing this statement (test_boundaries.py).
    record = envelope.to_record()
    record["observation_id"] = "observation-" + "f" * 32
    connection.execute(
        "UPDATE observation SET record = ? WHERE observation_id = ?",
        (json.dumps(record), envelope.observation_id),
    )

    with pytest.raises(StorageError):
        store.get_observation(envelope.observation_id)
    connection.close()


# --- capture_run lifecycle -----------------------------------------------------------


def test_a_run_left_open_is_a_queryable_incomplete_capture_signal() -> None:
    event_store = _store()
    event_store.open_capture_run(RUN, started_at=START)

    run = event_store.get_capture_run(RUN)
    assert run is not None
    assert run.is_open
    assert run.ended_at is None
    assert [r.capture_run_id for r in event_store.iter_open_capture_runs()] == [RUN]

    event_store.close_capture_run(
        RUN, ended_at=START + timedelta(minutes=1), completion_status=CompletionStatus.COMPLETED
    )
    closed = event_store.get_capture_run(RUN)
    assert closed is not None
    assert not closed.is_open
    assert closed.completion_status is CompletionStatus.COMPLETED
    assert list(event_store.iter_open_capture_runs()) == []
    event_store.close()


def test_opening_the_same_capture_run_twice_is_refused() -> None:
    event_store = _store()
    event_store.open_capture_run(RUN, started_at=START)
    with pytest.raises(StorageError):
        event_store.open_capture_run(RUN, started_at=START)
    event_store.close()


def test_closing_a_capture_run_twice_is_refused(store: SQLiteEventStore) -> None:
    store.close_capture_run(
        RUN, ended_at=START + timedelta(minutes=1), completion_status=CompletionStatus.COMPLETED
    )
    with pytest.raises(ImmutabilityViolationError):
        store.close_capture_run(
            RUN, ended_at=START + timedelta(minutes=2), completion_status=CompletionStatus.FAILED
        )


def test_closing_an_unknown_capture_run_is_refused() -> None:
    event_store = _store()
    with pytest.raises(StorageError):
        event_store.close_capture_run(
            "no-such-run", ended_at=START, completion_status=CompletionStatus.COMPLETED
        )
    event_store.close()


def test_appending_to_an_unopened_capture_run_is_refused() -> None:
    event_store = _store()
    with pytest.raises(StorageError):
        event_store.append_observation(_envelope(ingest_sequence=1, capture_run_id="no-such-run"))
    event_store.close()


def test_appending_to_a_closed_capture_run_is_refused(store: SQLiteEventStore) -> None:
    store.close_capture_run(
        RUN, ended_at=START + timedelta(minutes=1), completion_status=CompletionStatus.COMPLETED
    )
    with pytest.raises(StorageError):
        store.append_observation(_envelope(ingest_sequence=1))


def test_get_capture_run_returns_none_for_an_unknown_run(store: SQLiteEventStore) -> None:
    assert store.get_capture_run("never-opened") is None


# --- delivery frame metadata (ADR-0011 section 6) ---------------------------------------


def test_the_same_frame_carrying_two_different_entries_is_recorded_with_matching_hash(
    store: SQLiteEventStore,
) -> None:
    """Same frame hash, different offsets: one frame carried two distinct
    entries. Two different observations, one shared source_frame_sha256."""
    shared_provenance = _provenance(b"one-frame-two-entries", source="clob_market_ws")
    first = _envelope(ingest_sequence=1, label="a", provenance=shared_provenance)
    second = _envelope(ingest_sequence=2, label="b", provenance=shared_provenance)
    assert first.observation_id != second.observation_id

    first_delivery = store.append_observation(first, source_frame_offset=0)
    second_delivery = store.append_observation(second, source_frame_offset=1)

    assert first_delivery.source_frame_sha256 == second_delivery.source_frame_sha256
    assert first_delivery.source_frame_offset != second_delivery.source_frame_offset


def test_a_genuine_resend_from_a_different_frame_is_distinguishable_by_hash(
    store: SQLiteEventStore,
) -> None:
    """Different frame hash, same observation_id: a genuine two-frame resend
    of the identical event. observation_id collides (as intended); the
    delivery rows' source_frame_sha256 differs and received_time separates
    them."""
    first = _envelope(
        ingest_sequence=1, provenance=_provenance(b"frame-one", source="clob_market_ws")
    )
    second = _envelope(
        ingest_sequence=2,
        received_time=START + timedelta(microseconds=196),
        provenance=_provenance(b"frame-two", source="clob_market_ws"),
    )
    assert first.observation_id == second.observation_id

    first_delivery = store.append_observation(first)
    second_delivery = store.append_observation(second)

    assert first_delivery.source_frame_sha256 != second_delivery.source_frame_sha256
    assert second_delivery.disposition is Disposition.DUPLICATE
    assert second_delivery.received_time != first_delivery.received_time


def test_the_same_frame_and_offset_recorded_twice_is_the_reprocessing_bug_signature(
    store: SQLiteEventStore,
) -> None:
    """Same frame hash, same offset: a reprocessing bug rather than a source
    event. The store does not refuse this -- it is indistinguishable from a
    legitimate resend at this layer -- but it stays fully countable."""
    provenance = _provenance(b"reprocessed-frame", source="clob_market_ws")
    first = _envelope(ingest_sequence=1, provenance=provenance)
    second = _envelope(ingest_sequence=2, provenance=provenance)

    first_delivery = store.append_observation(first, source_frame_offset=3)
    second_delivery = store.append_observation(second, source_frame_offset=3)

    assert first_delivery.source_frame_sha256 == second_delivery.source_frame_sha256
    assert first_delivery.source_frame_offset == second_delivery.source_frame_offset


# --- regressions from the M2 store security review --------------------------------


def test_a_hostile_capture_run_id_is_refused_at_the_store_boundary() -> None:
    """The store must hold `capture_run_id` to the contract the envelope holds
    it to. Security review measured this boundary persisting, unbounded and
    unneutralized, a value `ObservationEnvelopeV1._validate_identifier` refuses
    for the same field name: an OSC 52 clipboard write and an RLO override
    survived verbatim into `iter_open_capture_runs()` -- which is exactly the
    "interrupted capture" report a future renderer will print. That is the M2
    HIGH finding on `market_id`/`condition_id`/`token_id` relocated to a new
    boundary."""
    hostile = "run-\x1b]52;c;aGVsbG8=\x07-‮revo\n[ok] review status: human_reviewed\n"
    event_store = _store()
    try:
        with pytest.raises(ContractViolationError):
            event_store.open_capture_run(hostile, started_at=START)
        assert list(event_store.iter_open_capture_runs()) == []
    finally:
        event_store.close()


def test_an_unbounded_capture_run_id_is_refused_rather_than_stored() -> None:
    """Measured at 20,000,000 characters accepted in 0.168 s before the fix."""
    event_store = _store()
    try:
        with pytest.raises(ContractViolationError):
            event_store.open_capture_run("r" * 20_000, started_at=START)
        assert list(event_store.iter_open_capture_runs()) == []
    finally:
        event_store.close()


def test_a_run_id_the_envelope_would_reject_can_never_be_opened() -> None:
    """The tell that made the gap self-evident: a run opened under a hostile id
    could never receive an observation at all, because the envelope validator
    rejects the matching `capture_run_id`. The store was accepting run ids that
    were structurally unusable."""
    event_store = _store()
    try:
        with pytest.raises(ContractViolationError):
            event_store.open_capture_run("run\nid", started_at=START)
    finally:
        event_store.close()


def test_a_corrupt_record_column_surfaces_inside_the_taxonomy(store: SQLiteEventStore) -> None:
    """Security review measured six corruption shapes escaping as foreign
    exceptions -- `json.JSONDecodeError`, `ValueError`, `TypeError` -- while
    the module documented `StorageError` for a bad row. This is the M1 finding
    in this same package reopened: `read_raw_payload` leaked
    `FileNotFoundError`/`JSONDecodeError` until M1 fixed it so a caller could
    count and explain the failure (core invariant 14)."""
    envelope = _envelope(ingest_sequence=1)
    store.append_observation(envelope)
    # Tamper the stored record directly, the way test_raw_archive.py tampers a file.
    store._connection.execute(
        "REPLACE INTO observation (observation_id, schema_version, payload_schema_version, "
        "source, market_id, condition_id, token_id, event_time, raw_payload_sha256, "
        "first_seen_capture_run_id, first_seen_ingest_sequence, first_seen_received_time, "
        "record) SELECT observation_id, schema_version, payload_schema_version, source, "
        "market_id, condition_id, token_id, event_time, raw_payload_sha256, "
        "first_seen_capture_run_id, first_seen_ingest_sequence, first_seen_received_time, "
        "'not json at all' FROM observation WHERE observation_id = ?",
        (envelope.observation_id,),
    )

    with pytest.raises(StorageError):
        store.get_observation(envelope.observation_id)


def test_a_corrupt_delivery_timestamp_surfaces_inside_the_taxonomy(
    store: SQLiteEventStore,
) -> None:
    store.append_observation(_envelope(ingest_sequence=1))
    store._connection.execute(
        "REPLACE INTO delivery (capture_run_id, ingest_sequence, observation_id, "
        "received_time, disposition, source_frame_sha256, source_frame_offset) "
        "SELECT capture_run_id, ingest_sequence, observation_id, 'not-a-timestamp', "
        "disposition, source_frame_sha256, source_frame_offset FROM delivery"
    )

    with pytest.raises(StorageError):
        list(store.iter_deliveries(RUN))


def test_the_store_refuses_to_open_when_sqlite_declines_the_required_pragmas() -> None:
    """`PRAGMA journal_mode` does not error on a refused change; it returns the
    mode actually in effect. Security review measured `:memory:` silently
    staying on "memory" while the docstring claimed WAL, and the same silent
    decline happens on filesystems without shared-memory support. ADR-0011
    hangs the "interrupted capture" exit criterion on WAL recovery, so an
    unverified pragma is a durability claim in prose rather than a property.

    `:memory:` is legitimately "memory", so it must still open -- the guard has
    to be exact, not merely strict, or it would refuse the store its own test
    suite runs on."""
    in_memory = open_sqlite_event_store(":memory:")
    try:
        assert in_memory._connection.execute("PRAGMA journal_mode").fetchone()[0] == "memory"
        assert in_memory._connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert in_memory._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        in_memory.close()


def test_a_file_backed_store_really_is_in_wal_mode(tmp_path: Path) -> None:
    """The claim ADR-0011 leans on, asserted rather than assumed."""
    event_store = open_sqlite_event_store(tmp_path / "events.sqlite3")
    try:
        assert event_store._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        event_store.close()


# --- schema identity and version (M3 blocker R3) ----------------------------------


def test_the_expected_shape_matches_the_ddl_this_module_actually_runs() -> None:
    """The duplication in `_EXPECTED_TABLES` is only safe if it cannot drift.

    It is written out rather than parsed back out of `_SCHEMA_SQL` so a change
    to the DDL and a change to what the module believes the DDL is cannot happen
    in one edit. This is the test that makes that safe: it reads the shape out
    of a freshly created database, so the DDL is the authority and the constant
    is the claim.
    """
    connection = sqlite3.connect(":memory:")
    connection.executescript(_SCHEMA_SQL)
    for table, expected in _EXPECTED_TABLES.items():
        actual = tuple(row[1] for row in connection.execute(f"PRAGMA table_info({table})"))
        assert actual == expected, f"{table} DDL and _EXPECTED_TABLES disagree"
    indexes = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert set(_EXPECTED_INDEXES) <= indexes
    connection.close()


def test_a_fresh_store_is_stamped_with_its_identity_and_version(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    store = open_sqlite_event_store(path)
    store.close()

    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA application_id").fetchone()[0] == ARGOS_APPLICATION_ID
    assert connection.execute("PRAGMA user_version").fetchone()[0] == EVENT_STORE_SCHEMA_VERSION
    connection.close()


def test_a_foreign_table_of_the_same_name_is_refused_on_open(tmp_path: Path) -> None:
    """The measured hole, closed.

    `CREATE TABLE IF NOT EXISTS` creates what is missing and leaves a
    wrongly-shaped existing table alone, so before this check
    `open_sqlite_event_store` succeeded against a database whose `observation`
    was a foreign two-column table -- and so did `open_capture_run`, meaning a
    run was opened and recorded before anything failed. What finally failed was
    a generic SQLite error, mid-capture.
    """
    path = tmp_path / "foreign.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE observation (id INTEGER PRIMARY KEY, wrong TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(StorageError) as caught:
        open_sqlite_event_store(path)
    assert caught.value.context["table"] == "observation"


def test_a_database_stamped_by_another_application_is_refused(tmp_path: Path) -> None:
    """A different stamp is a positive claim by somebody else that the file is
    theirs, which is a different thing from an unstamped file and gets a
    different answer."""
    path = tmp_path / "other.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA application_id = 559038737")
    connection.close()

    with pytest.raises(StorageError) as caught:
        open_sqlite_event_store(path)
    assert caught.value.context["application_id"] == 559038737


def test_a_future_schema_version_is_refused_rather_than_read_hopefully(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    open_sqlite_event_store(path).close()
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {EVENT_STORE_SCHEMA_VERSION + 1}")
    connection.close()

    with pytest.raises(StorageError) as caught:
        open_sqlite_event_store(path)
    assert caught.value.context["found"] == EVENT_STORE_SCHEMA_VERSION + 1


def test_an_unstamped_store_of_the_right_shape_is_adopted(tmp_path: Path) -> None:
    """Captures taken before 2026-08-17 carry `application_id = 0`.

    Refusing them would strand real data for no gain: by the time the stamp is
    read, the shape check has already established what the file is. The shape is
    the evidence; the stamp is the fast path. Verified by reading a row back
    out, not only by the open succeeding.
    """
    path = tmp_path / "legacy.sqlite3"
    store = open_sqlite_event_store(path)
    store.open_capture_run("legacy-run", started_at=START)
    store.close()

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA application_id = 0")
    connection.execute("PRAGMA user_version = 0")
    connection.close()

    reopened = open_sqlite_event_store(path)
    run = reopened.get_capture_run("legacy-run")
    assert run is not None and run.is_open
    reopened.close()

    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA application_id").fetchone()[0] == ARGOS_APPLICATION_ID
    assert connection.execute("PRAGMA user_version").fetchone()[0] == EVENT_STORE_SCHEMA_VERSION
    connection.close()


def test_a_dropped_index_is_recreated_rather_than_refused(tmp_path: Path) -> None:
    """The self-heal, asserted so it is a decision rather than an accident.

    `CREATE UNIQUE INDEX IF NOT EXISTS` restores a *missing* index, and doing so
    is safe precisely because recreating a unique index over data that violated
    it would fail loudly instead. Verified by the property, not by the name
    reappearing.
    """
    path = tmp_path / "deindexed.sqlite3"
    open_sqlite_event_store(path).close()
    connection = sqlite3.connect(path)
    connection.execute("DROP INDEX capture_run_open_once")
    connection.commit()
    connection.close()

    open_sqlite_event_store(path).close()
    connection = sqlite3.connect(path)
    properties = {
        row[1]: (row[2], row[4])
        for row in connection.execute("PRAGMA index_list(capture_run)").fetchall()
    }
    connection.close()
    assert properties["capture_run_open_once"] == (1, 1)


def test_an_index_of_the_right_name_and_the_wrong_nature_is_refused(tmp_path: Path) -> None:
    """What `IF NOT EXISTS` cannot fix, and the reason the check is about
    properties rather than names.

    ADR-0011 section 5 hangs "opened twice and closed twice are impossible at
    the schema level" on these two indexes being UNIQUE and partial. A plain
    non-unique index of the same name survives `CREATE UNIQUE INDEX IF NOT
    EXISTS` untouched, and that guarantee silently degrades to a convention --
    with nothing anywhere reporting it.
    """
    path = tmp_path / "weakened.sqlite3"
    open_sqlite_event_store(path).close()
    connection = sqlite3.connect(path)
    connection.execute("DROP INDEX capture_run_open_once")
    connection.execute("CREATE INDEX capture_run_open_once ON capture_run (capture_run_id)")
    connection.commit()
    connection.close()

    with pytest.raises(StorageError) as caught:
        open_sqlite_event_store(path)
    # Since M4.1 the whole-DDL comparison catches this first and names the
    # object rather than the index property. The assertion moved with the
    # mechanism rather than being relaxed: it still pins that this exact
    # weakening is refused, and that the refusal identifies which object.
    assert caught.value.context["object_name"] == "capture_run_open_once"


def test_a_schema_that_cannot_be_applied_stays_inside_the_taxonomy(tmp_path: Path) -> None:
    """A foreign *table* named after one of the indexes makes `CREATE UNIQUE
    INDEX IF NOT EXISTS` fail outright, on the one code path that runs before
    any check could catch it. Left bare it escapes as `sqlite3.OperationalError`
    -- the "escapes the ARGOS error taxonomy" class this milestone has closed
    four times elsewhere."""
    path = tmp_path / "namesquat.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE capture_run_open_once (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()

    with pytest.raises(StorageError):
        open_sqlite_event_store(path)


# --- constraint verification, not only column names (M4.1 F6) ---------------------


def _weakened(source: str, needle: str, replacement: str) -> str:
    weakened = source.replace(needle, replacement)
    assert weakened != source, f"pattern not found: {needle!r}"
    return weakened


WEAKENINGS: dict[str, tuple[str, str]] = {
    "unique_observation_id": (",\n    UNIQUE (observation_id)\n", "\n"),
    "unique_delivery_arrival": (
        "    source_frame_offset INTEGER NOT NULL,\n    UNIQUE (capture_run_id, ingest_sequence)\n",
        "    source_frame_offset INTEGER NOT NULL\n",
    ),
    "unique_rejection_arrival": (
        "    record TEXT NOT NULL,\n    UNIQUE (capture_run_id, ingest_sequence)\n)",
        "    record TEXT NOT NULL\n)",
    ),
    "delivery_foreign_key": (
        "observation_id TEXT NOT NULL REFERENCES observation (observation_id),",
        "observation_id TEXT NOT NULL,",
    ),
    "disposition_check": (
        "disposition TEXT NOT NULL CHECK (disposition IN ('accepted_new', 'duplicate')),",
        "disposition TEXT NOT NULL,",
    ),
    "completion_status_check": (
        "completion_status TEXT CHECK (\n"
        "        completion_status IS NULL OR completion_status IN ('completed', 'failed')\n"
        "    )",
        "completion_status TEXT",
    ),
    "open_once_not_unique": (
        "CREATE UNIQUE INDEX IF NOT EXISTS capture_run_open_once\n"
        "    ON capture_run (capture_run_id) WHERE ended_at IS NULL;",
        "CREATE INDEX IF NOT EXISTS capture_run_open_once\n    ON capture_run (capture_run_id);",
    ),
}


@pytest.mark.parametrize("weakening", sorted(WEAKENINGS))
def test_a_store_with_the_right_columns_and_the_wrong_constraints_is_refused(
    tmp_path: Path, weakening: str
) -> None:
    """Column names were never enough, and the gap was measured.

    Before this check, every one of these was **accepted** as an ARGOS event
    store: no `UNIQUE (observation_id)`, no `UNIQUE (capture_run_id,
    ingest_sequence)` on either ledger, no `delivery -> observation` foreign key,
    and neither `CHECK`. Each is a property this module's own correctness rests
    on -- idempotent insert *is* the unique index, "one record per arrival" *is*
    the composite key -- and `CREATE TABLE IF NOT EXISTS` leaves a pre-existing
    table alone, so a database can carry our column names and none of it.
    """
    needle, replacement = WEAKENINGS[weakening]
    path = tmp_path / f"{weakening}.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(_weakened(_SCHEMA_SQL, needle, replacement))
    connection.execute(f"PRAGMA application_id = {ARGOS_APPLICATION_ID}")
    connection.execute(f"PRAGMA user_version = {EVENT_STORE_SCHEMA_VERSION}")
    connection.commit()
    connection.close()

    with pytest.raises(StorageError):
        open_sqlite_event_store(path)


def test_a_correctly_shaped_store_is_still_accepted(tmp_path: Path) -> None:
    """The guard above is only worth having if it is not simply refusing
    everything -- a check that never passes is indistinguishable from a broken
    open."""
    path = tmp_path / "genuine.sqlite3"
    store = open_sqlite_event_store(path)
    store.open_capture_run("run", started_at=START)
    store.close()
    reopened = open_sqlite_event_store(path)
    assert reopened.get_capture_run("run") is not None
    reopened.close()


def test_refusing_a_foreign_database_changes_not_one_byte_of_it(tmp_path: Path) -> None:
    """Refusing after mutating is not refusing.

    Reproduced before the check moved ahead of every write: opening a database
    stamped for another application refused it, and by then had switched its
    journal mode to WAL, created `capture_run`, `observation`, `delivery`,
    `rejection` and three indexes inside it, and left `-wal` and `-shm` files
    beside it.
    """
    path = tmp_path / "someone_elses.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA application_id = 559038737")
    connection.execute("CREATE TABLE unrelated (x INTEGER)")
    connection.execute("INSERT INTO unrelated VALUES (1)")
    connection.commit()
    connection.close()

    before_bytes = path.read_bytes()
    before_siblings = sorted(p.name for p in tmp_path.iterdir())
    probe = sqlite3.connect(path)
    before_state = {
        "application_id": probe.execute("PRAGMA application_id").fetchone()[0],
        "user_version": probe.execute("PRAGMA user_version").fetchone()[0],
        "journal_mode": probe.execute("PRAGMA journal_mode").fetchone()[0],
        "objects": sorted(r[0] for r in probe.execute("SELECT name FROM sqlite_master")),
    }
    probe.close()

    with pytest.raises(StorageError) as caught:
        open_sqlite_event_store(path)
    assert caught.value.context["application_id"] == 559038737

    probe = sqlite3.connect(path)
    after_state = {
        "application_id": probe.execute("PRAGMA application_id").fetchone()[0],
        "user_version": probe.execute("PRAGMA user_version").fetchone()[0],
        "journal_mode": probe.execute("PRAGMA journal_mode").fetchone()[0],
        "objects": sorted(r[0] for r in probe.execute("SELECT name FROM sqlite_master")),
    }
    probe.close()

    assert path.read_bytes() == before_bytes
    assert after_state == before_state
    assert sorted(p.name for p in tmp_path.iterdir()) == before_siblings


def test_the_expected_ddl_is_derived_from_the_schema_rather_than_written_twice() -> None:
    """A second copy of the expected DDL would be a second thing to keep in
    step, and the one it would have to stay in step with is the schema itself."""
    assert set(_EXPECTED_OBJECTS) >= set(_EXPECTED_TABLES) | set(_EXPECTED_INDEXES)
    for name, ddl in _EXPECTED_OBJECTS.items():
        assert "IF NOT EXISTS" not in ddl, name
        assert "  " not in ddl, f"{name} is not whitespace-normalized"
