"""The append-only raw payload archive.

Core invariant 7: raw data is immutable. The archive's whole job is to make that
mechanical rather than a convention.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest

from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.errors import ImmutabilityViolationError, StorageError
from argos.store import read_raw_payload, write_raw_payload

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


def test_reading_an_unknown_hash_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(StorageError):
        read_raw_payload(tmp_path, "f" * 64)


def test_tampering_is_detected_on_read(tmp_path: Path) -> None:
    """A silently edited capture file is worse than a missing one."""
    path = write_raw_payload(tmp_path, raw=RAW, provenance=_provenance())
    path.write_bytes(RAW + b" ")
    with pytest.raises(ImmutabilityViolationError):
        read_raw_payload(tmp_path, sha256_hex(RAW))
