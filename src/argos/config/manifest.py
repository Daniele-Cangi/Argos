"""Run manifests.

Core invariant 13: every run records configuration, code revision, schema
versions, and data provenance. The manifest is built from an injected clock so
that a replay run produces an identical manifest on every execution.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, ClassVar, cast

from pydantic import Field, field_serializer, field_validator

from argos.clock import Clock, ensure_utc
from argos.config.settings import Settings
from argos.domain.versioning import VersionedModel, freeze, thaw


class RunManifest(VersionedModel):
    """Immutable description of one ARGOS run."""

    schema_version: ClassVar[str] = "run_manifest.v1"

    run_id: str = Field(min_length=1)
    mode: str = Field(min_length=1)
    created_at: datetime
    argos_version: str
    code_revision: str | None = None
    config_fingerprint: str
    settings_snapshot: Mapping[str, Any]

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

    def describe(self) -> str:
        """Return a one-line human summary for CLI output and logs."""
        revision = self.code_revision or "unknown-revision"
        return (
            f"{self.run_id} mode={self.mode} at={self.created_at.isoformat()} "
            f"revision={revision} config={self.config_fingerprint[:12]}"
        )


def build_run_manifest(
    *,
    settings: Settings,
    clock: Clock,
    run_id: str,
    mode: str,
    code_revision: str | None = None,
) -> RunManifest:
    """Build a manifest for a run, stamping it with the injected clock's time."""
    from argos import __version__

    return RunManifest(
        run_id=run_id,
        mode=mode,
        created_at=ensure_utc(clock.now()),
        argos_version=__version__,
        code_revision=code_revision,
        config_fingerprint=settings.fingerprint(),
        settings_snapshot=settings.snapshot(),
    )
