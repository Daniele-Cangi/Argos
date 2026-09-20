"""Resumable single-owner polling with explicit failure gaps.

The transition functions are deterministic. Filesystem ownership and checkpoint
persistence are adapters, so fault tests can exercise the state machine without
network or wall-clock access.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from importlib import import_module
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, ClassVar, Protocol, Self, cast

import orjson
from pydantic import Field, field_validator, model_validator

from argos.clock import ensure_utc
from argos.domain.provenance import SHA256_LENGTH
from argos.domain.versioning import VersionedModel


class _WindowsLockModule(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, fd: int, mode: int, nbytes: int) -> None: ...


class _PosixLockModule(Protocol):
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, fd: int, operation: int) -> None: ...


def _lock_nonblocking(handle: BinaryIO) -> None:
    if os.name == "nt":
        windows_lock = cast(_WindowsLockModule, import_module("msvcrt"))
        windows_lock.locking(handle.fileno(), windows_lock.LK_NBLCK, 1)
    else:  # pragma: no cover - exercised by the Ubuntu CI job
        posix_lock = cast(_PosixLockModule, import_module("fcntl"))
        posix_lock.flock(handle.fileno(), posix_lock.LOCK_EX | posix_lock.LOCK_NB)


def _unlock(handle: BinaryIO) -> None:
    if os.name == "nt":
        windows_lock = cast(_WindowsLockModule, import_module("msvcrt"))
        windows_lock.locking(handle.fileno(), windows_lock.LK_UNLCK, 1)
    else:  # pragma: no cover - exercised by the Ubuntu CI job
        posix_lock = cast(_PosixLockModule, import_module("fcntl"))
        posix_lock.flock(handle.fileno(), posix_lock.LOCK_UN)


class MonitorGapV1(VersionedModel):
    """An interval during which the owner could not persist a successful poll."""

    schema_version: ClassVar[str] = "monitor_gap.v1"

    attempted_ordinal: int = Field(ge=0)
    started_at: datetime
    ended_at: datetime
    reason: str = Field(min_length=1)

    @field_validator("started_at", "ended_at")
    @classmethod
    def _utc_times(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _ordered(self) -> MonitorGapV1:
        if self.ended_at < self.started_at:
            raise ValueError("monitor gap end cannot precede its start")
        if not self.reason.strip():
            raise ValueError("monitor gap reason must not be blank")
        return self


class ResumableMonitorCheckpointV1(VersionedModel):
    """Last durably acknowledged poll and every observed interruption."""

    schema_version: ClassVar[str] = "resumable_monitor_checkpoint.v1"

    campaign_id: str = Field(min_length=1)
    configuration_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    next_ordinal: int = Field(ge=0)
    last_receipt_id: str | None = None
    last_record_sha256: str | None = Field(
        default=None, min_length=SHA256_LENGTH, max_length=SHA256_LENGTH
    )
    last_success_at: datetime | None = None
    updated_at: datetime
    gaps: tuple[MonitorGapV1, ...] = ()

    @field_validator("configuration_sha256", "last_record_sha256")
    @classmethod
    def _hex_digests(cls, value: str | None) -> str | None:
        if value is None:
            return None
        lowered = value.lower()
        if not all(character in "0123456789abcdef" for character in lowered):
            raise ValueError("checkpoint digests must be hexadecimal")
        return lowered

    @field_validator("last_success_at", "updated_at")
    @classmethod
    def _utc_times(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    @model_validator(mode="after")
    def _coherent_chain_head(self) -> ResumableMonitorCheckpointV1:
        head = (self.last_receipt_id, self.last_record_sha256, self.last_success_at)
        if self.next_ordinal == 0 and any(item is not None for item in head):
            raise ValueError("empty checkpoint cannot carry a receipt-chain head")
        if self.next_ordinal > 0 and any(item is None for item in head):
            raise ValueError("nonempty checkpoint requires a complete receipt-chain head")
        if self.last_success_at is not None and self.updated_at < self.last_success_at:
            raise ValueError("checkpoint update cannot precede its last success")
        if any(gap.attempted_ordinal > self.next_ordinal for gap in self.gaps):
            raise ValueError("gap cannot refer to a future ordinal")
        return self


@dataclass(frozen=True, slots=True)
class PollCommit:
    """Receipt-chain head returned after a poll is durably persisted."""

    ordinal: int
    receipt_id: str
    record_sha256: str
    persisted_at: datetime
    previous_receipt_id: str | None


def checkpoint_after_poll(
    checkpoint: ResumableMonitorCheckpointV1,
    commit: PollCommit,
) -> ResumableMonitorCheckpointV1:
    """Advance exactly one ordinal after verifying chain continuity."""

    persisted_at = ensure_utc(commit.persisted_at)
    if commit.ordinal != checkpoint.next_ordinal:
        raise ValueError("poll commit ordinal does not match checkpoint")
    if commit.previous_receipt_id != checkpoint.last_receipt_id:
        raise ValueError("poll commit breaks the receipt predecessor chain")
    if not commit.receipt_id.strip():
        raise ValueError("poll commit receipt id must not be blank")
    digest = commit.record_sha256.lower()
    if len(digest) != SHA256_LENGTH or not all(c in "0123456789abcdef" for c in digest):
        raise ValueError("poll commit record digest must be sha256 hexadecimal")
    if checkpoint.last_success_at is not None and persisted_at < checkpoint.last_success_at:
        raise ValueError("poll commit time regresses")
    return checkpoint.model_copy(
        update={
            "next_ordinal": checkpoint.next_ordinal + 1,
            "last_receipt_id": commit.receipt_id,
            "last_record_sha256": digest,
            "last_success_at": persisted_at,
            "updated_at": persisted_at,
        }
    )


def checkpoint_after_failure(
    checkpoint: ResumableMonitorCheckpointV1,
    *,
    started_at: datetime,
    ended_at: datetime,
    reason: str,
) -> ResumableMonitorCheckpointV1:
    """Record an interruption without consuming the unpersisted ordinal."""

    gap = MonitorGapV1(
        attempted_ordinal=checkpoint.next_ordinal,
        started_at=started_at,
        ended_at=ended_at,
        reason=reason,
    )
    return checkpoint.model_copy(
        update={"updated_at": gap.ended_at, "gaps": (*checkpoint.gaps, gap)}
    )


class ExclusiveFileLease:
    """Process-scoped OS lock; the kernel releases it after a crash."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: BinaryIO | None = None

    def __enter__(self) -> Self:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            _lock_nonblocking(handle)
        except OSError as error:
            handle.close()
            raise RuntimeError("monitor already has an exclusive owner") from error
        self._handle = handle
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        handle = self._handle
        if handle is None:
            return
        handle.seek(0)
        _unlock(handle)
        handle.close()
        self._handle = None


