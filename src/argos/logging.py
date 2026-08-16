"""Structured logging.

Logs are evidence, so every line is JSON with a UTC timestamp and an explicit
level. Failures are logged through :meth:`argos.errors.ArgosError.as_record` so
that error codes and rejection reasons stay countable.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import orjson
import structlog
from structlog.typing import EventDict, FilteringBoundLogger, WrappedLogger

from argos.errors import ArgosError


def _render_json(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> str:
    return orjson.dumps(event_dict, default=str, option=orjson.OPT_SORT_KEYS).decode()


def _unpack_argos_error(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
    """Expand an ``error=ArgosError(...)`` keyword into its structured fields."""
    error = event_dict.get("error")
    if isinstance(error, ArgosError):
        record = error.as_record()
        event_dict["error"] = record["message"]
        event_dict["error_code"] = record["error_code"]
        event_dict["error_context"] = record["context"]
    return event_dict


def configure_logging(level: str = "INFO", *, stream: Any = None) -> None:
    """Configure process-wide structured logging. Safe to call more than once."""
    numeric_level = logging.getLevelNamesMapping().get(level.upper())
    if numeric_level is None:
        raise ValueError(f"unknown log level: {level!r}")

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _unpack_argos_error,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _render_json,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stderr),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> FilteringBoundLogger:
    """Return a bound logger for ``name``."""
    logger: FilteringBoundLogger = structlog.get_logger(name)
    return logger
