"""Adversarial offline guards for the unlaunched asynchronous M4 cohort."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from runpy import run_path

import pytest
from pydantic import ValidationError
from test_prospective_evidence import DECLARED_AT, WINDOW_START, _protocol

from argos.domain.market import MarketDefinitionV1
from argos.errors import SchemaVersionError
from argos.evaluation.async_cohort import (
    AsynchronousCohortProtocolV1,
    CohortBlockV1,
    GammaSelectionV1,
    select_block_candidates,
    validate_block_admission,
)
from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    load_persisted_record,
    persist_evidence_record,
)


def _candidate(**updates: object) -> AsynchronousCohortProtocolV1:
    blocks = tuple(
        CohortBlockV1(
            ordinal=index,
            start=WINDOW_START + timedelta(hours=index - 1),
            end=WINDOW_START + timedelta(hours=index),
            intended_targets=4,
        )
        for index in range(1, 5)
    )
    selection = GammaSelectionV1(
        base_url="https://gamma-api.polymarket.com",
        query=(("active", "true"), ("limit", "100")),
        minimum_liquidity=Decimal("1000"),
        minimum_outcome_price=Decimal("0.05"),
        maximum_outcome_price=Decimal("0.95"),
        target_end_min=WINDOW_START + timedelta(days=1),
        target_end_max=WINDOW_START + timedelta(days=30),
    )
    fields = {
        **_protocol().model_dump(mode="python"),
        "experiment_id": "synthetic-m4-cohort",
        "declared_at": DECLARED_AT,
        "observation_window_start": blocks[0].start,
        "observation_window_end": blocks[-1].end,
        "minimum_intended_resolved_target_count": 16,
        "market_selection_mechanism": (
            "four independent UTC blocks, liquidity then numeric market id"
        ),
        "stopping_rule": "no replacement; retain unresolved targets after operational review",
        "blocks": blocks,
        "selection": selection,
        "capture_max_seconds_per_target": 120,
        "capture_max_frames_per_target": 500,
        "capture_separate_database_per_target": True,
        "capture_subscribe_both_tokens": True,
        "capture_raw_archive": True,
        "distinct_event_identity": True,
        "replacement_allowed": False,
        "reject_short_block_before_forecast": True,
        "pending_retained_in_denominator": True,
        "operational_review_at": WINDOW_START + timedelta(days=14),
        "initial_poll_interval_seconds": 300,
        "post_review_poll_interval_seconds": 3600,
    }
    fields.update(updates)
    return AsynchronousCohortProtocolV1.model_validate(fields)


def test_nested_protocol_round_trip_preserves_versions_and_exact_plan() -> None:
    protocol = _candidate()
    record = protocol.to_record()
    assert record["blocks"][0]["schema_version"] == CohortBlockV1.schema_version
    assert record["selection"]["schema_version"] == GammaSelectionV1.schema_version
    assert AsynchronousCohortProtocolV1.from_record(record) == protocol
    record["blocks"][0]["schema_version"] = "m4_cohort_block.v0"
    with pytest.raises(SchemaVersionError, match="not supported"):
        AsynchronousCohortProtocolV1.from_record(record)


def test_protocol_receipt_reloads_the_same_versioned_blocks(tmp_path: Path) -> None:
    protocol = _candidate()
    receipt = persist_evidence_record(
        tmp_path,
        record=protocol,
        experiment_id=protocol.experiment_id,
        artifact_kind=EvidenceArtifactKind.EXPERIMENT_PROTOCOL,
        artifact_id=protocol.experiment_id,
        persisted_at=protocol.declared_at,
    )
    assert receipt.artifact_schema_version == protocol.schema_version
    assert load_persisted_record(tmp_path, receipt, AsynchronousCohortProtocolV1) == protocol


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"blocks": _candidate().blocks[:3]}, "four ordered blocks"),
        ({"blocks": tuple(reversed(_candidate().blocks))}, "four ordered blocks"),
        ({"minimum_intended_resolved_target_count": 15}, "16 intended"),
        ({"scientific_minimum_resolved_target_count": 16}, "calibration sufficiency"),
        ({"capture_max_seconds_per_target": 121}, "less than or equal to 120"),
        ({"capture_max_frames_per_target": 501}, "less than or equal to 500"),
        ({"replacement_allowed": True}, "no-replacement"),
        ({"distinct_event_identity": False}, "independence"),
        ({"capture_raw_archive": False}, "capture"),
        ({"pending_retained_in_denominator": False}, "cohort capture"),
        ({"minimum_yes_outcomes_for_calibration": 1}, "cannot weaken calibration"),
        ({"operational_review_at": _candidate().observation_window_end}, "review"),
        ({"post_review_poll_interval_seconds": 60}, "post-review cadence"),
    ],
)
def test_protocol_rejects_invalid_cohort_plan(update: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _candidate(**update)


def test_blocks_cannot_overlap_or_be_too_short() -> None:
    original = _candidate()
    overlapping = original.blocks[1].model_copy(
        update={"start": original.blocks[0].end - timedelta(seconds=1)}
    )
    with pytest.raises(ValidationError, match="must not overlap"):
        _candidate(blocks=(original.blocks[0], overlapping, *original.blocks[2:]))
    short = original.blocks[1].model_copy(
        update={"end": original.blocks[1].start + timedelta(seconds=1)}
    )
    with pytest.raises(ValidationError, match="cannot fit"):
        _candidate(blocks=(original.blocks[0], short, *original.blocks[2:]))


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"base_url": "http://gamma-api.polymarket.com"}, "public HTTPS"),
        ({"base_url": "https://gamma-api.polymarket.com:444"}, "public HTTPS"),
        ({"base_url": "https://user:pass@gamma-api.polymarket.com"}, "public HTTPS"),
        ({"query": (("limit", "100"), ("active", "true"))}, "sorted"),
        ({"query": (("limit", "100"), ("limit", "200"))}, "unique"),
        ({"minimum_outcome_price": Decimal("0.95")}, "increasing"),
    ],
)
def test_selection_is_exact_public_and_deterministic(
    update: dict[str, object], message: str
) -> None:
    selection = _candidate().selection
    with pytest.raises(ValidationError, match=message):
        GammaSelectionV1.model_validate({**selection.model_dump(mode="python"), **update})


def test_block_admission_is_atomic_and_never_reuses_event() -> None:
    protocol = _candidate()
    valid = ("event-a", "event-b", "event-c", "event-d")
    at = protocol.blocks[1].start + timedelta(seconds=1)
    earlier_events = frozenset({"earlier-1", "earlier-2", "earlier-3", "earlier-4"})
    validate_block_admission(
        protocol, block_ordinal=2, selected_at=at, event_ids=valid,
        earlier_event_ids=earlier_events,
    )
    cases = [
        (at, valid[:3], earlier_events, "short"),
        (at, ("event-a", "event-a", "event-c", "event-d"), earlier_events, "more than once"),
        (
            at,
            valid,
            frozenset({"event-c", "earlier-2", "earlier-3", "earlier-4"}),
            "more than once",
        ),
        (protocol.blocks[1].end, valid, earlier_events, "outside frozen block"),
        (at, valid, frozenset(), "missing predecessors"),
    ]
    for selected_at, event_ids, earlier, message in cases:
        with pytest.raises(ValueError, match=message):
            validate_block_admission(
                protocol, block_ordinal=2, selected_at=selected_at,
                event_ids=event_ids, earlier_event_ids=earlier,
            )


def _market(market_id: int, *, event_id: str | None = None) -> MarketDefinitionV1:
    return MarketDefinitionV1(
        market_id=str(market_id),
        condition_id="0x" + f"{market_id:064x}",
        slug=f"synthetic-{market_id}",
        event_id=event_id or f"event-{market_id}",
        question=f"Will synthetic event {market_id} resolve Yes?",
        description="Synthetic test rules.",
        resolution_source="Synthetic test source.",
        start_time=WINDOW_START - timedelta(days=1),
        end_time=WINDOW_START + timedelta(days=2),
        active=True,
        closed=False,
        archived=False,
        liquidity=Decimal(10000 - market_id),
        outcomes=("Yes", "No"),
        outcome_token_map={"Yes": str(market_id * 2), "No": str(market_id * 2 + 1)},
        raw_payload_sha256="a" * 64,
        normalized_at=WINDOW_START,
        normalizer_version="synthetic-test/1",
    )


def test_offline_selector_is_deterministic_atomic_and_independent() -> None:
    protocol = _candidate()
    markets = (
        _market(7), _market(4), _market(2, event_id="event-1"),
        _market(3), _market(1), _market(5),
    )
    raw = {
        market.market_id: {
            "enableOrderBook": True,
            "acceptingOrders": True,
            "outcomePrices": '["0.4", "0.6"]',
        }
        for market in markets
    }
    selected = select_block_candidates(
        protocol, block_ordinal=1,
        selected_at=protocol.blocks[0].start + timedelta(seconds=1),
        markets=markets, raw_by_market_id=raw, earlier_event_ids=frozenset(),
    )
    assert tuple(market.market_id for market in selected) == ("1", "3", "4", "5")
    assert len({market.event_id for market in selected}) == 4
    reordered = select_block_candidates(
        protocol, block_ordinal=1,
        selected_at=protocol.blocks[0].start + timedelta(seconds=1),
        markets=tuple(reversed(markets)), raw_by_market_id=raw,
        earlier_event_ids=frozenset(),
    )
    assert reordered == selected
    raw["3"]["acceptingOrders"] = False
    raw["4"]["outcomePrices"] = '["NaN", "0.6"]'
    with pytest.raises(ValueError, match="short cohort block"):
        select_block_candidates(
            protocol, block_ordinal=1,
            selected_at=protocol.blocks[0].start + timedelta(seconds=1),
            markets=markets, raw_by_market_id=raw, earlier_event_ids=frozenset(),
        )


_SCRIPT = run_path(
    str(Path(__file__).resolve().parents[1] / "scripts" / "m4_async_cohort_preflight.py")
)
preflight_protocol = _SCRIPT["preflight_protocol"]


def test_preflight_cannot_masquerade_as_freeze() -> None:
    protocol = _candidate()
    report = preflight_protocol(
        protocol.to_record(), revision=protocol.code_revision,
        checked_at=protocol.declared_at + timedelta(minutes=1),
    )
    assert report["status"] == "PREFLIGHT_ONLY_NOT_FROZEN"
    assert report["intended_targets"] == 16
    with pytest.raises(ValueError, match="revision disagrees"):
        preflight_protocol(
            protocol.to_record(), revision="different", checked_at=protocol.declared_at
        )
    with pytest.raises(ValueError, match="already begun"):
        preflight_protocol(
            protocol.to_record(), revision=protocol.code_revision,
            checked_at=protocol.blocks[0].start,
        )
