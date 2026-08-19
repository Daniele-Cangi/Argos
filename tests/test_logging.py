import io

import orjson
import pytest
import structlog

from argos.errors import IngestionError, RejectionReason, SourceTimeoutError
from argos.logging import configure_logging, get_logger


def _emit(level: str, action: str) -> list[dict[str, object]]:
    stream = io.StringIO()
    configure_logging(level, stream=stream)
    logger = get_logger("test")
    if action == "info":
        logger.info("captured", market_id="0x01")
    else:
        logger.debug("verbose")
    lines = [line for line in stream.getvalue().splitlines() if line]
    return [orjson.loads(line) for line in lines]


def test_log_lines_are_json_with_level_and_utc_timestamp() -> None:
    (record,) = _emit("INFO", "info")
    assert record["event"] == "captured"
    assert record["level"] == "info"
    assert record["market_id"] == "0x01"
    assert str(record["timestamp"]).endswith("Z")


def test_level_filtering_drops_lower_levels() -> None:
    assert _emit("INFO", "debug") == []
    assert _emit("DEBUG", "debug") != []


def test_argos_errors_are_logged_as_structured_fields() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    get_logger("test").warning(
        "rejected",
        error=SourceTimeoutError("timeout", reason=RejectionReason.LATE_EVENT.value),
    )
    record = orjson.loads(stream.getvalue().strip())
    assert record["error_code"] == "argos.source_timeout"
    assert record["error_context"] == {"reason": "late_event"}


def test_unknown_level_is_rejected() -> None:
    with pytest.raises(ValueError):
        configure_logging("LOUD")


def _capture() -> io.StringIO:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    return stream


def _records(stream: io.StringIO) -> list[dict[str, object]]:
    return [orjson.loads(line) for line in stream.getvalue().splitlines() if line]


# --- correlation context ----------------------------------------------------------


def test_bound_context_is_attached_to_every_line_until_cleared() -> None:
    """Run and market correlation is what makes a log line countable evidence."""
    stream = _capture()
    logger = get_logger("test")
    structlog.contextvars.bind_contextvars(run_id="run-1", market_id="0x01")
    logger.info("first")
    logger.info("second")
    structlog.contextvars.clear_contextvars()
    logger.info("third")

    first, second, third = _records(stream)
    assert first["run_id"] == second["run_id"] == "run-1"
    assert first["market_id"] == "0x01"
    assert "run_id" not in third


def test_an_explicit_keyword_is_not_overwritten_by_bound_context() -> None:
    stream = _capture()
    structlog.contextvars.bind_contextvars(market_id="0x01")
    get_logger("test").info("captured", market_id="0x02")
    (record,) = _records(stream)
    assert record["market_id"] == "0x02"


# --- exception rendering ----------------------------------------------------------


def test_an_unexpected_exception_is_rendered_as_a_traceback_field() -> None:
    stream = _capture()
    try:
        raise RuntimeError("upstream blew up")
    except RuntimeError:
        get_logger("test").exception("capture failed")

    (record,) = _records(stream)
    assert record["level"] == "error"
    assert "RuntimeError: upstream blew up" in str(record["exception"])
    assert "Traceback" in str(record["exception"])


def test_a_traceback_never_splits_a_log_line() -> None:
    """One record per line is a hard requirement for downstream counting."""
    stream = _capture()
    try:
        raise RuntimeError("multi\nline\nfailure")
    except RuntimeError:
        get_logger("test").error("rejected", exc_info=True)

    lines = [line for line in stream.getvalue().splitlines() if line]
    assert len(lines) == 1
    assert "\n" in str(orjson.loads(lines[0])["exception"])


# --- error taxonomy in logs -------------------------------------------------------


def test_a_rejection_reason_survives_into_the_log_line() -> None:
    stream = _capture()
    error = IngestionError("duplicate book update", reason=RejectionReason.DUPLICATE_EVENT)
    get_logger("test").warning("rejected", error=error)

    (record,) = _records(stream)
    assert record["error_code"] == "argos.ingestion"
    assert record["error"] == "duplicate book update"
    assert record["error_context"] == {"reason": "duplicate_event"}


def test_logging_an_error_does_not_mutate_the_error() -> None:
    stream = _capture()
    error = SourceTimeoutError("timeout", endpoint="/markets")
    get_logger("test").warning("rejected", error=error)
    get_logger("test").warning("rejected", error=error)

    assert error.context == {"endpoint": "/markets"}
    assert error.as_record()["message"] == "timeout"
    assert len(_records(stream)) == 2


def test_a_non_argos_error_value_is_left_untouched() -> None:
    stream = _capture()
    get_logger("test").warning("rejected", error="plain string")
    get_logger("test").warning("rejected", error=ValueError("foreign"))

    plain, foreign = _records(stream)
    assert plain["error"] == "plain string"
    assert "error_code" not in plain
    assert "foreign" in str(foreign["error"])
    assert "error_code" not in foreign


# --- canonical output -------------------------------------------------------------


def test_log_keys_are_emitted_in_sorted_order() -> None:
    """Deterministic key order keeps golden log comparisons meaningful."""
    stream = _capture()
    get_logger("test").info("captured", zeta=1, alpha=2, market_id="0x01")
    (line,) = [line for line in stream.getvalue().splitlines() if line]
    keys = list(orjson.loads(line).keys())
    assert keys == sorted(keys)


def test_a_value_json_cannot_encode_is_stringified_rather_than_dropped() -> None:
    stream = _capture()
    get_logger("test").info("captured", cursor=object(), path=__import__("pathlib").Path("/tmp/x"))
    (record,) = _records(stream)
    assert "object object" in str(record["cursor"])
    assert record["path"] == str(__import__("pathlib").Path("/tmp/x"))


def test_reconfiguring_the_level_takes_effect_immediately() -> None:
    """configure_logging is called once per process today; it must stay re-entrant."""
    first = _capture()
    get_logger("test").debug("verbose")
    assert len(_records(first)) == 1

    second = io.StringIO()
    configure_logging("WARNING", stream=second)
    logger = get_logger("test")
    logger.debug("verbose")
    logger.info("captured")
    logger.warning("degraded")
    assert [record["event"] for record in _records(second)] == ["degraded"]


@pytest.mark.parametrize("level", ["debug", "Info", "WARNING", "error", "critical"])
def test_level_names_are_case_insensitive(level: str) -> None:
    configure_logging(level, stream=io.StringIO())
