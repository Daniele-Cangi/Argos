"""Per-request, append-only provenance for late Gamma lifecycle observations.

The raw archive is content-addressed: identical responses share one payload and
one original sidecar. A separate retrieval record is therefore needed for each
poll to preserve when those same bytes were actually observed again.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

import orjson
from pydantic import Field, field_validator, model_validator

from argos.clock import ensure_utc
from argos.domain.provenance import SourceProvenanceV1
from argos.domain.versioning import VersionedModel, ensure_supported_version
from argos.evaluation.bundle import record_sha256
from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    EvidencePersistenceReceiptV1,
    LifecycleObservationV1,
    build_persistence_receipt_id,
    load_persisted_record,
)
from argos.store.raw_archive import archive_relative_location, read_raw_payload

__all__ = [
    "LifecyclePollRetrievalV1",
    "build_lifecycle_poll_retrieval_id",
    "load_lifecycle_poll_retrievals",
    "verify_lifecycle_poll_retrieval",
]


def build_lifecycle_poll_retrieval_id(
    *,
    schedule_id: str,
    experiment_id: str,
    target_id: str,
    ordinal: int,
    observation_id: str,
    attempted_at: datetime,
    provenance: SourceProvenanceV1,
) -> str:
    material = {
        "identity_version": "lifecycle_poll_retrieval.v1",
        "schedule_id": schedule_id,
        "experiment_id": experiment_id,
        "target_id": target_id,
        "ordinal": ordinal,
        "observation_id": observation_id,
        "attempted_at": ensure_utc(attempted_at).isoformat(),
        "provenance": provenance.to_record(),
    }
    return f"lifecycle-poll-retrieval-{record_sha256(material)[:32]}"


class LifecyclePollRetrievalV1(VersionedModel):
    """First-hand source provenance for one specific late poll attempt."""

    schema_version: ClassVar[str] = "lifecycle_poll_retrieval.v1"

    retrieval_id: str = Field(min_length=1)
    schedule_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    ordinal: int = Field(gt=0)
    observation_id: str = Field(min_length=1)
    attempted_at: datetime
    provenance: SourceProvenanceV1

    @field_validator("attempted_at")
    @classmethod
    def _utc_attempt(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _first_hand_retrieval(self) -> LifecyclePollRetrievalV1:
        if self.provenance.source != "gamma" or self.provenance.reconstructed:
            raise ValueError("late retrieval requires first-hand Gamma provenance")
        if self.provenance.retrieved_at < self.attempted_at:
            raise ValueError("Gamma retrieval precedes its poll attempt")
        expected = build_lifecycle_poll_retrieval_id(
            schedule_id=self.schedule_id,
            experiment_id=self.experiment_id,
            target_id=self.target_id,
            ordinal=self.ordinal,
            observation_id=self.observation_id,
            attempted_at=self.attempted_at,
            provenance=self.provenance,
        )
        if self.retrieval_id != expected:
            raise ValueError("late retrieval identity disagrees with its evidence")
        return self

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        record["provenance"] = self.provenance.to_record()
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> LifecyclePollRetrievalV1:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["provenance"] = SourceProvenanceV1.from_record(dict(payload["provenance"]))
        return cls.model_validate(payload)


def load_lifecycle_poll_retrievals(
    evidence_dir: Path, *, experiment_id: str, target_id: str
) -> dict[str, tuple[LifecyclePollRetrievalV1, EvidencePersistenceReceiptV1]]:
    """Reload every selected retrieval and its canonical archive receipt."""
    found: dict[str, tuple[LifecyclePollRetrievalV1, EvidencePersistenceReceiptV1]] = {}
    for path in sorted((evidence_dir / "argos_evidence").glob("*.raw.json")):
        digest = path.name.removesuffix(".raw.json")
        raw, provenance = read_raw_payload(evidence_dir, digest)
        try:
            decoded = orjson.loads(raw)
        except orjson.JSONDecodeError as error:
            raise ValueError("evidence archive contains malformed JSON") from error
        if not isinstance(decoded, dict):
            raise ValueError("evidence archive record is not a JSON object")
        if decoded.get("schema_version") != LifecyclePollRetrievalV1.schema_version:
            continue
        retrieval = LifecyclePollRetrievalV1.from_record(decoded)
        if retrieval.experiment_id != experiment_id or retrieval.target_id != target_id:
            continue
        if (
            provenance.reconstructed
            or provenance.source != "argos_evidence"
            or provenance.endpoint
            != f"argos-evidence://{retrieval.schema_version}/{retrieval.retrieval_id}"
            or raw != orjson.dumps(retrieval.to_record(), option=orjson.OPT_SORT_KEYS)
        ):
            raise ValueError("late retrieval evidence is not first-hand canonical")
        fields: dict[str, Any] = {
            "experiment_id": retrieval.experiment_id,
            "artifact_kind": EvidenceArtifactKind.LIFECYCLE_POLL_RETRIEVAL,
            "artifact_id": retrieval.retrieval_id,
            "artifact_schema_version": retrieval.schema_version,
            "artifact_sha256": provenance.raw_sha256,
            "artifact_byte_length": provenance.byte_length,
            "persisted_at": provenance.retrieved_at,
            "storage_backend": "content_addressed_raw_archive.v1",
            "storage_identity": archive_relative_location(provenance),
        }
        receipt = EvidencePersistenceReceiptV1(
            receipt_id=build_persistence_receipt_id(**fields), **fields
        )
        load_persisted_record(evidence_dir, receipt, LifecyclePollRetrievalV1)
        if retrieval.observation_id in found:
            raise ValueError("duplicate late retrieval evidence for one lifecycle observation")
        found[retrieval.observation_id] = (retrieval, receipt)
    return found


def verify_lifecycle_poll_retrieval(
    observation: LifecycleObservationV1,
    observation_receipt: EvidencePersistenceReceiptV1,
    raw: bytes,
    retrievals: dict[str, tuple[LifecyclePollRetrievalV1, EvidencePersistenceReceiptV1]],
) -> None:
    """Bind a lifecycle claim to its distinct, earlier durable request record."""
    pair = retrievals.get(observation.lifecycle_observation_id)
    if pair is None:
        raise ValueError("missing per-poll retrieval evidence for lifecycle observation")
    retrieval, receipt = pair
    source = retrieval.provenance
    if (
        retrieval.experiment_id != observation.experiment_id
        or retrieval.target_id != observation.target_id
        or retrieval.ordinal != observation.ordinal
        or retrieval.observation_id != observation.lifecycle_observation_id
        or source.source != observation.source
        or source.endpoint != observation.endpoint
        or source.retrieved_at != observation.retrieved_at
        or source.raw_sha256 != observation.raw_payload_sha256
        or source.byte_length != observation.byte_length
        or not source.matches(raw)
        or receipt.persisted_at < source.retrieved_at
        or receipt.persisted_at > observation_receipt.persisted_at
    ):
        raise ValueError("per-poll retrieval evidence disagrees with lifecycle observation")
