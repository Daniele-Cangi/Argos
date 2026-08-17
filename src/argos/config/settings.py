"""Immutable, execution-disabled settings.

Core invariant 13: configuration is part of the experiment. Settings are frozen
once loaded and expose a stable :meth:`Settings.fingerprint` that run manifests
record, so any output can be traced back to the configuration that produced it.

**What the fingerprint covers is itself a decision** (:class:`FingerprintScope`,
and ``docs/DECISION_LOG.md`` 2026-08-17). It covers configuration that can
change *what a run produces*, and deliberately not configuration that only
changes *where the output goes* or *how much the operator is told*.
``docs/02_ARCHITECTURE.md`` names output storage location as a component that
legitimately differs between live and replay, and
``docs/04_DATA_CONTRACTS.md`` requires that "repeated replay of identical
input, code, config, and mode must produce identical state hash" — so a
fingerprint that changed when a replay wrote to a different directory would
make two runs of one experiment look like two experiments. The full
configuration is still recorded verbatim in
``RunManifest.settings_snapshot``; nothing is dropped, only the *hash* is
scoped.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import orjson
from pydantic import Field, ValidationError, field_validator, model_validator

# `FieldInfo` is imported for the fingerprint-scope lookup below rather than for
# typing convenience: reading `json_schema_extra` off an untyped mapping is
# exactly where an unclassified field would slip through unnoticed.
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, SettingsConfigDict

from argos.errors import ConfigurationError, ExecutionProhibitedError

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

ENV_PREFIX = "ARGOS_"

FINGERPRINT_SCOPE_KEY = "argos_fingerprint_scope"
"""Key under which every field declares its :class:`FingerprintScope`.

