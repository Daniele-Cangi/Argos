"""Proof-backed target exclusions and the version-forward prospective aggregate."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar, cast

import orjson
from pydantic import Field, field_validator, model_validator

from argos.clock import ensure_utc
from argos.config.manifest import RunManifest, RunMode, WorkingTreeStatus
from argos.domain.lasttrade import LastTradePriceV1, parse_last_trade_price
from argos.domain.observation import (
    ObservationSource,
    RejectedObservationV1,
    recompute_rejection_id,
)
from argos.domain.provenance import sha256_hex
from argos.domain.versioning import VersionedModel, ensure_supported_version
from argos.errors import RejectionReason
from argos.evaluation import prospective_aggregation as aggregation_v1
from argos.evaluation.bundle import record_sha256
from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    EvidencePersistenceReceiptV1,
    ProspectiveExperimentProtocolV1,
    ProspectiveTargetV1,
    verify_receipt_for_record,
)
from argos.evaluation.prospective_aggregation import (
    ProspectiveExperimentReportV1,
    ProspectiveTargetContributionV1,
    ProspectiveTargetExclusionReason,
)
from argos.evaluation.prospective_bundle import EvaluationRunBundleV3
from argos.store.raw_archive import archive_relative_location

__all__ = [
    "CaptureRejectionEvidenceV1",
    "ProspectiveExperimentBundleV2",
    "ProspectiveTargetExclusionV2",
    "aggregate_prospective_experiment_v2",
    "build_capture_rejection_evidence",
    "build_target_exclusion_v2",
    "prospective_experiment_digest_v2",
]


class CaptureRejectionEvidenceV1(VersionedModel):
    """Self-validating proof that frozen capture code rejected a real last trade."""

    schema_version: ClassVar[str] = "capture_rejection_evidence.v1"

    evidence_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    capture_run_manifest: RunManifest
    ingest_sequence: int = Field(gt=0)
    rejection: RejectedObservationV1
    raw_payload_utf8: str = Field(min_length=1)
    raw_payload_location: str = Field(min_length=1)

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        record["capture_run_manifest"] = self.capture_run_manifest.to_record()
        record["rejection"] = self.rejection.to_record()
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> CaptureRejectionEvidenceV1:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["capture_run_manifest"] = RunManifest.from_record(
            dict(payload["capture_run_manifest"])
        )
        payload["rejection"] = RejectedObservationV1.from_record(dict(payload["rejection"]))
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def _proof_is_semantically_bound(self) -> CaptureRejectionEvidenceV1:
        rejection = self.rejection
        manifest = self.capture_run_manifest
        raw = self.raw_payload_utf8.encode("utf-8")

        if recompute_rejection_id(rejection) != rejection.rejection_id:
            raise ValueError("capture rejection identity is not recomputable")
        if (
            len(raw) != rejection.provenance.byte_length
            or sha256_hex(raw) != rejection.raw_payload_sha256
        ):
            raise ValueError("embedded raw payload disagrees with rejection provenance")
        expected_location = archive_relative_location(rejection.provenance)
        if self.raw_payload_location != expected_location:
            raise ValueError("raw payload location disagrees with rejection provenance")
        if (
            rejection.received_time != rejection.provenance.retrieved_at
            or rejection.rejected_at < rejection.received_time
        ):
            raise ValueError("rejection chronology disagrees with source receipt time")
        if (
            manifest.mode is not RunMode.CAPTURE
            or manifest.capture_run_id != rejection.capture_run_id
            or manifest.run_id != rejection.capture_run_id
        ):
            raise ValueError("capture manifest does not name the rejection run")
        if manifest.code_revision is None or manifest.working_tree is not WorkingTreeStatus.CLEAN:
            raise ValueError("capture rejection requires a clean identified revision")
        if manifest.created_at < rejection.rejected_at:
            raise ValueError("capture manifest predates the rejection it summarizes")
        if manifest.run_parameters.get("raw_archive") is not True:
            raise ValueError("capture rejection proof requires raw archival")
        if RejectedObservationV1.schema_version not in manifest.schema_versions:
            raise ValueError("capture manifest omits the rejection schema")
        if LastTradePriceV1.schema_version in manifest.schema_versions:
            raise ValueError("capture manifest cannot claim the rejected trade was modeled")
        if (
            rejection.reason is not RejectionReason.UNKNOWN_EVENT_TYPE
            or rejection.source is not ObservationSource.CLOB_MARKET_WS
            or rejection.source_event_type != "last_trade_price"
        ):
            raise ValueError("proof is not an unmodeled standalone last_trade_price rejection")

        trade = _parse_embedded_trade(raw)
        if trade.condition_id != rejection.condition_id or trade.asset_id != rejection.token_id:
            raise ValueError("standalone trade scope disagrees with its rejection")

        expected_id = _capture_evidence_id(
            experiment_id=self.experiment_id,
            target_id=self.target_id,
            manifest=manifest,
            ingest_sequence=self.ingest_sequence,
            rejection=rejection,
            raw_payload_location=self.raw_payload_location,
        )
        if self.evidence_id != expected_id:
            raise ValueError("capture rejection evidence_id disagrees with its evidence")
        return self

    def parsed_trade(self) -> LastTradePriceV1:
        """Return the strictly parsed trade whose bytes this proof embeds."""

        return _parse_embedded_trade(self.raw_payload_utf8.encode("utf-8"))


class ProspectiveTargetExclusionV2(VersionedModel):
    """A target exclusion whose reason is proven by immutable capture evidence."""

    schema_version: ClassVar[str] = "prospective_target_exclusion.v2"

    exclusion_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    target: ProspectiveTargetV1
    target_receipt: EvidencePersistenceReceiptV1
    reason: ProspectiveTargetExclusionReason
    excluded_at: datetime
    capture_evidence: CaptureRejectionEvidenceV1
    capture_evidence_receipt: EvidencePersistenceReceiptV1

    @field_validator("excluded_at")
    @classmethod
    def _anchor_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        record["target"] = self.target.to_record()
        record["target_receipt"] = self.target_receipt.to_record()
        record["capture_evidence"] = self.capture_evidence.to_record()
        record["capture_evidence_receipt"] = self.capture_evidence_receipt.to_record()
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> ProspectiveTargetExclusionV2:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["target"] = ProspectiveTargetV1.from_record(dict(payload["target"]))
        payload["target_receipt"] = EvidencePersistenceReceiptV1.from_record(
            dict(payload["target_receipt"])
        )
        payload["capture_evidence"] = CaptureRejectionEvidenceV1.from_record(
            dict(payload["capture_evidence"])
        )
        payload["capture_evidence_receipt"] = EvidencePersistenceReceiptV1.from_record(
            dict(payload["capture_evidence_receipt"])
        )
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def _exclusion_is_semantically_bound(self) -> ProspectiveTargetExclusionV2:
        if (
            self.experiment_id != self.target.experiment_id
            or self.target_receipt.experiment_id != self.experiment_id
            or self.target_receipt.artifact_kind is not EvidenceArtifactKind.TARGET_DECLARATION
            or self.target_receipt.artifact_id != self.target.target_id
        ):
            raise ValueError("proof-backed exclusion names a different target")
        verify_receipt_for_record(self.target_receipt, self.target)

        evidence = self.capture_evidence
        receipt = self.capture_evidence_receipt
        if (
            evidence.experiment_id != self.experiment_id
            or evidence.target_id != self.target.target_id
            or receipt.experiment_id != self.experiment_id
            or receipt.artifact_kind is not EvidenceArtifactKind.CAPTURE_REJECTION
            or receipt.artifact_id != evidence.evidence_id
        ):
            raise ValueError("capture evidence receipt names a different exclusion proof")
        verify_receipt_for_record(receipt, evidence)
        if self.reason is not ProspectiveTargetExclusionReason.UNMODELED_STANDALONE_LAST_TRADE:
            raise ValueError("capture-backed exclusion has the wrong reason")

        trade = evidence.parsed_trade()
        if trade.condition_id != self.target.condition_id or trade.asset_id not in {
            self.target.yes_token_id,
            self.target.no_token_id,
        }:
            raise ValueError("capture rejection is outside the selected target scope")
        subscribed = evidence.capture_run_manifest.run_parameters.get("subscribed_token_ids")
        if not isinstance(subscribed, tuple) or frozenset(str(item) for item in subscribed) != {
            self.target.yes_token_id,
            self.target.no_token_id,
        }:
            raise ValueError("capture manifest subscriptions disagree with the target")
        if self.excluded_at != evidence.rejection.received_time:
            raise ValueError("target exclusion time must equal the first proven rejection time")

        expected_id = _target_exclusion_id(
            experiment_id=self.experiment_id,
            target=self.target,
            target_receipt=self.target_receipt,
            reason=self.reason,
            excluded_at=self.excluded_at,
            capture_evidence=evidence,
            capture_evidence_receipt=receipt,
        )
        if self.exclusion_id != expected_id:
            raise ValueError("exclusion_id disagrees with its proof-backed evidence")
        return self


class ProspectiveExperimentBundleV2(VersionedModel):
    """Aggregate boundary whose exclusions are evidence-bearing V2 records."""

    schema_version: ClassVar[str] = "prospective_experiment_bundle.v2"

    protocol: ProspectiveExperimentProtocolV1
    protocol_receipt: EvidencePersistenceReceiptV1
    target_bundles: tuple[EvaluationRunBundleV3, ...]
    target_exclusions: tuple[ProspectiveTargetExclusionV2, ...]
    contributions: tuple[ProspectiveTargetContributionV1, ...]
    report: ProspectiveExperimentReportV1
    evidence_digest: str = Field(min_length=64, max_length=64)

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        record.update(
            {
                "protocol": self.protocol.to_record(),
                "protocol_receipt": self.protocol_receipt.to_record(),
                "target_bundles": [item.to_record() for item in self.target_bundles],
                "target_exclusions": [item.to_record() for item in self.target_exclusions],
                "contributions": [item.to_record() for item in self.contributions],
                "report": self.report.to_record(),
            }
        )
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> ProspectiveExperimentBundleV2:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["protocol"] = ProspectiveExperimentProtocolV1.from_record(dict(payload["protocol"]))
        payload["protocol_receipt"] = EvidencePersistenceReceiptV1.from_record(
            dict(payload["protocol_receipt"])
        )
        payload["target_bundles"] = tuple(
            EvaluationRunBundleV3.from_record(dict(item)) for item in payload["target_bundles"]
        )
        payload["target_exclusions"] = tuple(
            ProspectiveTargetExclusionV2.from_record(dict(item))
            for item in payload["target_exclusions"]
        )
        payload["contributions"] = tuple(
            ProspectiveTargetContributionV1.from_record(dict(item))
            for item in payload["contributions"]
        )
        payload["report"] = ProspectiveExperimentReportV1.from_record(dict(payload["report"]))
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def _aggregate_is_recomputable(self) -> ProspectiveExperimentBundleV2:
        verify_receipt_for_record(self.protocol_receipt, self.protocol)
        if (
            self.protocol_receipt.experiment_id != self.protocol.experiment_id
            or self.protocol_receipt.artifact_kind is not EvidenceArtifactKind.EXPERIMENT_PROTOCOL
        ):
            raise ValueError("aggregate protocol receipt names different evidence")

        target_ids = [item.target.target_id for item in self.target_bundles]
        exclusion_ids = [item.target.target_id for item in self.target_exclusions]
        if len(set((*target_ids, *exclusion_ids))) != len(target_ids) + len(exclusion_ids):
            raise ValueError("one selected target may appear only once in an experiment")
        if any(
            item.protocol.experiment_id != self.protocol.experiment_id
            for item in self.target_bundles
        ) or any(
            item.experiment_id != self.protocol.experiment_id for item in self.target_exclusions
        ):
            raise ValueError("target evidence names a different experiment")

        for exclusion in self.target_exclusions:
            manifest = exclusion.capture_evidence.capture_run_manifest
            received = exclusion.capture_evidence.rejection.received_time
            if (
                manifest.code_revision != self.protocol.code_revision
                or manifest.config_fingerprint != self.protocol.config_fingerprint
            ):
                raise ValueError("capture proof disagrees with the frozen protocol")
            if not (
                self.protocol.observation_window_start
                <= received
                <= self.protocol.observation_window_end
            ):
                raise ValueError("capture rejection is outside the frozen observation window")

        expected_contributions = aggregation_v1._target_contributions(self.target_bundles)
        if self.contributions != expected_contributions:
            raise ValueError("target contributions disagree with the last admissible evaluations")
        report_exclusions = cast(
            Sequence[aggregation_v1.ProspectiveTargetExclusionV1],
            self.target_exclusions,
        )
        expected_report = aggregation_v1._experiment_report(
            protocol=self.protocol,
            bundles=self.target_bundles,
            exclusions=report_exclusions,
            contributions=self.contributions,
            created_at=self.report.created_at,
            observation_complete=self.report.observation_complete,
        )
        if self.report != expected_report:
            raise ValueError("experiment report disagrees with its target evidence")
        expected_digest = prospective_experiment_digest_v2(
            protocol=self.protocol,
            protocol_receipt=self.protocol_receipt,
            target_bundles=self.target_bundles,
            target_exclusions=self.target_exclusions,
            contributions=self.contributions,
            report=self.report,
        )
        if self.evidence_digest != expected_digest:
            raise ValueError("evidence_digest disagrees with the prospective experiment")
        return self


def build_capture_rejection_evidence(
    *,
    experiment_id: str,
    target_id: str,
    capture_run_manifest: RunManifest,
    ingest_sequence: int,
    rejection: RejectedObservationV1,
    raw_payload: bytes,
    raw_payload_location: str,
) -> CaptureRejectionEvidenceV1:
    try:
        raw_text = raw_payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("capture rejection raw payload is not UTF-8") from error
    evidence_id = _capture_evidence_id(
        experiment_id=experiment_id,
        target_id=target_id,
        manifest=capture_run_manifest,
        ingest_sequence=ingest_sequence,
        rejection=rejection,
        raw_payload_location=raw_payload_location,
    )
    return CaptureRejectionEvidenceV1(
        evidence_id=evidence_id,
        experiment_id=experiment_id,
        target_id=target_id,
        capture_run_manifest=capture_run_manifest,
        ingest_sequence=ingest_sequence,
        rejection=rejection,
        raw_payload_utf8=raw_text,
        raw_payload_location=raw_payload_location,
    )


def build_target_exclusion_v2(
    *,
    experiment_id: str,
    target: ProspectiveTargetV1,
    target_receipt: EvidencePersistenceReceiptV1,
    capture_evidence: CaptureRejectionEvidenceV1,
    capture_evidence_receipt: EvidencePersistenceReceiptV1,
) -> ProspectiveTargetExclusionV2:
    reason = ProspectiveTargetExclusionReason.UNMODELED_STANDALONE_LAST_TRADE
    excluded_at = capture_evidence.rejection.received_time
    exclusion_id = _target_exclusion_id(
        experiment_id=experiment_id,
        target=target,
        target_receipt=target_receipt,
        reason=reason,
        excluded_at=excluded_at,
        capture_evidence=capture_evidence,
        capture_evidence_receipt=capture_evidence_receipt,
    )
    return ProspectiveTargetExclusionV2(
        exclusion_id=exclusion_id,
        experiment_id=experiment_id,
        target=target,
        target_receipt=target_receipt,
        reason=reason,
        excluded_at=excluded_at,
        capture_evidence=capture_evidence,
        capture_evidence_receipt=capture_evidence_receipt,
    )


def aggregate_prospective_experiment_v2(
    *,
    protocol: ProspectiveExperimentProtocolV1,
    protocol_receipt: EvidencePersistenceReceiptV1,
    target_bundles: Sequence[EvaluationRunBundleV3],
    target_exclusions: Sequence[ProspectiveTargetExclusionV2],
    created_at: datetime,
    observation_complete: bool,
) -> ProspectiveExperimentBundleV2:
    bundles = tuple(sorted(target_bundles, key=lambda item: item.target.selection_rank))
    exclusions = tuple(sorted(target_exclusions, key=lambda item: item.target.selection_rank))
    contributions = aggregation_v1._target_contributions(bundles)
    report_exclusions = cast(
        Sequence[aggregation_v1.ProspectiveTargetExclusionV1],
        exclusions,
    )
    report = aggregation_v1._experiment_report(
        protocol=protocol,
        bundles=bundles,
        exclusions=report_exclusions,
        contributions=contributions,
        created_at=ensure_utc(created_at),
        observation_complete=observation_complete,
    )
    digest = prospective_experiment_digest_v2(
        protocol=protocol,
        protocol_receipt=protocol_receipt,
        target_bundles=bundles,
        target_exclusions=exclusions,
        contributions=contributions,
        report=report,
    )
    return ProspectiveExperimentBundleV2(
        protocol=protocol,
        protocol_receipt=protocol_receipt,
        target_bundles=bundles,
        target_exclusions=exclusions,
        contributions=contributions,
        report=report,
        evidence_digest=digest,
    )


def prospective_experiment_digest_v2(
    *,
    protocol: ProspectiveExperimentProtocolV1,
    protocol_receipt: EvidencePersistenceReceiptV1,
    target_bundles: Sequence[EvaluationRunBundleV3],
    target_exclusions: Sequence[ProspectiveTargetExclusionV2],
    contributions: Sequence[ProspectiveTargetContributionV1],
    report: ProspectiveExperimentReportV1,
) -> str:
    return record_sha256(
        {
            "version": "prospective_experiment_evidence.v2",
            "protocol": protocol.to_record(),
            "protocol_receipt": protocol_receipt.to_record(),
            "target_bundles": [item.to_record() for item in target_bundles],
            "target_exclusions": [item.to_record() for item in target_exclusions],
            "contributions": [item.to_record() for item in contributions],
            "report": report.to_record(),
        }
    )


def _parse_embedded_trade(raw: bytes) -> LastTradePriceV1:
    try:
        payload = orjson.loads(raw)
    except orjson.JSONDecodeError as error:
        raise ValueError("embedded capture payload is not JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("embedded capture payload is not a JSON object")
    return parse_last_trade_price(payload)


def _capture_evidence_id(
    *,
    experiment_id: str,
    target_id: str,
    manifest: RunManifest,
    ingest_sequence: int,
    rejection: RejectedObservationV1,
    raw_payload_location: str,
) -> str:
    digest = record_sha256(
        {
            "version": "capture_rejection_evidence_identity.v1",
            "experiment_id": experiment_id,
            "target_id": target_id,
            "capture_manifest": manifest.to_record(),
            "ingest_sequence": ingest_sequence,
            "rejection": rejection.to_record(),
            "raw_payload_location": raw_payload_location,
        }
    )
    return f"capture-rejection-{digest[:32]}"


def _target_exclusion_id(
    *,
    experiment_id: str,
    target: ProspectiveTargetV1,
    target_receipt: EvidencePersistenceReceiptV1,
    reason: ProspectiveTargetExclusionReason,
    excluded_at: datetime,
    capture_evidence: CaptureRejectionEvidenceV1,
    capture_evidence_receipt: EvidencePersistenceReceiptV1,
) -> str:
    material = (
        "prospective_target_exclusion_identity.v2",
        experiment_id,
        target.target_id,
        target_receipt.receipt_id,
        reason.value,
        ensure_utc(excluded_at).isoformat(),
        capture_evidence.evidence_id,
        capture_evidence_receipt.receipt_id,
    )
    encoded = "|".join(f"{len(part)}:{part}" for part in material)
    return f"target-exclusion-{hashlib.sha256(encoded.encode()).hexdigest()[:32]}"
