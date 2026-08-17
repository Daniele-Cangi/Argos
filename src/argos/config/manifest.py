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

``run_manifest.v5`` adds ``run_parameters``, closing a real gap in core
invariant 13 that the M2 closure architecture review found: *"every run records
configuration, code revision, schema versions, and data provenance"*, and a
capture run recorded none of the inputs that are not settings. The important
one is ``subscribed_token_ids``. ``argos.ingestion.capture`` fans out over the
*configured* token set, sorted — its own docstring calls that "the load-bearing
one for M3", because it is what makes ``ingest_sequence`` allocation a function
of configuration rather than of connection topology — and that set arrived from
``--token-id`` flags, which are not settings and were therefore in no durable
artifact anywhere. A capture could not be reproduced from its own manifest.

Two version bumps in one day, for two unrelated reasons, is worth a word. The
alternative was to widen ``v4`` after committing it, which would leave two
different record shapes sharing one version string inside this branch's own
history — the exact ambiguity the ``v3``→``v4`` bump exists to prevent. A
version number is cheap; a version that means two things is not.

``run_manifest.v4`` was **not** a clean bump, and the difference matters. No
field was added or removed; what changed is the *meaning* of
``config_fingerprint``, which now covers only experiment-scoped settings and no
longer covers ``data_dir`` (see
:class:`argos.config.settings.FingerprintScope`). Two manifests carrying the
same version string and two incomparable fingerprints is exactly the ambiguity
a schema version exists to prevent, and unlike the two bumps below, ``v3``
manifests really were written — the three live captures of 2026-08-15 each left
one on disk. No migration is provided because nothing in this repository reads
a manifest back; the version bump is there so that a future reader cannot
compare the two meanings without noticing.

``run_manifest.v3`` was a clean, unmigrated bump: no ``v2`` manifest had ever
been persisted either (M1 discovery emits
none; the standalone ``argos manifest`` command only prints one). It adds
``capture_run_id``, the one field the M2 capture-CLI slice needs to satisfy
core invariant 13 for a capture run without violating the constraint
``docs/STATUS.md`` recorded from the pre-M2 pacing slice: an M2 capture
manifest must **not** embed one ``SourceProvenanceV1`` per ingested event,
because event volume makes that unbounded in a way M1 discovery's
one-provenance-per-page never was. A capture run therefore leaves
``input_provenance`` empty and points at its event store instead —
``capture_run_id`` is that pointer. The provenance of every ingested event is
already stored once, per record, in the event store's ``observation``/
``rejection`` rows (ADR-0011); enumerating it again here would be exactly the
unbounded growth the constraint forbids. A future reader wanting a
manifest-level summary queries ``EventStore.counts_for_capture_run(capture_run_id)``
rather than reading it off the manifest itself.
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
from argos.domain.versioning import FrozenDict, VersionedModel, freeze, thaw

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

    schema_version: ClassVar[str] = "run_manifest.v5"

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

    capture_run_id: str | None = None
    """The ``capture_run`` (``argos.store.event_store``, ADR-0011) this run
    opened, for a ``RunMode.CAPTURE`` run. ``None`` for every other mode.
    This is deliberately the *only* capture-specific field on the manifest —
    see the module docstring for why ``input_provenance`` stays empty for a
    capture run rather than growing one entry per ingested event: the event
    store already carries that provenance once per record, keyed by this same
    id, and a reader wanting a summary queries
    ``EventStore.counts_for_capture_run(capture_run_id)`` instead of reading
    it off this manifest."""

    config_fingerprint: str
    """SHA-256 over the *experiment-scoped* configuration only
    (:meth:`argos.config.settings.Settings.fingerprint`). Deliberately not a
    hash of ``settings_snapshot``: two runs differing only in where they write
    output are the same experiment, and ``docs/02_ARCHITECTURE.md`` names output
    location as a component that legitimately differs between live and replay.
    The full configuration is beside it in ``settings_snapshot``, so nothing is
    lost — only the hash is scoped."""

    settings_snapshot: Mapping[str, Any]
    """Every setting, verbatim, including the environment-scoped ones the
    fingerprint excludes. This is what makes the scoping safe: an auditor asking
    "where did this run write?" reads it here rather than losing it."""

    run_parameters: Mapping[str, Any] = FrozenDict({})
    """The run's inputs that are **not** settings — the operator's own arguments.

    Core invariant 13 asks every run to record its configuration, and a
    ``Settings`` snapshot is only part of that: a capture's subscribed token
    ids, its stopping bounds and whether it archived raw frames all arrive as
    command-line arguments, and none of them was recorded anywhere durable
    before this field existed. ``subscribed_token_ids`` is the one that matters
    most — it is what ``argos.ingestion.capture`` fans out over, and therefore
    what determines ``ingest_sequence`` allocation.

    Recorded in the form the loop actually used (deduplicated and sorted), not
    in the order the flags happened to appear, because the sorted set is the
    determinant and the flag order is not.

    Deliberately an open mapping rather than one typed field per mode: a
    ``replay`` run's parameters are not a capture's, and modelling every mode's
    arguments on one class would make the contract change every time a CLI flag
    does. Frozen on validation like ``settings_snapshot``, so a manifest cannot
    be edited after the fact.
    """

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

    @field_validator("settings_snapshot", "run_parameters")
    @classmethod
    def _freeze_snapshot(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], freeze(value))

    @field_serializer("settings_snapshot", "run_parameters")
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
    capture_run_id: str | None = None,
    run_parameters: Mapping[str, Any] | None = None,
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
        capture_run_id=capture_run_id,
        config_fingerprint=settings.fingerprint(),
        settings_snapshot=settings.snapshot(),
        run_parameters=dict(run_parameters or {}),
        schema_versions=tuple(schema_versions),
        input_provenance=tuple(input_provenance),
    )