Carried in pydantic's ``json_schema_extra`` rather than in a module-level set of
field names, so the classification travels with the field it describes. A set
kept beside the model is a second place that has to be edited, and the failure
mode of forgetting is silent inclusion of something that should not be hashed —
which is the defect this whole mechanism exists to remove.
"""


class FingerprintScope(StrEnum):
    """Whether a setting can change what a run produces.

    Every field on :class:`Settings` declares one. There is no default, and
    ``tests/test_boundaries.py`` fails when a field does not declare one, so
    adding a setting forces the question to be answered rather than answered by
    omission.
    """

    EXPERIMENT = "experiment"
    """Can change what ARGOS obtains, accepts, or normalizes — therefore inside
    the fingerprint. Endpoints decide which data is fetched at all; the timeout,
    attempt budget and jitter seed decide which retries happen and therefore
    which frames a capture contains (ADR-0009 makes the seed configuration
    precisely so a run stays reproducible); the execution flag is inside the
    hash so that the prohibition every run asserts is part of what the run
    recorded about itself, not a claim beside it."""

    ENVIRONMENT = "environment"
    """Changes where output goes or how much the operator is told, and can
    change no record — therefore outside the fingerprint, while still recorded
    verbatim in ``settings_snapshot``. Two runs differing only here are the same
    experiment, and a hash that disagreed would be asserting otherwise."""


_EXPERIMENT: dict[str, Any] = {FINGERPRINT_SCOPE_KEY: FingerprintScope.EXPERIMENT.value}
_ENVIRONMENT: dict[str, Any] = {FINGERPRINT_SCOPE_KEY: FingerprintScope.ENVIRONMENT.value}


# docs/09_SECURITY.md: reject non-HTTPS endpoints. The integrity of the payloads
# is the product, so a mistyped or hostile base URL must not silently downgrade
# the transport that carries them.
_REST_SCHEMES = ("https://",)
_WS_SCHEMES = ("wss://",)


class Settings(BaseSettings):
    """Read-only research configuration, loaded from ``ARGOS_*`` variables."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        frozen=True,
        extra="forbid",
        case_sensitive=False,
    )

    gamma_base_url: str = Field(
        default="https://gamma-api.polymarket.com", json_schema_extra=_EXPERIMENT
    )
    data_base_url: str = Field(
        default="https://data-api.polymarket.com", json_schema_extra=_EXPERIMENT
    )
    clob_base_url: str = Field(default="https://clob.polymarket.com", json_schema_extra=_EXPERIMENT)
    clob_market_ws_url: str = Field(
        default="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        json_schema_extra=_EXPERIMENT,
    )

    data_dir: Path = Field(default=Path(".data"), json_schema_extra=_ENVIRONMENT)
    """Where output goes. Outside the fingerprint (:class:`FingerprintScope`):
    `docs/02_ARCHITECTURE.md` lists output storage location among the components
    that may differ between live and replay, so hashing it would make one
    experiment replayed into two directories look like two experiments."""

    log_level: LogLevel = Field(default="INFO", json_schema_extra=_ENVIRONMENT)
    """Outside the fingerprint: `configure_logging` passes it to structlog's
    level filter and nothing else reads it, so it changes how much an operator
    is told and cannot change a record. Verified rather than assumed —
    `argos.logging` is the only consumer."""

    # Ceilings matter as much as floors: docs/09_SECURITY.md requires capped retry
    # rates, and an unbounded attempt count turns a transient 5xx from a public
    # endpoint into a self-inflicted request storm.
    http_timeout_seconds: float = Field(default=10.0, gt=0, le=120, json_schema_extra=_EXPERIMENT)
    http_max_attempts: int = Field(default=5, ge=1, le=10, json_schema_extra=_EXPERIMENT)

    # ADR-0009: retry-backoff jitter is drawn from an adapter-owned
    # `random.Random`, never from the `random` module's shared global state, so
    # one market's backoff never depends on how many siblings retried before it
    # in the same capture loop. The seed is configuration, not a runtime
    # accident, so it is recorded in the run manifest alongside everything else
    # that can change a capture's output.
    source_jitter_seed: int = Field(default=0, ge=0, le=2**32 - 1, json_schema_extra=_EXPERIMENT)

    execution_enabled: bool = Field(default=False, json_schema_extra=_EXPERIMENT)
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

    def experiment_snapshot(self) -> dict[str, Any]:
        """Return only the settings that can change what a run produces.

        The complement of this view is not discarded: :meth:`snapshot` still
        returns everything and is what a manifest stores. This is the subset the
        fingerprint hashes, and it is derived from each field's declared
        :class:`FingerprintScope` rather than from a list of names kept
        elsewhere.

        Because the excluded fields are *absent* from the hashed mapping rather
        than blanked in it, reclassifying a field from ``ENVIRONMENT`` to
        ``EXPERIMENT`` changes the fingerprint — which is correct. A
        reclassification is a change to what "the same configuration" means, and
        it should not be invisible.
        """
        return {
            name: value
            for name, value in self.snapshot().items()
            if fingerprint_scope(name) is FingerprintScope.EXPERIMENT
        }

    def fingerprint(self) -> str:
        """Return a stable SHA-256 over the experiment-scoped configuration.

        **Narrower than it was before 2026-08-17**, when it covered every field
        including ``data_dir``. The reasoning is in the module docstring and in
        ``docs/DECISION_LOG.md``; the consequence worth stating here is that a
        fingerprint recorded by an earlier build is **not comparable** to one
        recorded now. ``RunManifest`` is bumped to ``run_manifest.v4`` so that
        incomparability is mechanical rather than a footnote — unlike the v1→v2
        and v2→v3 bumps, manifests carrying the older meaning really were
        written, by the three live captures of 2026-08-15.
        """
        canonical = orjson.dumps(self.experiment_snapshot(), option=orjson.OPT_SORT_KEYS)
        return hashlib.sha256(canonical).hexdigest()


def fingerprint_scope(field_name: str) -> FingerprintScope:
    """Return the declared :class:`FingerprintScope` of one settings field.

    Raises :class:`ConfigurationError` when a field declares none. Failing
    closed matters more than it looks: the alternative default would have to be
    either "hash it" (which silently re-creates the ``data_dir`` defect for the
    next field somebody adds) or "do not hash it" (which silently drops a real
    experiment variable out of the record that exists to identify the
    experiment). Neither default is safe, so there is none.
    """
    field: FieldInfo | None = Settings.model_fields.get(field_name)
    if field is None:
        raise ConfigurationError("no such settings field", field=field_name)
    extra = field.json_schema_extra
    declared = extra.get(FINGERPRINT_SCOPE_KEY) if isinstance(extra, Mapping) else None
    if not isinstance(declared, str):
        raise ConfigurationError(
            "settings field declares no fingerprint scope; every field must say "
            "whether it can change what a run produces",
            field=field_name,
        )
    return FingerprintScope(declared)


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
    except ValidationError as exc:
        # Only field names and error types: pydantic embeds the offending value in
        # its message, and an operator who pastes a credential into the wrong
        # ARGOS_* variable must not see it echoed into stderr or CI logs
        # (docs/09_SECURITY.md).
        raise ConfigurationError(
            "invalid ARGOS configuration",
            problems=[
                {"field": ".".join(str(part) for part in error["loc"]), "error": error["type"]}
                for error in exc.errors(include_url=False)
            ],
        ) from exc
    except Exception as exc:
        raise ConfigurationError("invalid ARGOS configuration", detail=type(exc).__name__) from exc
