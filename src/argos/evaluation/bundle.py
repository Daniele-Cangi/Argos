"""Digest-bound records behind one evaluation run.

The bundle is the M4 claim boundary. A summary alone is not evidence: it must
name the ordered replay trajectory, the resolution and contract records, every
forecast/evaluation, and every exclusion that produced its counts.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar

import orjson
from pydantic import Field, model_validator

from argos.baselines.forecast import MarketBaselineForecastV2
from argos.compiler.contract import CompiledMarketContractV1
from argos.domain.versioning import VersionedModel, ensure_supported_version
from argos.evaluation.report import EvaluationReportV2
from argos.evaluation.scoring import ForecastEvaluationV2
from argos.resolution.gamma_resolution import ResolutionV1

__all__ = [
    "DecisionReason",
    "EvaluationDecisionV1",
    "EvaluationExclusionV1",
    "EvaluationPolicyV1",
    "EvaluationPolicyV2",
    "EvaluationRunBundleV1",
    "EvaluationRunBundleV2",
    "ExclusionReason",
    "record_sha256",
]

TRAJECTORY_HASH_VERSION = "evaluation_input_trajectory.v1"
INFORMATION_STATE_HASH_VERSION = "evaluation_information_state.v1"


class DecisionReason(StrEnum):
    INCLUDED_INFORMATION_CHANGE = "included_information_change"
    REJECTED_ARRIVAL = "rejected_arrival"
    DUPLICATE_DELIVERY = "duplicate_delivery"
    UNHANDLED_PAYLOAD = "unhandled_payload"
    UNSCOPED = "unscoped"
    IRRELEVANT_SCOPE = "irrelevant_scope"
    TARGET_NOT_SEEDED = "target_not_seeded"
    TARGET_STATE_UNCHANGED = "target_state_unchanged"


class ExclusionReason(StrEnum):
    ABSTAINED = "abstained"
    RESOLUTION_TIME_UNKNOWN = "resolution_time_unknown"
    POST_RESOLUTION = "post_resolution"
    CAPTURE_INCOMPLETE = "capture_incomplete"
    REPLAY_INTEGRITY = "replay_integrity"
    CONTRACT_IDENTITY_MISSING = "contract_identity_missing"


class EvaluationPolicyV1(VersionedModel):
    """The policy decisions that determine whether a point can be scored."""

    schema_version: ClassVar[str] = "evaluation_policy.v1"

    emission_policy: str = "target_information_state_change"
    resolution_finality_policy: str = "final_only"
    temporal_policy: str = "strict_known_resolution_cutoff"
    post_resolution_policy: str = "exclude"
    headline_weighting_unit: str = "resolved_target"
    minimum_resolved_targets_for_calibration: int = Field(default=2, ge=2)


class EvaluationPolicyV2(VersionedModel):
    """Claim policy with structural and scientific sufficiency kept separate."""

    schema_version: ClassVar[str] = "evaluation_policy.v2"

    emission_policy: str = "target_information_state_change"
    resolution_finality_policy: str = "final_only"
    temporal_policy: str = "strict_known_resolution_cutoff"
    post_resolution_policy: str = "exclude"
    headline_weighting_unit: str = "resolved_target"
    structural_minimum_independent_resolved_targets: int = Field(default=2, ge=2)
    calibration_claim_policy: str = "predeclared_multi_target_protocol_required"

    @property
    def minimum_resolved_targets_for_calibration(self) -> int:
        """Refuse the old scientific interpretation at its likely call site."""
        raise AttributeError(
            "two targets are only a structural floor; calibration sufficiency must "
            "come from a predeclared multi-target protocol"
        )


class EvaluationDecisionV1(VersionedModel):
    """Why one replay arrival did or did not create a forecast information state."""

    schema_version: ClassVar[str] = "evaluation_decision.v1"

    ingest_sequence: int = Field(gt=0)
    arrival_kind: str = Field(min_length=1)
    observation_id: str | None = Field(default=None, min_length=1)
    rejection_id: str | None = Field(default=None, min_length=1)
    dispatch_outcome: str | None = Field(default=None, min_length=1)
    target_state_included: bool
    reason: DecisionReason
    information_state_hash: str | None = Field(default=None, min_length=64, max_length=64)


class EvaluationExclusionV1(VersionedModel):
    """One persisted forecast point that headline scoring did not use."""

    schema_version: ClassVar[str] = "evaluation_exclusion.v1"

    forecast_id: str = Field(min_length=1)
    reason: ExclusionReason
    detail: str = Field(min_length=1)


def record_sha256(record: Mapping[str, Any] | Sequence[Any]) -> str:
    """Canonical digest for a record or ordered record collection."""
    return hashlib.sha256(orjson.dumps(record, option=orjson.OPT_SORT_KEYS)).hexdigest()


class EvaluationRunBundleV1(VersionedModel):
    """Self-contained, hash-checked evidence for one evaluation run."""

    schema_version: ClassVar[str] = "evaluation_run_bundle.v1"

    evaluation_run_id: str = Field(min_length=1)
    policy: EvaluationPolicyV1
    resolution: ResolutionV1
    contract: CompiledMarketContractV1 | None = None
    report: EvaluationReportV2
    forecasts: tuple[MarketBaselineForecastV2, ...]
    evaluations: tuple[ForecastEvaluationV2, ...]
    decisions: tuple[EvaluationDecisionV1, ...]
    exclusions: tuple[EvaluationExclusionV1, ...]
    evidence_digest: str = Field(min_length=64, max_length=64)

    def to_record(self) -> dict[str, Any]:
        """Serialize every nested persistent contract with its own version."""
        record = super().to_record()
        record["policy"] = self.policy.to_record()
        record["resolution"] = self.resolution.to_record()
        record["contract"] = self.contract.to_record() if self.contract else None
        record["report"] = self.report.to_record()
        record["forecasts"] = [item.to_record() for item in self.forecasts]
        record["evaluations"] = [item.to_record() for item in self.evaluations]
        record["decisions"] = [item.to_record() for item in self.decisions]
        record["exclusions"] = [item.to_record() for item in self.exclusions]
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> EvaluationRunBundleV1:
        """Restore nested versioned children before validating the bundle."""
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["policy"] = EvaluationPolicyV1.from_record(dict(payload["policy"]))
        payload["resolution"] = ResolutionV1.from_record(dict(payload["resolution"]))
        contract = payload.get("contract")
        payload["contract"] = (
            CompiledMarketContractV1.from_record(dict(contract)) if contract is not None else None
        )
        payload["report"] = EvaluationReportV2.from_record(dict(payload["report"]))
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
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def _evidence_is_bound_and_linked(self) -> EvaluationRunBundleV1:
        if self.report.evaluation_run_id != self.evaluation_run_id:
            raise ValueError("bundle and report name different evaluation runs")
        forecast_ids = {forecast.forecast_id for forecast in self.forecasts}
        if len(forecast_ids) != len(self.forecasts):
            raise ValueError("forecast_id must be unique within an evaluation bundle")
        if any(item.forecast_id not in forecast_ids for item in self.evaluations):
            raise ValueError("an evaluation names a forecast absent from its bundle")
        if any(item.forecast_id not in forecast_ids for item in self.exclusions):
            raise ValueError("an exclusion names a forecast absent from its bundle")
        expected = bundle_evidence_digest(
            evaluation_run_id=self.evaluation_run_id,
            policy=self.policy,
            resolution=self.resolution,
            contract=self.contract,
            report=self.report,
            forecasts=self.forecasts,
            evaluations=self.evaluations,
            decisions=self.decisions,
            exclusions=self.exclusions,
        )
        if self.evidence_digest != expected:
            raise ValueError("evidence_digest disagrees with the bundle's records")
        return self


def bundle_evidence_digest(
    *,
    evaluation_run_id: str,
    policy: EvaluationPolicyV1,
    resolution: ResolutionV1,
    contract: CompiledMarketContractV1 | None,
    report: EvaluationReportV2,
    forecasts: Sequence[MarketBaselineForecastV2],
    evaluations: Sequence[ForecastEvaluationV2],
    decisions: Sequence[EvaluationDecisionV1],
    exclusions: Sequence[EvaluationExclusionV1],
) -> str:
    material = {
        "version": "evaluation_bundle_evidence.v1",
        "evaluation_run_id": evaluation_run_id,
        "policy": policy.to_record(),
        "resolution": resolution.to_record(),
        "contract": contract.to_record() if contract else None,
        "report": report.to_record(),
        "forecasts": [item.to_record() for item in forecasts],
        "evaluations": [item.to_record() for item in evaluations],
        "decisions": [item.to_record() for item in decisions],
        "exclusions": [item.to_record() for item in exclusions],
    }
    return record_sha256(material)


class EvaluationRunBundleV2(VersionedModel):
    """Bundle whose policy cannot mistake a structural floor for sufficiency."""

    schema_version: ClassVar[str] = "evaluation_run_bundle.v2"

    evaluation_run_id: str = Field(min_length=1)
    policy: EvaluationPolicyV2
    resolution: ResolutionV1
    contract: CompiledMarketContractV1 | None = None
    report: EvaluationReportV2
    forecasts: tuple[MarketBaselineForecastV2, ...]
    evaluations: tuple[ForecastEvaluationV2, ...]
    decisions: tuple[EvaluationDecisionV1, ...]
    exclusions: tuple[EvaluationExclusionV1, ...]
    evidence_digest: str = Field(min_length=64, max_length=64)

    def to_record(self) -> dict[str, Any]:
        """Serialize every nested persistent contract with its own version."""
        record = super().to_record()
        record["policy"] = self.policy.to_record()
        record["resolution"] = self.resolution.to_record()
        record["contract"] = self.contract.to_record() if self.contract else None
        record["report"] = self.report.to_record()
        record["forecasts"] = [item.to_record() for item in self.forecasts]
        record["evaluations"] = [item.to_record() for item in self.evaluations]
        record["decisions"] = [item.to_record() for item in self.decisions]
        record["exclusions"] = [item.to_record() for item in self.exclusions]
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> EvaluationRunBundleV2:
        """Restore the v2 policy and the existing versioned evidence children."""
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["policy"] = EvaluationPolicyV2.from_record(dict(payload["policy"]))
        payload["resolution"] = ResolutionV1.from_record(dict(payload["resolution"]))
        contract = payload.get("contract")
        payload["contract"] = (
            CompiledMarketContractV1.from_record(dict(contract)) if contract is not None else None
        )
        payload["report"] = EvaluationReportV2.from_record(dict(payload["report"]))
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
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def _evidence_is_bound_and_linked(self) -> EvaluationRunBundleV2:
        if self.report.evaluation_run_id != self.evaluation_run_id:
            raise ValueError("bundle and report name different evaluation runs")
        forecast_ids = {forecast.forecast_id for forecast in self.forecasts}
        if len(forecast_ids) != len(self.forecasts):
            raise ValueError("forecast_id must be unique within an evaluation bundle")
        if any(item.forecast_id not in forecast_ids for item in self.evaluations):
            raise ValueError("an evaluation names a forecast absent from its bundle")
        if any(item.forecast_id not in forecast_ids for item in self.exclusions):
            raise ValueError("an exclusion names a forecast absent from its bundle")
        expected = bundle_evidence_digest_v2(
            evaluation_run_id=self.evaluation_run_id,
            policy=self.policy,
            resolution=self.resolution,
            contract=self.contract,
            report=self.report,
            forecasts=self.forecasts,
            evaluations=self.evaluations,
            decisions=self.decisions,
            exclusions=self.exclusions,
        )
        if self.evidence_digest != expected:
            raise ValueError("evidence_digest disagrees with the bundle's records")
        return self


def bundle_evidence_digest_v2(
    *,
    evaluation_run_id: str,
    policy: EvaluationPolicyV2,
    resolution: ResolutionV1,
    contract: CompiledMarketContractV1 | None,
    report: EvaluationReportV2,
    forecasts: Sequence[MarketBaselineForecastV2],
    evaluations: Sequence[ForecastEvaluationV2],
    decisions: Sequence[EvaluationDecisionV1],
    exclusions: Sequence[EvaluationExclusionV1],
) -> str:
    """Bind a v2 bundle without changing the already-declared v1 digest."""
    material = {
        "version": "evaluation_bundle_evidence.v2",
        "evaluation_run_id": evaluation_run_id,
        "policy": policy.to_record(),
        "resolution": resolution.to_record(),
        "contract": contract.to_record() if contract else None,
        "report": report.to_record(),
        "forecasts": [item.to_record() for item in forecasts],
        "evaluations": [item.to_record() for item in evaluations],
        "decisions": [item.to_record() for item in decisions],
        "exclusions": [item.to_record() for item in exclusions],
    }
    return record_sha256(material)
