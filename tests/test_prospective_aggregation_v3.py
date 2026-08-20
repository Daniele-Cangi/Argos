"""Adversarial tests for evidence-bound prospective aggregate V3."""

from __future__ import annotations

from datetime import timedelta

import pytest
from test_prospective_aggregation_v2 import _proof_parts

from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    ProspectiveExperimentProtocolV1,
    ProspectiveExperimentProtocolV2,
    persist_evidence_record,
)
from argos.evaluation.prospective_aggregation_v2 import build_target_exclusion_v2
from argos.evaluation.prospective_aggregation_v3 import (
    ProspectiveExperimentBundleV3,
    aggregate_prospective_experiment_v3,
)


def _protocol_v2(
    protocol: ProspectiveExperimentProtocolV1,
    *,
    max_seconds: int = 120,
) -> ProspectiveExperimentProtocolV2:
    return ProspectiveExperimentProtocolV2.model_validate(
        {
            **protocol.model_dump(mode="python"),
            "lifecycle_deadline": protocol.observation_window_end + timedelta(days=1),
            "lifecycle_poll_interval_seconds": 300,
            "capture_max_seconds_per_target": max_seconds,
            "capture_max_frames_per_target": 500,
            "capture_separate_database_per_target": True,
            "capture_subscribe_both_tokens": True,
            "capture_raw_archive": True,
        }
    )


async def _aggregate_parts(tmp_path, *, max_seconds: int = 120):
    target_bundle, _, _, _, proof, proof_receipt = await _proof_parts(tmp_path / "proof")
    exclusion = build_target_exclusion_v2(
        experiment_id=target_bundle.protocol.experiment_id,
        target=target_bundle.target,
        target_receipt=target_bundle.target_receipt,
        capture_evidence=proof,
        capture_evidence_receipt=proof_receipt,
    )
    protocol = _protocol_v2(target_bundle.protocol, max_seconds=max_seconds)
    protocol_receipt = persist_evidence_record(
        tmp_path / "protocol-evidence",
        record=protocol,
        experiment_id=protocol.experiment_id,
        artifact_kind=EvidenceArtifactKind.EXPERIMENT_PROTOCOL,
        artifact_id=protocol.experiment_id,
        persisted_at=protocol.declared_at,
    )
    return protocol, protocol_receipt, exclusion, proof_receipt


async def test_aggregate_v3_round_trip_binds_operational_protocol(tmp_path) -> None:
    protocol, receipt, exclusion, proof_receipt = await _aggregate_parts(tmp_path)
    aggregate = aggregate_prospective_experiment_v3(
        protocol=protocol,
        protocol_receipt=receipt,
        target_bundles=(),
        target_exclusions=(exclusion,),
        created_at=proof_receipt.persisted_at + timedelta(seconds=1),
        observation_complete=False,
    )

    assert ProspectiveExperimentBundleV3.from_record(aggregate.to_record()) == aggregate


async def test_global_redigest_cannot_expand_excluded_capture_bound(tmp_path) -> None:
    protocol, receipt, exclusion, proof_receipt = await _aggregate_parts(
        tmp_path,
        max_seconds=121,
    )

    with pytest.raises(ValueError, match="capture duration disagrees"):
        aggregate_prospective_experiment_v3(
            protocol=protocol,
            protocol_receipt=receipt,
            target_bundles=(),
            target_exclusions=(exclusion,),
            created_at=proof_receipt.persisted_at + timedelta(seconds=1),
            observation_complete=False,
        )
