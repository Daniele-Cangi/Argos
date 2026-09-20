from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from argos.evaluation.technical_campaign import TechnicalScenario
from argos.monitoring.faults import (
    TERMINAL_STATE_MATRIX_V1,
    DeterministicFaultAdapter,
    DeterministicFaultScheduleV1,
    InjectedNetworkLoss,
    InjectedProcessTermination,
    InjectedStorageRefusal,
    TechnicalTerminalState,
    TerminalStateFixtureAdapter,
)
from argos.monitoring.resumable import (
    PollCommit,
    ResumableMonitor,
    ResumableMonitorCheckpointV1,
)

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)


def _fault_schedule(
    scenario: TechnicalScenario,
    attempts: tuple[int, ...] = (1,),
) -> DeterministicFaultScheduleV1:
    return DeterministicFaultScheduleV1(
        schedule_id=f"schedule-{scenario.value.lower()}",
        scenario=scenario,
        activation_attempts=attempts,
    )


def _terminal_schedule() -> DeterministicFaultScheduleV1:
    return DeterministicFaultScheduleV1(
        schedule_id="schedule-terminal-matrix-v1",
        scenario=TechnicalScenario.TERMINAL_SIMULATION,
        terminal_states=TERMINAL_STATE_MATRIX_V1,
    )


@pytest.mark.parametrize(
    ("scenario", "error"),
    [
        (TechnicalScenario.NETWORK_INTERRUPTION, InjectedNetworkLoss),
        (TechnicalScenario.PROCESS_INTERRUPTION, InjectedProcessTermination),
        (TechnicalScenario.STORAGE_INTERRUPTION, InjectedStorageRefusal),
    ],
)
def test_fault_occurs_at_exact_attempt_without_calling_operation(
    scenario: TechnicalScenario, error: type[RuntimeError]
) -> None:
    adapter = DeterministicFaultAdapter(_fault_schedule(scenario))
    calls: list[int] = []

    assert adapter.invoke(lambda: calls.append(0) or "first") == "first"
    with pytest.raises(error, match="attempt 1"):
        adapter.invoke(lambda: calls.append(1) or "fabricated")
    assert adapter.invoke(lambda: calls.append(2) or "third") == "third"
    assert calls == [0, 2]
    assert adapter.next_attempt == 3


def test_two_adapters_do_not_share_attempt_state() -> None:
    schedule = _fault_schedule(TechnicalScenario.NETWORK_INTERRUPTION, (0,))
    first = DeterministicFaultAdapter(schedule)
    second = DeterministicFaultAdapter(schedule)
    with pytest.raises(InjectedNetworkLoss):
        first.invoke(lambda: None)
    with pytest.raises(InjectedNetworkLoss):
        second.invoke(lambda: None)
    assert first.next_attempt == second.next_attempt == 1


@pytest.mark.parametrize(
    "attempts",
    [(-1,), (2, 1), (1, 1)],
)
def test_activation_attempts_must_be_nonnegative_unique_and_ordered(
    attempts: tuple[int, ...],
) -> None:
    with pytest.raises(ValidationError):
        _fault_schedule(TechnicalScenario.NETWORK_INTERRUPTION, attempts)


def test_fault_schedule_requires_at_least_one_activation() -> None:
    with pytest.raises(ValidationError, match="requires activation attempts"):
        _fault_schedule(TechnicalScenario.STORAGE_INTERRUPTION, ())


def test_nonterminal_schedule_cannot_smuggle_terminal_states() -> None:
    with pytest.raises(ValidationError, match="cannot carry terminal states"):
        DeterministicFaultScheduleV1(
            schedule_id="bad",
            scenario=TechnicalScenario.NETWORK_INTERRUPTION,
            activation_attempts=(0,),
            terminal_states=(TechnicalTerminalState.UNKNOWN,),
        )


