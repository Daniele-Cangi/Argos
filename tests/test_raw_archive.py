"""The append-only raw payload archive.

Core invariant 7: raw data is immutable. The archive's whole job is to make that
mechanical rather than a convention.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
import pytest
from pydantic import ValidationError

from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.errors import ImmutabilityViolationError, SchemaVersionError, StorageError
from argos.store import archive_relative_location, read_raw_payload, write_raw_payload

RAW = b'[{"id":"1","question":"Will it?"}]'
RETRIEVED = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def _provenance(raw: bytes = RAW, **overrides: object) -> SourceProvenanceV1:
    fields: dict[str, object] = {
        "source": "gamma",
        "endpoint": "https://gamma-api.polymarket.com/markets?limit=1",
        "http_status": 200,
        "retrieved_at": RETRIEVED,
        "raw_sha256": sha256_hex(raw),
        "byte_length": len(raw),
    }
    fields.update(overrides)
    return SourceProvenanceV1(**fields)  # type: ignore[arg-type]


def test_a_payload_is_stored_under_its_own_hash(tmp_path: Path) -> None:
    path = write_raw_payload(tmp_path, raw=RAW, provenance=_provenance())
    assert path.name == f"{sha256_hex(RAW)}.raw.json"
    assert path.parent.name == "gamma"
    assert path.read_bytes() == RAW


def test_the_provenance_is_stored_beside_the_bytes(tmp_path: Path) -> None:
    path = write_raw_payload(tmp_path, raw=RAW, provenance=_provenance())
    meta = orjson.loads(path.with_name(f"{sha256_hex(RAW)}.meta.json").read_bytes())
    assert meta["schema_version"] == "source_provenance.v1"
    assert meta["raw_sha256"] == sha256_hex(RAW)
    assert meta["endpoint"].startswith("https://")


def test_rewriting_the_same_payload_is_idempotent(tmp_path: Path) -> None:
    """Re-running a capture must not fail just because it saw the same page."""
    first = write_raw_payload(tmp_path, raw=RAW, provenance=_provenance())
    second = write_raw_payload(tmp_path, raw=RAW, provenance=_provenance())
    assert first == second


def test_an_archived_payload_is_never_replaced(tmp_path: Path) -> None:
    path = write_raw_payload(tmp_path, raw=RAW, provenance=_provenance())
    path.write_bytes(b'[{"id":"1","question":"Tampered"}]')
    with pytest.raises(ImmutabilityViolationError):
        write_raw_payload(tmp_path, raw=RAW, provenance=_provenance())


def test_a_payload_that_contradicts_its_provenance_is_refused(tmp_path: Path) -> None:
    with pytest.raises(StorageError) as caught:
        write_raw_payload(tmp_path, raw=b"different bytes", provenance=_provenance())
    assert caught.value.code == "argos.storage"


def test_a_payload_reads_back_byte_identical(tmp_path: Path) -> None:
    write_raw_payload(tmp_path, raw=RAW, provenance=_provenance())
    raw, provenance = read_raw_payload(tmp_path, sha256_hex(RAW))
    assert raw == RAW
    assert provenance == _provenance()


@pytest.mark.parametrize("hostile", ["../outside", "/tmp/absolute", "a/../../b", "Gamma"])
def test_the_provenance_contract_refuses_a_source_name_that_is_a_path(hostile: str) -> None:
    """The archive builds a directory from `source`, so the contract constrains it."""
    with pytest.raises(ValidationError):
        _provenance(source=hostile)


@pytest.mark.parametrize("hostile", ["../outside", "/tmp/absolute"])
def test_the_writer_refuses_to_escape_even_if_a_bad_source_gets_through(
    tmp_path: Path, hostile: str
) -> None:
    """Defence in depth: `model_construct` skips validators, so the write checks too."""
    archive = tmp_path / "archive"
    archive.mkdir()
    smuggled = SourceProvenanceV1.model_construct(
        **{**_provenance().model_dump(), "source": hostile}
    )
    with pytest.raises(StorageError):
        write_raw_payload(archive, raw=RAW, provenance=smuggled)
    assert list(tmp_path.iterdir()) == [archive]


def test_reading_an_unknown_hash_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(StorageError):
        read_raw_payload(tmp_path, "f" * 64)


def test_tampering_is_detected_on_read(tmp_path: Path) -> None:
    """A silently edited capture file is worse than a missing one."""
    path = write_raw_payload(tmp_path, raw=RAW, provenance=_provenance())
    path.write_bytes(RAW + b" ")
    with pytest.raises(ImmutabilityViolationError):
        read_raw_payload(tmp_path, sha256_hex(RAW))


# --- more than one payload ------------------------------------------------------------


def test_two_different_payloads_coexist_under_their_own_hashes(tmp_path: Path) -> None:
    other = b'[{"id":"2","question":"Will it not?"}]'
    first = write_raw_payload(tmp_path, raw=RAW, provenance=_provenance())
    second = write_raw_payload(tmp_path, raw=other, provenance=_provenance(other))
    assert first != second
    assert read_raw_payload(tmp_path, sha256_hex(RAW))[0] == RAW
    assert read_raw_payload(tmp_path, sha256_hex(other))[0] == other


def test_an_empty_payload_is_archivable(tmp_path: Path) -> None:
    """`[]` past the last page is a real answer and must stay auditable."""
    write_raw_payload(tmp_path, raw=b"", provenance=_provenance(b""))
    raw, provenance = read_raw_payload(tmp_path, sha256_hex(b""))
    assert raw == b""
    assert provenance.byte_length == 0


def test_payloads_from_different_sources_do_not_collide(tmp_path: Path) -> None:
    write_raw_payload(tmp_path, raw=RAW, provenance=_provenance())
    write_raw_payload(tmp_path, raw=RAW, provenance=_provenance(source="clob"))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["clob", "gamma"]


# --- a hash is not a path -------------------------------------------------------------


@pytest.mark.parametrize(
    "lookup",
    ["../../etc/passwd", "/etc/passwd", "..", "gamma/" + "a" * 64],
    ids=["traversal", "absolute", "parent", "nested"],
)
def test_a_lookup_key_cannot_escape_the_archive_directory(tmp_path: Path, lookup: str) -> None:
    """The lookup key is interpolated into a glob; it must never reach outside."""
    write_raw_payload(tmp_path, raw=RAW, provenance=_provenance())
    (tmp_path.parent / "outside.raw.json").write_bytes(b"secret")
    with pytest.raises(StorageError):
        read_raw_payload(tmp_path, lookup)


# --- the provenance sidecar -----------------------------------------------------------


def test_a_foreign_sidecar_schema_is_refused(tmp_path: Path) -> None:
    path = write_raw_payload(tmp_path, raw=RAW, provenance=_provenance())
    meta = path.with_name(f"{sha256_hex(RAW)}.meta.json")
    record = orjson.loads(meta.read_bytes())
    record["schema_version"] = "source_provenance.v9"
    meta.write_bytes(orjson.dumps(record))
    with pytest.raises(SchemaVersionError):
        read_raw_payload(tmp_path, sha256_hex(RAW))


@pytest.mark.parametrize("damage", ["missing", "corrupt", "empty"])
def test_a_damaged_sidecar_is_reported_as_a_storage_error(tmp_path: Path, damage: str) -> None:
    path = write_raw_payload(tmp_path, raw=RAW, provenance=_provenance())
    meta = path.with_name(f"{sha256_hex(RAW)}.meta.json")
    if damage == "missing":
        meta.unlink()
    elif damage == "corrupt":
        meta.write_bytes(b"{not json")
    else:
        meta.write_bytes(b"")

    with pytest.raises(StorageError):
        read_raw_payload(tmp_path, sha256_hex(RAW))


def test_a_restored_sidecar_is_marked_as_a_reconstruction(tmp_path: Path) -> None:
    """A repair is not a first-hand record: the bytes were retrieved earlier, from
    somewhere this call knows nothing about. Writing the new fetch's time and URL
    as if they were the original would quietly falsify provenance (invariant 7)."""
    first = _provenance(endpoint="https://gamma-api.polymarket.com/first")
    path = write_raw_payload(tmp_path, raw=RAW, provenance=first)
    path.with_name(f"{sha256_hex(RAW)}.meta.json").unlink()

    later = _provenance(
        endpoint="https://gamma-api.polymarket.com/second",
        retrieved_at=RETRIEVED + timedelta(days=200),
    )
    write_raw_payload(tmp_path, raw=RAW, provenance=later)

    _, restored = read_raw_payload(tmp_path, sha256_hex(RAW))
    assert restored.reconstructed is True


def test_a_first_hand_record_is_not_marked_as_reconstructed(tmp_path: Path) -> None:
    write_raw_payload(tmp_path, raw=RAW, provenance=_provenance())
    _, provenance = read_raw_payload(tmp_path, sha256_hex(RAW))
    assert provenance.reconstructed is False


def test_a_symlink_at_the_temp_path_cannot_be_written_through(tmp_path: Path) -> None:
    """`os.replace` protects the final path; the temp path needs its own guard.

    The refusal is now an `ImmutabilityViolationError` rather than the bare
    `FileExistsError` `O_EXCL` used to surface, because the stale-`.partial`
    repair added in the M2 store slice has to tell a crash leftover (a regular
    file, clearable) apart from a planted symlink (refused). Asserting the
    taxonomy type rather than `OSError` is what keeps the two apart: an
    unconditional unlink would still have passed an `OSError` assertion by
    deleting the symlink and reporting nothing at all.
    """
    archive = tmp_path / "archive"
    (archive / "gamma").mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_bytes(b"original")
    planted = archive / "gamma" / f"{sha256_hex(RAW)}.meta.json.partial"
    planted.symlink_to(victim)

    with pytest.raises(ImmutabilityViolationError):
        write_raw_payload(archive, raw=RAW, provenance=_provenance())
    assert victim.read_bytes() == b"original"
    assert planted.is_symlink(), "the planted symlink must be refused, not silently removed"


def test_rewriting_a_payload_restores_a_lost_sidecar(tmp_path: Path) -> None:
    path = write_raw_payload(tmp_path, raw=RAW, provenance=_provenance())
    path.with_name(f"{sha256_hex(RAW)}.meta.json").unlink()
    write_raw_payload(tmp_path, raw=RAW, provenance=_provenance())
    assert path.with_name(f"{sha256_hex(RAW)}.meta.json").exists()


# --- the stored location is join-safe (M2 closure security review, S2) ------------


@pytest.mark.parametrize(
    "hostile_source",
    ["../elsewhere", "/etc", "a/b", "a\b", ".", "..", "a b", "A", ""],
)
def test_a_hostile_source_can_never_reach_a_stored_location(hostile_source: str) -> None:
    """`archive_relative_location` composes a value that now lives in a durable
    record and that a consumer will join back onto an archive root, so a
    separator or a traversal segment in it would escape that root at read time
    -- one layer further out than the containment check in `write_raw_payload`,
    which only guards the write.

    The defence is that `SourceProvenanceV1.source` cannot hold such a value at
    all: `^[a-z0-9][a-z0-9_-]{0,31}$` admits no slash, backslash, or dot. Asserted at
    the contract rather than at the composition, because that is where the
    property actually lives -- and a test that only checked the composed string
    would keep passing if the pattern were ever relaxed.
    """
    with pytest.raises(ValidationError):
        SourceProvenanceV1(
            source=hostile_source,
            endpoint="/book",
            http_status=200,
            retrieved_at=RETRIEVED,
            raw_sha256=sha256_hex(b"{}"),
            byte_length=2,
        )


def test_a_stored_location_stays_inside_the_archive_when_joined_back(tmp_path: Path) -> None:
    """The composed value, joined to a root, resolves inside that root -- and it
    is the same string `write_raw_payload` wrote to, not a parallel guess."""
    payload = b'{"ok": true}'
    provenance = SourceProvenanceV1(
        source="clob_market_ws",
        endpoint="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        http_status=None,
        retrieved_at=RETRIEVED,
        raw_sha256=sha256_hex(payload),
        byte_length=len(payload),
    )
    written = write_raw_payload(tmp_path, raw=payload, provenance=provenance)
    location = archive_relative_location(provenance)

    assert not Path(location).is_absolute()
    joined = (tmp_path / location).resolve()
    assert joined.is_relative_to(tmp_path.resolve())
    assert joined == written.resolve()
