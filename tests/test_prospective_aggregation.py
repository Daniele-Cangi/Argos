from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_evaluation_trust import CUTOFF
from test_prospective_bundle import _valid_result

from argos.evaluation.bundle import record_sha256
from argos.evaluation.prospective_aggregation import (
    CalibrationVerdict,
    MeasurementLayerVerdict,
    ProspectiveExperimentBundleV1,
    aggregate_prospective_experiment,
)


def _redigest(record: dict[str, Any]) -> None:
    record["evidence_digest"] = record_sha256(
        {
            "version": "prospective_experiment_evidence.v1",
            "protocol": record["protocol"],
            "protocol_receipt": record["protocol_receipt"],
            "target_bundles": record["target_bundles"],
            "target_exclusions": record["target_exclusions"],
            "contributions": record["contributions"],
            "report": record["report"],
        }
    )


async def test_one_target_is_partial_and_never_reweights_its_trajectory(
    tmp_path: Path,
) -> None:
    target = (await _valid_result(tmp_path)).bundle
    aggregate = aggregate_prospective_experiment(
        protocol=target.protocol,
        protocol_receipt=target.protocol_receipt,
        target_bundles=(target,),
        created_at=CUTOFF + timedelta(hours=2),
        observation_complete=False,
    )

    assert aggregate.report.measurement_layer_verdict is MeasurementLayerVerdict.PARTIALLY_VALIDATED
    assert aggregate.report.calibration_verdict is CalibrationVerdict.NOT_ESTABLISHED
    assert aggregate.report.resolved_target_count == 1
    assert aggregate.report.contribution_count == 1
    assert aggregate.contributions[0].weight == 1
    assert (
        aggregate.contributions[0].forecast_id
        == max(
            target.evaluations,
            key=lambda item: next(
                forecast.as_of_received_time
                for forecast in target.forecasts
                if forecast.forecast_id == item.forecast_id
            ),
        ).forecast_id
    )
    assert ProspectiveExperimentBundleV1.from_record(aggregate.to_record()) == aggregate


async def test_completed_experiment_must_account_for_frozen_target_count(
    tmp_path: Path,
) -> None:
    target = (await _valid_result(tmp_path)).bundle
    with pytest.raises(ValueError, match="account for every intended target"):
        aggregate_prospective_experiment(
            protocol=target.protocol,
            protocol_receipt=target.protocol_receipt,
            target_bundles=(target,),
            created_at=CUTOFF + timedelta(hours=2),
            observation_complete=True,
        )


async def test_digest_valid_false_aggregate_verdict_is_refused(tmp_path: Path) -> None:
    target = (await _valid_result(tmp_path)).bundle
    aggregate = aggregate_prospective_experiment(
        protocol=target.protocol,
        protocol_receipt=target.protocol_receipt,
        target_bundles=(target,),
        created_at=CUTOFF + timedelta(hours=2),
        observation_complete=False,
    )
    record = deepcopy(aggregate.to_record())
    record["report"]["measurement_layer_verdict"] = MeasurementLayerVerdict.PASSED.value
    _redigest(record)

    with pytest.raises(ValueError, match="report disagrees"):
        ProspectiveExperimentBundleV1.from_record(record)


async def test_duplicate_target_cannot_supply_two_independent_units(tmp_path: Path) -> None:
    target = (await _valid_result(tmp_path)).bundle
    with pytest.raises(ValueError, match="only once"):
        aggregate_prospective_experiment(
            protocol=target.protocol,
            protocol_receipt=target.protocol_receipt,
            target_bundles=(target, target),
            created_at=CUTOFF + timedelta(hours=2),
            observation_complete=True,
        )
