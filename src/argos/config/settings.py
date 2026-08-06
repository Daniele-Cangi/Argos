"""Immutable, execution-disabled settings.

Core invariant 13: configuration is part of the experiment. Settings are frozen
once loaded and expose a stable :meth:`Settings.fingerprint` that run manifests
record, so any output can be traced back to the configuration that produced it.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import orjson
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from argos.errors import ConfigurationError, ExecutionProhibitedError

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

ENV_PREFIX = "ARGOS_"

_REST_SCHEMES = ("https://", "http://")
_WS_SCHEMES = ("wss://", "ws://")


class Settings(BaseSettings):
    """Read-only research configuration, loaded from ``ARGOS_*`` variables."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        frozen=True,
        extra="forbid",
        case_sensitive=False,
    )

    gamma_base_url: str = "https://gamma-api.polymarket.com"
    data_base_url: str = "https://data-api.polymarket.com"
    clob_base_url: str = "https://clob.polymarket.com"
    clob_market_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    data_dir: Path = Path(".data")
    log_level: LogLevel = "INFO"

    http_timeout_seconds: float = Field(default=10.0, gt=0)
    http_max_attempts: int = Field(default=5, ge=1)

    execution_enabled: bool = False
    """Always false through M4. Present so that enabling it fails loudly (ADR-0007)."""

    @field_validator("gamma_base_url", "data_base_url", "clob_base_url")
    @classmethod
    def _validate_rest_url(cls, value: str) -> str:
        return _validate_url(value, _REST_SCHEMES)

    @field_validator("clob_market_ws_url")
    @classmethod
    def _validate_ws_url(cls, value: str) -> str:
        return _validate_url(value, _WS_SCHEMES)

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _reject_execution(self) -> Settings:
        if self.execution_enabled:
            raise ExecutionProhibitedError(
                "ARGOS_EXECUTION_ENABLED must be false: the research core has no "
                "execution capability and enabling it requires the owner gate",
                milestone_scope="M0-M4",
                adr="ADR-0007",
            )
        return self

    def snapshot(self) -> dict[str, Any]:
        """Return a canonical JSON-compatible view of the configuration.

        No field holds a credential; if one is ever added it must be excluded here
        before this snapshot reaches a manifest or a log line.
        """
        return self.model_dump(mode="json")

    def fingerprint(self) -> str:
        """Return a stable SHA-256 over the configuration snapshot."""
        canonical = orjson.dumps(self.snapshot(), option=orjson.OPT_SORT_KEYS)
        return hashlib.sha256(canonical).hexdigest()


def _validate_url(value: str, schemes: tuple[str, ...]) -> str:
    cleaned = value.strip().rstrip("/")
    if not cleaned.startswith(schemes):
        raise ValueError(f"URL must start with one of {schemes}: {value!r}")
    return cleaned


def unknown_environment_keys(environ: Mapping[str, str]) -> list[str]:
    """Return ``ARGOS_*`` variables that do not map to a settings field.

    pydantic-settings ignores unrecognized prefixed variables. That would let a
    typo (``ARGOS_LOG_LEVL``) silently keep a default, and would let a stray
    credential-looking variable sit in the run environment unnoticed.
    """
    known = {f"{ENV_PREFIX}{name}".upper() for name in Settings.model_fields}
    return sorted(
        key for key in environ if key.upper().startswith(ENV_PREFIX) and key.upper() not in known
    )


def load_settings(**overrides: Any) -> Settings:
    """Load settings from the environment, translating failures into ARGOS errors."""
    unknown = unknown_environment_keys(os.environ)
    if unknown:
        raise ConfigurationError(
            "unrecognized ARGOS_* environment variables; fix the name or remove them",
            variables=unknown,
        )
    try:
        return Settings(**overrides)
    except ExecutionProhibitedError:
        raise
    except Exception as exc:  # pydantic ValidationError and friends
        raise ConfigurationError("invalid ARGOS configuration", detail=str(exc)) from exc
