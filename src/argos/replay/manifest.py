"""The replay manifest, and the one function that produces one.

``docs/04_DATA_CONTRACTS.md`` specifies ``ReplayManifestV1`` and then states the
property this whole milestone exists to establish: "repeated replay of identical
input, code, config, and mode must produce identical state hash and record
counts". Two amendments to that specification are recorded here rather than made
silently:

- ``replay_mode`` gains ``stepwise``, because ``docs/07_MILESTONES.md`` names
  stepwise a deliverable in the same breath as accelerated. Dropping it to fit a
  two-value enum would be the "specified contract silently dropped" failure that
  blocked M1.
- ``working_tree`` joins ``code_revision``, for the reason already accepted for
  ``RunManifest``: a dirty tree makes a revision string misattribute the code
  that produced a run, and a replay whose entire claim is reproducibility is the
  worst place to leave that ambiguous.

The manifest also states, in its own field, something the specification's phrase
"identical ... and mode" gets subtly wrong: the mode is **not** an input to the
hash. Two replays of one capture in three different pacing modes produce one
hash, which is asserted directly (ADR-0012 section 7). The mode is recorded
because it describes how the run was performed, not because it changes what the
run produced.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import Field, field_serializer, field_validator

from argos.clock import ensure_utc
from argos.config.manifest import WorkingTreeStatus
from argos.domain.versioning import FrozenDict, VersionedModel, freeze, thaw
from argos.projections.dispatch import STATE_HASH_VERSION
from argos.replay.session import ReplayMode

__all__ = ["LATE_EVENT_POLICY_KIND", "ReplayManifestV1", "ReplayResultStatus"]

LATE_EVENT_POLICY_KIND = "mark_only"
"""The only late-event policy that exists, named so the manifest says which.

ADR-0012 section 4: the watermark marks a late event and applies it in arrival
order; it never reorders, buffers, or drops. Recording the *kind* alongside the
tolerance means a future policy cannot be introduced without every existing
manifest becoming visibly a different policy's output.
"""


class ReplayResultStatus(StrEnum):
    """How a replay ended."""

    COMPLETED = "completed"
    FAILED = "failed"


class ReplayManifestV1(VersionedModel):
    """One replay run, recorded so it can be compared with another."""

    schema_version: ClassVar[str] = "replay_manifest.v1"

    replay_run_id: str = Field(min_length=1)
    source_capture_run_id: str = Field(min_length=1)

    code_revision: str | None = None
    working_tree: WorkingTreeStatus = WorkingTreeStatus.UNKNOWN
    config_fingerprint: str = Field(min_length=1)
    """Named ``config_sha256`` in ``docs/04_DATA_CONTRACTS.md``; this is the same
    value under the name the rest of the codebase already uses. Since 2026-08-17
    it covers experiment-scoped settings only, so a replay writing to a different
    directory records the same fingerprint — which is what makes "identical
    config" a usable claim for a replay at all."""

    started_at: datetime
    finished_at: datetime

    replay_mode: ReplayMode
    state_hash_version: str = STATE_HASH_VERSION
    """Carried explicitly so a pinned hash from an older encoding is visibly
    from an older encoding, rather than merely unequal."""

    input_first_sequence: int | None = None
    input_last_sequence: int | None = None
    input_arrival_count: int = Field(ge=0)
    """``input_event_count`` in the specification. Renamed because what is
    counted is *arrivals* — a duplicate is one more arrival of the same event —
    and the distinction is the whole reason the delivery record exists."""

    output_state_hash: str = Field(min_length=1)
    output_record_counts: Mapping[str, Any]

    late_event_policy: Mapping[str, Any]
    result_status: ReplayResultStatus

    source_completion_status: str | None = None
    """How the *capture* being replayed ended, or ``None`` if it never closed.

    Not in the specification, and it belongs here: replaying an interrupted
    capture is legitimate and often the point, but a manifest that did not say
    so would present a partial capture's state hash as if it described a
    complete one.
    """

    @field_validator("started_at", "finished_at")
    @classmethod
    def _anchor(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("output_record_counts", "late_event_policy")
    @classmethod
    def _freeze(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        frozen: Mapping[str, Any] = freeze(value)
        return frozen

    @field_serializer("output_record_counts", "late_event_policy")
    def _thaw(self, value: Mapping[str, Any]) -> dict[str, Any]:
        thawed: dict[str, Any] = thaw(value)
        return thawed

    def describe(self) -> str:
        """A one-line human summary for CLI output."""
        return (
            f"{self.replay_run_id} <- {self.source_capture_run_id} "
            f"mode={self.replay_mode} arrivals={self.input_arrival_count} "
            f"state={self.output_state_hash[:16]} status={self.result_status}"
        )


def late_event_policy_record(allowed_lateness: timedelta) -> Mapping[str, Any]:
    """The manifest's description of the late-event policy in force.

    Microseconds as an integer rather than seconds as a float: a manifest is
    compared byte for byte against another manifest, and a float's rendering is
    the wrong place to discover that ``0.1`` is not ``0.1``.
    """
    return FrozenDict(
        {
            "kind": LATE_EVENT_POLICY_KIND,
            "allowed_lateness_microseconds": int(
                allowed_lateness / timedelta(microseconds=1),
            ),
        }
    )
