"""Append-only archive for raw source payloads.

Deliberately the dumbest thing that satisfies core invariant 7: bytes go in, they
are named by their own hash, and an existing file is never overwritten. It is not
the event store — that is an M2 deliverable with durability and replay
requirements this does not attempt to meet — and it holds no index, no query
path, and no schema beyond the provenance sidecar.
"""

from __future__ import annotations

import os
import stat
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
    recorded = provenance
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
        # But a repair is not a first-hand record. These bytes were retrieved at
        # some earlier moment from some earlier URL, and this call knows only when
        # *it* fetched them — so the reconstruction is marked rather than passed
        # off as the original provenance (invariant 7).
        recorded = SourceProvenanceV1(**{**provenance.model_dump(), "reconstructed": True})

    target.parent.mkdir(parents=True, exist_ok=True)
    # Sidecar first, then payload, each renamed into place: a torn write then
    # leaves at worst an orphan sidecar, never a payload whose provenance is
    # unknown and never a truncated file that fails its own hash check.
    _write_atomically(
        sidecar,
        orjson.dumps(recorded.to_record(), option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS),
    )
    _write_atomically(target, raw)
    return target


def _write_atomically(path: Path, payload: bytes) -> None:
    """Write via a temp file and rename, refusing to follow a symlink at either.

    ``os.replace`` onto the final path replaces a symlink rather than writing
    through it, but the temp path needs its own guard: a pre-placed symlink there
    would let a payload be written straight through to a file outside the archive.

    The temp file's contents are ``fsync``-ed before the rename, and the
    containing directory is ``fsync``-ed after it, so a crash cannot make the
    rename durable before the bytes it points at are (`docs/BACKLOG.md`: "a
    power loss can make the rename durable before the contents"). Without the
    first fsync, the rename can reach disk while the write behind it is still
    sitting in the page cache; without the second, the directory entry created
    by the rename is not itself guaranteed durable until its own fsync
    completes. An observation whose raw payload cannot be read back after a
    crash cannot satisfy core invariant 7, and the M2 event store's
    ``raw_payload_sha256`` reference depends on this file actually being there.
    """
    temporary = path.with_name(f"{path.name}.partial")
    descriptor = _open_temporary(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    # The rename and the directory fsync are inside the guard too. Independent
    # testing and security review both measured them outside it, with two
    # consequences: the failure crossed write_raw_payload's boundary as a bare
    # OSError with no ARGOS code, and -- worse -- the rename this fsync exists
    # to make durable had *already happened*, so the call reported failure while
    # the file was genuinely on disk. A durability routine that lies about
    # whether it succeeded is worse than one that does not sync at all.
    try:
        os.replace(temporary, path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise StorageError(
            "could not move the archived payload into place",
            path=str(path),
            error=str(error),
        ) from error
    try:
        _fsync_directory(path.parent)
    except OSError as error:
        raise StorageError(
            "the archived payload was written but its directory entry could not "
            "be made durable; the file is present and readable now, but a crash "
            "before the next sync may lose it",
            path=str(path),
            error=str(error),
        ) from error


def _open_temporary(temporary: Path) -> int:
    """Open the temp file, clearing a stale one left behind by a real crash.

    ``O_EXCL`` is the right guard against a pre-placed symlink, but adversarial
    testing showed it also means a ``.partial`` left by an actual process kill
    blocks *every future write of that hash, permanently*, with a bare
    ``FileExistsError`` — and that it wedges the documented sidecar repair path
    (``write_raw_payload``: "re-archiving identical bytes is the only chance to
    repair it"). That is precisely the crash this slice's fsync work exists to
    reason about, so leaving the archive unable to heal from it would be the
    wrong half of the problem solved.

    Clearing it is safe because the archive is content-addressed: a ``.partial``
    is unreferenced by construction — nothing can have read it, since only the
    post-rename name is ever read — and the caller is about to rewrite the same
    bytes under the same hash.

    **Only a regular file is cleared.** The first draft of this repair
    unconditionally unlinked whatever sat at the temp path, which silently
    downgraded the M1 symlink guard from "refuse" to "delete and proceed" —
    ``O_CREAT | O_EXCL`` reports a planted symlink as ``FileExistsError``, not
    ``ELOOP``, so the retry swallowed exactly the attack the guard exists to
    stop. The existing symlink regression test caught it. Anything that is not
    a regular file is refused, loudly, as before: a crash leaves a regular file
    behind, and an attacker leaves something else.
    """
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
    try:
        return os.open(temporary, flags, 0o600)
    except FileExistsError:
        pass

    try:
        existing = os.lstat(temporary)
    except OSError as error:
        raise StorageError(
            "a partial file blocks this archive write and could not be inspected",
            path=str(temporary),
            error=str(error),
        ) from error
    if not stat.S_ISREG(existing.st_mode):
        raise ImmutabilityViolationError(
            "the archive temp path is not a regular file; refusing to clear it",
            path=str(temporary),
        )

    try:
        os.unlink(temporary)
        return os.open(temporary, flags, 0o600)
    except OSError as error:
        raise StorageError(
            "a stale partial file blocks this archive write and could not be cleared",
            path=str(temporary),
            error=str(error),
        ) from error


def _fsync_directory(directory: Path) -> None:
    """``fsync`` a directory so a rename inside it is durable, not just visible.

    POSIX only guarantees a rename survives a crash once the directory entry
    itself has been synced; syncing the file it points at is not enough.
    """
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
