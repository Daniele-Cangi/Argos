"""Adversarial tests for proof-backed prospective exclusions and aggregate V2."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import orjson
import pytest
from test_prospective_bundle import _valid_result

from argos.clock import ReplayClock
from argos.config import Settings
from argos.config.manifest import RunMode, WorkingTreeStatus, build_run_manifest
from argos.domain.observation import (
    ObservationSource,
    RejectedObservationV1,
    build_rejected_observation,
)
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.errors import RejectionReason
from argos.evaluation import prospective_aggregation_v2 as aggregation_v2
from argos.evaluation.prospective import EvidenceArtifactKind, persist_evidence_record
from argos.evaluation.prospective_aggregation import (
    CalibrationVerdict,
    MeasurementLayerVerdict,
    ProspectiveTargetExclusionReason,
)
from argos.evaluation.prospective_aggregation_v2 import (
    CaptureRejectionEvidenceV1,
    ProspectiveExperimentBundleV2,
    ProspectiveTargetExclusionV2,
    aggregate_prospective_experiment_v2,
    build_capture_rejection_evidence,
    build_target_exclusion_v2,
)
from argos.store.raw_archive import archive_relative_location


def _unchecked(model: Any, **updates: Any) -> Any:
    values = {name: getattr(model, name) for name in type(model).model_fields}
    values.update(updates)
    return type(model).model_construct(**values)


def _raw(condition_id: str, token_id: str) -> bytes:
    return orjson.dumps(
        {
            "market": condition_id,
            "asset_id": token_id,
            "price": "0.54",
            "size": "18.518517",
            "fee_rate_bps": "0",
            "side": "BUY",
            "timestamp": "1787178044130",
            "event_type": "last_trade_price",
            "transaction_hash": "0x" + "a" * 64,
        }
    )


async def _proof_parts(
    tmp_path: Path,
    *,
    condition_id: str | None = None,
    token_id: str | None = None,
    code_revision: str | None = None,
    received_offset: timedelta = timedelta(minutes=1),
) -> tuple[Any, bytes, RejectedObservationV1, Any, CaptureRejectionEvidenceV1, Any]:
    target_bundle = (await _valid_result(tmp_path / "bundle")).bundle
    target = target_bundle.target
    protocol = target_bundle.protocol
    condition = condition_id or target.condition_id
    token = token_id or target.yes_token_id
    raw = _raw(condition, token)
    received = protocol.observation_window_start + received_offset
    provenance = SourceProvenanceV1(
        source=ObservationSource.CLOB_MARKET_WS.value,
        endpoint="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        retrieved_at=received,
        raw_sha256=sha256_hex(raw),
        byte_length=len(raw),
    )
    run_id = f"{protocol.experiment_id}-proof-target"
    rejection = build_rejected_observation(
        reason=RejectionReason.UNKNOWN_EVENT_TYPE,
        detail="no payload model is wired for event_type 'last_trade_price' yet",
        source=ObservationSource.CLOB_MARKET_WS,
        source_event_type="last_trade_price",
        condition_id=condition,
        token_id=token,
        provenance=provenance,
        received_time=received,
        rejected_at=received + timedelta(milliseconds=1),
        capture_run_id=run_id,
    )
    settings = Settings()
    manifest = build_run_manifest(
        settings=settings,
        clock=ReplayClock(received + timedelta(minutes=1)),
        run_id=run_id,
        mode=RunMode.CAPTURE,
        code_revision=code_revision or protocol.code_revision,
        working_tree=WorkingTreeStatus.CLEAN,
        schema_versions=(RejectedObservationV1.schema_version,),
        capture_run_id=run_id,
        run_parameters={
            "subscribed_token_ids": [target.yes_token_id, target.no_token_id],
            "max_seconds": 120,
            "max_frames": 500,
            "raw_archive": True,
        },
    )
    proof = build_capture_rejection_evidence(
        experiment_id=protocol.experiment_id,
        target_id=target.target_id,
        capture_run_manifest=manifest,
        ingest_sequence=7,
        rejection=rejection,
        raw_payload=raw,
        raw_payload_location=archive_relative_location(provenance),
    )
    receipt = persist_evidence_record(
        tmp_path / "evidence",
        record=proof,
        experiment_id=protocol.experiment_id,
        artifact_kind=EvidenceArtifactKind.CAPTURE_REJECTION,
        artifact_id=proof.evidence_id,
        persisted_at=manifest.created_at + timedelta(seconds=1),
    )
    return target_bundle, raw, rejection, manifest, proof, receipt


async def test_proof_backed_exclusion_and_aggregate_round_trip(tmp_path: Path) -> None:
    target_bundle, _, _, _, proof, proof_receipt = await _proof_parts(tmp_path)
    exclusion = build_target_exclusion_v2(
        experiment_id=target_bundle.protocol.experiment_id,
        target=target_bundle.target,
        target_receipt=target_bundle.target_receipt,
        capture_evidence=proof,
        capture_evidence_receipt=proof_receipt,
    )
    aggregate = aggregate_prospective_experiment_v2(
        protocol=target_bundle.protocol,
        protocol_receipt=target_bundle.protocol_receipt,
        target_bundles=(),
        target_exclusions=(exclusion,),
        created_at=proof_receipt.persisted_at + timedelta(seconds=1),
        observation_complete=False,
    )

    assert CaptureRejectionEvidenceV1.from_record(proof.to_record()) == proof
    assert ProspectiveTargetExclusionV2.from_record(exclusion.to_record()) == exclusion
    assert ProspectiveExperimentBundleV2.from_record(aggregate.to_record()) == aggregate
    assert aggregate.report.measurement_layer_verdict is MeasurementLayerVerdict.BLOCKED
    assert aggregate.report.calibration_verdict is CalibrationVerdict.NOT_EVALUABLE
    assert aggregate.report.excluded_target_count == 1
    assert aggregate.contributions == ()


async def test_proof_rejects_internally_false_capture_claims(tmp_path: Path) -> None:
    _, _, rejection, manifest, proof, _ = await _proof_parts(tmp_path)
    wrong_reason = build_rejected_observation(
        reason=RejectionReason.MALFORMED_PAYLOAD,
        detail=rejection.detail,
        source=rejection.source,
        source_event_type=rejection.source_event_type,
        condition_id=rejection.condition_id,
        token_id=rejection.token_id,
        provenance=rejection.provenance,
        received_time=rejection.received_time,
        rejected_at=rejection.rejected_at,
        capture_run_id=rejection.capture_run_id,
    )
    wrong_scope = build_rejected_observation(
        reason=rejection.reason,
        detail=rejection.detail,
        source=rejection.source,
        source_event_type=rejection.source_event_type,
        condition_id="0x" + "f" * 64,
        token_id=rejection.token_id,
        provenance=rejection.provenance,
        received_time=rejection.received_time,
        rejected_at=rejection.rejected_at,
        capture_run_id=rejection.capture_run_id,
    )

    cases = [
        (
            _unchecked(proof, raw_payload_utf8=proof.raw_payload_utf8 + " "),
            "raw payload disagrees",
        ),
        (_unchecked(proof, raw_payload_location="wrong/raw.json"), "location disagrees"),
        (
            _unchecked(
                proof,
                rejection=_unchecked(
                    rejection,
                    received_time=rejection.received_time + timedelta(seconds=1),
                ),
            ),
            "chronology disagrees",
        ),
        (
            _unchecked(proof, capture_run_manifest=_unchecked(manifest, mode=RunMode.REPLAY)),
            "does not name the rejection run",
        ),
        (
            _unchecked(
                proof,
                capture_run_manifest=_unchecked(
                    manifest,
                    working_tree=WorkingTreeStatus.DIRTY,
                ),
            ),
            "clean identified revision",
        ),
        (
            _unchecked(
                proof,
                capture_run_manifest=_unchecked(
                    manifest,
                    created_at=rejection.rejected_at - timedelta(seconds=1),
                ),
            ),
            "predates the rejection",
        ),
        (
            _unchecked(
                proof,
                capture_run_manifest=_unchecked(
                    manifest,
                    run_parameters={
                        **dict(manifest.run_parameters),
                        "raw_archive": False,
                    },
                ),
            ),
            "requires raw archival",
        ),
        (
            _unchecked(
                proof,
                capture_run_manifest=_unchecked(manifest, schema_versions=()),
            ),
            "omits the rejection schema",
        ),
        (
            _unchecked(
                proof,
                capture_run_manifest=_unchecked(
                    manifest,
                    schema_versions=(
                        *manifest.schema_versions,
                        "last_trade_price.v1",
                    ),
                ),
            ),
            "cannot claim the rejected trade was modeled",
        ),
        (
            _unchecked(proof, rejection=wrong_reason),
            "not an unmodeled standalone",
        ),
        (_unchecked(proof, rejection=wrong_scope), "trade scope disagrees"),
        (_unchecked(proof, evidence_id="proof-wrong"), "evidence_id disagrees"),
    ]
    for attacked, message in cases:
        with pytest.raises(ValueError, match=message):
            attacked._proof_is_semantically_bound()


async def test_global_redigest_cannot_move_rejection_to_another_target(
    tmp_path: Path,
) -> None:
    target_bundle = (await _valid_result(tmp_path / "seed")).bundle
    foreign_condition = "0x" + "f" * 64
    _, _, _, _, proof, proof_receipt = await _proof_parts(
        tmp_path / "foreign",
        condition_id=foreign_condition,
    )

    with pytest.raises(ValueError, match="outside the selected target scope"):
        build_target_exclusion_v2(
            experiment_id=target_bundle.protocol.experiment_id,
            target=target_bundle.target,
            target_receipt=target_bundle.target_receipt,
            capture_evidence=proof,
            capture_evidence_receipt=proof_receipt,
        )


async def test_global_redigest_cannot_rewrite_protocol_revision_or_window(
    tmp_path: Path,
) -> None:
    target_bundle, _, _, _, wrong_revision_proof, receipt = await _proof_parts(
        tmp_path / "revision",
        code_revision="f" * 40,
    )
    exclusion = build_target_exclusion_v2(
        experiment_id=target_bundle.protocol.experiment_id,
        target=target_bundle.target,
        target_receipt=target_bundle.target_receipt,
        capture_evidence=wrong_revision_proof,
        capture_evidence_receipt=receipt,
    )
    with pytest.raises(ValueError, match="frozen protocol"):
        aggregate_prospective_experiment_v2(
            protocol=target_bundle.protocol,
            protocol_receipt=target_bundle.protocol_receipt,
            target_bundles=(),
            target_exclusions=(exclusion,),
            created_at=receipt.persisted_at + timedelta(seconds=1),
            observation_complete=False,
        )

    late_bundle, _, _, _, late_proof, late_receipt = await _proof_parts(
        tmp_path / "late",
        received_offset=timedelta(days=365),
    )
    late_exclusion = build_target_exclusion_v2(
        experiment_id=late_bundle.protocol.experiment_id,
        target=late_bundle.target,
        target_receipt=late_bundle.target_receipt,
        capture_evidence=late_proof,
        capture_evidence_receipt=late_receipt,
    )
    with pytest.raises(ValueError, match="outside the frozen observation window"):
        aggregate_prospective_experiment_v2(
            protocol=late_bundle.protocol,
            protocol_receipt=late_bundle.protocol_receipt,
            target_bundles=(),
            target_exclusions=(late_exclusion,),
            created_at=late_receipt.persisted_at + timedelta(seconds=1),
            observation_complete=False,
        )


async def test_proof_rejects_invalid_encoding_json_and_identity(tmp_path: Path) -> None:
    target_bundle, _, rejection, manifest, proof, _ = await _proof_parts(tmp_path)
    with pytest.raises(ValueError, match="not UTF-8"):
        build_capture_rejection_evidence(
            experiment_id=target_bundle.protocol.experiment_id,
            target_id=target_bundle.target.target_id,
            capture_run_manifest=manifest,
            ingest_sequence=7,
            rejection=rejection,
            raw_payload=b"\xff",
            raw_payload_location=proof.raw_payload_location,
        )
    with pytest.raises(ValueError, match="not JSON"):
        aggregation_v2._parse_embedded_trade(b"{")
    with pytest.raises(ValueError, match="not a JSON object"):
        aggregation_v2._parse_embedded_trade(b"[]")
    with pytest.raises(ValueError, match="identity is not recomputable"):
        _unchecked(
            proof,
            rejection=_unchecked(rejection, rejection_id="rejection-wrong"),
        )._proof_is_semantically_bound()


async def test_exclusion_v2_rejects_false_cross_links(tmp_path: Path) -> None:
    target_bundle, raw, rejection, manifest, proof, proof_receipt = await _proof_parts(tmp_path)
    exclusion = build_target_exclusion_v2(
        experiment_id=target_bundle.protocol.experiment_id,
        target=target_bundle.target,
        target_receipt=target_bundle.target_receipt,
        capture_evidence=proof,
        capture_evidence_receipt=proof_receipt,
    )
    cases = [
        (
            _unchecked(
                exclusion,
                target=_unchecked(
                    target_bundle.target,
                    experiment_id="other-experiment",
                ),
            ),
            "different target",
        ),
        (
            _unchecked(
                exclusion,
                capture_evidence=_unchecked(proof, target_id="target-other"),
            ),
            "different exclusion proof",
        ),
        (
            _unchecked(
                exclusion,
                reason=ProspectiveTargetExclusionReason.CAPTURE_INCOMPLETE,
            ),
            "wrong reason",
        ),
        (
            _unchecked(
                exclusion,
                excluded_at=exclusion.excluded_at + timedelta(seconds=1),
            ),
            "first proven rejection time",
        ),
        (
            _unchecked(exclusion, exclusion_id="target-exclusion-wrong"),
            "exclusion_id disagrees",
        ),
    ]
    for attacked, message in cases:
        with pytest.raises(ValueError, match=message):
            attacked._exclusion_is_semantically_bound()

    wrong_manifest = manifest.model_copy(
        update={
            "run_parameters": {
                **dict(manifest.run_parameters),
                "subscribed_token_ids": [target_bundle.target.yes_token_id],
            }
        }
    )
    wrong_subscription_proof = build_capture_rejection_evidence(
        experiment_id=target_bundle.protocol.experiment_id,
        target_id=target_bundle.target.target_id,
        capture_run_manifest=wrong_manifest,
        ingest_sequence=7,
        rejection=rejection,
        raw_payload=raw,
        raw_payload_location=proof.raw_payload_location,
    )
    wrong_subscription_receipt = persist_evidence_record(
        tmp_path / "wrong-subscription-evidence",
        record=wrong_subscription_proof,
        experiment_id=target_bundle.protocol.experiment_id,
        artifact_kind=EvidenceArtifactKind.CAPTURE_REJECTION,
        artifact_id=wrong_subscription_proof.evidence_id,
        persisted_at=wrong_manifest.created_at + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="subscriptions disagree"):
        build_target_exclusion_v2(
            experiment_id=target_bundle.protocol.experiment_id,
            target=target_bundle.target,
            target_receipt=target_bundle.target_receipt,
            capture_evidence=wrong_subscription_proof,
            capture_evidence_receipt=wrong_subscription_receipt,
        )


async def test_aggregate_v2_rejects_false_recomputed_children(tmp_path: Path) -> None:
    target_bundle, _, _, _, proof, proof_receipt = await _proof_parts(tmp_path)
    exclusion = build_target_exclusion_v2(
        experiment_id=target_bundle.protocol.experiment_id,
        target=target_bundle.target,
        target_receipt=target_bundle.target_receipt,
        capture_evidence=proof,
        capture_evidence_receipt=proof_receipt,
    )
    aggregate = aggregate_prospective_experiment_v2(
        protocol=target_bundle.protocol,
        protocol_receipt=target_bundle.protocol_receipt,
        target_bundles=(),
        target_exclusions=(exclusion,),
        created_at=proof_receipt.persisted_at + timedelta(seconds=1),
        observation_complete=False,
    )
    cases = [
        (
            _unchecked(
                aggregate,
                protocol_receipt=_unchecked(
                    aggregate.protocol_receipt,
                    experiment_id="other-experiment",
                ),
            ),
            "protocol receipt names different evidence",
        ),
        (
            _unchecked(aggregate, target_bundles=(target_bundle,)),
            "only once",
        ),
        (
            _unchecked(
                aggregate,
                target_exclusions=(_unchecked(exclusion, experiment_id="other-experiment"),),
            ),
            "different experiment",
        ),
        (
            _unchecked(aggregate, contributions=(object(),)),
            "contributions disagree",
        ),
        (
            _unchecked(
                aggregate,
                report=_unchecked(
                    aggregate.report,
                    excluded_target_count=2,
                ),
            ),
            "report disagrees",
        ),
        (
            _unchecked(aggregate, evidence_digest="0" * 64),
            "evidence_digest disagrees",
        ),
    ]
    for attacked, message in cases:
        with pytest.raises(ValueError, match=message):
            attacked._aggregate_is_recomputable()
