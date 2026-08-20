"""Version-forward prospective evaluation boundary for evidence-bound deadlines."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

from pydantic import model_validator

from argos.baselines.forecast import MarketBaselineForecastV2
from argos.compiler.contract import CompiledMarketContractV1
from argos.config.manifest import RunManifest, RunMode, WorkingTreeStatus
from argos.domain.market import MarketDefinitionV1
from argos.domain.versioning import ensure_supported_version
from argos.evaluation.bundle import (
    EvaluationDecisionV1,
    EvaluationExclusionV1,
    EvaluationPolicyV2,
    record_sha256,
)
from argos.evaluation.prospective import (
    EvidencePersistenceReceiptV1,
    LifecycleObservationV1,
    ProspectiveExperimentProtocolV2,
    ProspectiveTargetV1,
    ResolutionCutoffEvidenceV1,
)
from argos.evaluation.prospective_bundle import EvaluationRunBundleV3, bundle_evidence_digest_v3
from argos.evaluation.report import EvaluationReportV3
from argos.evaluation.scoring import ForecastEvaluationV2
from argos.resolution.gamma_resolution import ResolutionV1

__all__ = ["EvaluationRunBundleV4", "bundle_evidence_digest_v4"]


def bundle_evidence_digest_v4(v3_digest: str, manifest: RunManifest) -> str:
    """Bind the complete V3 claim plus the exact capture manifest."""

    return record_sha256(
        {
            "prospective_v3_evidence_digest": v3_digest,
            "capture_run_manifest": manifest.to_record(),
        }
    )


class EvaluationRunBundleV4(EvaluationRunBundleV3):
    """A V3-shaped claim whose protocol structurally bounds its cutoff."""

    schema_version: ClassVar[str] = "evaluation_run_bundle.v4"

    protocol: ProspectiveExperimentProtocolV2

    capture_run_manifest: RunManifest

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        record["capture_run_manifest"] = self.capture_run_manifest.to_record()
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> EvaluationRunBundleV4:
        """Restore every nested child while requiring the V2 protocol."""

        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["policy"] = EvaluationPolicyV2.from_record(dict(payload["policy"]))
        payload["protocol"] = ProspectiveExperimentProtocolV2.from_record(dict(payload["protocol"]))
        payload["protocol_receipt"] = EvidencePersistenceReceiptV1.from_record(
            dict(payload["protocol_receipt"])
        )
        payload["market"] = MarketDefinitionV1.from_record(dict(payload["market"]))
        payload["market_receipt"] = EvidencePersistenceReceiptV1.from_record(
            dict(payload["market_receipt"])
        )
        payload["contract"] = CompiledMarketContractV1.from_record(dict(payload["contract"]))
        payload["contract_receipt"] = EvidencePersistenceReceiptV1.from_record(
            dict(payload["contract_receipt"])
        )
        payload["target"] = ProspectiveTargetV1.from_record(dict(payload["target"]))
        payload["target_receipt"] = EvidencePersistenceReceiptV1.from_record(
            dict(payload["target_receipt"])
        )
        payload["lifecycle_observations"] = tuple(
            LifecycleObservationV1.from_record(dict(item))
            for item in payload["lifecycle_observations"]
        )
        payload["lifecycle_receipts"] = tuple(
            EvidencePersistenceReceiptV1.from_record(dict(item))
            for item in payload["lifecycle_receipts"]
        )
        payload["cutoff_evidence"] = ResolutionCutoffEvidenceV1.from_record(
            dict(payload["cutoff_evidence"])
        )
        payload["cutoff_receipt"] = EvidencePersistenceReceiptV1.from_record(
            dict(payload["cutoff_receipt"])
        )
        payload["resolution"] = ResolutionV1.from_record(dict(payload["resolution"]))
        payload["report"] = EvaluationReportV3.from_record(dict(payload["report"]))
        payload["forecasts"] = tuple(
            MarketBaselineForecastV2.from_record(dict(item)) for item in payload["forecasts"]
        )
        payload["evaluations"] = tuple(
            ForecastEvaluationV2.from_record(dict(item)) for item in payload["evaluations"]
        )
        payload["decisions"] = tuple(
            EvaluationDecisionV1.from_record(dict(item)) for item in payload["decisions"]
        )
        payload["exclusions"] = tuple(
            EvaluationExclusionV1.from_record(dict(item)) for item in payload["exclusions"]
        )
        payload["capture_run_manifest"] = RunManifest.from_record(
            dict(payload["capture_run_manifest"])
        )
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def _cutoff_is_inside_frozen_lifecycle(self) -> EvaluationRunBundleV4:
        deadline = self.protocol.lifecycle_deadline
        if (
            self.cutoff_evidence.retrieved_at > deadline
            or self.cutoff_evidence.selected_cutoff > deadline
        ):
            raise ValueError("resolution cutoff is after the frozen lifecycle deadline")
        return self

    @model_validator(mode="after")
    def _capture_manifest_honors_protocol(self) -> EvaluationRunBundleV4:
        manifest = self.capture_run_manifest
        capture_run_id = manifest.capture_run_id
        if (
            manifest.mode is not RunMode.CAPTURE
            or capture_run_id is None
            or manifest.run_id != capture_run_id
            or self.report.source_capture_run_ids != (capture_run_id,)
        ):
            raise ValueError("capture manifest does not name the evaluated target run")
        if (
            manifest.code_revision != self.protocol.code_revision
            or manifest.working_tree is not WorkingTreeStatus.CLEAN
            or manifest.config_fingerprint != self.protocol.config_fingerprint
        ):
            raise ValueError("capture manifest disagrees with the frozen protocol runtime")
        parameters = manifest.run_parameters
        subscribed = parameters.get("subscribed_token_ids")
        if (
            not isinstance(subscribed, Sequence)
            or isinstance(subscribed, (str, bytes))
            or set(subscribed) != {self.target.yes_token_id, self.target.no_token_id}
            or len(subscribed) != 2
        ):
            raise ValueError("capture manifest does not subscribe exactly both target tokens")
        try:
            max_seconds = Decimal(str(parameters.get("max_seconds")))
        except InvalidOperation as error:
            raise ValueError("capture duration is not a numeric frozen bound") from error
        if max_seconds != Decimal(self.protocol.capture_max_seconds_per_target):
            raise ValueError("capture duration disagrees with the frozen protocol")
        max_frames = parameters.get("max_frames")
        if (
            not isinstance(max_frames, int)
            or isinstance(max_frames, bool)
            or max_frames != self.protocol.capture_max_frames_per_target
        ):
            raise ValueError("capture frame bound disagrees with the frozen protocol")
        if parameters.get("raw_archive") is not self.protocol.capture_raw_archive:
            raise ValueError("capture raw-archive policy disagrees with the frozen protocol")
        return self

    def _expected_evidence_digest(self) -> str:
        v3_digest = bundle_evidence_digest_v3(
            evaluation_run_id=self.evaluation_run_id,
            policy=self.policy,
            protocol=self.protocol,
            protocol_receipt=self.protocol_receipt,
            market=self.market,
            market_receipt=self.market_receipt,
            contract=self.contract,
            contract_receipt=self.contract_receipt,
            target=self.target,
            target_receipt=self.target_receipt,
            lifecycle_observations=self.lifecycle_observations,
            lifecycle_receipts=self.lifecycle_receipts,
            cutoff_evidence=self.cutoff_evidence,
            cutoff_receipt=self.cutoff_receipt,
            resolution=self.resolution,
            report=self.report,
            forecasts=self.forecasts,
            evaluations=self.evaluations,
            decisions=self.decisions,
            exclusions=self.exclusions,
        )
        return bundle_evidence_digest_v4(v3_digest, self.capture_run_manifest)