def test_terminal_schedule_must_cover_exact_complete_matrix() -> None:
    with pytest.raises(ValidationError, match="complete state matrix"):
        DeterministicFaultScheduleV1(
            schedule_id="incomplete",
            scenario=TechnicalScenario.TERMINAL_SIMULATION,
            terminal_states=(TechnicalTerminalState.UNKNOWN,),
        )
    with pytest.raises(ValidationError, match="cannot carry fault attempts"):
        DeterministicFaultScheduleV1(
            schedule_id="mixed",
            scenario=TechnicalScenario.TERMINAL_SIMULATION,
            activation_attempts=(0,),
            terminal_states=TERMINAL_STATE_MATRIX_V1,
        )


def test_unrelated_scenario_is_refused() -> None:
    with pytest.raises(ValidationError, match="limited to T4--T7"):
        DeterministicFaultScheduleV1(
            schedule_id="wrong",
            scenario=TechnicalScenario.FUNCTIONAL,
        )


def test_adapter_types_cannot_be_mixed() -> None:
    with pytest.raises(ValueError, match="T4, T5, or T6"):
        DeterministicFaultAdapter(_terminal_schedule())
    with pytest.raises(ValueError, match="requires a T7"):
        TerminalStateFixtureAdapter(_fault_schedule(TechnicalScenario.PROCESS_INTERRUPTION))


def test_terminal_fixture_yields_every_state_once_in_frozen_order() -> None:
    adapter = TerminalStateFixtureAdapter(_terminal_schedule())
    observed = tuple(adapter.read_next() for _ in TERMINAL_STATE_MATRIX_V1)
    assert observed == TERMINAL_STATE_MATRIX_V1
    with pytest.raises(StopIteration, match="exhausted"):
        adapter.read_next()


def test_schedule_round_trip_preserves_the_exact_script() -> None:
    schedule = _fault_schedule(TechnicalScenario.NETWORK_INTERRUPTION, (0, 2, 5))
    assert DeterministicFaultScheduleV1.from_record(schedule.to_record()) == schedule


def test_blank_schedule_identity_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        DeterministicFaultScheduleV1(
            schedule_id=" ",
            scenario=TechnicalScenario.NETWORK_INTERRUPTION,
            activation_attempts=(0,),
        )


@pytest.mark.parametrize(
    ("scenario", "error"),
    [
        (TechnicalScenario.NETWORK_INTERRUPTION, InjectedNetworkLoss),
        (TechnicalScenario.PROCESS_INTERRUPTION, InjectedProcessTermination),
        (TechnicalScenario.STORAGE_INTERRUPTION, InjectedStorageRefusal),
    ],
)
def test_monitor_records_gap_then_resumes_same_ordinal_after_fault(
    tmp_path: Path,
    scenario: TechnicalScenario,
    error: type[RuntimeError],
) -> None:
    monitor = ResumableMonitor(tmp_path / "checkpoint.json", tmp_path / "monitor.lock")
    monitor.save(
        ResumableMonitorCheckpointV1(
            campaign_id="fault-campaign-v1",
            configuration_sha256="a" * 64,
            next_ordinal=0,
            updated_at=NOW,
        )
    )
    adapter = DeterministicFaultAdapter(_fault_schedule(scenario, (0,)))
    failure_times = iter((NOW, NOW + timedelta(seconds=1)))

    def poll(checkpoint: ResumableMonitorCheckpointV1) -> PollCommit:
        return adapter.invoke(
            lambda: PollCommit(
                ordinal=checkpoint.next_ordinal,
                receipt_id="receipt-0",
                record_sha256="b" * 64,
                persisted_at=NOW + timedelta(seconds=2),
                previous_receipt_id=checkpoint.last_receipt_id,
            )
        )

    with pytest.raises(error):
        monitor.run_once(poll=poll, failure_time=lambda: next(failure_times))
    failed = monitor.load()
    assert failed.next_ordinal == 0
    assert failed.last_receipt_id is None
    assert failed.gaps[0].attempted_ordinal == 0

    resumed = monitor.run_once(
        poll=poll,
        failure_time=lambda: NOW + timedelta(seconds=2),
    )
    assert resumed.next_ordinal == 1
    assert resumed.last_receipt_id == "receipt-0"
    assert len(resumed.gaps) == 1
