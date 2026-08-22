"""Portable indexes for publishing compact proof-bearing claim artifacts."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

import orjson
from pydantic import Field, field_validator, model_validator

from argos.domain.provenance import SHA256_LENGTH, sha256_hex
from argos.domain.versioning import VersionedModel, ensure_supported_version
from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    EvidencePersistenceReceiptV1,
    verify_receipt_for_record,
)
from argos.evaluation.prospective_aggregation_v2 import ProspectiveExperimentBundleV2
from argos.evaluation.prospective_aggregation_v3 import ProspectiveExperimentBundleV3
from argos.evaluation.prospective_terminal import ProspectiveExperimentBundleV4

__all__ = ["ProspectiveClaimArtifactIndexV1", "verify_published_claim_artifact"]


class ProspectiveClaimArtifactIndexV1(VersionedModel):
    """Repository-relative identity and receipt for one published claim bundle."""

    schema_version: ClassVar[str] = "prospective_claim_artifact_index.v1"

    experiment_id: str = Field(min_length=1)
    bundle_relative_path: str = Field(min_length=1)
    bundle_schema_version: str = Field(min_length=1)
    bundle_evidence_digest: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    bundle_artifact_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    bundle_byte_length: int = Field(gt=0)
    aggregate_receipt: EvidencePersistenceReceiptV1
    published_from_storage_identity: str = Field(min_length=1)

    @field_validator("bundle_relative_path", "published_from_storage_identity")
    @classmethod
    def _relative_paths(cls, value: str) -> str:
        path = PurePosixPath(value.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("claim artifact paths must stay relative")
        return path.as_posix()

    @field_validator("bundle_evidence_digest", "bundle_artifact_sha256")
    @classmethod
    def _hex_digests(cls, value: str) -> str:
        lowered = value.lower()
        if not all(character in "0123456789abcdef" for character in lowered):
            raise ValueError("claim artifact digests must be hexadecimal")
        return lowered

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        record["aggregate_receipt"] = self.aggregate_receipt.to_record()
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> ProspectiveClaimArtifactIndexV1:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["aggregate_receipt"] = EvidencePersistenceReceiptV1.from_record(
            dict(payload["aggregate_receipt"])
        )
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def _receipt_names_the_bundle(self) -> ProspectiveClaimArtifactIndexV1:
        receipt = self.aggregate_receipt
        if (
            receipt.experiment_id != self.experiment_id
            or receipt.artifact_kind is not EvidenceArtifactKind.EXPERIMENT_AGGREGATE
            or receipt.artifact_id != self.bundle_evidence_digest
            or receipt.artifact_schema_version != self.bundle_schema_version
            or receipt.artifact_sha256 != self.bundle_artifact_sha256
            or receipt.artifact_byte_length != self.bundle_byte_length
            or receipt.storage_identity != self.published_from_storage_identity
        ):
            raise ValueError("claim artifact index disagrees with its aggregate receipt")
        return self


def verify_published_claim_artifact(
    index: ProspectiveClaimArtifactIndexV1,
    directory: Path,
) -> ProspectiveExperimentBundleV2 | ProspectiveExperimentBundleV3 | ProspectiveExperimentBundleV4:
    """Read, hash, parse and semantically validate a published aggregate."""

    root = directory.resolve()
    candidate = directory / PurePosixPath(index.bundle_relative_path)
    if candidate.is_symlink():
        raise ValueError("claim artifact cannot be a symbolic link")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError("claim artifact resolves outside its index directory") from error
    raw = resolved.read_bytes()
    if len(raw) != index.bundle_byte_length or sha256_hex(raw) != index.bundle_artifact_sha256:
        raise ValueError("published claim artifact bytes disagree with the index")
    payload = orjson.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("published claim artifact is not a JSON object")
    schema_version = payload.get("schema_version")
    if schema_version == ProspectiveExperimentBundleV2.schema_version:
        bundle: (
            ProspectiveExperimentBundleV2
            | ProspectiveExperimentBundleV3
            | ProspectiveExperimentBundleV4
        ) = ProspectiveExperimentBundleV2.from_record(payload)
    elif schema_version == ProspectiveExperimentBundleV3.schema_version:
        bundle = ProspectiveExperimentBundleV3.from_record(payload)
    elif schema_version == ProspectiveExperimentBundleV4.schema_version:
        bundle = ProspectiveExperimentBundleV4.from_record(payload)
    else:
        raise ValueError("published claim artifact uses an unsupported bundle schema")
    if (
        type(bundle).schema_version != index.bundle_schema_version
        or bundle.protocol.experiment_id != index.experiment_id
        or bundle.evidence_digest != index.bundle_evidence_digest
    ):
        raise ValueError("published claim bundle identity disagrees with the index")
    verify_receipt_for_record(index.aggregate_receipt, bundle)
    return bundle
