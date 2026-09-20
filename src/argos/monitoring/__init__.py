"""Crash-safe supervision primitives for bounded technical campaigns."""

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
    ExclusiveFileLease,
    MonitorGapV1,
    PollCommit,
    ResumableMonitor,
    ResumableMonitorCheckpointV1,
    checkpoint_after_failure,
    checkpoint_after_poll,
    write_atomic_checkpoint,
)

__all__ = [
    "TERMINAL_STATE_MATRIX_V1",
    "DeterministicFaultAdapter",
    "DeterministicFaultScheduleV1",
    "ExclusiveFileLease",
    "InjectedNetworkLoss",
    "InjectedProcessTermination",
    "InjectedStorageRefusal",
    "MonitorGapV1",
    "PollCommit",
    "ResumableMonitor",
    "ResumableMonitorCheckpointV1",
    "TechnicalTerminalState",
    "TerminalStateFixtureAdapter",
    "checkpoint_after_failure",
    "checkpoint_after_poll",
    "write_atomic_checkpoint",
]
