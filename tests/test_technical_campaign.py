from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from argos.evaluation.technical_campaign import (
    TechnicalCampaignV1,
    TechnicalScenario,
    TechnicalScenarioResultV1,
    TechnicalScenarioSpecV1,
    TechnicalScenarioStatus,
)

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)
CAMPAIGN_ID = "technical-campaign-20260920-v1"


def _spec(scenario: TechnicalScenario) -> TechnicalScenarioSpecV1:
    return TechnicalScenarioSpecV1(
        scenario=scenario,
        maximum_duration_seconds=600,
        maximum_frame_count=500,
        expected_outcome="checkpoint and evidence persist",
        fault_injection=None,
    )


def _result(
    scenario: TechnicalScenario,
    status: TechnicalScenarioStatus = TechnicalScenarioStatus.PASSED,
) -> TechnicalScenarioResultV1:
    common = dict(
        campaign_id=CAMPAIGN_ID,
        scenario=scenario,
        status=status,
        observed_frame_count=10,
    )
    if status is TechnicalScenarioStatus.PASSED:
        return TechnicalScenarioResultV1(
            **common,
            started_at=NOW,
            last_checkpoint_at=NOW + timedelta(minutes=9),
            ended_at=NOW + timedelta(minutes=10),
            artifact_identities=(f"sha256:{scenario.value}",),
            observed_outcome="checkpoint and evidence persisted",
        )
    if status is TechnicalScenarioStatus.NOT_RUN:
        common["observed_frame_count"] = 0
        return TechnicalScenarioResultV1(
            **common,
            reason="prerequisite failed",
            follow_up_action="run after prerequisite passes",
        )
    return TechnicalScenarioResultV1(
        **common,
        started_at=NOW,
        last_checkpoint_at=NOW + timedelta(minutes=9),
        ended_at=(NOW + timedelta(minutes=10))
        if status is TechnicalScenarioStatus.FAILED
        else None,
        reason="injected interruption was not recovered",
        follow_up_action="repair and rerun this scenario",
    )


def _campaign(
    results: tuple[TechnicalScenarioResultV1, ...] | None = None,
    specifications: tuple[TechnicalScenarioSpecV1, ...] | None = None,
    **updates: object,
) -> TechnicalCampaignV1:
    actual_results = results or tuple(_result(item) for item in TechnicalScenario)
    statuses = [item.status for item in actual_results]
    values = dict(
        campaign_id=CAMPAIGN_ID,
        created_at=NOW,
        code_revision="a" * 40,
        configuration_sha256="b" * 64,
        input_identities=("fixture:deterministic-v1",),
        specifications=specifications or tuple(_spec(item) for item in TechnicalScenario),
        results=actual_results,
        passed_count=statuses.count(TechnicalScenarioStatus.PASSED),
        failed_count=statuses.count(TechnicalScenarioStatus.FAILED),
        incomplete_count=statuses.count(TechnicalScenarioStatus.INCOMPLETE),
        not_run_count=statuses.count(TechnicalScenarioStatus.NOT_RUN),
    )
    values.update(updates)
    return TechnicalCampaignV1.model_validate(values)


def test_complete_passing_campaign_is_qualified_and_round_trips() -> None:
    campaign = _campaign()
    assert campaign.technically_qualified
    assert TechnicalCampaignV1.from_record(campaign.to_record()) == campaign


@pytest.mark.parametrize("collection", ["specifications", "results"])
def test_a_campaign_cannot_hide_an_omitted_scenario(collection: str) -> None:
    values = tuple(_spec(item) for item in TechnicalScenario)
    if collection == "results":
        values = tuple(_result(item) for item in TechnicalScenario)  # type: ignore[assignment]
    with pytest.raises(ValidationError, match="exactly T1--T8"):
        _campaign(**{collection: values[:-1]})


def test_duplicate_result_is_rejected_even_when_all_scenarios_are_present() -> None:
    results = tuple(_result(item) for item in TechnicalScenario)
    with pytest.raises(ValidationError, match="duplicate scenario results"):
        _campaign(results=(*results, results[0]))


def test_declared_counts_cannot_make_a_failed_campaign_look_green() -> None:
    results = tuple(
        _result(
            item,
            TechnicalScenarioStatus.FAILED
            if item is TechnicalScenario.NETWORK_INTERRUPTION
            else TechnicalScenarioStatus.PASSED,
        )
        for item in TechnicalScenario
    )
    with pytest.raises(ValidationError, match="status counts disagree"):
        _campaign(results=results, passed_count=8, failed_count=0)


def test_passed_requires_persisted_artifact_identity() -> None:
    with pytest.raises(ValidationError, match="PASSED requires artifacts"):
        TechnicalScenarioResultV1(
            campaign_id=CAMPAIGN_ID,
            scenario=TechnicalScenario.FUNCTIONAL,
            status=TechnicalScenarioStatus.PASSED,
            started_at=NOW,
            last_checkpoint_at=NOW,
            ended_at=NOW,
            observed_frame_count=1,
            observed_outcome="looked fine",
        )


def test_not_run_is_explicit_and_cannot_smuggle_execution_evidence() -> None:
    with pytest.raises(ValidationError, match="NOT_RUN cannot carry execution timestamps"):
        TechnicalScenarioResultV1(
            campaign_id=CAMPAIGN_ID,
            scenario=TechnicalScenario.ENDURANCE,
            status=TechnicalScenarioStatus.NOT_RUN,
            started_at=NOW,
            observed_frame_count=0,
            reason="deferred",
            follow_up_action="run later",
        )


def test_incomplete_does_not_claim_a_terminal_end() -> None:
    with pytest.raises(ValidationError, match="INCOMPLETE must not claim"):
        TechnicalScenarioResultV1(
            campaign_id=CAMPAIGN_ID,
            scenario=TechnicalScenario.PROCESS_INTERRUPTION,
            status=TechnicalScenarioStatus.INCOMPLETE,
            started_at=NOW,
            last_checkpoint_at=NOW,
            ended_at=NOW,
            observed_frame_count=1,
            reason="host stopped",
            follow_up_action="resume from checkpoint",
        )


def test_foreign_campaign_result_is_rejected() -> None:
    results = list(_result(item) for item in TechnicalScenario)
    results[0] = results[0].model_copy(update={"campaign_id": "other"})
    with pytest.raises(ValidationError, match="different campaign"):
        _campaign(results=tuple(results))
