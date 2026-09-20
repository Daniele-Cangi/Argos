"""Deterministic fault adapters for the bounded T4--T7 campaign.

The adapters never consult a clock, random source, network, process table, or
filesystem.  A versioned schedule completely determines every activation, so a
test can prove recovery behaviour without waiting for a real outage.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import ClassVar, TypeVar

from pydantic import field_validator, model_validator

from argos.domain.versioning import VersionedModel
from argos.evaluation.technical_campaign import TechnicalScenario

__all__ = [
    "TERMINAL_STATE_MATRIX_V1",
    "DeterministicFaultAdapter",
    "DeterministicFaultScheduleV1",
    "InjectedNetworkLoss",
    "InjectedProcessTermination",
    "InjectedStorageRefusal",
    "TechnicalTerminalState",
    "TerminalStateFixtureAdapter",
]

ResultT = TypeVar("ResultT")


class InjectedFault(RuntimeError):
    """Base class for deliberately injected, non-production failures."""

    scenario: TechnicalScenario

    def __init__(self, attempt: int) -> None:
        self.attempt = attempt
        super().__init__(f"{self.scenario.value} injected at attempt {attempt}")


class InjectedNetworkLoss(InjectedFault):
    scenario = TechnicalScenario.NETWORK_INTERRUPTION


class InjectedProcessTermination(InjectedFault):
    """Cooperative unit fixture; T5 crash proof must kill a real subprocess."""

    scenario = TechnicalScenario.PROCESS_INTERRUPTION


class InjectedStorageRefusal(InjectedFault):
    scenario = TechnicalScenario.STORAGE_INTERRUPTION


class TechnicalTerminalState(StrEnum):
    UNKNOWN = "UNKNOWN"
    PROPOSED = "PROPOSED"
    DISPUTED = "DISPUTED"
    FINAL = "FINAL"
    ADMINISTRATIVE_CLOSE = "ADMINISTRATIVE_CLOSE"


TERMINAL_STATE_MATRIX_V1 = (
    TechnicalTerminalState.UNKNOWN,
    TechnicalTerminalState.PROPOSED,
    TechnicalTerminalState.DISPUTED,
    TechnicalTerminalState.FINAL,
    TechnicalTerminalState.ADMINISTRATIVE_CLOSE,
)


class DeterministicFaultScheduleV1(VersionedModel):
    """Complete activation script for one T4--T7 fault scenario."""

    schema_version: ClassVar[str] = "deterministic_fault_schedule.v1"

    schedule_id: str
    scenario: TechnicalScenario
    activation_attempts: tuple[int, ...] = ()
    terminal_states: tuple[TechnicalTerminalState, ...] = ()

    @field_validator("schedule_id")
    @classmethod
    def _schedule_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("fault schedule id must not be blank")
        return value

    @field_validator("activation_attempts")
    @classmethod
    def _attempts(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(attempt < 0 for attempt in value):
            raise ValueError("fault activation attempts must be nonnegative")
        if tuple(sorted(set(value))) != value:
            raise ValueError("fault activation attempts must be unique and ordered")
        return value

    @model_validator(mode="after")
    def _scenario_configuration(self) -> DeterministicFaultScheduleV1:
        fault_scenarios = {
            TechnicalScenario.NETWORK_INTERRUPTION,
            TechnicalScenario.PROCESS_INTERRUPTION,
            TechnicalScenario.STORAGE_INTERRUPTION,
        }
        if self.scenario in fault_scenarios:
            if not self.activation_attempts:
                raise ValueError("an injected fault scenario requires activation attempts")
            if self.terminal_states:
                raise ValueError("nonterminal fault schedule cannot carry terminal states")
            return self
        if self.scenario is TechnicalScenario.TERMINAL_SIMULATION:
            if self.activation_attempts:
                raise ValueError("terminal-state schedule cannot carry fault attempts")
            # Frozen explicitly: adding a future enum member must not invalidate
            # already-persisted v1 schedules.
            expected = TERMINAL_STATE_MATRIX_V1
            if self.terminal_states != expected:
                raise ValueError("terminal-state schedule must cover the complete state matrix")
            return self
        raise ValueError("deterministic fault schedules are limited to T4--T7")


class DeterministicFaultAdapter:
    """Invoke an operation unless its zero-based attempt is scheduled to fail."""

    _errors: ClassVar[dict[TechnicalScenario, type[InjectedFault]]] = {
        TechnicalScenario.NETWORK_INTERRUPTION: InjectedNetworkLoss,
        TechnicalScenario.PROCESS_INTERRUPTION: InjectedProcessTermination,
        TechnicalScenario.STORAGE_INTERRUPTION: InjectedStorageRefusal,
    }

    def __init__(self, schedule: DeterministicFaultScheduleV1) -> None:
        if schedule.scenario not in self._errors:
            raise ValueError("fault adapter requires a T4, T5, or T6 schedule")
        self._schedule = schedule

    def invoke(self, attempt: int, operation: Callable[[], ResultT]) -> ResultT:
        """Apply the frozen schedule to an explicit, externally owned cursor."""

        if attempt < 0:
            raise ValueError("fault attempt must be nonnegative")
        error = self._errors[self._schedule.scenario]
        if attempt in self._schedule.activation_attempts:
            raise error(attempt)
        return operation()


class TerminalStateFixtureAdapter:
    """Yield the declared T7 lifecycle matrix in its exact frozen order."""

    def __init__(self, schedule: DeterministicFaultScheduleV1) -> None:
        if schedule.scenario is not TechnicalScenario.TERMINAL_SIMULATION:
            raise ValueError("terminal adapter requires a T7 schedule")
        self._states = schedule.terminal_states
        self._next_index = 0

    def read_next(self) -> TechnicalTerminalState:
        if self._next_index >= len(self._states):
            raise StopIteration("terminal-state fixture is exhausted")
        state = self._states[self._next_index]
        self._next_index += 1
        return state
