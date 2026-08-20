"""Adversarial properties the pre-owner M4 tests did not exercise."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_replay import CAPTURE_START, TOKEN, _ListFrameSource, _recorded_frames

from argos.baselines import BaselineMethod, MarketBaselineForecastV2
from argos.clock import ReplayClock
from argos.compiler import CompiledMarketContractV1
from argos.config import Settings
from argos.evaluation import (
    EvaluationReportV2,
    EvaluationResult,
    EvaluationRunBundleV1,
    EvaluationRunBundleV2,
    ForecastEvaluationV2,
    evaluate_capture,
)
from argos.evaluation.bundle import (
    DecisionReason,
    EvaluationDecisionV1,
    EvaluationExclusionV1,
    EvaluationPolicyV1,
    EvaluationPolicyV2,
    ExclusionReason,
    bundle_evidence_digest,
    bundle_evidence_digest_v2,
)
from argos.ingestion.capture import run_capture
from argos.resolution import ResolutionV1, normalize_clob_resolution
from argos.store.event_store import EventStore, open_sqlite_event_store

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
CUTOFF = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
CONDITION = "0x94a39addec8bd24a2d03deeaa43bdee6a2b11eca403cd12495eb24132cc23173"
RESOLUTION_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "clob" / "market_resolved.raw.json"
)


def _resolution() -> ResolutionV1:
    raw = RESOLUTION_FIXTURE.read_bytes()
    result = normalize_clob_resolution(
        json.loads(raw),
        source_payload_sha256=hashlib.sha256(raw).hexdigest(),
        normalized_at=NOW,
        yes_token_id=TOKEN,
    )
    assert isinstance(result, ResolutionV1)
    return result.model_copy(update={"resolved_at": CUTOFF})


def _contract() -> CompiledMarketContractV1:
    return CompiledMarketContractV1(
        contract_id="contract-" + "a" * 32,
        market_id=CONDITION,
        condition_id=CONDITION,
        source_market_hash="b" * 64,
        compiler_version="test-compiler/1",
        compiled_at=NOW,
        proposition="Recorded tennis market",
    )


async def _capture(
    run_id: str, *, duplicate_final_frame: bool = False, include_sibling: bool = False
) -> EventStore:
    frames = _recorded_frames()
    if duplicate_final_frame:
        frames.append(frames[-1])
    subscribed = [TOKEN]
    if include_sibling:
        subscribed.append(_sibling_token(frames))
    store = open_sqlite_event_store(":memory:")
    await run_capture(
        frame_source=_ListFrameSource(frames),
        store=store,
        clock=ReplayClock(CAPTURE_START),
        capture_run_id=run_id,
        subscribed_token_ids=subscribed,
    )
    return store


def _sibling_token(frames: list[object]) -> str:
    for frame in frames:
        decoded = json.loads(frame.text)  # type: ignore[attr-defined]
        events = decoded if isinstance(decoded, list) else [decoded]
        for event in events:
            if not isinstance(event, dict):
                continue
            for entry in event.get("price_changes", []):
                if entry["asset_id"] != TOKEN:
                    return str(entry["asset_id"])
    raise AssertionError("recorded fixture carries no sibling token")


def _series(result: EvaluationResult) -> list[tuple[str, str | None, bool]]:
    return [
        (
            item.method.value,
            str(item.raw_score) if item.raw_score is not None else None,
            item.abstained,
        )
        for item in result.forecasts
    ]


def _redigest_bundle_record(record: dict[str, Any]) -> None:
    """Make outer bytes valid after an adversarial sibling-record mutation."""
    policy = EvaluationPolicyV2.from_record(dict(record["policy"]))
    resolution = ResolutionV1.from_record(dict(record["resolution"]))
    contract_record = record["contract"]
    contract = (
        CompiledMarketContractV1.from_record(dict(contract_record))
        if contract_record is not None
        else None
    )
    report = EvaluationReportV2.from_record(dict(record["report"]))
    forecasts = tuple(
        MarketBaselineForecastV2.from_record(dict(item)) for item in record["forecasts"]
    )
    evaluations = tuple(
        ForecastEvaluationV2.from_record(dict(item)) for item in record["evaluations"]
    )
    decisions = tuple(EvaluationDecisionV1.from_record(dict(item)) for item in record["decisions"])
    exclusions = tuple(
        EvaluationExclusionV1.from_record(dict(item)) for item in record["exclusions"]
    )
    record["evidence_digest"] = bundle_evidence_digest_v2(
        evaluation_run_id=str(record["evaluation_run_id"]),
        policy=policy,
        resolution=resolution,
        contract=contract,
        report=report,
        forecasts=forecasts,
        evaluations=evaluations,
        decisions=decisions,
        exclusions=exclusions,
    )


async def test_duplicate_and_sibling_traffic_cannot_reweight_the_target() -> None:
    clean_store = await _capture("clean")
    duplicate_store = await _capture("duplicate", duplicate_final_frame=True)
    sibling_store = await _capture("sibling", include_sibling=True)
    try:

        def run(store: EventStore, run_id: str) -> EvaluationResult:
            return evaluate_capture(
                store=store,
                capture_run_id=run_id,
                resolution=_resolution(),
                token_id=TOKEN,
                settings=Settings(),
                clock=ReplayClock(NOW),
                evaluation_run_id=f"evaluation-{run_id}",
                contract=_contract(),
                methods=(BaselineMethod.MIDPOINT, BaselineMethod.PERSISTENCE),
            )

        clean = run(clean_store, "clean")
        duplicate = run(duplicate_store, "duplicate")
        sibling = run(sibling_store, "sibling")

        assert (
            clean.report.target_information_state_count
            == duplicate.report.target_information_state_count
        )
        assert (
            clean.report.target_information_state_count
            == sibling.report.target_information_state_count
        )
        assert _series(clean) == _series(duplicate) == _series(sibling)
        assert (
            clean.report.scored_count
            == duplicate.report.scored_count
            == sibling.report.scored_count
        )
        assert any(item.reason is DecisionReason.DUPLICATE_DELIVERY for item in duplicate.decisions)
        assert any(item.reason is DecisionReason.IRRELEVANT_SCOPE for item in sibling.decisions)
        assert clean.report.source_trajectory_hash != duplicate.report.source_trajectory_hash
        assert clean.report.source_trajectory_hash != sibling.report.source_trajectory_hash
    finally:
        clean_store.close()
        duplicate_store.close()
        sibling_store.close()


async def test_bundle_round_trip_verifies_every_child_digest() -> None:
    store = await _capture("bundle")
    try:
        result = evaluate_capture(
            store=store,
            capture_run_id="bundle",
            resolution=_resolution(),
            token_id=TOKEN,
            settings=Settings(),
            clock=ReplayClock(NOW),
            evaluation_run_id="evaluation-bundle",
            contract=_contract(),
            methods=(BaselineMethod.MIDPOINT,),
        )
        record = result.bundle.to_record()
        assert result.evaluations == result.bundle.evaluations
        assert EvaluationRunBundleV2.from_record(record) == result.bundle
        record["evidence_digest"] = "0" * 64
        with pytest.raises(ValueError, match="evidence_digest"):
            EvaluationRunBundleV2.from_record(record)

        legacy_policy = EvaluationPolicyV1()
        legacy_report = result.report.model_copy(
            update={"evaluation_policy_version": legacy_policy.schema_version}
        )
        legacy = EvaluationRunBundleV1(
            evaluation_run_id=result.bundle.evaluation_run_id,
            policy=legacy_policy,
            resolution=result.bundle.resolution,
            contract=result.bundle.contract,
            report=legacy_report,
            forecasts=result.forecasts,
            evaluations=result.evaluations,
            decisions=result.decisions,
            exclusions=result.exclusions,
            evidence_digest=bundle_evidence_digest(
                evaluation_run_id=result.bundle.evaluation_run_id,
                policy=legacy_policy,
                resolution=result.bundle.resolution,
                contract=result.bundle.contract,
                report=legacy_report,
                forecasts=result.forecasts,
                evaluations=result.evaluations,
                decisions=result.decisions,
                exclusions=result.exclusions,
            ),
        )
        assert EvaluationRunBundleV1.from_record(legacy.to_record()) == legacy
    finally:
        store.close()


async def test_bundle_links_are_validated_before_the_digest() -> None:
    store = await _capture("bundle-links")
    try:
        result = evaluate_capture(
            store=store,
            capture_run_id="bundle-links",
            resolution=_resolution(),
            token_id=TOKEN,
            settings=Settings(),
            clock=ReplayClock(NOW),
            evaluation_run_id="evaluation-bundle-links",
            contract=_contract(),
            methods=(BaselineMethod.MIDPOINT,),
        )
        bundle = result.bundle
        with pytest.raises(ValueError, match="different evaluation runs"):
            bundle.model_copy(
                update={
                    "report": result.report.model_copy(
                        update={"evaluation_run_id": "different-run"}
                    )
                }
            )
        with pytest.raises(ValueError, match="forecast_id must be unique"):
            bundle.model_copy(update={"forecasts": (result.forecasts[0],) * 2})
        with pytest.raises(ValueError, match="evaluation names a forecast absent"):
            bundle.model_copy(update={"forecasts": ()})
        missing = EvaluationExclusionV1(
            forecast_id="forecast-absent-from-bundle",
            reason=ExclusionReason.ABSTAINED,
            detail="adversarial link test",
        )
        with pytest.raises(ValueError, match="exclusion names a forecast absent"):
            bundle.model_copy(update={"exclusions": (*result.exclusions, missing)})
    finally:
        store.close()


async def test_digest_valid_report_contradictions_are_refused() -> None:
    store = await _capture("bundle-report-claims")
    try:
        result = evaluate_capture(
            store=store,
            capture_run_id="bundle-report-claims",
            resolution=_resolution(),
            token_id=TOKEN,
            settings=Settings(),
            clock=ReplayClock(NOW),
            evaluation_run_id="evaluation-bundle-report-claims",
            contract=_contract(),
            methods=(BaselineMethod.MIDPOINT,),
        )
        source = result.bundle.to_record()
        report_links: dict[str, Any] = {
            "evaluation_policy_version": "evaluation_policy.v1",
            "resolution_id": "resolution-contradiction",
            "resolution_record_sha256": "0" * 64,
            "resolution_status": "proposed",
            "resolution_normalizer_version": "contradictory-normalizer/1",
            "resolution_cutoff": datetime(2026, 8, 19, tzinfo=UTC).isoformat(),
            "contract_id": "contract-contradiction",
            "contract_record_sha256": "0" * 64,
        }
        for field_name, contradictory_value in report_links.items():
            record = deepcopy(source)
            record["report"][field_name] = contradictory_value
            _redigest_bundle_record(record)
            with pytest.raises(ValueError, match=rf"report\.{field_name} disagrees"):
                EvaluationRunBundleV2.from_record(record)

        report_count_fields = (
            "arrival_count",
            "target_information_state_count",
            "forecast_count",
            "forecast_point_count",
            "scored_count",
            "scored_forecast_point_count",
            "abstention_count",
            "unresolved_count",
            "resolved_target_count",
        )
        for field_name in report_count_fields:
            record = deepcopy(source)
            record["report"][field_name] += 1
            _redigest_bundle_record(record)
            with pytest.raises(ValueError, match=rf"report\.{field_name} disagrees"):
                EvaluationRunBundleV2.from_record(record)

        record = deepcopy(source)
        record["report"]["child_record_digests"]["forecasts"] = "0" * 64
        _redigest_bundle_record(record)
        with pytest.raises(ValueError, match="child_record_digests disagrees"):
            EvaluationRunBundleV2.from_record(record)
    finally:
        store.close()


async def test_digest_valid_child_contradictions_are_refused() -> None:
    store = await _capture("bundle-child-claims")
    try:
        result = evaluate_capture(
            store=store,
            capture_run_id="bundle-child-claims",
            resolution=_resolution(),
            token_id=TOKEN,
            settings=Settings(),
            clock=ReplayClock(NOW),
            evaluation_run_id="evaluation-bundle-child-claims",
            contract=_contract(),
            methods=(BaselineMethod.MIDPOINT,),
        )
        source = result.bundle.to_record()

        evaluation_links = {
            "evaluation_run_id": ("different-run", "different run"),
            "resolution_id": ("different-resolution", "different resolution"),
            "contract_id": ("different-contract", "different contract"),
        }
        for field_name, (contradictory_value, message) in evaluation_links.items():
            record = deepcopy(source)
            record["evaluations"][0][field_name] = contradictory_value
            _redigest_bundle_record(record)
            with pytest.raises(ValueError, match=message):
                EvaluationRunBundleV2.from_record(record)

        forecast_links = {
            "evaluation_run_id": ("different-run", "different evaluation run"),
            "contract_id": ("different-contract", "different contract"),
            "market_id": ("different-market", "different market"),
        }
        for field_name, (contradictory_value, message) in forecast_links.items():
            record = deepcopy(source)
            record["forecasts"][0][field_name] = contradictory_value
            _redigest_bundle_record(record)
            with pytest.raises(ValueError, match=message):
                EvaluationRunBundleV2.from_record(record)

        record = deepcopy(source)
        record["evaluations"][0]["forecast_method"] = BaselineMethod.LAST_TRADE.value
        _redigest_bundle_record(record)
        with pytest.raises(ValueError, match="disagrees with the forecast"):
            EvaluationRunBundleV2.from_record(record)

        record = deepcopy(source)
        record["exclusions"].append(
            EvaluationExclusionV1(
                forecast_id=str(record["evaluations"][0]["forecast_id"]),
                reason=ExclusionReason.ABSTAINED,
                detail="digest-valid scored/excluded contradiction",
            ).to_record()
        )
        _redigest_bundle_record(record)
        with pytest.raises(ValueError, match="both scored and excluded"):
            EvaluationRunBundleV2.from_record(record)

        record = deepcopy(source)
        record["evaluations"].pop()
        _redigest_bundle_record(record)
        with pytest.raises(ValueError, match="scored or excluded exactly once"):
            EvaluationRunBundleV2.from_record(record)

        record = deepcopy(source)
        record["evaluations"].append(deepcopy(record["evaluations"][0]))
        _redigest_bundle_record(record)
        with pytest.raises(ValueError, match="evaluated at most once"):
            EvaluationRunBundleV2.from_record(record)

        record = deepcopy(source)
        exclusion = EvaluationExclusionV1(
            forecast_id=str(record["evaluations"][0]["forecast_id"]),
            reason=ExclusionReason.ABSTAINED,
            detail="duplicate exclusion contradiction",
        ).to_record()
        record["exclusions"].extend((exclusion, exclusion))
        _redigest_bundle_record(record)
        with pytest.raises(ValueError, match="excluded at most once"):
            EvaluationRunBundleV2.from_record(record)

        record = deepcopy(source)
        record["decisions"].append(deepcopy(record["decisions"][0]))
        _redigest_bundle_record(record)
        with pytest.raises(ValueError, match="ingest_sequence must be unique"):
            EvaluationRunBundleV2.from_record(record)
    finally:
        store.close()


def test_contract_and_capture_identity_are_checked_before_replay() -> None:
    store = open_sqlite_event_store(":memory:")
    try:
        common = {
            "store": store,
            "capture_run_id": "missing-capture",
            "resolution": _resolution(),
            "token_id": TOKEN,
            "settings": Settings(),
            "clock": ReplayClock(NOW),
            "evaluation_run_id": "evaluation-refusal",
        }
        mismatched_contract = _contract().model_copy(update={"condition_id": "different-condition"})
        with pytest.raises(ValueError, match="different conditions"):
            evaluate_capture(**common, contract=mismatched_contract)
        with pytest.raises(ValueError, match="no such capture_run"):
            evaluate_capture(**common, contract=_contract())
    finally:
        store.close()


def test_resolution_identity_changes_when_yes_mapping_changes() -> None:
    raw = RESOLUTION_FIXTURE.read_bytes()
    payload = json.loads(raw)
    tokens = [item["token_id"] for item in payload["tokens"]]
    first = normalize_clob_resolution(
        payload,
        source_payload_sha256=hashlib.sha256(raw).hexdigest(),
        normalized_at=NOW,
        yes_token_id=tokens[0],
    )
    second = normalize_clob_resolution(
        payload,
        source_payload_sha256=hashlib.sha256(raw).hexdigest(),
        normalized_at=NOW,
        yes_token_id=tokens[1],
    )
    assert isinstance(first, ResolutionV1) and isinstance(second, ResolutionV1)
    assert first.winning_outcome != second.winning_outcome
    assert first.resolution_id != second.resolution_id


def test_two_targets_are_a_structural_floor_not_calibration_sufficiency() -> None:
    policy = EvaluationPolicyV2()
    assert policy.structural_minimum_independent_resolved_targets == 2
    assert policy.calibration_claim_policy == "predeclared_multi_target_protocol_required"
    with pytest.raises(AttributeError, match="structural floor"):
        _ = policy.minimum_resolved_targets_for_calibration
