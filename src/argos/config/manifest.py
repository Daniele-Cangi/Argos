"""Run manifests.

Core invariant 13: every run records configuration, code revision, schema
versions, and data provenance. The manifest is built from an injected clock so
that a replay run produces an identical manifest on every execution.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from pydantic import Field

from argos.clock import Clock, ensure_utc
from argos.config.settings import Settings
from argos.domain.versioning import VersionedModel


class RunManifest(VersionedModel):
    """Immutable description of one ARGOS run."""

    schema_version: ClassVar[str] = "run_manifest.v1"

    run_id: str = Field(min_length=1)
    mode: str = Field(min_length=1)
    created_at: datetime
    argos_version: str
    code_revision: str | None = None
    config_fingerprint: str
    settings_snapshot: dict[str, Any]

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
