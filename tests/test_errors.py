import pytest

from argos.errors import (
    ArgosError,
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
