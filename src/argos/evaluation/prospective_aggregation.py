"""Equal-target aggregation and independent M4/calibration verdicts."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar, cast

from pydantic import Field, field_serializer, field_validator, model_validator

from argos.clock import ensure_utc
from argos.domain.versioning import VersionedModel, ensure_supported_version, freeze, thaw
from argos.evaluation.bundle import record_sha256
from argos.evaluation.calibration import calibration_report
from argos.evaluation.numeric import evaluation_context
from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    EvidencePersistenceReceiptV1,
    ProspectiveExperimentProtocolV1,
    ProspectiveTargetV1,
    verify_receipt_for_record,
)
from argos.evaluation.prospective_bundle import EvaluationRunBundleV3
from argos.evaluation.scoring import ForecastEvaluationV2

__all__ = [
    "CalibrationVerdict",
    "MeasurementLayerVerdict",
    "ProspectiveExperimentBundleV1",
    "ProspectiveExperimentReportV1",
    "ProspectiveTargetContributionV1",
    "ProspectiveTargetExclusionReason",
    "ProspectiveTargetExclusionV1",
    "aggregate_prospective_experiment",
    "build_target_exclusion",
    "prospective_experiment_digest",
]


class MeasurementLayerVerdict(StrEnum):
    PASSED = "M4_MEASUREMENT_LAYER_PASSED"
    PARTIALLY_VALIDATED = "M4_PARTIALLY_VALIDATED"
    BLOCKED = "M4_BLOCKED"


class CalibrationVerdict(StrEnum):
    ESTABLISHED = "CALIBRATION_ESTABLISHED"
    NOT_ESTABLISHED = "CALIBRATION_NOT_ESTABLISHED"
    NOT_EVALUABLE = "CALIBRATION_NOT_EVALUABLE"


class ProspectiveTargetExclusionReason(StrEnum):
    UNMODELED_STANDALONE_LAST_TRADE = "unmodeled_standalone_last_trade_price"
    CAPTURE_INCOMPLETE = "capture_incomplete"
    CONTRACT_UNAVAILABLE = "contract_unavailable_before_forecast"
    LIFECYCLE_EVIDENCE_INVALID = "lifecycle_evidence_invalid"
    UNRESOLVED_AT_WINDOW_END = "unresolved_at_window_end"
    DISPUTED_AT_WINDOW_END = "disputed_at_window_end"


class ProspectiveTargetExclusionV1(VersionedModel):
    """A selected target that remains visible after becoming inadmissible."""

    schema_version: ClassVar[str] = "prospective_target_exclusion.v1"

    exclusion_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    target: ProspectiveTargetV1
    target_receipt: EvidencePersistenceReceiptV1
    reason: ProspectiveTargetExclusionReason
    detail: str = Field(min_length=1)
    excluded_at: datetime

    @field_validator("excluded_at")
    @classmethod
    def _anchor_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _bind_target(self) -> ProspectiveTargetExclusionV1:
        if self.experiment_id != self.target.experiment_id:
            raise ValueError("target exclusion names a different experiment")
        if (
            self.target_receipt.experiment_id != self.experiment_id
            or self.target_receipt.artifact_kind is not EvidenceArtifactKind.TARGET_DECLARATION
            or self.target_receipt.artifact_id != self.target.target_id
        ):
            raise ValueError("target exclusion receipt names a different target")
        verify_receipt_for_record(self.target_receipt, self.target)
        expected = _identity(
            "prospective_target_exclusion_identity.v1",
            self.experiment_id,
            self.target.target_id,
            self.target_receipt.receipt_id,
            self.reason.value,
            self.detail,
            self.excluded_at.isoformat(),
            prefix="target-exclusion",
        )
        if self.exclusion_id != expected:
            raise ValueError("exclusion_id disagrees with the excluded target evidence")
        if self.excluded_at < self.target_receipt.persisted_at:
            raise ValueError("a target cannot be excluded before it was durably selected")
        return self

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        record["target"] = self.target.to_record()
        record["target_receipt"] = self.target_receipt.to_record()
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> ProspectiveTargetExclusionV1:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["target"] = ProspectiveTargetV1.from_record(dict(payload["target"]))
        payload["target_receipt"] = EvidencePersistenceReceiptV1.from_record(
            dict(payload["target_receipt"])
        )
        return cls.model_validate(payload)


class ProspectiveTargetContributionV1(VersionedModel):
    """One equally weighted, independent target contribution for one method."""

    schema_version: ClassVar[str] = "prospective_target_contribution.v1"

    contribution_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    target_bundle_evidence_digest: str = Field(min_length=64, max_length=64)
    category: str | None = None
    method: str = Field(min_length=1)
    forecast_id: str = Field(min_length=1)
    evaluation_id: str = Field(min_length=1)
    evaluation_record_sha256: str = Field(min_length=64, max_length=64)
    score: Decimal = Field(ge=0, le=1)
    outcome_yes: int = Field(ge=0, le=1)
    brier_score: Decimal
    log_loss: Decimal
    absolute_error: Decimal
    weight: Decimal = Field(default=Decimal(1), gt=0)

    @model_validator(mode="after")
    def _identity_is_bound(self) -> ProspectiveTargetContributionV1:
        expected = _identity(
            "prospective_target_contribution_identity.v1",
            self.experiment_id,
            self.target_id,
            self.target_bundle_evidence_digest,
            self.method,
            self.forecast_id,
            self.evaluation_id,
            self.evaluation_record_sha256,
            prefix="target-contribution",
        )
        if self.contribution_id != expected:
            raise ValueError("contribution_id disagrees with its target evaluation")
        if self.weight != 1:
            raise ValueError("the frozen protocol requires equal unit weight per target")
        return self


class ProspectiveExperimentReportV1(VersionedModel):
    """Aggregate counts and two deliberately independent verdicts."""

    schema_version: ClassVar[str] = "prospective_experiment_report.v1"

    experiment_id: str = Field(min_length=1)
    created_at: datetime
    observation_complete: bool
    selected_target_count: int = Field(ge=0)
    resolved_target_count: int = Field(ge=0)
    excluded_target_count: int = Field(ge=0)
    contribution_count: int = Field(ge=0)
    category_count: int = Field(ge=0)
    yes_outcome_count: int = Field(ge=0)
    no_outcome_count: int = Field(ge=0)
    measurement_layer_verdict: MeasurementLayerVerdict
    calibration_verdict: CalibrationVerdict
    method_diagnostics: Mapping[str, Any]
    uncertainty: Mapping[str, Any]
    target_bundle_digests: Mapping[str, str]
    contribution_collection_digest: str = Field(min_length=64, max_length=64)
    exclusion_collection_digest: str = Field(min_length=64, max_length=64)
    limitations: tuple[str, ...]

    @field_validator("created_at")
    @classmethod
    def _anchor_created_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("method_diagnostics", "uncertainty", "target_bundle_digests")
    @classmethod
    def _freeze_mapping(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], freeze(value))

    @field_serializer("method_diagnostics", "uncertainty", "target_bundle_digests")
    def _thaw_mapping(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw(value)  # type: ignore[no-any-return]

    @field_validator("limitations")
    @classmethod
    def _require_limitations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or not all(item.strip() for item in value):
            raise ValueError("an experiment report must state its limitations")
        return value


class ProspectiveExperimentBundleV1(VersionedModel):
    """Self-validating multi-target experiment boundary."""

    schema_version: ClassVar[str] = "prospective_experiment_bundle.v1"

    protocol: ProspectiveExperimentProtocolV1
    protocol_receipt: EvidencePersistenceReceiptV1
    target_bundles: tuple[EvaluationRunBundleV3, ...]
    target_exclusions: tuple[ProspectiveTargetExclusionV1, ...]
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
    def from_record(cls, record: dict[str, Any]) -> ProspectiveExperimentBundleV1:
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
            ProspectiveTargetExclusionV1.from_record(dict(item))
            for item in payload["target_exclusions"]
        )
        payload["contributions"] = tuple(
            ProspectiveTargetContributionV1.from_record(dict(item))
            for item in payload["contributions"]
        )
        payload["report"] = ProspectiveExperimentReportV1.from_record(dict(payload["report"]))
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def _aggregate_is_recomputable(self) -> ProspectiveExperimentBundleV1:
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
        for bundle in self.target_bundles:
            if bundle.protocol != self.protocol or bundle.protocol_receipt != self.protocol_receipt:
                raise ValueError("target bundle disagrees with the aggregate protocol evidence")
        if any(
            item.experiment_id != self.protocol.experiment_id for item in self.target_exclusions
        ):
            raise ValueError("target exclusion names a different experiment")
        expected_contributions = _target_contributions(self.target_bundles)
        if self.contributions != expected_contributions:
            raise ValueError("target contributions disagree with the last admissible evaluations")
        expected_report = _experiment_report(
            protocol=self.protocol,
            bundles=self.target_bundles,
            exclusions=self.target_exclusions,
            contributions=self.contributions,
            created_at=self.report.created_at,
            observation_complete=self.report.observation_complete,
        )
        if self.report != expected_report:
            raise ValueError("experiment report disagrees with its target evidence")
        expected_digest = prospective_experiment_digest(
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


def aggregate_prospective_experiment(
    *,
    protocol: ProspectiveExperimentProtocolV1,
    protocol_receipt: EvidencePersistenceReceiptV1,
    target_bundles: Sequence[EvaluationRunBundleV3],
    target_exclusions: Sequence[ProspectiveTargetExclusionV1] = (),
    created_at: datetime,
    observation_complete: bool,
) -> ProspectiveExperimentBundleV1:
    bundles = tuple(sorted(target_bundles, key=lambda item: item.target.selection_rank))
    exclusions = tuple(sorted(target_exclusions, key=lambda item: item.target.selection_rank))
    contributions = _target_contributions(bundles)
    report = _experiment_report(
        protocol=protocol,
        bundles=bundles,
        exclusions=exclusions,
        contributions=contributions,
        created_at=ensure_utc(created_at),
        observation_complete=observation_complete,
    )
    digest = prospective_experiment_digest(
        protocol=protocol,
        protocol_receipt=protocol_receipt,
        target_bundles=bundles,
        target_exclusions=exclusions,
        contributions=contributions,
        report=report,
    )
    return ProspectiveExperimentBundleV1(
        protocol=protocol,
        protocol_receipt=protocol_receipt,
        target_bundles=bundles,
        target_exclusions=exclusions,
        contributions=contributions,
        report=report,
        evidence_digest=digest,
    )


def build_target_exclusion(
    *,
    experiment_id: str,
    target: ProspectiveTargetV1,
    target_receipt: EvidencePersistenceReceiptV1,
    reason: ProspectiveTargetExclusionReason,
    detail: str,
    excluded_at: datetime,
) -> ProspectiveTargetExclusionV1:
    anchored = ensure_utc(excluded_at)
    exclusion_id = _identity(
        "prospective_target_exclusion_identity.v1",
        experiment_id,
        target.target_id,
        target_receipt.receipt_id,
        reason.value,
        detail,
        anchored.isoformat(),
        prefix="target-exclusion",
    )
    return ProspectiveTargetExclusionV1(
        exclusion_id=exclusion_id,
        experiment_id=experiment_id,
        target=target,
        target_receipt=target_receipt,
        reason=reason,
        detail=detail,
        excluded_at=anchored,
    )


def prospective_experiment_digest(
    *,
    protocol: ProspectiveExperimentProtocolV1,
    protocol_receipt: EvidencePersistenceReceiptV1,
    target_bundles: Sequence[EvaluationRunBundleV3],
    target_exclusions: Sequence[ProspectiveTargetExclusionV1],
    contributions: Sequence[ProspectiveTargetContributionV1],
    report: ProspectiveExperimentReportV1,
) -> str:
    return record_sha256(
        {
            "version": "prospective_experiment_evidence.v1",
            "protocol": protocol.to_record(),
            "protocol_receipt": protocol_receipt.to_record(),
            "target_bundles": [item.to_record() for item in target_bundles],
            "target_exclusions": [item.to_record() for item in target_exclusions],
            "contributions": [item.to_record() for item in contributions],
            "report": report.to_record(),
        }
    )


def _target_contributions(
    bundles: Sequence[EvaluationRunBundleV3],
) -> tuple[ProspectiveTargetContributionV1, ...]:
    contributions: list[ProspectiveTargetContributionV1] = []
    for bundle in bundles:
        forecasts = {item.forecast_id: item for item in bundle.forecasts}
        latest: dict[str, ForecastEvaluationV2] = {}
        for evaluation in bundle.evaluations:
            current = latest.get(evaluation.forecast_method)
            if current is None or (
                forecasts[evaluation.forecast_id].as_of_received_time,
                forecasts[evaluation.forecast_id].as_of_ingest_sequence,
                evaluation.forecast_id,
            ) > (
                forecasts[current.forecast_id].as_of_received_time,
                forecasts[current.forecast_id].as_of_ingest_sequence,
                current.forecast_id,
            ):
                latest[evaluation.forecast_method] = evaluation
        for method, evaluation in sorted(latest.items()):
            evaluation_digest = record_sha256(evaluation.to_record())
            identity = _identity(
                "prospective_target_contribution_identity.v1",
                bundle.protocol.experiment_id,
                bundle.target.target_id,
                bundle.evidence_digest,
                method,
                evaluation.forecast_id,
                evaluation.evaluation_id,
                evaluation_digest,
                prefix="target-contribution",
            )
            contributions.append(
                ProspectiveTargetContributionV1(
                    contribution_id=identity,
                    experiment_id=bundle.protocol.experiment_id,
                    target_id=bundle.target.target_id,
                    target_bundle_evidence_digest=bundle.evidence_digest,
                    category=bundle.target.category,
                    method=method,
                    forecast_id=evaluation.forecast_id,
                    evaluation_id=evaluation.evaluation_id,
                    evaluation_record_sha256=evaluation_digest,
                    score=evaluation.score,
                    outcome_yes=evaluation.outcome_yes,
                    brier_score=evaluation.brier_score,
                    log_loss=evaluation.log_loss,
                    absolute_error=evaluation.absolute_error,
                )
            )
    return tuple(contributions)


def _experiment_report(
    *,
    protocol: ProspectiveExperimentProtocolV1,
    bundles: Sequence[EvaluationRunBundleV3],
    exclusions: Sequence[ProspectiveTargetExclusionV1],
    contributions: Sequence[ProspectiveTargetContributionV1],
    created_at: datetime,
    observation_complete: bool,
) -> ProspectiveExperimentReportV1:
    if observation_complete and len(bundles) + len(exclusions) != (
        protocol.minimum_intended_resolved_target_count
    ):
        raise ValueError("a completed experiment must account for every intended target")
    if len(bundles) + len(exclusions) > protocol.minimum_intended_resolved_target_count:
        raise ValueError("experiment exceeds its frozen intended target count")
    resolved = len(bundles)
    measurement = (
        MeasurementLayerVerdict.PASSED
        if observation_complete
        and resolved >= protocol.structural_minimum_independent_resolved_targets
        else (
            MeasurementLayerVerdict.PARTIALLY_VALIDATED
            if resolved
            else MeasurementLayerVerdict.BLOCKED
        )
    )
    calibration = (
        CalibrationVerdict.NOT_EVALUABLE
        if not contributions
        else CalibrationVerdict.NOT_ESTABLISHED
    )
    by_method: dict[str, list[ProspectiveTargetContributionV1]] = {}
    for contribution in contributions:
        by_method.setdefault(contribution.method, []).append(contribution)
    evaluations_by_id = {
        evaluation.evaluation_id: evaluation
        for bundle in bundles
        for evaluation in bundle.evaluations
    }
    diagnostics: dict[str, Any] = {}
    for method, members in sorted(by_method.items()):
        evaluations = [evaluations_by_id[item.evaluation_id] for item in members]
        calibration_record = calibration_report(
            evaluations,
            method=method,
            bin_count=len(protocol.calibration_bin_edges) - 1,
        ).as_record()
        with evaluation_context():
            mean_absolute_error = sum((item.absolute_error for item in members), Decimal(0)) / len(
                members
            )
        diagnostics[method] = {
            **calibration_record,
            "independent_target_count": len(members),
            "mean_absolute_error": str(mean_absolute_error),
            "target_weight": "1",
        }
    categories = {bundle.target.category or "unknown" for bundle in bundles}
    yes_count = sum(
        bundle.resolution.winning_token_id == bundle.target.yes_token_id for bundle in bundles
    )
    no_count = resolved - yes_count
    scientific_thresholds_met = (
        observation_complete
        and resolved >= protocol.scientific_minimum_resolved_target_count
        and len(categories) >= protocol.minimum_category_count_for_calibration
        and yes_count >= protocol.minimum_yes_outcomes_for_calibration
        and no_count >= protocol.minimum_no_outcomes_for_calibration
    )
    uncertainty = {
        "status": "not_estimated",
        "reason": (
            "baseline_scores_are_not_calibrated_probabilities"
            if scientific_thresholds_met
            else "protocol_scientific_sufficiency_not_met"
        ),
    }
    return ProspectiveExperimentReportV1(
        experiment_id=protocol.experiment_id,
        created_at=created_at,
        observation_complete=observation_complete,
        selected_target_count=resolved + len(exclusions),
        resolved_target_count=resolved,
        excluded_target_count=len(exclusions),
        contribution_count=len(contributions),
        category_count=len(categories) if bundles else 0,
        yes_outcome_count=yes_count,
        no_outcome_count=no_count,
        measurement_layer_verdict=measurement,
        calibration_verdict=calibration,
        method_diagnostics=diagnostics,
        uncertainty=uncertainty,
        target_bundle_digests={item.target.target_id: item.evidence_digest for item in bundles},
        contribution_collection_digest=record_sha256([item.to_record() for item in contributions]),
        exclusion_collection_digest=record_sha256([item.to_record() for item in exclusions]),
        limitations=(
            "Within-target trajectory points are dependent; only the last admissible "
            "point per method contributes one unit per independently resolved target.",
            "This aggregate evaluates uncalibrated market baselines and cannot establish "
            "an ARGOS probability calibration claim.",
        ),
    )


def _identity(version: str, *parts: str, prefix: str) -> str:
    material = (version, *parts)
    encoded = "|".join(f"{len(part)}:{part}" for part in material)
    return f"{prefix}-{hashlib.sha256(encoded.encode()).hexdigest()[:32]}"
