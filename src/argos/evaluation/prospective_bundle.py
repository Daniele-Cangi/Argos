"""The prospective M4 claim boundary.

V2 remains the historical corrected single-target boundary.  V3 adds the
records ADR-0014 requires before a forecast can be admitted: a frozen protocol,
durable market/contract/target receipts, an ordered lifecycle chain, and an
explicit cutoff derived from the first final observation (or a source terminal
timestamp when the protocol selected that basis).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from pydantic import Field, model_validator

from argos.baselines.forecast import MarketBaselineForecastV2
from argos.compiler.contract import CompiledMarketContractV1
from argos.domain.market import MarketDefinitionV1
from argos.domain.versioning import VersionedModel, ensure_supported_version
from argos.evaluation.bundle import (
    EvaluationDecisionV1,
    EvaluationExclusionV1,
    EvaluationPolicyV2,
    record_sha256,
)
from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    EvidencePersistenceReceiptV1,
    LifecycleObservationV1,
    ProspectiveExperimentProtocolV1,
    ProspectiveTargetV1,
    ResolutionCutoffEvidenceV1,
    verify_receipt_for_record,
)
from argos.evaluation.report import EvaluationReportV3, HeadlineStatus
from argos.evaluation.scoring import ForecastEvaluationV2
from argos.resolution.gamma_resolution import ResolutionStatus, ResolutionV1

__all__ = ["EvaluationRunBundleV3", "bundle_evidence_digest_v3"]


class EvaluationRunBundleV3(VersionedModel):
    """A self-contained prospective single-target evaluation claim."""

    schema_version: ClassVar[str] = "evaluation_run_bundle.v3"

    evaluation_run_id: str = Field(min_length=1)
    policy: EvaluationPolicyV2
    protocol: ProspectiveExperimentProtocolV1
    protocol_receipt: EvidencePersistenceReceiptV1
    market: MarketDefinitionV1
    market_receipt: EvidencePersistenceReceiptV1
    contract: CompiledMarketContractV1
    contract_receipt: EvidencePersistenceReceiptV1
    target: ProspectiveTargetV1
    target_receipt: EvidencePersistenceReceiptV1
    lifecycle_observations: tuple[LifecycleObservationV1, ...]
    lifecycle_receipts: tuple[EvidencePersistenceReceiptV1, ...]
    cutoff_evidence: ResolutionCutoffEvidenceV1
    cutoff_receipt: EvidencePersistenceReceiptV1
    resolution: ResolutionV1
    report: EvaluationReportV3
    forecasts: tuple[MarketBaselineForecastV2, ...]
    evaluations: tuple[ForecastEvaluationV2, ...]
    decisions: tuple[EvaluationDecisionV1, ...]
    exclusions: tuple[EvaluationExclusionV1, ...]
    evidence_digest: str = Field(min_length=64, max_length=64)

    def to_record(self) -> dict[str, Any]:
        """Serialize every nested persistent child with its own schema version."""
        record = super().to_record()
        record.update(
            {
                "policy": self.policy.to_record(),
                "protocol": self.protocol.to_record(),
                "protocol_receipt": self.protocol_receipt.to_record(),
                "market": self.market.to_record(),
                "market_receipt": self.market_receipt.to_record(),
                "contract": self.contract.to_record(),
                "contract_receipt": self.contract_receipt.to_record(),
                "target": self.target.to_record(),
                "target_receipt": self.target_receipt.to_record(),
                "lifecycle_observations": [
                    item.to_record() for item in self.lifecycle_observations
                ],
                "lifecycle_receipts": [item.to_record() for item in self.lifecycle_receipts],
                "cutoff_evidence": self.cutoff_evidence.to_record(),
                "cutoff_receipt": self.cutoff_receipt.to_record(),
                "resolution": self.resolution.to_record(),
                "report": self.report.to_record(),
                "forecasts": [item.to_record() for item in self.forecasts],
                "evaluations": [item.to_record() for item in self.evaluations],
                "decisions": [item.to_record() for item in self.decisions],
                "exclusions": [item.to_record() for item in self.exclusions],
            }
        )
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> EvaluationRunBundleV3:
        """Restore and version-check every nested prospective child."""
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["policy"] = EvaluationPolicyV2.from_record(dict(payload["policy"]))
        payload["protocol"] = ProspectiveExperimentProtocolV1.from_record(dict(payload["protocol"]))
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
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def _prospective_claim_is_semantically_bound(self) -> EvaluationRunBundleV3:
        self._validate_receipts()
        self._validate_target_and_contract()
        self._validate_lifecycle_and_cutoff()
        self._validate_report_links()
        self._validate_children()

        expected = bundle_evidence_digest_v3(
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
        if self.evidence_digest != expected:
            raise ValueError("evidence_digest disagrees with the prospective bundle records")
        return self

    def _validate_receipts(self) -> None:
        expected = (
            (
                self.protocol_receipt,
                self.protocol,
                EvidenceArtifactKind.EXPERIMENT_PROTOCOL,
                self.protocol.experiment_id,
            ),
            (
                self.market_receipt,
                self.market,
                EvidenceArtifactKind.MARKET_DEFINITION,
                self.market.market_id,
            ),
            (
                self.contract_receipt,
                self.contract,
                EvidenceArtifactKind.COMPILED_CONTRACT,
                self.contract.contract_id,
            ),
            (
                self.target_receipt,
                self.target,
                EvidenceArtifactKind.TARGET_DECLARATION,
                self.target.target_id,
            ),
            (
                self.cutoff_receipt,
                self.cutoff_evidence,
                EvidenceArtifactKind.RESOLUTION_CUTOFF,
                self.cutoff_evidence.cutoff_evidence_id,
            ),
        )
        for receipt, record, kind, artifact_id in expected:
            _verify_receipt_link(
                receipt,
                record,
                experiment_id=self.protocol.experiment_id,
                kind=kind,
                artifact_id=artifact_id,
            )
        if len(self.lifecycle_receipts) != len(self.lifecycle_observations):
            raise ValueError("every lifecycle observation must carry one persistence receipt")
        for observation, receipt in zip(
            self.lifecycle_observations, self.lifecycle_receipts, strict=True
        ):
            _verify_receipt_link(
                receipt,
                observation,
                experiment_id=self.protocol.experiment_id,
                kind=EvidenceArtifactKind.LIFECYCLE_OBSERVATION,
                artifact_id=observation.lifecycle_observation_id,
            )
            if receipt.persisted_at < observation.retrieved_at:
                raise ValueError("a lifecycle receipt cannot predate its source retrieval")
        if self.protocol_receipt.persisted_at < self.protocol.declared_at:
            raise ValueError("protocol receipt cannot predate protocol declaration")
        if self.protocol_receipt.persisted_at >= self.target.selected_at:
            raise ValueError("protocol must be durably available before target selection")
        if self.market_receipt.persisted_at > self.contract.compiled_at:
            raise ValueError("market evidence must be persisted before contract compilation")
        if self.contract_receipt.persisted_at < self.contract.compiled_at:
            raise ValueError("contract receipt cannot predate compilation")
        if self.target_receipt.persisted_at < self.target.selected_at:
            raise ValueError("target receipt cannot predate selection")
        if self.cutoff_receipt.persisted_at < self.cutoff_evidence.retrieved_at:
            raise ValueError("cutoff receipt cannot predate its final source observation")

    def _validate_target_and_contract(self) -> None:
        experiment_id = self.protocol.experiment_id
        if self.target.experiment_id != experiment_id:
            raise ValueError("target names a different experiment than its protocol")
        if self.target.cutoff_basis is not self.protocol.cutoff_basis:
            raise ValueError("target cutoff basis disagrees with the frozen protocol")
        if (self.target.market_id, self.target.condition_id) != (
            self.market.market_id,
            self.market.condition_id,
        ):
            raise ValueError("target scope disagrees with its persisted market definition")
        if set(self.market.outcomes) != {"Yes", "No"} or set(self.market.outcome_token_map) != {
            "Yes",
            "No",
        }:
            raise ValueError("prospective target market has no standard Yes/No token map")
        market_yes = self.market.token_id_for("Yes")
        market_no = self.market.token_id_for("No")
        if (self.target.yes_token_id, self.target.no_token_id) != (market_yes, market_no):
            raise ValueError("target token mapping disagrees with its market definition")
        if (
            self.target.market_record_sha256 != record_sha256(self.market.to_record())
            or self.target.market_receipt_id != self.market_receipt.receipt_id
        ):
            raise ValueError("target market identity disagrees with its market evidence")
        if (
            self.contract.market_id,
            self.contract.condition_id,
            self.contract.source_market_hash,
        ) != (self.market.market_id, self.market.condition_id, self.market.raw_payload_sha256):
            raise ValueError("compiled contract disagrees with its source market")
        if (
            self.target.contract_id != self.contract.contract_id
            or self.target.contract_record_sha256 != record_sha256(self.contract.to_record())
            or self.target.contract_receipt_id != self.contract_receipt.receipt_id
        ):
            raise ValueError("target contract identity disagrees with its contract evidence")
        if self.target.selection_rank > self.protocol.minimum_intended_resolved_target_count:
            raise ValueError("target selection rank exceeds the frozen intended population")
        if self.market_receipt.persisted_at > self.target.selected_at:
            raise ValueError("market evidence must be durable before target selection")
        if self.contract_receipt.persisted_at > self.target.selected_at:
            raise ValueError("contract evidence must be durable before target selection")

    def _validate_lifecycle_and_cutoff(self) -> None:
        if not self.lifecycle_observations:
            raise ValueError("prospective evaluation requires lifecycle observations")
        previous: LifecycleObservationV1 | None = None
        first_final: LifecycleObservationV1 | None = None
        for expected_ordinal, observation in enumerate(self.lifecycle_observations, start=1):
            if observation.ordinal != expected_ordinal:
                raise ValueError("lifecycle ordinals must be contiguous from one")
            if (observation.experiment_id, observation.target_id) != (
                self.protocol.experiment_id,
                self.target.target_id,
            ):
                raise ValueError("lifecycle observation names a different experiment or target")
            expected_previous = previous.lifecycle_observation_id if previous else None
            if observation.previous_observation_id != expected_previous:
                raise ValueError("lifecycle predecessor chain is broken")
            if previous is not None and observation.retrieved_at <= previous.retrieved_at:
                raise ValueError("lifecycle retrieval times must be strictly increasing")
            if observation.retrieved_at < self.target.selected_at:
                raise ValueError("lifecycle evidence cannot predate target selection")
            if first_final is None and observation.finality is ResolutionStatus.FINAL:
                first_final = observation
            previous = observation
        if first_final is None:
            raise ValueError("no lifecycle observation proves final settlement")
        cutoff = self.cutoff_evidence
        if (cutoff.experiment_id, cutoff.target_id, cutoff.cutoff_basis) != (
            self.protocol.experiment_id,
            self.target.target_id,
            self.protocol.cutoff_basis,
        ):
            raise ValueError("cutoff evidence disagrees with protocol or target scope")
        cutoff_links = (
            cutoff.lifecycle_observation_id,
            cutoff.source,
            cutoff.endpoint,
            cutoff.source_time,
            cutoff.retrieved_at,
            cutoff.raw_payload_sha256,
            cutoff.byte_length,
            cutoff.raw_payload_location,
            cutoff.resolution_id,
            cutoff.resolution_record_sha256,
        )
        first_final_links = (
            first_final.lifecycle_observation_id,
            first_final.source,
            first_final.endpoint,
            first_final.source_time,
            first_final.retrieved_at,
            first_final.raw_payload_sha256,
            first_final.byte_length,
            first_final.raw_payload_location,
            first_final.resolution_id,
            first_final.resolution_record_sha256,
        )
        if cutoff_links != first_final_links:
            raise ValueError("cutoff evidence does not name the first observed final settlement")
        if self.resolution.resolution_status is not ResolutionStatus.FINAL:
            raise ValueError("prospective evaluation requires a final resolution")
        if (
            cutoff.resolution_id != self.resolution.resolution_id
            or cutoff.resolution_record_sha256 != record_sha256(self.resolution.to_record())
            or cutoff.raw_payload_sha256 != self.resolution.source_payload_sha256
        ):
            raise ValueError("cutoff evidence disagrees with its resolution record")
        if self.resolution.normalized_at < cutoff.retrieved_at:
            raise ValueError("resolution normalization cannot predate its source retrieval")
        if self.resolution.condition_id != self.target.condition_id:
            raise ValueError("resolution condition disagrees with the prospective target")
        if self.resolution.winning_token_id not in (
            self.target.yes_token_id,
            self.target.no_token_id,
        ):
            raise ValueError("resolution winner is outside the target token mapping")

    def _validate_report_links(self) -> None:
        expected: dict[str, object] = {
            "evaluation_run_id": self.evaluation_run_id,
            "experiment_id": self.protocol.experiment_id,
            "protocol_record_sha256": self.protocol_receipt.artifact_sha256,
            "protocol_receipt_id": self.protocol_receipt.receipt_id,
            "target_id": self.target.target_id,
            "target_receipt_id": self.target_receipt.receipt_id,
            "market_receipt_id": self.market_receipt.receipt_id,
            "contract_receipt_id": self.contract_receipt.receipt_id,
            "cutoff_evidence_id": self.cutoff_evidence.cutoff_evidence_id,
            "cutoff_receipt_id": self.cutoff_receipt.receipt_id,
            "cutoff_basis": self.cutoff_evidence.cutoff_basis.value,
            "resolution_source_time": self.cutoff_evidence.source_time,
            "resolution_retrieved_at": self.cutoff_evidence.retrieved_at,
            "resolution_cutoff": self.cutoff_evidence.selected_cutoff,
            "lifecycle_observation_count": len(self.lifecycle_observations),
            "evaluation_policy_version": self.policy.schema_version,
            "resolution_id": self.resolution.resolution_id,
            "resolution_record_sha256": record_sha256(self.resolution.to_record()),
            "resolution_status": self.resolution.resolution_status.value,
            "resolution_normalizer_version": self.resolution.normalizer_version,
            "contract_id": self.contract.contract_id,
            "contract_record_sha256": record_sha256(self.contract.to_record()),
        }
        for field_name, value in expected.items():
            if getattr(self.report, field_name) != value:
                raise ValueError(f"report.{field_name} disagrees with prospective evidence")
        reproducibility_links: dict[str, object] = {
            "code_revision": self.protocol.code_revision,
            "working_tree": self.protocol.working_tree,
            "config_fingerprint": self.protocol.config_fingerprint,
            "log_loss_epsilon": self.protocol.log_loss_epsilon,
            "calibration_bin_count": len(self.protocol.calibration_bin_edges) - 1,
        }
        for field_name, value in reproducibility_links.items():
            if getattr(self.report, field_name) != value:
                raise ValueError(f"report.{field_name} disagrees with the frozen protocol")
        if self.report.created_at < self.cutoff_receipt.persisted_at:
            raise ValueError("prospective report cannot predate its cutoff receipt")
        if self.report.headline_status is not HeadlineStatus.NOT_ESTABLISHED:
            raise ValueError("one prospective target cannot establish a calibration headline")
        if "single_target_runner_cannot_establish_calibration" not in self.report.headline_reasons:
            raise ValueError("single-target calibration limitation must remain explicit")

    def _validate_children(self) -> None:
        forecast_ids = {item.forecast_id for item in self.forecasts}
        if len(forecast_ids) != len(self.forecasts):
            raise ValueError("forecast_id must be unique within a prospective bundle")
        evaluation_ids = [item.forecast_id for item in self.evaluations]
        exclusion_ids = [item.forecast_id for item in self.exclusions]
        if len(set(evaluation_ids)) != len(evaluation_ids):
            raise ValueError("one forecast may be evaluated at most once")
        if len(set(exclusion_ids)) != len(exclusion_ids):
            raise ValueError("one forecast may be excluded at most once")
        evaluated, excluded = set(evaluation_ids), set(exclusion_ids)
        if evaluated & excluded or evaluated | excluded != forecast_ids:
            raise ValueError("forecasts must form an exclusive scored-or-excluded partition")
        forecasts_by_id = {item.forecast_id: item for item in self.forecasts}
        preforecast_receipts = (
            self.protocol_receipt,
            self.market_receipt,
            self.contract_receipt,
            self.target_receipt,
        )
        for forecast in self.forecasts:
            if (
                forecast.evaluation_run_id,
                forecast.market_id,
                forecast.condition_id,
                forecast.token_id,
                forecast.contract_id,
            ) != (
                self.evaluation_run_id,
                self.target.market_id,
                self.target.condition_id,
                self.target.yes_token_id,
                self.contract.contract_id,
            ):
                raise ValueError("forecast scope disagrees with its prospective target")
            if not (
                self.protocol.observation_window_start
                <= forecast.as_of_received_time
                <= self.protocol.observation_window_end
            ):
                raise ValueError("forecast falls outside the frozen observation window")
            if any(
                receipt.persisted_at >= forecast.as_of_received_time
                for receipt in preforecast_receipts
            ):
                raise ValueError(
                    "protocol, contract and target receipts must precede every forecast"
                )
        for evaluation in self.evaluations:
            evaluated_forecast = forecasts_by_id[evaluation.forecast_id]
            if evaluated_forecast.as_of_received_time >= self.cutoff_evidence.selected_cutoff:
                raise ValueError("an evaluated forecast is not strictly before the proven cutoff")
            if (
                evaluation.evaluation_run_id,
                evaluation.resolution_id,
                evaluation.contract_id,
                evaluation.forecast_method,
                evaluation.condition_id,
                evaluation.token_id,
                evaluation.score,
                evaluation.calibration_status,
            ) != (
                self.evaluation_run_id,
                self.resolution.resolution_id,
                self.contract.contract_id,
                evaluated_forecast.method.value,
                evaluated_forecast.condition_id,
                evaluated_forecast.token_id,
                evaluated_forecast.raw_score,
                evaluated_forecast.calibration_status.value,
            ):
                raise ValueError("evaluation disagrees with its forecast or claim boundary")
        # The exact partition equality above already proves every exclusion and
        # evaluation references one of this bundle's forecasts.
        sequences = {item.ingest_sequence for item in self.decisions}
        if len(sequences) != len(self.decisions):
            raise ValueError("ingest_sequence must be unique within bundle decisions")

        expected_counts = {
            "arrival_count": len(self.decisions),
            "target_information_state_count": sum(
                item.target_state_included for item in self.decisions
            ),
            "forecast_count": len(self.forecasts),
            "forecast_point_count": len(self.forecasts),
            "scored_count": len(self.evaluations),
            "scored_forecast_point_count": len(self.evaluations),
            "abstention_count": sum(item.abstained for item in self.forecasts),
            "unresolved_count": 0,
            "resolved_target_count": 1,
            "headline_eligible_target_count": 1 if self.evaluations else 0,
        }
        for field_name, count in expected_counts.items():
            if getattr(self.report, field_name) != count:
                raise ValueError(f"report.{field_name} disagrees with its child records")
        expected_digests = _child_record_digests(
            forecasts=self.forecasts,
            evaluations=self.evaluations,
            decisions=self.decisions,
            exclusions=self.exclusions,
            lifecycle_observations=self.lifecycle_observations,
            lifecycle_receipts=self.lifecycle_receipts,
        )
        if dict(self.report.child_record_digests) != expected_digests:
            raise ValueError("report.child_record_digests disagrees with prospective children")


def bundle_evidence_digest_v3(
    *,
    evaluation_run_id: str,
    policy: EvaluationPolicyV2,
    protocol: ProspectiveExperimentProtocolV1,
    protocol_receipt: EvidencePersistenceReceiptV1,
    market: MarketDefinitionV1,
    market_receipt: EvidencePersistenceReceiptV1,
    contract: CompiledMarketContractV1,
    contract_receipt: EvidencePersistenceReceiptV1,
    target: ProspectiveTargetV1,
    target_receipt: EvidencePersistenceReceiptV1,
    lifecycle_observations: Sequence[LifecycleObservationV1],
    lifecycle_receipts: Sequence[EvidencePersistenceReceiptV1],
    cutoff_evidence: ResolutionCutoffEvidenceV1,
    cutoff_receipt: EvidencePersistenceReceiptV1,
    resolution: ResolutionV1,
    report: EvaluationReportV3,
    forecasts: Sequence[MarketBaselineForecastV2],
    evaluations: Sequence[ForecastEvaluationV2],
    decisions: Sequence[EvaluationDecisionV1],
    exclusions: Sequence[EvaluationExclusionV1],
) -> str:
    material = {
        "version": "evaluation_bundle_evidence.v3",
        "evaluation_run_id": evaluation_run_id,
        "policy": policy.to_record(),
        "protocol": protocol.to_record(),
        "protocol_receipt": protocol_receipt.to_record(),
        "market": market.to_record(),
        "market_receipt": market_receipt.to_record(),
        "contract": contract.to_record(),
        "contract_receipt": contract_receipt.to_record(),
        "target": target.to_record(),
        "target_receipt": target_receipt.to_record(),
        "lifecycle_observations": [item.to_record() for item in lifecycle_observations],
        "lifecycle_receipts": [item.to_record() for item in lifecycle_receipts],
        "cutoff_evidence": cutoff_evidence.to_record(),
        "cutoff_receipt": cutoff_receipt.to_record(),
        "resolution": resolution.to_record(),
        "report": report.to_record(),
        "forecasts": [item.to_record() for item in forecasts],
        "evaluations": [item.to_record() for item in evaluations],
        "decisions": [item.to_record() for item in decisions],
        "exclusions": [item.to_record() for item in exclusions],
    }
    return record_sha256(material)


def _verify_receipt_link(
    receipt: EvidencePersistenceReceiptV1,
    record: VersionedModel,
    *,
    experiment_id: str,
    kind: EvidenceArtifactKind,
    artifact_id: str,
) -> None:
    if (
        receipt.experiment_id,
        receipt.artifact_kind,
        receipt.artifact_id,
    ) != (experiment_id, kind, artifact_id):
        raise ValueError("persistence receipt names a different prospective artifact")
    verify_receipt_for_record(receipt, record)


def _child_record_digests(
    *,
    forecasts: Sequence[MarketBaselineForecastV2],
    evaluations: Sequence[ForecastEvaluationV2],
    decisions: Sequence[EvaluationDecisionV1],
    exclusions: Sequence[EvaluationExclusionV1],
    lifecycle_observations: Sequence[LifecycleObservationV1],
    lifecycle_receipts: Sequence[EvidencePersistenceReceiptV1],
) -> Mapping[str, str]:
    return {
        "forecasts": record_sha256([item.to_record() for item in forecasts]),
        "evaluations": record_sha256([item.to_record() for item in evaluations]),
        "decisions": record_sha256([item.to_record() for item in decisions]),
        "exclusions": record_sha256([item.to_record() for item in exclusions]),
        "lifecycle_observations": record_sha256(
            [item.to_record() for item in lifecycle_observations]
        ),
        "lifecycle_receipts": record_sha256([item.to_record() for item in lifecycle_receipts]),
    }
