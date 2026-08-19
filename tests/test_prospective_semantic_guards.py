"""Branch-complete semantic guard tests for the prospective M4 boundaries."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_evaluation_trust import CUTOFF
from test_prospective_bundle import _valid_result

from argos.clock import ReplayClock
from argos.config import Settings
from argos.evaluation import prospective_aggregation as aggregation_module
from argos.evaluation.bundle import EvaluationExclusionV1, ExclusionReason, record_sha256
from argos.evaluation.prospective import CutoffBasis
from argos.evaluation.prospective_aggregation import (
    CalibrationVerdict,
    MeasurementLayerVerdict,
    ProspectiveExperimentBundleV1,
    ProspectiveExperimentReportV1,
    ProspectiveTargetExclusionReason,
    aggregate_prospective_experiment,
    build_target_exclusion,
)
from argos.evaluation.report import HeadlineStatus
from argos.evaluation.run_v3 import evaluate_prospective_capture
from argos.resolution import ResolutionStatus


def _unchecked(model: Any, **updates: Any) -> Any:
    values = {name: getattr(model, name) for name in type(model).model_fields}
    values.update(updates)
    return type(model).model_construct(**values)


async def test_run_v3_accessors_and_preflight_guards(tmp_path: Path) -> None:
    result = await _valid_result(tmp_path)
    bundle = result.bundle
    assert result.report is bundle.report
    assert result.forecasts is bundle.forecasts
    assert result.evaluations is bundle.evaluations
    assert result.decisions is bundle.decisions
    assert result.exclusions is bundle.exclusions

    common = {
        "store": None,
        "capture_run_id": "not-reached",
        "resolution": bundle.resolution,
        "market": bundle.market,
        "contract": bundle.contract,
        "protocol": bundle.protocol,
        "protocol_receipt": bundle.protocol_receipt,
        "market_receipt": bundle.market_receipt,
        "contract_receipt": bundle.contract_receipt,
        "target": bundle.target,
        "target_receipt": bundle.target_receipt,
        "lifecycle_observations": bundle.lifecycle_observations,
        "lifecycle_receipts": bundle.lifecycle_receipts,
        "cutoff_evidence": bundle.cutoff_evidence,
        "cutoff_receipt": bundle.cutoff_receipt,
        "clock": ReplayClock(CUTOFF + timedelta(hours=3)),
        "evaluation_run_id": "preflight-guard",
    }
    wrong_settings = Settings(http_timeout_seconds=9.0)
    assert wrong_settings.fingerprint() != bundle.protocol.config_fingerprint
    with pytest.raises(ValueError, match="runtime settings"):
        evaluate_prospective_capture(settings=wrong_settings, **common)  # type: ignore[arg-type]

    wrong_policy = _unchecked(
        bundle.policy,
        structural_minimum_independent_resolved_targets=3,
    )
    with pytest.raises(ValueError, match="structural floor"):
        evaluate_prospective_capture(
            settings=Settings(),
            policy=wrong_policy,
            **common,  # type: ignore[arg-type]
        )


async def test_bundle_receipt_target_and_lifecycle_guards(tmp_path: Path) -> None:
    bundle = (await _valid_result(tmp_path)).bundle

    receipt_cases = [
        (
            {"lifecycle_receipts": bundle.lifecycle_receipts[:-1]},
            "every lifecycle observation",
        ),
        (
            {
                "lifecycle_receipts": (
                    bundle.lifecycle_receipts[0],
                    _unchecked(
                        bundle.lifecycle_receipts[1],
                        persisted_at=bundle.lifecycle_observations[1].retrieved_at
                        - timedelta(seconds=1),
                    ),
                )
            },
            "cannot predate its source retrieval",
        ),
        (
            {
                "protocol_receipt": _unchecked(
                    bundle.protocol_receipt,
                    persisted_at=bundle.protocol.declared_at - timedelta(seconds=1),
                )
            },
            "cannot predate protocol declaration",
        ),
        (
            {
                "protocol_receipt": _unchecked(
                    bundle.protocol_receipt,
                    persisted_at=bundle.target.selected_at,
                )
            },
            "before target selection",
        ),
        (
            {
                "market_receipt": _unchecked(
                    bundle.market_receipt,
                    persisted_at=bundle.contract.compiled_at + timedelta(seconds=1),
                )
            },
            "before contract compilation",
        ),
        (
            {
                "contract_receipt": _unchecked(
                    bundle.contract_receipt,
                    persisted_at=bundle.contract.compiled_at - timedelta(seconds=1),
                )
            },
            "cannot predate compilation",
        ),
        (
            {
                "target_receipt": _unchecked(
                    bundle.target_receipt,
                    persisted_at=bundle.target.selected_at - timedelta(seconds=1),
                )
            },
            "cannot predate selection",
        ),
        (
            {
                "cutoff_receipt": _unchecked(
                    bundle.cutoff_receipt,
                    persisted_at=bundle.cutoff_evidence.retrieved_at - timedelta(seconds=1),
                )
            },
            "cannot predate its final source",
        ),
    ]
    for updates, message in receipt_cases:
        attacked = _unchecked(bundle, **updates)
        with pytest.raises(ValueError, match=message):
            attacked._validate_receipts()

    other_basis = CutoffBasis.SOURCE_TERMINAL_TIMESTAMP
    target_cases = [
        (
            {"target": _unchecked(bundle.target, experiment_id="other-experiment")},
            "different experiment",
        ),
        (
            {"target": _unchecked(bundle.target, cutoff_basis=other_basis)},
            "cutoff basis",
        ),
        (
            {"target": _unchecked(bundle.target, market_id="other-market")},
            "target scope",
        ),
        (
            {
                "market": _unchecked(
                    bundle.market,
                    outcomes=("Maybe",),
                    outcome_token_map={"Maybe": bundle.target.yes_token_id},
                )
            },
            "standard Yes/No",
        ),
        (
            {"target": _unchecked(bundle.target, yes_token_id=bundle.target.no_token_id)},
            "token mapping",
        ),
        (
            {"target": _unchecked(bundle.target, market_record_sha256="0" * 64)},
            "target market identity",
        ),
        (
            {"contract": _unchecked(bundle.contract, source_market_hash="0" * 64)},
            "compiled contract",
        ),
        (
            {"target": _unchecked(bundle.target, contract_id="contract-other")},
            "target contract identity",
        ),
        (
            {
                "target": _unchecked(
                    bundle.target,
                    selection_rank=bundle.protocol.minimum_intended_resolved_target_count + 1,
                )
            },
            "selection rank",
        ),
        (
            {
                "market_receipt": _unchecked(
                    bundle.market_receipt,
                    persisted_at=bundle.target.selected_at + timedelta(seconds=1),
                )
            },
            "market evidence must be durable",
        ),
        (
            {
                "contract_receipt": _unchecked(
                    bundle.contract_receipt,
                    persisted_at=bundle.target.selected_at + timedelta(seconds=1),
                )
            },
            "contract evidence must be durable",
        ),
    ]
    for updates, message in target_cases:
        attacked = _unchecked(bundle, **updates)
        with pytest.raises(ValueError, match=message):
            attacked._validate_target_and_contract()

    first, final = bundle.lifecycle_observations

    def _linked_resolution_updates(resolution: Any) -> dict[str, Any]:
        resolution_digest = record_sha256(resolution.to_record())
        return {
            "lifecycle_observations": (
                first,
                _unchecked(
                    final,
                    resolution_record_sha256=resolution_digest,
                ),
            ),
            "cutoff_evidence": _unchecked(
                bundle.cutoff_evidence,
                resolution_record_sha256=resolution_digest,
            ),
            "resolution": resolution,
        }

    early_resolution = _unchecked(
        bundle.resolution,
        normalized_at=bundle.cutoff_evidence.retrieved_at - timedelta(seconds=1),
    )
    lifecycle_cases = [
        ({"lifecycle_observations": ()}, "requires lifecycle"),
        (
            {"lifecycle_observations": (_unchecked(first, ordinal=2), final)},
            "ordinals must be contiguous",
        ),
        (
            {
                "lifecycle_observations": (
                    _unchecked(first, experiment_id="other-experiment"),
                    final,
                )
            },
            "different experiment or target",
        ),
        (
            {
                "lifecycle_observations": (
                    first,
                    _unchecked(final, previous_observation_id="lifecycle-wrong"),
                )
            },
            "predecessor chain",
        ),
        (
            {
                "lifecycle_observations": (
                    first,
                    _unchecked(final, retrieved_at=first.retrieved_at),
                )
            },
            "strictly increasing",
        ),
        (
            {
                "lifecycle_observations": (
                    _unchecked(
                        first,
                        retrieved_at=bundle.target.selected_at - timedelta(seconds=1),
                    ),
                    final,
                )
            },
            "cannot predate target selection",
        ),
        (
            {
                "lifecycle_observations": (
                    first,
                    _unchecked(final, finality=ResolutionStatus.PROPOSED),
                )
            },
            "no lifecycle observation proves final",
        ),
        (
            {
                "cutoff_evidence": _unchecked(
                    bundle.cutoff_evidence,
                    target_id="target-other",
                )
            },
            "cutoff evidence disagrees with protocol",
        ),
        (
            {
                "cutoff_evidence": _unchecked(
                    bundle.cutoff_evidence,
                    raw_payload_sha256="0" * 64,
                )
            },
            "does not name the first observed final",
        ),
        (
            {
                "resolution": _unchecked(
                    bundle.resolution,
                    resolution_status=ResolutionStatus.PROPOSED,
                )
            },
            "requires a final resolution",
        ),
        (
            {
                "lifecycle_observations": (
                    first,
                    _unchecked(final, resolution_id="resolution-other"),
                ),
                "cutoff_evidence": _unchecked(
                    bundle.cutoff_evidence,
                    resolution_id="resolution-other",
                ),
            },
            "disagrees with its resolution",
        ),
        (
            _linked_resolution_updates(early_resolution),
            "normalization cannot predate",
        ),
        (
            _linked_resolution_updates(_unchecked(bundle.resolution, condition_id="0x" + "0" * 64)),
            "resolution condition",
        ),
        (
            _linked_resolution_updates(_unchecked(bundle.resolution, winning_token_id="1")),
            "winner is outside",
        ),
    ]
    for updates, message in lifecycle_cases:
        attacked = _unchecked(bundle, **updates)
        with pytest.raises(ValueError, match=message):
            attacked._validate_lifecycle_and_cutoff()


async def test_bundle_report_and_child_guards(tmp_path: Path) -> None:
    bundle = (await _valid_result(tmp_path)).bundle

    report_cases = [
        (
            _unchecked(
                bundle.report,
                created_at=bundle.cutoff_receipt.persisted_at - timedelta(seconds=1),
            ),
            "cannot predate its cutoff",
        ),
        (
            _unchecked(bundle.report, headline_status=HeadlineStatus.ESTABLISHED),
            "cannot establish a calibration",
        ),
        (
            _unchecked(bundle.report, headline_reasons=("another limitation",)),
            "calibration limitation",
        ),
    ]
    for report, message in report_cases:
        with pytest.raises(ValueError, match=message):
            _unchecked(bundle, report=report)._validate_report_links()

    forecast = bundle.forecasts[0]
    evaluation = bundle.evaluations[0]
    exclusion = EvaluationExclusionV1(
        forecast_id=forecast.forecast_id,
        reason=ExclusionReason.ABSTAINED,
        detail="guard-test exclusion",
    )
    assert len(bundle.decisions) >= 2
    child_cases = [
        ({"forecasts": (forecast, forecast)}, "forecast_id must be unique"),
        ({"evaluations": (evaluation, evaluation)}, "evaluated at most once"),
        ({"exclusions": (exclusion, exclusion)}, "excluded at most once"),
        ({"exclusions": (exclusion,)}, "exclusive scored-or-excluded partition"),
        (
            {"forecasts": (_unchecked(forecast, market_id="market-other"), *bundle.forecasts[1:])},
            "forecast scope",
        ),
        (
            {
                "forecasts": (
                    _unchecked(
                        forecast,
                        as_of_received_time=bundle.protocol.observation_window_start
                        - timedelta(seconds=1),
                    ),
                    *bundle.forecasts[1:],
                )
            },
            "outside the frozen observation window",
        ),
        (
            {
                "protocol_receipt": _unchecked(
                    bundle.protocol_receipt,
                    persisted_at=forecast.as_of_received_time,
                )
            },
            "receipts must precede every forecast",
        ),
        (
            {
                "forecasts": (
                    _unchecked(
                        forecast,
                        as_of_received_time=bundle.cutoff_evidence.selected_cutoff,
                    ),
                    *bundle.forecasts[1:],
                )
            },
            "not strictly before the proven cutoff",
        ),
        (
            {
                "evaluations": (
                    _unchecked(evaluation, resolution_id="resolution-other"),
                    *bundle.evaluations[1:],
                )
            },
            "evaluation disagrees",
        ),
        (
            {
                "decisions": (
                    bundle.decisions[0],
                    _unchecked(
                        bundle.decisions[1],
                        ingest_sequence=bundle.decisions[0].ingest_sequence,
                    ),
                    *bundle.decisions[2:],
                )
            },
            "ingest_sequence must be unique",
        ),
    ]
    for updates, message in child_cases:
        with pytest.raises(ValueError, match=message):
            _unchecked(bundle, **updates)._validate_children()


async def test_aggregation_exclusion_and_recomputation_guards(tmp_path: Path) -> None:
    target_bundle = (await _valid_result(tmp_path)).bundle
    exclusion = build_target_exclusion(
        experiment_id=target_bundle.protocol.experiment_id,
        target=target_bundle.target,
        target_receipt=target_bundle.target_receipt,
        reason=ProspectiveTargetExclusionReason.UNMODELED_STANDALONE_LAST_TRADE,
        detail="real standalone last_trade_price observed",
        excluded_at=CUTOFF + timedelta(hours=2),
    )
    blocked = aggregate_prospective_experiment(
        protocol=target_bundle.protocol,
        protocol_receipt=target_bundle.protocol_receipt,
        target_bundles=(),
        target_exclusions=(exclusion,),
        created_at=CUTOFF + timedelta(hours=3),
        observation_complete=False,
    )
    assert blocked.report.measurement_layer_verdict is MeasurementLayerVerdict.BLOCKED
    assert blocked.report.calibration_verdict is CalibrationVerdict.NOT_EVALUABLE
    assert ProspectiveExperimentBundleV1.from_record(blocked.to_record()) == blocked

    too_early = exclusion.target_receipt.persisted_at - timedelta(seconds=1)
    too_early_id = aggregation_module._identity(
        "prospective_target_exclusion_identity.v1",
        exclusion.experiment_id,
        exclusion.target.target_id,
        exclusion.target_receipt.receipt_id,
        exclusion.reason.value,
        exclusion.detail,
        too_early.isoformat(),
        prefix="target-exclusion",
    )

    invalid_exclusions = [
        (
            _unchecked(exclusion, experiment_id="other-experiment"),
            "different experiment",
        ),
        (
            _unchecked(
                exclusion,
                target_receipt=_unchecked(
                    exclusion.target_receipt,
                    artifact_id="target-other",
                ),
            ),
            "different target",
        ),
        (_unchecked(exclusion, exclusion_id="exclusion-wrong"), "exclusion_id"),
        (
            _unchecked(
                exclusion,
                exclusion_id=too_early_id,
                excluded_at=too_early,
            ),
            "before it was durably selected",
        ),
    ]
    for attacked, message in invalid_exclusions:
        with pytest.raises(ValueError, match=message):
            attacked._bind_target()

    valid = aggregate_prospective_experiment(
        protocol=target_bundle.protocol,
        protocol_receipt=target_bundle.protocol_receipt,
        target_bundles=(target_bundle,),
        created_at=CUTOFF + timedelta(hours=3),
        observation_complete=False,
    )
    contribution = valid.contributions[0]
    with pytest.raises(ValueError, match="contribution_id"):
        _unchecked(contribution, contribution_id="contribution-wrong")._identity_is_bound()
    with pytest.raises(ValueError, match="equal unit weight"):
        _unchecked(contribution, weight=Decimal(2))._identity_is_bound()

    invalid_report = deepcopy(valid.report.to_record())
    invalid_report["limitations"] = []
    with pytest.raises(ValueError, match="must state its limitations"):
        ProspectiveExperimentReportV1.from_record(invalid_report)

    wrong_receipt = _unchecked(
        valid.protocol_receipt,
        experiment_id="other-experiment",
    )
    with pytest.raises(ValueError, match="protocol receipt"):
        _unchecked(valid, protocol_receipt=wrong_receipt)._aggregate_is_recomputable()

    wrong_protocol = _unchecked(valid.protocol, experiment_id="other-experiment")
    wrong_target_bundle = _unchecked(target_bundle, protocol=wrong_protocol)
    with pytest.raises(ValueError, match="aggregate protocol"):
        _unchecked(valid, target_bundles=(wrong_target_bundle,))._aggregate_is_recomputable()

    wrong_exclusion = _unchecked(exclusion, experiment_id="other-experiment")
    with pytest.raises(ValueError, match="different experiment"):
        _unchecked(blocked, target_exclusions=(wrong_exclusion,))._aggregate_is_recomputable()

    wrong_contribution = _unchecked(contribution, score=Decimal("0.1"))
    with pytest.raises(ValueError, match="contributions disagree"):
        _unchecked(valid, contributions=(wrong_contribution,))._aggregate_is_recomputable()

    wrong_report = _unchecked(valid.report, selected_target_count=99)
    with pytest.raises(ValueError, match="report disagrees"):
        _unchecked(valid, report=wrong_report)._aggregate_is_recomputable()

    with pytest.raises(ValueError, match="evidence_digest"):
        _unchecked(valid, evidence_digest="0" * 64)._aggregate_is_recomputable()


async def test_aggregation_verdict_branches_are_independent(tmp_path: Path) -> None:
    target_bundle = (await _valid_result(tmp_path)).bundle
    one_target = aggregate_prospective_experiment(
        protocol=target_bundle.protocol,
        protocol_receipt=target_bundle.protocol_receipt,
        target_bundles=(target_bundle,),
        created_at=CUTOFF + timedelta(hours=3),
        observation_complete=False,
    )

    passed = aggregation_module._experiment_report(
        protocol=target_bundle.protocol,
        bundles=(target_bundle, target_bundle),
        exclusions=(),
        contributions=one_target.contributions * 2,
        created_at=CUTOFF + timedelta(hours=3),
        observation_complete=True,
    )
    assert passed.measurement_layer_verdict is MeasurementLayerVerdict.PASSED
    assert passed.calibration_verdict is CalibrationVerdict.NOT_ESTABLISHED

    scientific_protocol = _unchecked(
        target_bundle.protocol,
        minimum_intended_resolved_target_count=1,
        structural_minimum_independent_resolved_targets=1,
        scientific_minimum_resolved_target_count=1,
        minimum_category_count_for_calibration=1,
        minimum_yes_outcomes_for_calibration=0,
        minimum_no_outcomes_for_calibration=0,
    )
    scientific = aggregation_module._experiment_report(
        protocol=scientific_protocol,
        bundles=(target_bundle,),
        exclusions=(),
        contributions=one_target.contributions,
        created_at=CUTOFF + timedelta(hours=3),
        observation_complete=True,
    )
    assert scientific.uncertainty["reason"] == "baseline_scores_are_not_calibrated_probabilities"

    with pytest.raises(ValueError, match="exceeds its frozen intended target count"):
        aggregation_module._experiment_report(
            protocol=target_bundle.protocol,
            bundles=(target_bundle, target_bundle, target_bundle),
            exclusions=(),
            contributions=one_target.contributions,
            created_at=CUTOFF + timedelta(hours=3),
            observation_complete=False,
        )
