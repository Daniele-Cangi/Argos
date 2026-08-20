"""Branch-complete adversarial guards for prospective aggregate V3."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from test_prospective_aggregation_v2 import _unchecked
from test_prospective_aggregation_v3 import _aggregate_parts
from test_prospective_bundle import _valid_result

from argos.evaluation.prospective_aggregation_v3 import (
    ProspectiveExperimentBundleV3,
    aggregate_prospective_experiment_v3,
)


async def _excluded_aggregate(tmp_path) -> ProspectiveExperimentBundleV3:
    protocol, receipt, exclusion, proof_receipt = await _aggregate_parts(tmp_path)
    return aggregate_prospective_experiment_v3(
        protocol=protocol,
        protocol_receipt=receipt,
        target_bundles=(),
        target_exclusions=(exclusion,),
        created_at=proof_receipt.persisted_at + timedelta(seconds=1),
        observation_complete=False,
    )


def _with_manifest(aggregate, **updates: Any):
    exclusion = aggregate.target_exclusions[0]
    manifest = exclusion.capture_evidence.capture_run_manifest
    manifest_updates = dict(updates)
    parameter_updates = manifest_updates.pop("run_parameters", {})
    attacked_manifest = _unchecked(
        manifest,
        **manifest_updates,
        run_parameters={**dict(manifest.run_parameters), **parameter_updates},
    )
    attacked_proof = _unchecked(
        exclusion.capture_evidence,
        capture_run_manifest=attacked_manifest,
    )
    attacked_exclusion = _unchecked(exclusion, capture_evidence=attacked_proof)
    return _unchecked(aggregate, target_exclusions=(attacked_exclusion,))


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"capture_run_id": None}, "omits its capture run id"),
        ({"run_parameters": {"max_seconds": "not-a-number"}}, "not a numeric frozen bound"),
        ({"run_parameters": {"max_frames": 501}}, "frame bound disagrees"),
        ({"run_parameters": {"raw_archive": False}}, "raw-archive policy disagrees"),
    ],
)
async def test_v3_exclusion_manifest_guards(
    tmp_path,
    updates: dict[str, Any],
    message: str,
) -> None:
    aggregate = _with_manifest(await _excluded_aggregate(tmp_path), **updates)

    with pytest.raises(ValueError, match=message):
        aggregate._capture_bounds_match_protocol()


async def test_v3_rejects_duplicate_exclusion_capture_runs(tmp_path) -> None:
    aggregate = await _excluded_aggregate(tmp_path)
    exclusion = aggregate.target_exclusions[0]
    attacked = _unchecked(aggregate, target_exclusions=(exclusion, exclusion))

    with pytest.raises(ValueError, match="cannot share one capture run"):
        attacked._capture_bounds_match_protocol()


async def test_v3_validates_included_bundle_capture_run_uniqueness(tmp_path) -> None:
    bundle = (await _valid_result(tmp_path / "bundle", protocol_v2=True)).bundle
    aggregate = aggregate_prospective_experiment_v3(
        protocol=bundle.protocol,
        protocol_receipt=bundle.protocol_receipt,
        target_bundles=(bundle,),
        target_exclusions=(),
        created_at=bundle.report.created_at + timedelta(seconds=1),
        observation_complete=False,
    )
    assert aggregate.target_bundles == (bundle,)

    missing_manifest = _unchecked(bundle.capture_run_manifest, capture_run_id=None)
    missing_bundle = _unchecked(bundle, capture_run_manifest=missing_manifest)
    attacked = _unchecked(aggregate, target_bundles=(missing_bundle,))
    with pytest.raises(ValueError, match="omits its capture run id"):
        attacked._capture_bounds_match_protocol()

    attacked = _unchecked(aggregate, target_bundles=(bundle, bundle))
    with pytest.raises(ValueError, match="cannot share one capture run"):
        attacked._capture_bounds_match_protocol()
