"""Prospective single-target evaluation over an explicitly proven cutoff."""

from __future__ import annotations

from dataclasses import dataclass

from argos.baselines import BaselineMethod, MarketBaselineForecastV2
from argos.clock import Clock
from argos.compiler.contract import CompiledMarketContractV1
from argos.config.settings import Settings
from argos.domain.market import MarketDefinitionV1
from argos.evaluation.bundle import (
    EvaluationDecisionV1,
    EvaluationExclusionV1,
    EvaluationPolicyV2,
    record_sha256,
)
from argos.evaluation.prospective import (
    EvidencePersistenceReceiptV1,
    LifecycleObservationV1,
    ProspectiveExperimentProtocolV1,
    ProspectiveTargetV1,
    ResolutionCutoffEvidenceV1,
)
from argos.evaluation.prospective_bundle import (
    EvaluationRunBundleV3,
    bundle_evidence_digest_v3,
)
from argos.evaluation.report import EvaluationReportV3
from argos.evaluation.run_v2 import evaluate_capture
from argos.evaluation.scoring import ForecastEvaluationV2
from argos.resolution.gamma_resolution import ResolutionV1
from argos.store.event_store import EventStore

__all__ = ["ProspectiveEvaluationResult", "evaluate_prospective_capture"]


@dataclass(frozen=True, slots=True)
class ProspectiveEvaluationResult:
    """A V3 bundle and convenient immutable views of its scored children."""

    bundle: EvaluationRunBundleV3

    @property
    def report(self) -> EvaluationReportV3:
        return self.bundle.report

    @property
    def forecasts(self) -> tuple[MarketBaselineForecastV2, ...]:
        return self.bundle.forecasts

    @property
    def evaluations(self) -> tuple[ForecastEvaluationV2, ...]:
        return self.bundle.evaluations

    @property
    def decisions(self) -> tuple[EvaluationDecisionV1, ...]:
        return self.bundle.decisions

    @property
    def exclusions(self) -> tuple[EvaluationExclusionV1, ...]:
        return self.bundle.exclusions


