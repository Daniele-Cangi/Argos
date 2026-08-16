import pytest

from argos.errors import (
    ArgosError,
    ConfigurationError,
    ExecutionProhibitedError,
    IngestionError,
    RejectionReason,
    SourceTimeoutError,
)


def _all_error_classes(root: type[ArgosError] = ArgosError) -> list[type[ArgosError]]:
    found = [root]
    for subclass in root.__subclasses__():
        found.extend(_all_error_classes(subclass))
    return found


def test_every_error_code_is_unique() -> None:
    codes = [cls.code for cls in _all_error_classes()]
    assert len(codes) == len(set(codes))


def test_every_error_code_is_namespaced() -> None:
    assert all(cls.code.startswith("argos.") for cls in _all_error_classes())


def test_context_is_preserved_for_counting() -> None:
    error = SourceTimeoutError("gamma timed out", endpoint="/markets", attempt=3)
    assert error.as_record() == {
        "error_code": "argos.source_timeout",
        "message": "gamma timed out",
        "context": {"endpoint": "/markets", "attempt": 3},
    }


def test_rejected_input_always_carries_a_reason() -> None:
    with pytest.raises(TypeError):
        IngestionError("dropped")  # type: ignore[call-arg]

    error = IngestionError("duplicate book update", reason=RejectionReason.DUPLICATE_EVENT)
    assert error.reason is RejectionReason.DUPLICATE_EVENT
    assert error.as_record()["context"] == {"reason": "duplicate_event"}


def test_execution_prohibited_is_a_configuration_failure() -> None:
    assert issubclass(ExecutionProhibitedError, ArgosError)


def test_execution_prohibited_is_caught_by_a_configuration_handler() -> None:
    """Callers that guard configuration loading must not miss the execution refusal."""
    assert issubclass(ExecutionProhibitedError, ConfigurationError)
    with pytest.raises(ConfigurationError):
        raise ExecutionProhibitedError("execution is out of scope")


def test_as_record_hands_out_a_copy_rather_than_the_live_context() -> None:
    """A ledger writer that mutated its record must not rewrite the error itself."""
    error = SourceTimeoutError("gamma timed out", endpoint="/markets")
    record = error.as_record()
    record["context"]["endpoint"] = "/tampered"  # type: ignore[index]
    record["message"] = "tampered"
    assert error.context == {"endpoint": "/markets"}
    assert error.as_record()["context"] == {"endpoint": "/markets"}


def test_the_rejection_vocabulary_is_stable_and_machine_readable() -> None:
    """Counters key off these strings; renaming one silently rewrites history."""
    values = {reason.value for reason in RejectionReason}
    assert values == {
        "malformed_payload",
        "unknown_event_type",
        "schema_version_mismatch",
        "invalid_timestamp",
        "duplicate_event",
        "late_event",
        "out_of_order_event",
        "backpressure",
        "quarantined_mapping",
        "ambiguous_contract",
    }
    assert all(value == value.lower() and " " not in value for value in values)


@pytest.mark.parametrize(
    "required",
    [
        RejectionReason.DUPLICATE_EVENT,
        RejectionReason.LATE_EVENT,
        RejectionReason.OUT_OF_ORDER_EVENT,
        RejectionReason.BACKPRESSURE,
        RejectionReason.MALFORMED_PAYLOAD,
        RejectionReason.QUARANTINED_MAPPING,
    ],
)
def test_every_required_failure_case_has_a_reason_to_record(required: RejectionReason) -> None:
    """docs/13_TEST_STRATEGY.md lists these as mandatory failure cases for M1-M3."""
    error = IngestionError("rejected", reason=required)
    assert error.as_record()["context"] == {"reason": required.value}
    assert isinstance(error.as_record()["context"]["reason"], str)  # type: ignore[index]


def test_a_message_is_never_lost_between_raise_and_record() -> None:
    error = SourceTimeoutError("gamma timed out")
    assert str(error) == "gamma timed out"
    assert error.as_record() == {
        "error_code": "argos.source_timeout",
        "message": "gamma timed out",
        "context": {},
    }
