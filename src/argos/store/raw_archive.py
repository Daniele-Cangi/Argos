"""Append-only archive for raw source payloads.

Deliberately the dumbest thing that satisfies core invariant 7: bytes go in, they
are named by their own hash, and an existing file is never overwritten. It is not
the event store — that is an M2 deliverable with durability and replay
requirements this does not attempt to meet — and it holds no index, no query
path, and no schema beyond the provenance sidecar.
"""

from __future__ import annotations

import os
from pathlib import Path

import orjson

from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.errors import ImmutabilityViolationError, StorageError


def write_raw_payload(
    directory: Path,
    *,
    raw: bytes,
    provenance: SourceProvenanceV1,
) -> Path:
    """Archive ``raw`` under ``directory`` and return the path written.

    Refuses to write if the payload does not match its provenance, and refuses to
    replace an existing archive entry whose bytes differ — an identical rewrite is
    idempotent and allowed, because re-running a capture should not fail.
    """
    if not provenance.matches(raw):
        raise StorageError(
            "refusing to archive a payload that does not match its provenance",
            expected_sha256=provenance.raw_sha256,
            actual_sha256=sha256_hex(raw),
        )

    root = directory.resolve()
    target = root / provenance.source / f"{provenance.raw_sha256}.raw.json"
    if not target.resolve().parent.is_relative_to(root):
        # `source` is a plain string on the provenance contract, so a value like
        # "../elsewhere" or an absolute path would place the write outside the
        # archive entirely — pathlib discards the root on an absolute segment.
        raise StorageError(
            "refusing to write outside the archive directory",
            source=provenance.source,
            directory=str(root),
        )

    sidecar = target.with_name(f"{provenance.raw_sha256}.meta.json")
    if target.exists():
        if target.read_bytes() != raw:
            raise ImmutabilityViolationError(
                "an archived payload with this hash holds different bytes",
                path=str(target),
            )
        # Do not return early: a crash between the two writes can leave a payload
        # with no provenance, and re-archiving identical bytes is the only chance
        # to repair it.
        if sidecar.exists():
            return target

    target.parent.mkdir(parents=True, exist_ok=True)
    # Sidecar first, then payload, each renamed into place: a torn write then
    # leaves at worst an orphan sidecar, never a payload whose provenance is
    # unknown and never a truncated file that fails its own hash check.
    _write_atomically(
        sidecar,
        orjson.dumps(provenance.to_record(), option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS),
    )
    _write_atomically(target, raw)
    return target


def _write_atomically(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f"{path.name}.partial")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def read_raw_payload(directory: Path, raw_sha256: str) -> tuple[bytes, SourceProvenanceV1]:
    """Read back an archived payload, verifying it still hashes to its name."""
    matches = sorted(directory.glob(f"*/{raw_sha256}.raw.json"))
    if not matches:
        raise StorageError("no archived payload with that hash", raw_sha256=raw_sha256)

    path = matches[0]
    raw = path.read_bytes()
    sidecar = path.with_name(f"{raw_sha256}.meta.json")
    try:
        record = orjson.loads(sidecar.read_bytes())
    except FileNotFoundError as error:
        # Errors are data (invariant 14): a foreign exception here escapes the
        # taxonomy, so no caller can count or explain it.
        raise StorageError(
            "archived payload has no provenance sidecar", path=str(sidecar)
        ) from error
    except orjson.JSONDecodeError as error:
        raise StorageError(
            "archived provenance sidecar is not readable JSON", path=str(sidecar)
        ) from error
    provenance = SourceProvenanceV1.from_record(record)
    if not provenance.matches(raw):
        raise ImmutabilityViolationError(
            "an archived payload no longer matches its recorded hash",
            path=str(path),
            recorded_sha256=raw_sha256,
            actual_sha256=sha256_hex(raw),
        )
    return raw, provenance
