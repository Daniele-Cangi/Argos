from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from argos.evaluation.technical_campaign import TechnicalScenarioStatus
from argos.evaluation.technical_execution import (
    StabilityResourceSampleV1,
    StabilityScenarioEvidenceV1,
    assess_stability_scenario,
)

NOW = datetime(2026, 9, 21, 8, 0, tzinfo=UTC)


def _sample(ordinal: int, *, seconds: int | None = None, rss: int = 100, disk: int = 200):
    return StabilityResourceSampleV1(
        ordinal=ordinal,
        observed_at=NOW + timedelta(seconds=ordinal * 60 if seconds is None else seconds),
        resident_memory_bytes=rss,
        artifact_bytes=disk,
    )


def _evidence(**overrides: object) -> StabilityScenarioEvidenceV1:
    values: dict[str, object] = {
        "campaign_id": "campaign",
        "capture_run_id": "campaign-t2",
        "started_at": NOW,
        "ended_at": NOW + timedelta(minutes=2),
        "capture_completed": True,
        "capture_interrupted": False,
        "frames_consumed": 4,
        "loop_counts": (8, 0, 0),
        "store_counts": (8, 0, 0),
        "raw_payload_count": 4,
        "expected_sample_interval_seconds": 60,
        "maximum_sample_gap_seconds": 120,
        "maximum_resident_memory_bytes": 1_000,
        "maximum_artifact_bytes": 2_000,
        "samples": (_sample(0), _sample(1), _sample(2)),
        "artifact_identities": ("sha256:abc:resource-samples.json",),
    }
    values.update(overrides)
    return StabilityScenarioEvidenceV1.model_validate(values)


def test_t2_passes_with_complete_accounting_cadence_and_bounded_resources() -> None:
    result = assess_stability_scenario(_evidence())
    assert result.status is TechnicalScenarioStatus.PASSED
    assert result.reason is None
    assert result.last_checkpoint_at == NOW + timedelta(minutes=2)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"capture_completed": False}, "not completed"),
        ({"capture_interrupted": True}, "operator interruption"),
        ({"frames_consumed": 0}, "no frames"),
        ({"loop_counts": (7, 0, 1), "store_counts": (7, 0, 1)}, "rejected"),
        ({"store_counts": (7, 1, 0)}, "counts disagree"),
        ({"raw_payload_count": 0}, "raw archive is empty"),
        ({"samples": (_sample(0),)}, "fewer than two"),
        (
            {
                "ended_at": NOW + timedelta(minutes=3),
                "samples": (_sample(0), _sample(1, seconds=121)),
            },
            "cadence",
        ),
        ({"samples": (_sample(0), _sample(1, rss=1_001))}, "memory"),
        ({"samples": (_sample(0), _sample(1, disk=2_001))}, "storage"),
    ],
)
def test_t2_preserves_each_failed_invariant(overrides: dict[str, object], reason: str) -> None:
    result = assess_stability_scenario(_evidence(**overrides))
    assert result.status is TechnicalScenarioStatus.FAILED
    assert reason in (result.reason or "")


def test_t2_refuses_noncontiguous_or_out_of_order_samples() -> None:
    with pytest.raises(ValidationError):
        _evidence(samples=(_sample(0), _sample(2)))
    with pytest.raises(ValidationError):
        _evidence(samples=(_sample(0), _sample(1, seconds=0)))
