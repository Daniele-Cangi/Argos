"""Adversarial tests for the prospective M4 claim boundary."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_evaluation_trust import CONDITION, CUTOFF, TOKEN, _capture, _contract, _resolution
from test_prospective_evidence import _protocol
from test_replay import CAPTURE_START

from argos.baselines import BaselineMethod
from argos.clock import ReplayClock
from argos.config import Settings
from argos.config.manifest import RunManifest, RunMode, WorkingTreeStatus, build_run_manifest
from argos.domain.market import MarketDefinitionV1
from argos.evaluation.bundle import EvaluationExclusionV1, ExclusionReason, record_sha256
from argos.evaluation.prospective import (
    CutoffBasis,
    EvidenceArtifactKind,
    EvidencePersistenceReceiptV1,
    LifecycleObservationV1,
    ProspectiveExperimentProtocolV2,
    ProspectiveTargetV1,
    ResolutionCutoffEvidenceV1,
    build_cutoff_evidence_id,
    build_lifecycle_observation_id,
    build_target_id,
    persist_evidence_record,
)
from argos.evaluation.prospective_bundle import EvaluationRunBundleV3
from argos.evaluation.prospective_bundle_v4 import (
    EvaluationRunBundleV4,
    bundle_evidence_digest_v4,
)
from argos.evaluation.run_v3 import ProspectiveEvaluationResult, evaluate_prospective_capture
from argos.resolution import ResolutionStatus, ResolutionV1

EXPERIMENT_ID = "m4-prospective-bundle-test"
DECLARED = CAPTURE_START - timedelta(days=6)
OBSERVATION_START = DECLARED + timedelta(days=1)
MARKET_PERSISTED = DECLARED + timedelta(minutes=10)
CONTRACT_COMPILED = DECLARED + timedelta(minutes=20)
CONTRACT_PERSISTED = CONTRACT_COMPILED + timedelta(minutes=1)
TARGET_SELECTED = DECLARED + timedelta(minutes=30)
TARGET_PERSISTED = TARGET_SELECTED + timedelta(minutes=1)
PROPOSAL_RETRIEVED = CUTOFF - timedelta(hours=1)
FINAL_RETRIEVED = CUTOFF
FINAL_ENDPOINT = "https://clob.polymarket.com/markets/test-resolution"


def _persist(
    directory: Path,
    record: Any,
    kind: EvidenceArtifactKind,
    artifact_id: str,
    at: datetime,
) -> EvidencePersistenceReceiptV1:
    return persist_evidence_record(
        directory,
        record=record,
        experiment_id=EXPERIMENT_ID,
        artifact_kind=kind,
        artifact_id=artifact_id,
        persisted_at=at,
    )


def _observation(
    *,
    target_id: str,
    ordinal: int,
    previous: str | None,
    retrieved_at: datetime,
    raw_digest: str,
    finality: ResolutionStatus,
    resolution: ResolutionV1 | None = None,
) -> LifecycleObservationV1:
    resolution_id = resolution.resolution_id if resolution else None
    resolution_digest = record_sha256(resolution.to_record()) if resolution else None
    raw_location = f"clob/{raw_digest[:2]}/{raw_digest}.raw.json"
    fields: dict[str, Any] = {
        "experiment_id": EXPERIMENT_ID,
        "target_id": target_id,
        "ordinal": ordinal,
        "previous_observation_id": previous,
        "source": "clob",
        "endpoint": FINAL_ENDPOINT,
        "source_time": None,
        "retrieved_at": retrieved_at,
        "raw_payload_sha256": raw_digest,
        "byte_length": 100 + ordinal,
        "raw_payload_location": raw_location,
        "finality": finality,
        "resolution_id": resolution_id,
        "resolution_record_sha256": resolution_digest,
    }
    fields["lifecycle_observation_id"] = build_lifecycle_observation_id(
        experiment_id=EXPERIMENT_ID,
        target_id=target_id,
        ordinal=ordinal,
        previous_observation_id=previous,
        source="clob",
        endpoint=FINAL_ENDPOINT,
        source_time=None,
        retrieved_at=retrieved_at,
        raw_payload_sha256=raw_digest,
        byte_length=100 + ordinal,
        raw_payload_location=raw_location,
        finality=finality,
        resolution_id=resolution_id,
        resolution_record_sha256=resolution_digest,
    )
    return LifecycleObservationV1.model_validate(fields)


async def _valid_result(
    tmp_path: Path,
    *,
    protocol_v2: bool = False,
) -> ProspectiveEvaluationResult:
    settings = Settings()
    base_protocol = _protocol(
        experiment_id=EXPERIMENT_ID,
        declared_at=DECLARED,
        observation_window_start=OBSERVATION_START,
        observation_window_end=(
            CUTOFF - timedelta(hours=2) if protocol_v2 else CUTOFF + timedelta(days=1)
        ),
        code_revision="a" * 40,
        config_fingerprint=settings.fingerprint(),
    )
    if protocol_v2:
        protocol = ProspectiveExperimentProtocolV2.model_validate(
            {
                **base_protocol.model_dump(mode="python"),
                "lifecycle_deadline": CUTOFF + timedelta(hours=1),
                "lifecycle_poll_interval_seconds": 300,
                "capture_max_seconds_per_target": 120,
                "capture_max_frames_per_target": 500,
                "capture_separate_database_per_target": True,
                "capture_subscribe_both_tokens": True,
                "capture_raw_archive": True,
            }
        )
    else:
        protocol = base_protocol
    protocol_receipt = _persist(
        tmp_path,
        protocol,
        EvidenceArtifactKind.EXPERIMENT_PROTOCOL,
        protocol.experiment_id,
        DECLARED,
    )

    original_resolution = _resolution()
    assert original_resolution.winning_token_id is not None
    market = MarketDefinitionV1(
        market_id=CONDITION,
        condition_id=CONDITION,
        slug="prospective-test-market",
        question="Will the test condition resolve Yes?",
        description="A pinned integration-test contract.",
        resolution_source="Pinned public CLOB resolution fixture.",
        start_time=CAPTURE_START - timedelta(days=1),
        end_time=CUTOFF,
        active=True,
        closed=False,
        archived=False,
        restricted=False,
        category="sports",
        liquidity=None,
        volume=None,
        open_interest=None,
        outcomes=("Yes", "No"),
        outcome_token_map={"Yes": TOKEN, "No": original_resolution.winning_token_id},
        neg_risk=False,
        tick_size=None,
        source_updated_time=None,
        raw_payload_sha256="b" * 64,
        normalized_at=MARKET_PERSISTED,
        normalizer_version="prospective-test/1",
    )
    market_receipt = _persist(
        tmp_path,
        market,
        EvidenceArtifactKind.MARKET_DEFINITION,
        market.market_id,
        MARKET_PERSISTED,
    )
    contract = _contract().model_copy(update={"compiled_at": CONTRACT_COMPILED})
    contract_receipt = _persist(
        tmp_path,
        contract,
        EvidenceArtifactKind.COMPILED_CONTRACT,
        contract.contract_id,
        CONTRACT_PERSISTED,
    )
    target_id = build_target_id(
        experiment_id=EXPERIMENT_ID,
        market_id=market.market_id,
        condition_id=market.condition_id,
        yes_token_id=TOKEN,
        no_token_id=original_resolution.winning_token_id,
    )
    target = ProspectiveTargetV1(
        target_id=target_id,
        experiment_id=EXPERIMENT_ID,
        selection_rank=1,
        selected_at=TARGET_SELECTED,
        market_id=market.market_id,
        condition_id=market.condition_id,
        yes_token_id=TOKEN,
        no_token_id=original_resolution.winning_token_id,
        category=market.category,
        cutoff_basis=CutoffBasis.FIRST_OBSERVED_FINAL_SETTLEMENT,
        market_record_sha256=record_sha256(market.to_record()),
        market_receipt_id=market_receipt.receipt_id,
        contract_id=contract.contract_id,
        contract_record_sha256=record_sha256(contract.to_record()),
        contract_receipt_id=contract_receipt.receipt_id,
    )
    target_receipt = _persist(
        tmp_path,
        target,
        EvidenceArtifactKind.TARGET_DECLARATION,
        target.target_id,
        TARGET_PERSISTED,
    )

    # The generic V1 timestamp deliberately predates capture. V3 must score from
    # the independent first-observed-final cutoff, not from this legacy field.
    resolution = original_resolution.model_copy(
        update={
            "resolved_at": DECLARED,
            "normalized_at": FINAL_RETRIEVED + timedelta(minutes=1),
        }
    )
    proposed = _observation(
        target_id=target.target_id,
        ordinal=1,
        previous=None,
        retrieved_at=PROPOSAL_RETRIEVED,
        raw_digest="1" * 64,
        finality=ResolutionStatus.PROPOSED,
    )
    final = _observation(
        target_id=target.target_id,
        ordinal=2,
        previous=proposed.lifecycle_observation_id,
        retrieved_at=FINAL_RETRIEVED,
        raw_digest=resolution.source_payload_sha256,
        finality=ResolutionStatus.FINAL,
        resolution=resolution,
    )
    observations = (proposed, final)
    observation_receipts = tuple(
        _persist(
            tmp_path,
            observation,
            EvidenceArtifactKind.LIFECYCLE_OBSERVATION,
            observation.lifecycle_observation_id,
            observation.retrieved_at + timedelta(seconds=1),
        )
        for observation in observations
    )
    cutoff_fields: dict[str, Any] = {
        "experiment_id": EXPERIMENT_ID,
        "target_id": target.target_id,
        "cutoff_basis": CutoffBasis.FIRST_OBSERVED_FINAL_SETTLEMENT,
        "lifecycle_observation_id": final.lifecycle_observation_id,
        "source": final.source,
        "endpoint": final.endpoint,
        "source_time": final.source_time,
        "retrieved_at": final.retrieved_at,
        "selected_cutoff": final.retrieved_at,
        "raw_payload_sha256": final.raw_payload_sha256,
        "byte_length": final.byte_length,
        "raw_payload_location": final.raw_payload_location,
        "finality": final.finality,
        "resolution_id": resolution.resolution_id,
        "resolution_record_sha256": record_sha256(resolution.to_record()),
    }
    cutoff_fields["cutoff_evidence_id"] = build_cutoff_evidence_id(
        experiment_id=EXPERIMENT_ID,
        target_id=target.target_id,
        cutoff_basis=CutoffBasis.FIRST_OBSERVED_FINAL_SETTLEMENT,
        lifecycle_observation_id=final.lifecycle_observation_id,
        source=final.source,
        endpoint=final.endpoint,
        source_time=final.source_time,
        retrieved_at=final.retrieved_at,
        selected_cutoff=final.retrieved_at,
        raw_payload_sha256=final.raw_payload_sha256,
        byte_length=final.byte_length,
        raw_payload_location=final.raw_payload_location,
        resolution_id=resolution.resolution_id,
        resolution_record_sha256=record_sha256(resolution.to_record()),
    )
    cutoff = ResolutionCutoffEvidenceV1.model_validate(cutoff_fields)
    cutoff_receipt = _persist(
        tmp_path,
        cutoff,
        EvidenceArtifactKind.RESOLUTION_CUTOFF,
        cutoff.cutoff_evidence_id,
        FINAL_RETRIEVED + timedelta(minutes=2),
    )

    store = await _capture("prospective-bundle")
    capture_manifest = build_run_manifest(
        settings=settings,
        clock=ReplayClock(CAPTURE_START + timedelta(hours=2)),
        run_id="prospective-bundle",
        mode=RunMode.CAPTURE,
        code_revision=protocol.code_revision,
        working_tree=WorkingTreeStatus.CLEAN,
        capture_run_id="prospective-bundle",
        run_parameters={
            "subscribed_token_ids": [target.yes_token_id, target.no_token_id],
            "max_seconds": 120,
            "max_frames": 500,
            "raw_archive": True,
        },
    )
    try:
        return evaluate_prospective_capture(
            store=store,
            capture_run_id="prospective-bundle",
            resolution=resolution,
            market=market,
            contract=contract,
            protocol=protocol,
            protocol_receipt=protocol_receipt,
            market_receipt=market_receipt,
            contract_receipt=contract_receipt,
            target=target,
            target_receipt=target_receipt,
            lifecycle_observations=observations,
            lifecycle_receipts=observation_receipts,
            cutoff_evidence=cutoff,
            cutoff_receipt=cutoff_receipt,
            settings=settings,
            clock=ReplayClock(FINAL_RETRIEVED + timedelta(hours=1)),
            evaluation_run_id="prospective-evaluation",
            methods=(BaselineMethod.MIDPOINT,),
            capture_run_manifest=capture_manifest if protocol_v2 else None,
        )
    finally:
        store.close()


def _redigest(record: dict[str, Any]) -> None:
    record["evidence_digest"] = record_sha256(
        {
            "version": "evaluation_bundle_evidence.v3",
            **{
                key: record[key]
                for key in (
                    "evaluation_run_id",
                    "policy",
                    "protocol",
                    "protocol_receipt",
                    "market",
                    "market_receipt",
                    "contract",
                    "contract_receipt",
                    "target",
                    "target_receipt",
                    "lifecycle_observations",
                    "lifecycle_receipts",
                    "cutoff_evidence",
                    "cutoff_receipt",
                    "resolution",
                    "report",
                    "forecasts",
                    "evaluations",
                    "decisions",
                    "exclusions",
                )
            },
        }
    )


def _redigest_v4(record: dict[str, Any]) -> None:
    _redigest(record)
    manifest = RunManifest.from_record(record["capture_run_manifest"])
    record["evidence_digest"] = bundle_evidence_digest_v4(record["evidence_digest"], manifest)


async def test_prospective_bundle_round_trip_uses_proven_cutoff(tmp_path: Path) -> None:
    result = await _valid_result(tmp_path)
    assert result.bundle.resolution.resolved_at < OBSERVATION_START
    assert result.report.resolution_cutoff == FINAL_RETRIEVED
    assert result.report.scored_count > 0
    assert EvaluationRunBundleV3.from_record(result.bundle.to_record()) == result.bundle


async def test_v4_round_trip_binds_deadline_and_capture_manifest(tmp_path: Path) -> None:
    result = await _valid_result(tmp_path, protocol_v2=True)
    assert isinstance(result.bundle, EvaluationRunBundleV4)
    assert result.bundle.capture_run_manifest.capture_run_id == "prospective-bundle"
    assert EvaluationRunBundleV4.from_record(result.bundle.to_record()) == result.bundle


async def test_global_redigest_cannot_move_cutoff_after_v2_deadline(
    tmp_path: Path,
) -> None:
    record = deepcopy(
        (await _valid_result(tmp_path / "source", protocol_v2=True)).bundle.to_record()
    )
    protocol_record = deepcopy(record["protocol"])
    protocol_record["lifecycle_deadline"] = (CUTOFF - timedelta(minutes=1)).isoformat()
    attacked_protocol = ProspectiveExperimentProtocolV2.from_record(protocol_record)
    receipt = _persist(
        tmp_path / "attacked-protocol",
        attacked_protocol,
        EvidenceArtifactKind.EXPERIMENT_PROTOCOL,
        attacked_protocol.experiment_id,
        DECLARED,
    )
    record["protocol"] = attacked_protocol.to_record()
    record["protocol_receipt"] = receipt.to_record()
    record["report"]["protocol_record_sha256"] = receipt.artifact_sha256
    record["report"]["protocol_receipt_id"] = receipt.receipt_id
    _redigest_v4(record)

    with pytest.raises(ValueError, match="after the frozen lifecycle deadline"):
        EvaluationRunBundleV4.from_record(record)


async def test_global_redigest_cannot_expand_v2_capture_limits(tmp_path: Path) -> None:
    record = deepcopy((await _valid_result(tmp_path, protocol_v2=True)).bundle.to_record())
    record["capture_run_manifest"]["run_parameters"]["max_seconds"] = 121
    _redigest_v4(record)

    with pytest.raises(ValueError, match="capture duration disagrees"):
        EvaluationRunBundleV4.from_record(record)


@pytest.mark.parametrize(
    ("field_name", "false_value"),
    [
        ("evaluation_policy_version", "evaluation_policy.v1"),
        ("resolution_id", "resolution-false"),
        ("resolution_record_sha256", "0" * 64),
        ("contract_id", "contract-false"),
        ("contract_record_sha256", "0" * 64),
        ("code_revision", "f" * 40),
        ("config_fingerprint", "f" * 64),
    ],
)
async def test_digest_valid_false_report_links_are_refused(
    tmp_path: Path, field_name: str, false_value: str
) -> None:
    record = deepcopy((await _valid_result(tmp_path)).bundle.to_record())
    record["report"][field_name] = false_value
    _redigest(record)
    with pytest.raises(ValueError, match=rf"report\.{field_name} disagrees"):
        EvaluationRunBundleV3.from_record(record)


async def test_digest_valid_false_counts_and_child_digests_are_refused(
    tmp_path: Path,
) -> None:
    source = (await _valid_result(tmp_path)).bundle.to_record()
    count_record = deepcopy(source)
    count_record["report"]["scored_count"] += 1
    _redigest(count_record)
    with pytest.raises(ValueError, match=r"report\.scored_count disagrees"):
        EvaluationRunBundleV3.from_record(count_record)

    digest_record = deepcopy(source)
    digest_record["report"]["child_record_digests"]["lifecycle_observations"] = "0" * 64
    _redigest(digest_record)
    with pytest.raises(ValueError, match="child_record_digests disagrees"):
        EvaluationRunBundleV3.from_record(digest_record)


async def test_digest_valid_false_child_partition_and_links_are_refused(
    tmp_path: Path,
) -> None:
    source = (await _valid_result(tmp_path)).bundle.to_record()
    wrong_resolution = deepcopy(source)
    wrong_resolution["evaluations"][0]["resolution_id"] = "resolution-false"
    _redigest(wrong_resolution)
    with pytest.raises(ValueError, match="evaluation disagrees"):
        EvaluationRunBundleV3.from_record(wrong_resolution)

    both = deepcopy(source)
    both["exclusions"].append(
        EvaluationExclusionV1(
            forecast_id=both["evaluations"][0]["forecast_id"],
            reason=ExclusionReason.ABSTAINED,
            detail="digest-valid contradictory partition",
        ).to_record()
    )
    _redigest(both)
    with pytest.raises(ValueError, match="exclusive scored-or-excluded partition"):
        EvaluationRunBundleV3.from_record(both)

    duplicate = deepcopy(source)
    duplicate["evaluations"].append(deepcopy(duplicate["evaluations"][0]))
    _redigest(duplicate)
    with pytest.raises(ValueError, match="evaluated at most once"):
        EvaluationRunBundleV3.from_record(duplicate)
