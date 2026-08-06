"""Append-only archive for raw source payloads.

Deliberately the dumbest thing that satisfies core invariant 7: bytes go in, they
are named by their own hash, and an existing file is never overwritten. It is not
the event store — that is an M2 deliverable with durability and replay
requirements this does not attempt to meet — and it holds no index, no query
path, and no schema beyond the provenance sidecar.
"""

from __future__ import annotations

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

    target = directory / provenance.source / f"{provenance.raw_sha256}.raw.json"
    if target.exists():
        if target.read_bytes() != raw:
            raise ImmutabilityViolationError(
                "an archived payload with this hash holds different bytes",
                path=str(target),
            )
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    target.with_name(f"{provenance.raw_sha256}.meta.json").write_bytes(
        orjson.dumps(provenance.to_record(), option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
    )
    return target


def read_raw_payload(directory: Path, raw_sha256: str) -> tuple[bytes, SourceProvenanceV1]:
    """Read back an archived payload, verifying it still hashes to its name."""
    matches = sorted(directory.glob(f"*/{raw_sha256}.raw.json"))
    if not matches:
        raise StorageError("no archived payload with that hash", raw_sha256=raw_sha256)

    path = matches[0]
    raw = path.read_bytes()
    provenance = SourceProvenanceV1.from_record(
        orjson.loads(path.with_name(f"{raw_sha256}.meta.json").read_bytes())
    )
    if not provenance.matches(raw):
        raise ImmutabilityViolationError(
            "an archived payload no longer matches its recorded hash",
            path=str(path),
            recorded_sha256=raw_sha256,
            actual_sha256=sha256_hex(raw),
        )
    return raw, provenance
