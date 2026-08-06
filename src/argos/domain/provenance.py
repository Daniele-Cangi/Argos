"""Provenance for everything ARGOS reads from a public source.

Core invariant 7: raw data is immutable. Normalization produces a new versioned
representation linked back to the bytes it came from, never a replacement for
them — so every normalized record carries the SHA-256 of its source payload and
every source payload carries where and when it was retrieved.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import ClassVar

from pydantic import Field, field_validator

from argos.clock import ensure_utc
from argos.domain.versioning import VersionedModel

SHA256_LENGTH = 64


def sha256_hex(payload: bytes) -> str:
    """Return the hex SHA-256 of a raw source payload."""
    return hashlib.sha256(payload).hexdigest()


class SourceProvenanceV1(VersionedModel):
    """Where a raw payload came from, and what it hashed to when it arrived."""

    schema_version: ClassVar[str] = "source_provenance.v1"

    source: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    http_status: int = Field(ge=100, le=599)
    retrieved_at: datetime
    raw_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    byte_length: int = Field(ge=0)

    @field_validator("retrieved_at")
    @classmethod
    def _anchor_retrieved_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("raw_sha256")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        lowered = value.lower()
        if not all(character in "0123456789abcdef" for character in lowered):
            raise ValueError("raw_sha256 must be hexadecimal")
        return lowered

    def matches(self, payload: bytes) -> bool:
        """Return whether ``payload`` is the exact byte string this describes."""
        return sha256_hex(payload) == self.raw_sha256 and len(payload) == self.byte_length
