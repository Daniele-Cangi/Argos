"""Adversarial properties the pre-owner M4 tests did not exercise."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_replay import CAPTURE_START, TOKEN, _ListFrameSource, _recorded_frames

from argos.baselines import BaselineMethod
from argos.clock import ReplayClock
from argos.compiler import CompiledMarketContractV1
from argos.config import Settings
from argos.evaluation import EvaluationResult, EvaluationRunBundleV1, evaluate_capture
from argos.evaluation.bundle import DecisionReason
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
        assert EvaluationRunBundleV1.from_record(record) == result.bundle
        record["evidence_digest"] = "0" * 64
        with pytest.raises(ValueError, match="evidence_digest"):
            EvaluationRunBundleV1.from_record(record)
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
