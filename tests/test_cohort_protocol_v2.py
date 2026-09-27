"""No-network tests for ADR-0020's declaration and compatibility boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from runpy import run_path
from typing import Any

import orjson
import pytest
from pydantic import ValidationError

from argos.errors import NaiveDatetimeError, SchemaVersionError
from argos.evaluation.async_cohort import (
    AsynchronousCohortProtocolV1,
    CohortBlockV1,
    GammaSelectionV1,
)
from argos.evaluation.cohort_protocol_v2 import AsynchronousCohortProtocolV2, CohortStratumV1
from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    load_persisted_record,
    persist_evidence_record,
)

START = datetime(2026, 10, 1, 12, tzinfo=UTC)
REVISION = "a" * 40
_SCRIPT = run_path(
    str(Path(__file__).resolve().parents[1] / "scripts" / "m4_async_cohort_preflight.py")
)
preflight_protocol = _SCRIPT["preflight_protocol"]


def _stratum(**updates: object) -> CohortStratumV1:
    return CohortStratumV1.model_validate(
        {
            "stratum_id": "narrow",
            "category": "reviewed-category",
            "minimum_liquidity": Decimal("1000"),
            "maximum_liquidity": Decimal("100000"),
            "minimum_spread": Decimal("0"),
            "maximum_spread": Decimal("0.1"),
            "minimum_horizon_seconds": 600,
            "maximum_horizon_seconds": 86400,
            "maximum_targets": 2,
            **updates,
        }
    )


def _protocol(**updates: object) -> AsynchronousCohortProtocolV2:
    blocks = tuple(
        CohortBlockV1(
            ordinal=index + 1,
            start=START + timedelta(hours=index),
            end=START + timedelta(hours=index, minutes=10),
            intended_targets=2,
        )
        for index in range(2)
    )
    return AsynchronousCohortProtocolV2.model_validate(
        {
            "experiment_id": "synthetic-cohort-v2",
            "declared_at": START - timedelta(days=1),
            "code_revision": REVISION,
            "config_fingerprint": "b" * 64,
            "target_population": "Reviewed category, public binary markets in declared bands",
            "blocks": blocks,
            "selection": GammaSelectionV1(
                base_url="https://gamma-api.polymarket.com",
                query=(("active", "true"), ("limit", "100")),
                minimum_liquidity=Decimal("1000"),
                minimum_outcome_price=Decimal("0.05"),
                maximum_outcome_price=Decimal("0.95"),
                target_end_min=START + timedelta(minutes=15),
                target_end_max=START + timedelta(days=2),
            ),
            "strata": (
                _stratum(),
                _stratum(stratum_id="wide", minimum_spread=Decimal("0.1"), maximum_spread=1),
            ),
            "target_budget": 4,
            "capture_max_seconds_per_target": 120,
            "capture_max_frames_per_target": 500,
            "capture_max_bytes_per_target": 1000000,
            "finalization_reserve_seconds": 30,
            "evidence_reserve_bytes": 1000000,
            "campaign_max_bytes": 5000000,
            "free_disk_margin_bytes": 1000000,
            "outcome_blind_margin_seconds": 60,
            "operational_review_at": blocks[-1].end,
            "follow_up_until": START + timedelta(days=14),
            "poll_interval_seconds": 10800,
            "maximum_poll_gap_seconds": 21600,
            "log_loss_epsilon": Decimal("0.000001"),
            **updates,
        }
    )


def test_v2_roundtrip_receipt_and_nonretroactive_version_boundary(tmp_path: Path) -> None:
    protocol = _protocol()
    record = orjson.loads(orjson.dumps(protocol.to_record()))
    assert AsynchronousCohortProtocolV2.from_record(record) == protocol
    assert record["strata"][0]["schema_version"] == CohortStratumV1.schema_version
    receipt = persist_evidence_record(
        tmp_path,
        record=protocol,
        experiment_id=protocol.experiment_id,
        artifact_kind=EvidenceArtifactKind.EXPERIMENT_PROTOCOL,
        artifact_id=protocol.experiment_id,
        persisted_at=protocol.declared_at,
    )
    assert load_persisted_record(tmp_path, receipt, AsynchronousCohortProtocolV2) == protocol
    with pytest.raises(SchemaVersionError):
        AsynchronousCohortProtocolV1.from_record(record)
    record["schema_version"] = AsynchronousCohortProtocolV1.schema_version
    with pytest.raises(SchemaVersionError):
        AsynchronousCohortProtocolV2.from_record(record)


@pytest.mark.parametrize("child", ["blocks", "selection", "strata"])
def test_nested_versions_cannot_be_discarded(child: str) -> None:
    record = _protocol().to_record()
    nested = record[child] if child == "selection" else record[child][0]
    nested["schema_version"] = "unsupported.v99"
    with pytest.raises(SchemaVersionError):
        AsynchronousCohortProtocolV2.from_record(record)


def test_partial_capacity_and_early_events_are_declarable_without_claiming_admission() -> None:
    original = _protocol()
    # Two slots, but enough time for only one worst-case capture in block 1.
    first = original.blocks[0].model_copy(update={"end": START + timedelta(seconds=150)})
    protocol = _protocol(blocks=(first, original.blocks[1]))
    assert protocol.selection.target_end_min < protocol.blocks[-1].start
    assert protocol.admission_policy == "retain_partial_blocks_no_slot_transfer"
    assert protocol.calibration_claim == "NOT_ESTABLISHED"
    assert protocol.primary_metric == "brier_score"
    assert protocol.snapshot_policy == "last_shared_state_before_capture_close"
    assert "scientific_minimum_resolved_target_count" not in protocol.to_record()


def test_single_target_declaration_and_variable_blocks_are_valid_not_calibration() -> None:
    original = _protocol()
    one = _protocol(
        blocks=(original.blocks[0].model_copy(update={"intended_targets": 1}),),
        strata=(_stratum(maximum_targets=1),),
        target_budget=1,
    )
    assert len(one.blocks) == 1
    assert one.calibration_claim == "NOT_ESTABLISHED"


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"blocks": ()}, "at least 1"),
        ({"strata": ()}, "at least 1"),
        ({"declared_at": START}, "precede"),
        ({"target_budget": 3}, "block slot caps"),
        ({"target_budget": True}, "valid integer"),
        ({"strata": (_stratum(),)}, "stratum caps"),
        (
            {"strata": (_stratum(), _stratum(minimum_spread=Decimal("0.1"), maximum_spread=1))},
            "identities must be unique",
        ),
        ({"strata": (_stratum(), _stratum(stratum_id="overlap"))}, "must not overlap"),
        (
            {"strata": (_stratum(maximum_targets=4, minimum_horizon_seconds=209),)},
            "outcome-blind margin",
        ),
        (
            {"strata": (_stratum(maximum_targets=4, minimum_liquidity=999),)},
            "discovery liquidity",
        ),
        ({"campaign_max_bytes": 4999999}, "cannot fund"),
        ({"operational_review_at": START}, "final block end"),
        ({"follow_up_until": START}, "include the operational review"),
        ({"maximum_poll_gap_seconds": 10799}, "scheduled interval"),
        ({"capture_max_seconds_per_target": 121}, "less than or equal"),
        ({"capture_max_frames_per_target": 501}, "less than or equal"),
        ({"capture_max_bytes_per_target": 0}, "greater than"),
        ({"free_disk_margin_bytes": 0}, "greater than"),
        ({"log_loss_epsilon": Decimal("0.5")}, "less than"),
        ({"log_loss_epsilon": Decimal("NaN")}, "finite number"),
        ({"log_loss_epsilon": 0}, "greater than"),
        ({"calibration_claim": "ESTABLISHED"}, "NOT_ESTABLISHED"),
        ({"admission_policy": "reject_short_block"}, "retain_partial"),
        ({"semantic_review_policy": "nonempty_rules_only"}, "human_review"),
        ({"replacement_policy": "replace_failed_forecast"}, "pre_admission_only"),
        ({"snapshot_policy": "last_before_finality"}, "last_shared_state"),
        ({"primary_metric": "absolute_error"}, "brier_score"),
        ({"working_tree_state": "DIRTY"}, "CLEAN"),
        ({"target_population": "  "}, "nonblank"),
        ({"code_revision": "main"}, "pattern"),
        ({"config_fingerprint": "hash"}, "pattern"),
        ({"undeclared_rule": True}, "Extra inputs"),
    ],
)
def test_invalid_declarations_fail_closed(updates: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _protocol(**updates)


def test_naive_time_is_not_silently_assumed_utc() -> None:
    with pytest.raises(NaiveDatetimeError, match="timezone"):
        _protocol(declared_at=START.replace(tzinfo=None))


@pytest.mark.parametrize(
    ("block_index", "update", "message"),
    [
        (1, {"ordinal": 3}, "contiguous ordinals"),
        (1, {"start": START + timedelta(minutes=9)}, "nonoverlapping"),
        (0, {"end": START + timedelta(seconds=149)}, "finalization reserve"),
    ],
)
def test_invalid_block_windows(block_index: int, update: dict[str, Any], message: str) -> None:
    original = _protocol()
    blocks = list(original.blocks)
    blocks[block_index] = blocks[block_index].model_copy(update=update)
    with pytest.raises(ValidationError, match=message):
        original.model_copy(update={"blocks": tuple(blocks)})


@pytest.mark.parametrize(
    "updates",
    [
        {"minimum_liquidity": 100000},
        {"minimum_spread": Decimal("0.1")},
        {"minimum_horizon_seconds": 86400},
        {"category": "  "},
        {"category": "reviewed-category "},
        {"maximum_liquidity": Decimal("Infinity")},
        {"stratum_id": "uncanonical ID"},
    ],
)
def test_strata_refuse_empty_or_ambiguous_bands(updates: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _stratum(**updates)


@pytest.mark.parametrize(
    "updates",
    [
        {"category": "other-category"},
        {"minimum_liquidity": 100000, "maximum_liquidity": 200000},
        {"minimum_liquidity": 0, "maximum_liquidity": 1000},
        {"minimum_spread": Decimal("0.1"), "maximum_spread": 1},
        {"minimum_horizon_seconds": 86400, "maximum_horizon_seconds": 172800},
        {"minimum_horizon_seconds": 1, "maximum_horizon_seconds": 600},
    ],
)
def test_half_open_strata_are_symmetric_and_disjoint(updates: dict[str, Any]) -> None:
    left = _stratum()
    right = _stratum(**updates)
    assert not left.overlaps(right)
    assert not right.overlaps(left)
    assert left.overlaps(left)


def test_preflight_reports_implementation_and_resource_limits_not_launch_readiness() -> None:
    protocol = _protocol()
    records = []
    for precision in (6, 28, 50):
        with localcontext() as context:
            context.prec = precision
            records.append(
                preflight_protocol(
                    protocol.to_record(), revision=REVISION, checked_at=START - timedelta(hours=1)
                )
            )
    assert records[0] == records[1] == records[2]
    result = records[0]
    assert result["status"] == "PREFLIGHT_ONLY_NOT_FROZEN"
    assert result["intended_targets"] == 4
    assert result["intended_targets_semantics"] == "maximum_slots_not_admitted_targets"
    assert result["live_launch_supported"] is False
    assert result["disk_space_checked"] is False
    assert result["required_initial_free_bytes"] == 6000000
    assert result["calibration_claim"] == "NOT_ESTABLISHED"


@pytest.mark.parametrize(
    ("revision", "checked_at", "message"),
    [
        ("c" * 40, START - timedelta(hours=1), "revision disagrees"),
        (REVISION, START, "already begun"),
        (REVISION, START - timedelta(days=2), "in the future"),
    ],
)
def test_v2_preflight_preserves_time_and_revision_guards(
    revision: str, checked_at: datetime, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        preflight_protocol(_protocol().to_record(), revision=revision, checked_at=checked_at)
