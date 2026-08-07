"""Run manifests.

Core invariant 13: every run records configuration, code revision, schema
versions, and data provenance. The manifest is built from an injected clock so
that a replay run produces an identical manifest on every execution.

``run_manifest.v2`` is a breaking change from ``run_manifest.v1``: ``mode``
becomes an enum, ``working_tree`` joins ``code_revision`` so a dirty tree no
longer misattributes the code that produced a run, and ``schema_versions`` /
``input_provenance`` close the rest of invariant 13. No ``v1`` manifest has
ever been persisted, so there is no migration path and none is written —
compatibility wrappers are avoided per project convention when there is
nothing yet to be compatible with.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar, cast

from pydantic import Field, field_serializer, field_validator, model_validator

from argos.clock import Clock, ensure_utc
from argos.config.settings import Settings
from argos.domain.provenance import SourceProvenanceV1
from argos.domain.versioning import VersionedModel, freeze, thaw

_SchemaVersion = Annotated[str, Field(min_length=1)]


class RunMode(StrEnum):
    """What kind of run produced a manifest.

    Membership tracks *actual* need, not anticipated milestones: discovery and
    audit exist in M1, capture is the next M2 deliverable, replay is M3's.
    ``inspect`` is the standalone ``argos manifest`` command's own mode — it
    describes no pipeline run, only a configuration snapshot, and predates all
    four. Nothing beyond M3 is represented; a future milestone adds its member
    when it actually needs one.
    """

    DISCOVER = "discover"
    AUDIT = "audit"
    CAPTURE = "capture"
    REPLAY = "replay"
    INSPECT = "inspect"


class WorkingTreeStatus(StrEnum):
    """Whether the working tree matched ``code_revision`` exactly.

    A tri-state rather than ``bool | None``: "unknown" and "clean" are
    different claims, and collapsing them would let a manifest built outside
    any git checkout (a wheel install) *look* like it asserted cleanliness.
    """

    CLEAN = "clean"
    DIRTY = "dirty"
    UNKNOWN = "unknown"


class RunManifest(VersionedModel):
    """Immutable description of one ARGOS run."""

    schema_version: ClassVar[str] = "run_manifest.v2"

    run_id: str = Field(min_length=1)
    mode: RunMode
    created_at: datetime
    argos_version: str
    code_revision: str | None = None
    working_tree: WorkingTreeStatus = WorkingTreeStatus.UNKNOWN
    """``UNKNOWN`` unless a git toplevel matching the running code was verified;
    see :func:`argos.cli._code_revision`. Never claim clean/dirty without that
    verification (enforced below), or a wheel install could report a stale
    ``CLEAN`` for code it cannot actually identify."""

    config_fingerprint: str
    settings_snapshot: Mapping[str, Any]

    schema_versions: tuple[_SchemaVersion, ...] = ()
    """The schema versions of every record this run reads or writes — for
    example ``("market_definition.v1", "compiled_market_contract.v1")`` for a
    discovery-and-compile run. Deduplicated and sorted on validation so two
    runs over the same schemas serialize identically regardless of the order
    the caller happened to collect them in. Distinct from this class's own
    ``schema_version``: that names the manifest's own record; this names the
    data the run produced or consumed."""

    input_provenance: tuple[SourceProvenanceV1, ...] = ()
    """Which raw payloads (by ``raw_payload_sha256``) and which source
    endpoints fed this run, reusing :class:`SourceProvenanceV1` rather than a
    parallel shape. Sorted on validation for the same reason as
    ``schema_versions``. Empty for a run that touches no source payload (for
    example ``inspect``), never omitted for one that does."""

    @field_validator("created_at")
    @classmethod
    def _normalize_created_at(cls, value: datetime) -> datetime:
        """Anchor the timestamp on validation, not only in :func:`build_run_manifest`.

        A manifest read back from disk must be as trustworthy as one just built,
        and two manifests describing the same instant must serialize identically.
        """
        return ensure_utc(value)

    @field_validator("settings_snapshot")
    @classmethod
    def _freeze_snapshot(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], freeze(value))

    @field_serializer("settings_snapshot")
    def _serialize_snapshot(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], thaw(value))

    @field_validator("schema_versions")
    @classmethod
    def _canonicalize_schema_versions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @field_validator("input_provenance")
    @classmethod
    def _sort_input_provenance(
        cls, value: tuple[SourceProvenanceV1, ...]
    ) -> tuple[SourceProvenanceV1, ...]:
        return tuple(
            sorted(
                value,
                key=lambda item: (
                    item.source,
                    item.endpoint,
                    item.raw_sha256,
                    item.retrieved_at.isoformat(),
                    item.reconstructed,
                ),
            )
        )

    @model_validator(mode="after")
    def _working_tree_needs_a_trusted_revision(self) -> RunManifest:
        if self.code_revision is None and self.working_tree is not WorkingTreeStatus.UNKNOWN:
            raise ValueError(
                "working_tree cannot be clean/dirty without a trusted code_revision; "
                "use WorkingTreeStatus.UNKNOWN when the revision could not be proven"
            )
        return self

    def describe(self) -> str:
        """Return a one-line human summary for CLI output and logs."""
        revision = self.code_revision or "unknown-revision"
        return (
            f"{self.run_id} mode={self.mode} at={self.created_at.isoformat()} "
            f"revision={revision} working_tree={self.working_tree} "
            f"config={self.config_fingerprint[:12]}"
        )


def build_run_manifest(
    *,
    settings: Settings,
    clock: Clock,
    run_id: str,
    mode: RunMode,
    code_revision: str | None = None,
    working_tree: WorkingTreeStatus = WorkingTreeStatus.UNKNOWN,
    schema_versions: Collection[str] = (),
    input_provenance: Collection[SourceProvenanceV1] = (),
) -> RunManifest:
    """Build a manifest for a run, stamping it with the injected clock's time."""
    from argos import __version__

    return RunManifest(
        run_id=run_id,
        mode=mode,
        created_at=ensure_utc(clock.now()),
        argos_version=__version__,
        code_revision=code_revision,
        working_tree=working_tree,
        config_fingerprint=settings.fingerprint(),
        settings_snapshot=settings.snapshot(),
        schema_versions=tuple(schema_versions),
        input_provenance=tuple(input_provenance),
    )