class ResumableMonitor:
    """Filesystem adapter that commits one poll or one explicit failure gap."""

    def __init__(self, checkpoint_path: Path, lock_path: Path) -> None:
        self._checkpoint_path = checkpoint_path
        self._lock_path = lock_path

    def load(self) -> ResumableMonitorCheckpointV1:
        raw = orjson.loads(self._checkpoint_path.read_bytes())
        if not isinstance(raw, dict):
            raise ValueError("monitor checkpoint is not a JSON object")
        return ResumableMonitorCheckpointV1.from_record(raw)

    def save(self, checkpoint: ResumableMonitorCheckpointV1) -> None:
        raw = orjson.dumps(checkpoint.to_record(), option=orjson.OPT_SORT_KEYS)
        temporary = self._checkpoint_path.with_suffix(self._checkpoint_path.suffix + ".partial")
        self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._checkpoint_path)

    def run_once(
        self,
        *,
        poll: Callable[[ResumableMonitorCheckpointV1], PollCommit],
        failure_time: Callable[[], datetime],
    ) -> ResumableMonitorCheckpointV1:
        with ExclusiveFileLease(self._lock_path):
            checkpoint = self.load()
            started_at = ensure_utc(failure_time())
            try:
                commit = poll(checkpoint)
            except Exception as error:
                ended_at = ensure_utc(failure_time())
                failed = checkpoint_after_failure(
                    checkpoint,
                    started_at=started_at,
                    ended_at=ended_at,
                    reason=f"{type(error).__name__}: {error}",
                )
                self.save(failed)
                raise
            advanced = checkpoint_after_poll(checkpoint, commit)
            self.save(advanced)
            return advanced
