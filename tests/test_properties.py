"""Property tests.

docs/13_TEST_STRATEGY.md asks for Hypothesis coverage of serialization round trips
and boundary validation. The example-based suites pin the cases we thought of; these
search for the ones we did not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import orjson
import pytest
from hypothesis import given
from hypothesis import strategies as st

from argos.clock import ReplayClock, ensure_utc
from argos.config import RunManifest, Settings
from argos.config.settings import ENV_PREFIX, unknown_environment_keys
from argos.errors import ClockRegressionError, NaiveDatetimeError

# Timestamps outside this window cannot be produced by a Polymarket capture and only
# exercise datetime's own overflow behaviour at the type boundary.
EARLIEST = datetime(1970, 1, 1)
LATEST = datetime(2100, 1, 1)

SAFE_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")), min_size=1, max_size=40
)

AWARE_MOMENTS = st.datetimes(
    min_value=EARLIEST, max_value=LATEST, timezones=st.timezones(), allow_imaginary=False
)
UTC_MOMENTS = st.datetimes(min_value=EARLIEST, max_value=LATEST, timezones=st.just(UTC))
DURATIONS = st.floats(min_value=0.0, max_value=86_400.0, allow_nan=False, allow_infinity=False)


# --- ensure_utc -------------------------------------------------------------------


@given(moment=AWARE_MOMENTS)
def test_ensure_utc_preserves_the_instant_for_any_timezone(moment: datetime) -> None:
    converted = ensure_utc(moment)
    assert converted == moment
    assert converted.tzinfo is UTC
    assert converted.utcoffset() == timedelta(0)


@given(moment=AWARE_MOMENTS)
def test_ensure_utc_is_idempotent(moment: datetime) -> None:
    once = ensure_utc(moment)
    assert ensure_utc(once) == once
    assert ensure_utc(once).isoformat() == once.isoformat()


@given(moment=st.datetimes(min_value=EARLIEST, max_value=LATEST))
def test_ensure_utc_never_guesses_a_timezone(moment: datetime) -> None:
    with pytest.raises(NaiveDatetimeError) as caught:
        ensure_utc(moment)
    assert caught.value.context["value"] == moment.isoformat()


# --- replay clock -----------------------------------------------------------------


@given(start=UTC_MOMENTS, steps=st.lists(DURATIONS, max_size=25))
def test_replay_time_never_goes_backwards_and_is_reproducible(
    start: datetime, steps: list[float]
) -> None:
    def run() -> list[datetime]:
        clock = ReplayClock(start)
        stamps = [clock.now()]
        for step in steps:
            clock.advance_by(step)
            stamps.append(clock.now())
        return stamps

    stamps = run()
    assert stamps == sorted(stamps)
    assert stamps[0] == start
    assert stamps[-1] >= start
    assert run() == stamps


@given(start=UTC_MOMENTS, target=AWARE_MOMENTS)
def test_advance_to_either_moves_forward_or_refuses_without_side_effects(
    start: datetime, target: datetime
) -> None:
    clock = ReplayClock(start)
    if target >= start:
        clock.advance_to(target)
        assert clock.now() == target
        assert clock.now().tzinfo is UTC
    else:
        with pytest.raises(ClockRegressionError):
            clock.advance_to(target)
        assert clock.now() == start


@given(start=UTC_MOMENTS, steps=st.lists(DURATIONS, min_size=1, max_size=10))
def test_the_same_durations_in_any_order_reach_the_same_instant(
    start: datetime, steps: list[float]
) -> None:
    """Replay time must not depend on the order the scheduler happens to emit gaps in."""

    def total(order: list[float]) -> datetime:
        clock = ReplayClock(start)
        for step in order:
            clock.advance_by(step)
        return clock.now()

    assert total(steps) == total(list(reversed(steps)))


# --- record round trips -----------------------------------------------------------

# A snapshot is JSON on the wire, so integers stay inside the range JSON encoders
# accept; `settings_snapshot: dict[str, Any]` does not enforce that itself.
JSON_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**63), max_value=2**63 - 1),
    SAFE_TEXT,
)

MANIFESTS = st.builds(
    RunManifest,
    run_id=SAFE_TEXT,
    mode=SAFE_TEXT,
    created_at=UTC_MOMENTS,
    argos_version=SAFE_TEXT,
    code_revision=st.one_of(st.none(), SAFE_TEXT),
    config_fingerprint=SAFE_TEXT,
    settings_snapshot=st.dictionaries(SAFE_TEXT, JSON_SCALARS, max_size=6),
)


@given(manifest=MANIFESTS)
def test_a_record_survives_a_serialization_round_trip(manifest: RunManifest) -> None:
    record = manifest.to_record()
    assert RunManifest.from_record(record) == manifest
    assert record["schema_version"] == "run_manifest.v1"


@given(manifest=MANIFESTS)
def test_serialization_is_canonical_and_stable(manifest: RunManifest) -> None:
    encode = lambda value: orjson.dumps(value, option=orjson.OPT_SORT_KEYS)  # noqa: E731
    once = encode(manifest.to_record())
    twice = encode(RunManifest.from_record(orjson.loads(once)).to_record())
    assert once == twice


@given(manifest=MANIFESTS, foreign=SAFE_TEXT)
def test_a_foreign_schema_version_is_never_accepted(manifest: RunManifest, foreign: str) -> None:
    from argos.errors import SchemaVersionError

    record = manifest.to_record()
    record["schema_version"] = foreign
    if foreign == RunManifest.schema_version:
        assert RunManifest.from_record(record) == manifest
    else:
        with pytest.raises(SchemaVersionError):
            RunManifest.from_record(record)


# --- configuration ----------------------------------------------------------------


@given(
    log_level=st.sampled_from(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
    timeout=st.floats(min_value=0.001, max_value=120.0, allow_nan=False, allow_infinity=False),
    # one below the ceiling: the property varies attempts + 1, which must stay legal
    attempts=st.integers(min_value=1, max_value=9),
)
def test_the_fingerprint_identifies_the_configuration_exactly(
    log_level: str, timeout: float, attempts: int
) -> None:
    def build(**overrides: Any) -> Settings:
        base: dict[str, Any] = {
            "log_level": log_level,
            "http_timeout_seconds": timeout,
            "http_max_attempts": attempts,
        }
        base.update(overrides)
        return Settings(**base)

    reference = build()
    assert reference.fingerprint() == build().fingerprint()
    assert len(reference.fingerprint()) == 64
    assert reference.fingerprint() != build(http_max_attempts=attempts + 1).fingerprint()
    assert reference.fingerprint() != build(data_dir="/somewhere/else").fingerprint()


@given(
    attempts=st.one_of(st.integers(max_value=0), st.floats(allow_nan=False), SAFE_TEXT),
)
def test_a_retry_count_below_one_is_never_accepted(attempts: object) -> None:
    from pydantic import ValidationError

    try:
        loaded = Settings(http_max_attempts=attempts)  # type: ignore[arg-type]
    except (ValidationError, ValueError):
        return
    assert loaded.http_max_attempts >= 1


@given(name=SAFE_TEXT.filter(lambda text: "=" not in text and "\x00" not in text))
def test_any_prefixed_variable_is_either_a_known_field_or_reported(name: str) -> None:
    variable = f"{ENV_PREFIX}{name}"
    reported = unknown_environment_keys({variable: "value"})
    is_field = name.upper() in {field.upper() for field in Settings.model_fields}
    assert reported == ([] if is_field else [variable])


@given(field=st.sampled_from(sorted(Settings.model_fields)), upper=st.booleans())
def test_a_known_field_is_recognised_in_any_case(field: str, upper: bool) -> None:
    name = f"{ENV_PREFIX}{field}"
    assert unknown_environment_keys({name.upper() if upper else name.lower(): "value"}) == []