def evaluate_prospective_capture(
    *,
    store: EventStore,
    capture_run_id: str,
    resolution: ResolutionV1,
    market: MarketDefinitionV1,
    contract: CompiledMarketContractV1,
    protocol: ProspectiveExperimentProtocolV1,
    protocol_receipt: EvidencePersistenceReceiptV1,
    market_receipt: EvidencePersistenceReceiptV1,
    contract_receipt: EvidencePersistenceReceiptV1,
    target: ProspectiveTargetV1,
    target_receipt: EvidencePersistenceReceiptV1,
    lifecycle_observations: tuple[LifecycleObservationV1, ...],
    lifecycle_receipts: tuple[EvidencePersistenceReceiptV1, ...],
    cutoff_evidence: ResolutionCutoffEvidenceV1,
    cutoff_receipt: EvidencePersistenceReceiptV1,
    settings: Settings,
    clock: Clock,
    evaluation_run_id: str,
    methods: tuple[BaselineMethod, ...] = (
        BaselineMethod.MIDPOINT,
        BaselineMethod.DISPLAYED_PRICE,
        BaselineMethod.LAST_TRADE,
        BaselineMethod.PERSISTENCE,
    ),
    policy: EvaluationPolicyV2 | None = None,
    extra_limitations: tuple[str, ...] = (),
) -> ProspectiveEvaluationResult:
    """Evaluate one target without treating ``ResolutionV1.resolved_at`` as its cutoff.

    The V2 replay engine remains the single implementation of information-state
    emission and scoring.  It receives an ephemeral, validated resolution view
    whose ``resolved_at`` is the independently proven V3 cutoff.  That view is
    never persisted: the resulting V3 bundle carries the original resolution
    record and the separate cutoff evidence that justified temporal admission.
    """
    if settings.fingerprint() != protocol.config_fingerprint:
        raise ValueError("runtime settings disagree with the frozen protocol")
    selected_policy = policy or EvaluationPolicyV2()
    if (
        selected_policy.structural_minimum_independent_resolved_targets
        != protocol.structural_minimum_independent_resolved_targets
    ):
        raise ValueError("evaluation policy structural floor disagrees with the protocol")

    scoring_resolution = ResolutionV1.model_validate(
        {**resolution.model_dump(mode="python"), "resolved_at": cutoff_evidence.selected_cutoff}
    )
    v2 = evaluate_capture(
        store=store,
        capture_run_id=capture_run_id,
        resolution=scoring_resolution,
        token_id=target.yes_token_id,
        settings=settings,
        clock=clock,
        evaluation_run_id=evaluation_run_id,
        contract=contract,
        methods=methods,
        epsilon=protocol.log_loss_epsilon,
        bin_count=len(protocol.calibration_bin_edges) - 1,
        code_revision=protocol.code_revision,
        working_tree=protocol.working_tree,
        extra_limitations=extra_limitations,
        policy=selected_policy,
    )

    child_digests = {
        "forecasts": record_sha256([item.to_record() for item in v2.forecasts]),
        "evaluations": record_sha256([item.to_record() for item in v2.evaluations]),
        "decisions": record_sha256([item.to_record() for item in v2.decisions]),
        "exclusions": record_sha256([item.to_record() for item in v2.exclusions]),
        "lifecycle_observations": record_sha256(
            [item.to_record() for item in lifecycle_observations]
        ),
        "lifecycle_receipts": record_sha256([item.to_record() for item in lifecycle_receipts]),
    }
    report_fields = v2.report.model_dump(mode="python")
    report_fields.update(
        {
            "resolution_record_sha256": record_sha256(resolution.to_record()),
            "resolution_cutoff": cutoff_evidence.selected_cutoff,
            "child_record_digests": child_digests,
            "experiment_id": protocol.experiment_id,
            "protocol_record_sha256": protocol_receipt.artifact_sha256,
            "protocol_receipt_id": protocol_receipt.receipt_id,
            "target_id": target.target_id,
            "target_receipt_id": target_receipt.receipt_id,
            "market_receipt_id": market_receipt.receipt_id,
            "contract_receipt_id": contract_receipt.receipt_id,
            "cutoff_evidence_id": cutoff_evidence.cutoff_evidence_id,
            "cutoff_receipt_id": cutoff_receipt.receipt_id,
            "cutoff_basis": cutoff_evidence.cutoff_basis.value,
            "resolution_source_time": cutoff_evidence.source_time,
            "resolution_retrieved_at": cutoff_evidence.retrieved_at,
            "lifecycle_observation_count": len(lifecycle_observations),
        }
    )
    report = EvaluationReportV3.model_validate(report_fields)
    digest = bundle_evidence_digest_v3(
        evaluation_run_id=evaluation_run_id,
        policy=selected_policy,
        protocol=protocol,
        protocol_receipt=protocol_receipt,
        market=market,
        market_receipt=market_receipt,
        contract=contract,
        contract_receipt=contract_receipt,
        target=target,
        target_receipt=target_receipt,
        lifecycle_observations=lifecycle_observations,
        lifecycle_receipts=lifecycle_receipts,
        cutoff_evidence=cutoff_evidence,
        cutoff_receipt=cutoff_receipt,
        resolution=resolution,
        report=report,
        forecasts=v2.forecasts,
        evaluations=v2.evaluations,
        decisions=v2.decisions,
        exclusions=v2.exclusions,
    )
    bundle = EvaluationRunBundleV3(
        evaluation_run_id=evaluation_run_id,
        policy=selected_policy,
        protocol=protocol,
        protocol_receipt=protocol_receipt,
        market=market,
        market_receipt=market_receipt,
        contract=contract,
        contract_receipt=contract_receipt,
        target=target,
        target_receipt=target_receipt,
        lifecycle_observations=lifecycle_observations,
        lifecycle_receipts=lifecycle_receipts,
        cutoff_evidence=cutoff_evidence,
        cutoff_receipt=cutoff_receipt,
        resolution=resolution,
        report=report,
        forecasts=v2.forecasts,
        evaluations=v2.evaluations,
        decisions=v2.decisions,
        exclusions=v2.exclusions,
        evidence_digest=digest,
    )
    return ProspectiveEvaluationResult(bundle=bundle)
