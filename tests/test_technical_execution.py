from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from argos.evaluation.technical_campaign import TechnicalScenarioStatus
from argos.evaluation.technical_execution import (
    FunctionalScenarioEvidenceV1,
    T1_MAXIMUM_DURATION_SECONDS,
    assess_functional_scenario,
)

NOW = datetime(2026, 9, 20, 20, 0, tzinfo=UTC)


def _evidence(**overrides: object) -> FunctionalScenarioEvidenceV1:
    values: dict[str, object] = {
        "campaign_id": "m4-technical-20260920-v1",
        "capture_run_id": "m4-technical-20260920-v1-t1",
        "started_at": NOW,
        "ended_at": NOW + timedelta(minutes=10),
        "capture_completed": True,
        "capture_interrupted": False,
        "loop_accepted": 12,
        "loop_duplicate": 1,
        "loop_rejected": 0,
        "store_accepted": 12,
        "store_duplicate": 1,
        "store_rejected": 0,
        "frames_consumed": 13,
        "raw_payload_count": 12,
        "database_sha256_before_replay": "a" * 64,
        "database_sha256_after_first_replay": "a" * 64,
        "database_sha256_after_second_replay": "a" * 64,
        "first_replay_state_hash": "b" * 64,
        "second_replay_state_hash": "b" * 64,
        "artifact_identities": ("sha256:abc:capture-report.json",),
    }
    values.update(overrides)
    return FunctionalScenarioEvidenceV1.model_validate(values)


def test_t1_passes_only_with_complete_consistent_immutable_evidence() -> None:
    result = assess_functional_scenario(_evidence())
    assert result.status is TechnicalScenarioStatus.PASSED
    assert result.observed_frame_count == 13
    assert result.reason is None


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"capture_completed": False}, "not completed"),
        ({"capture_interrupted": True}, "operator interruption"),
        ({"frames_consumed": 0}, "no frames"),
        ({"loop_rejected": 1, "store_rejected": 1}, "rejected observations"),
        ({"store_accepted": 11}, "counts disagree"),
        ({"raw_payload_count": 0}, "raw archive is empty"),
        ({"database_sha256_after_first_replay": "c" * 64}, "changed the capture database"),
        ({"second_replay_state_hash": "c" * 64}, "different state hashes"),
    ],
)
def test_t1_preserves_each_failed_invariant(overrides: dict[str, object], reason: str) -> None:
    result = assess_functional_scenario(_evidence(**overrides))
    assert result.status is TechnicalScenarioStatus.FAILED
    assert reason in (result.reason or "")
    assert result.follow_up_action is not None


def test_t1_reports_all_simultaneous_failures() -> None:
    result = assess_functional_scenario(
        _evidence(
            capture_completed=False,
            frames_consumed=0,
            raw_payload_count=0,
            second_replay_state_hash="c" * 64,
        )
    )
    assert result.reason is not None
    assert result.reason.count(";") == 3


def test_t1_fails_when_end_to_end_duration_exceeds_frozen_bound() -> None:
    result = assess_functional_scenario(
        _evidence(ended_at=NOW + timedelta(seconds=T1_MAXIMUM_DURATION_SECONDS + 1))
    )
    assert result.status is TechnicalScenarioStatus.FAILED
    assert "duration exceeded" in (result.reason or "")


def test_evidence_refuses_invalid_timeline_and_hashes() -> None:
    with pytest.raises(ValidationError):
        _evidence(ended_at=NOW - timedelta(seconds=1))
    with pytest.raises(ValidationError):
        _evidence(first_replay_state_hash="not-a-hash".ljust(64, "z"))
