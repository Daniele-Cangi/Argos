from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from argos.evaluation.technical_campaign import TechnicalScenarioStatus
from argos.evaluation.technical_execution import (
    T3_EXPECTED_CHECKPOINT_INTERVAL_SECONDS,
    T3_MAXIMUM_ARTIFACT_BYTES,
    T3_MAXIMUM_CHECKPOINT_GAP_SECONDS,
    T3_MAXIMUM_RESIDENT_MEMORY_BYTES,
    EnduranceCheckpointV1,
    EnduranceScenarioEvidenceV1,
    assess_endurance_scenario,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _checkpoints() -> tuple[EnduranceCheckpointV1, ...]:
    first = EnduranceCheckpointV1(
        ordinal=0,
        observed_at=NOW,
        state="RUNNING",
        resident_memory_bytes=100,
        artifact_bytes=200,
    )
    terminal = EnduranceCheckpointV1(
        ordinal=1,
        observed_at=NOW + timedelta(minutes=5),
        state="COMPLETED",
        resident_memory_bytes=110,
        artifact_bytes=250,
        previous_checkpoint_sha256=first.checkpoint_sha256,
    )
    return first, terminal


def _evidence(**overrides: object) -> EnduranceScenarioEvidenceV1:
    values: dict[str, object] = {
        "campaign_id": "campaign",
        "capture_run_id": "campaign-t3",
        "started_at": NOW,
        "ended_at": NOW + timedelta(minutes=5),
        "capture_completed": True,
        "capture_interrupted": False,
        "frames_consumed": 10,
        "decode_failures": 0,
        "unknown_event_type": 0,
        "loop_counts": (20, 0, 0),
        "store_counts": (20, 0, 0),
        "raw_payload_count": 10,
        "expected_checkpoint_interval_seconds": T3_EXPECTED_CHECKPOINT_INTERVAL_SECONDS,
        "maximum_checkpoint_gap_seconds": T3_MAXIMUM_CHECKPOINT_GAP_SECONDS,
        "maximum_resident_memory_bytes": T3_MAXIMUM_RESIDENT_MEMORY_BYTES,
        "maximum_artifact_bytes": T3_MAXIMUM_ARTIFACT_BYTES,
        "checkpoints": _checkpoints(),
        "terminal_checkpoint_reloaded": True,
        "artifact_identities": ("sha256:abc:checkpoint-index.json",),
    }
    values.update(overrides)
    return EnduranceScenarioEvidenceV1.model_validate(values)


def test_t3_passes_with_intact_chain_and_reloadable_terminal_state() -> None:
    result = assess_endurance_scenario(_evidence())
    assert result.status is TechnicalScenarioStatus.PASSED
    assert result.last_checkpoint_at == NOW + timedelta(minutes=5)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"capture_completed": False}, "not completed"),
        ({"capture_interrupted": True}, "operator interruption"),
        ({"frames_consumed": 0}, "no frames"),
        ({"loop_counts": (19, 0, 1), "store_counts": (19, 0, 1)}, "rejected"),
        ({"store_counts": (19, 1, 0)}, "counts disagree"),
        ({"raw_payload_count": 0}, "raw archive is empty"),
        ({"terminal_checkpoint_reloaded": False}, "not reloadable"),
        ({"checkpoint_errors": ("AccessDenied",)}, "reported errors"),
        ({"decode_failures": 1}, "decode failures"),
        ({"unknown_event_type": 1}, "unknown event types"),
    ],
)
def test_t3_preserves_each_failed_invariant(overrides: dict[str, object], reason: str) -> None:
    result = assess_endurance_scenario(_evidence(**overrides))
    assert result.status is TechnicalScenarioStatus.FAILED
    assert reason in (result.reason or "")


def test_t3_refuses_a_broken_checkpoint_chain() -> None:
    first, terminal = _checkpoints()
    broken = terminal.model_copy(update={"previous_checkpoint_sha256": "f" * 64})
    with pytest.raises(ValidationError, match="hash chain"):
        _evidence(checkpoints=(first, broken))


def test_t3_refuses_weakened_frozen_bounds() -> None:
    with pytest.raises(ValidationError, match="frozen protocol"):
        _evidence(maximum_checkpoint_gap_seconds=900)
    with pytest.raises(ValidationError, match="frozen protocol"):
        _evidence(maximum_artifact_bytes=T3_MAXIMUM_ARTIFACT_BYTES * 2)
