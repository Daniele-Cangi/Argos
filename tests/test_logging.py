import io

import orjson
import pytest

from argos.errors import RejectionReason, SourceTimeoutError
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
