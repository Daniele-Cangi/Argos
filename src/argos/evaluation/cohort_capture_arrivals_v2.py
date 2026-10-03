"""Durable per-arrival evidence, independent of a terminal capture journal.

Fixed ordinal receipt files and a single exclusive seal pin the original chain.
Content addressing alone cannot pin a resend count. These anchors are local
immutable evidence, not signatures or protection against rewriting all storage.
An interrupted chain is retained, but is not a resumable capture checkpoint.
"""

from __future__ import annotations

import os
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
    load_persisted_record,
    persist_evidence_record,
    verify_receipt_for_record,
)
from argos.store.raw_archive import _fsync_directory, read_raw_payload, write_raw_payload


class CohortCaptureArrivalV1(VersionedModel):
    """One actual frame arrival, even if normalization emits no store row."""

    schema_version: ClassVar[str] = "m4_cohort_capture_arrival.v1"
    capture_run_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    ordinal: int = Field(gt=0, strict=True)
    included: bool = Field(strict=True)
    provenance: SourceProvenanceV1
    processed_at: datetime
    previous_receipt_id: str | None = Field(default=None, min_length=1)

    @field_validator("processed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _firsthand(self) -> CohortCaptureArrivalV1:
        if (
            self.provenance.source != "clob_market_ws"
            or self.provenance.reconstructed
            or self.processed_at < self.provenance.retrieved_at
            or (self.ordinal == 1) != (self.previous_receipt_id is None)
        ):
            raise ValueError("arrival requires firsthand chronology and a linked ordinal")
        return self

    @property
    def arrival_id(self) -> str:
        return f"cohort-arrival-{record_sha256(self.to_record())}"

    def to_record(self) -> dict[str, Any]:
        return {**super().to_record(), "provenance": self.provenance.to_record()}

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> CohortCaptureArrivalV1:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["provenance"] = SourceProvenanceV1.from_record(payload["provenance"])
        return cls.model_validate(payload)


class CohortCaptureArrivalSealV1(VersionedModel):
    """Immutable terminal count/root, including at most one excluded boundary."""

    schema_version: ClassVar[str] = "m4_cohort_capture_arrival_seal.v1"
    capture_run_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    arrival_count: int = Field(ge=0, strict=True)
    last_receipt_id: str | None = Field(default=None, min_length=1)
    sealed_at: datetime

    @field_validator("sealed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _root(self) -> CohortCaptureArrivalSealV1:
        if (self.arrival_count == 0) != (self.last_receipt_id is None):
            raise ValueError("arrival seal count/root disagree")
        return self

    @property
    def seal_id(self) -> str:
        return f"cohort-arrival-seal-{record_sha256(self.to_record())}"


def _pin_receipt(path: Path, receipt: EvidencePersistenceReceiptV1) -> None:
    """Exclusive fixed-name anchor: no replacement, including identical retries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = orjson.dumps(receipt.to_record(), option=orjson.OPT_SORT_KEYS)
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)
    _fsync_directory(path.parent.parent)
    if path.read_bytes() != raw:
        raise ValueError("arrival anchor read-back disagrees with written receipt")


def persist_capture_arrival(
    arrival: CohortCaptureArrivalV1,
    *,
    experiment_id: str,
    raw: bytes,
    raw_archive_dir: Path,
    evidence_archive_dir: Path,
    persisted_at: datetime,
) -> EvidencePersistenceReceiptV1:
    if ensure_utc(persisted_at) < arrival.processed_at:
        raise ValueError("arrival receipt predates processing")
    if (raw_archive_dir / "arrival-chain" / "seal.json").exists():
        raise ValueError("cannot append an arrival after its exclusive seal")
    archive = raw_archive_dir if arrival.included else raw_archive_dir / "excluded-boundary"
    write_raw_payload(archive, raw=raw, provenance=arrival.provenance)
    receipt = persist_evidence_record(
        evidence_archive_dir,
        record=arrival,
        experiment_id=experiment_id,
        artifact_kind=EvidenceArtifactKind.COHORT_CAPTURE_ARRIVAL,
        artifact_id=arrival.arrival_id,
        persisted_at=persisted_at,
    )
    _pin_receipt(raw_archive_dir / "arrival-chain" / f"{arrival.ordinal:08d}.json", receipt)
    return receipt


def persist_capture_arrival_seal(
    seal: CohortCaptureArrivalSealV1,
    *,
    experiment_id: str,
    raw_archive_dir: Path,
    evidence_archive_dir: Path,
    persisted_at: datetime,
) -> EvidencePersistenceReceiptV1:
    if ensure_utc(persisted_at) < seal.sealed_at:
        raise ValueError("arrival seal receipt predates seal")
    receipt = persist_evidence_record(
        evidence_archive_dir,
        record=seal,
        experiment_id=experiment_id,
        artifact_kind=EvidenceArtifactKind.COHORT_CAPTURE_ARRIVAL_SEAL,
        artifact_id=seal.seal_id,
        persisted_at=persisted_at,
    )
    _pin_receipt(raw_archive_dir / "arrival-chain" / "seal.json", receipt)
    return receipt


def verify_capture_arrival_chain(
    seal: CohortCaptureArrivalSealV1,
    seal_receipt: EvidencePersistenceReceiptV1,
    *,
    experiment_id: str,
    raw_archive_dir: Path,
    evidence_archive_dir: Path,
) -> tuple[CohortCaptureArrivalV1, ...]:
    anchors = raw_archive_dir / "arrival-chain"
    pinned = EvidencePersistenceReceiptV1.from_record(
        orjson.loads((anchors / "seal.json").read_bytes())
    )
    if pinned != seal_receipt or (
        pinned.experiment_id,
        pinned.artifact_kind,
        pinned.artifact_id,
    ) != (experiment_id, EvidenceArtifactKind.COHORT_CAPTURE_ARRIVAL_SEAL, seal.seal_id):
        raise ValueError("journal differs from independently pinned arrival seal")
    verify_receipt_for_record(pinned, seal)
    if load_persisted_record(evidence_archive_dir, pinned, CohortCaptureArrivalSealV1) != seal:
        raise ValueError("archived arrival seal differs")
    if pinned.persisted_at < seal.sealed_at:
        raise ValueError("arrival seal receipt predates seal")
    expected = {
        "seal.json",
        *(f"{ordinal:08d}.json" for ordinal in range(1, seal.arrival_count + 1)),
    }
    if {path.name for path in anchors.iterdir()} != expected:
        raise ValueError("arrival anchor inventory has a gap or extra tail")
    arrivals = []
    previous: EvidencePersistenceReceiptV1 | None = None
    for ordinal in range(1, seal.arrival_count + 1):
        receipt = EvidencePersistenceReceiptV1.from_record(
            orjson.loads((anchors / f"{ordinal:08d}.json").read_bytes())
        )
        arrival = load_persisted_record(evidence_archive_dir, receipt, CohortCaptureArrivalV1)
        if (
            (arrival.capture_run_id, arrival.target_id, arrival.ordinal)
            != (seal.capture_run_id, seal.target_id, ordinal)
            or arrival.previous_receipt_id != (previous.receipt_id if previous else None)
            or receipt.experiment_id != experiment_id
            or receipt.artifact_kind is not EvidenceArtifactKind.COHORT_CAPTURE_ARRIVAL
            or receipt.artifact_id != arrival.arrival_id
            or not arrival.processed_at <= receipt.persisted_at <= seal.sealed_at
            or (previous is not None and previous.persisted_at > arrival.processed_at)
        ):
            raise ValueError("arrival receipt chain identity, order or chronology disagrees")
        archive = raw_archive_dir if arrival.included else raw_archive_dir / "excluded-boundary"
        raw, first = read_raw_payload(archive, arrival.provenance.raw_sha256)
        if (
            not arrival.provenance.matches(raw)
            or first.reconstructed
            or first.source != arrival.provenance.source
            or first.endpoint != arrival.provenance.endpoint
            or first.retrieved_at > arrival.provenance.retrieved_at
        ):
            raise ValueError("arrival bytes/provenance differ from raw archive")
        arrivals.append(arrival)
        previous = receipt
    if seal.last_receipt_id != (previous.receipt_id if previous else None):
        raise ValueError("arrival seal does not terminate the original receipt chain")
    return tuple(arrivals)
