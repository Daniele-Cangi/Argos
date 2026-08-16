"""ARGOS error taxonomy.

Core invariant 14 states that errors are data. Every failure raised inside ARGOS
carries a stable machine-readable ``code`` plus structured context so that late,
duplicated, malformed, rejected, and missing events can be counted and explained
instead of being silently discarded.

The taxonomy is intentionally shallow and closed: adapters translate foreign
exceptions into these types at the boundary, and counters key off ``code`` and
:class:`RejectionReason` rather than off exception class names.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import ClassVar


class RejectionReason(StrEnum):
    """Reason attached to every input ARGOS refuses to accept.

    Ingestion never drops an event without recording one of these values.
    """

    MALFORMED_PAYLOAD = "malformed_payload"
    UNKNOWN_EVENT_TYPE = "unknown_event_type"
    SCHEMA_VERSION_MISMATCH = "schema_version_mismatch"
    INVALID_TIMESTAMP = "invalid_timestamp"
    DUPLICATE_EVENT = "duplicate_event"
    LATE_EVENT = "late_event"
    OUT_OF_ORDER_EVENT = "out_of_order_event"
    BACKPRESSURE = "backpressure"
    QUARANTINED_MAPPING = "quarantined_mapping"
    AMBIGUOUS_CONTRACT = "ambiguous_contract"


class ArgosError(Exception):
    """Base class for every error ARGOS raises deliberately."""

    code: ClassVar[str] = "argos.error"

    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message)
        self.message = message
        self.context: Mapping[str, object] = dict(context)

    def as_record(self) -> dict[str, object]:
        """Return a log/ledger-ready representation of the failure."""
        return {
            "error_code": self.code,
            "message": self.message,
            "context": dict(self.context),
        }


# --- configuration and contract -------------------------------------------------


class ConfigurationError(ArgosError):
    """Settings, environment, or run manifest are invalid or unsafe."""

    code = "argos.configuration"


class ExecutionProhibitedError(ConfigurationError):
    """Something attempted to enable trading execution inside the research core.

    Execution is outside the autonomous scope through M4 (ADR-0007).
    """

    code = "argos.execution_prohibited"


class ContractViolationError(ArgosError):
    """A domain invariant was violated by internal code."""

    code = "argos.contract_violation"


class SchemaVersionError(ContractViolationError):
    """A persistent record is missing or carries an unsupported schema version."""

    code = "argos.schema_version"


# --- clock ----------------------------------------------------------------------


class ClockError(ArgosError):
    """Base class for clock misuse."""

    code = "argos.clock"


class ClockRegressionError(ClockError):
    """A clock was asked to move backwards."""

    code = "argos.clock_regression"


class NaiveDatetimeError(ClockError):
    """A timestamp without an explicit UTC timezone entered the system."""

    code = "argos.naive_datetime"


class InvalidDurationError(ClockError, ValueError):
    """A duration is negative or not finite.

    Also a :class:`ValueError` so that callers written against the stdlib sleep
    contract keep working, while the failure still carries a countable code.
    """

    code = "argos.invalid_duration"


# --- sources and ingestion ------------------------------------------------------


class SourceError(ArgosError):
    """A public data source failed."""

    code = "argos.source"


class SourceUnavailableError(SourceError):
    """The source could not be reached within its retry policy."""

    code = "argos.source_unavailable"


class SourceTimeoutError(SourceError):
    """The source exceeded its declared timeout."""

    code = "argos.source_timeout"


class SourceProtocolError(SourceError):
    """The source answered with an unusable protocol-level response."""

    code = "argos.source_protocol"


class IngestionError(ArgosError):
    """An observation could not be accepted."""

    code = "argos.ingestion"

    def __init__(self, message: str, *, reason: RejectionReason, **context: object) -> None:
        super().__init__(message, reason=reason.value, **context)
        self.reason = reason


# --- storage and replay ---------------------------------------------------------


class StorageError(ArgosError):
    """The event store or ledger failed."""

    code = "argos.storage"


class ImmutabilityViolationError(StorageError):
    """Code attempted to overwrite or mutate an immutable record.

    Core invariant 7: normalization creates a new versioned representation, it
    never replaces the source payload.
    """

    code = "argos.immutability_violation"


class ReplayError(ArgosError):
    """A replay run could not be reproduced faithfully."""

    code = "argos.replay"


class DeterminismError(ReplayError):
    """Identical input, code, and configuration produced a different result."""

    code = "argos.determinism"
