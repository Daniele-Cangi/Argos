import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar

import orjson
import pytest
from pydantic import ValidationError

from argos.clock import ReplayClock
from argos.config import RunManifest, Settings, build_run_manifest, load_settings
from argos.config.settings import unknown_environment_keys
from argos.domain.versioning import VersionedModel
from argos.errors import (
    ConfigurationError,
    ExecutionProhibitedError,
    NaiveDatetimeError,
    SchemaVersionError,
)

START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[1]


def _manifest(**overrides: Any) -> RunManifest:
    fields: dict[str, Any] = {
        "settings": Settings(),
        "clock": ReplayClock(START),
        "run_id": "run-1",
        "mode": "replay",
    }
    fields.update(overrides)
    return build_run_manifest(**fields)


def test_defaults_point_at_public_endpoints() -> None:
    settings = Settings()
    assert settings.gamma_base_url.startswith("https://")
    assert settings.clob_market_ws_url.startswith("wss://")
    assert settings.execution_enabled is False


def test_settings_are_immutable() -> None:
    settings = Settings()
    with pytest.raises(ValidationError):
        settings.log_level = "DEBUG"  # type: ignore[misc]


def test_execution_enabled_is_rejected() -> None:
    with pytest.raises(ExecutionProhibitedError) as caught:
        Settings(execution_enabled=True)
    assert caught.value.code == "argos.execution_prohibited"


def test_execution_enabled_is_rejected_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARGOS_EXECUTION_ENABLED", "true")
    with pytest.raises(ExecutionProhibitedError):
        load_settings()


def test_environment_overrides_are_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGOS_LOG_LEVEL", "debug")
    monkeypatch.setenv("ARGOS_DATA_DIR", "/tmp/argos-capture")
    settings = load_settings()
    assert settings.log_level == "DEBUG"
    assert settings.data_dir == Path("/tmp/argos-capture")


def test_invalid_url_scheme_is_reported_as_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARGOS_GAMMA_BASE_URL", "ftp://gamma-api.polymarket.com")
    with pytest.raises(ConfigurationError) as caught:
        load_settings()
    assert caught.value.code == "argos.configuration"


def test_unknown_setting_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGOS_PRIVATE_KEY", "0xdeadbeef")
    with pytest.raises(ConfigurationError):
        load_settings()


def test_fingerprint_is_stable_and_sensitive() -> None:
    baseline = Settings().fingerprint()
    assert baseline == Settings().fingerprint()
    assert baseline != Settings(log_level="DEBUG").fingerprint()


def test_manifest_uses_the_injected_clock_and_is_reproducible() -> None:
    settings = Settings()
    first = build_run_manifest(
        settings=settings, clock=ReplayClock(START), run_id="run-1", mode="replay"
    )
    second = build_run_manifest(
        settings=settings, clock=ReplayClock(START), run_id="run-1", mode="replay"
    )
    assert first.created_at == START
    assert first.to_record() == second.to_record()
    assert first.config_fingerprint == settings.fingerprint()


def test_manifest_round_trips_with_its_schema_version() -> None:
    manifest = build_run_manifest(
        settings=Settings(), clock=ReplayClock(START), run_id="run-1", mode="replay"
    )
    record = manifest.to_record()
    assert record["schema_version"] == "run_manifest.v1"
    assert RunManifest.from_record(record) == manifest


def test_manifest_rejects_a_foreign_schema_version() -> None:
    manifest = build_run_manifest(
        settings=Settings(), clock=ReplayClock(START), run_id="run-1", mode="replay"
    )
    record = manifest.to_record()
    record["schema_version"] = "run_manifest.v2"
    with pytest.raises(SchemaVersionError):
        RunManifest.from_record(record)


def test_manifest_is_immutable() -> None:
    manifest = build_run_manifest(
        settings=Settings(), clock=ReplayClock(START), run_id="run-1", mode="replay"
    )
    with pytest.raises(ValidationError):
        manifest.run_id = "run-2"  # type: ignore[misc]


def test_the_settings_snapshot_cannot_be_edited_after_the_fact() -> None:
    """frozen=True only blocks attribute assignment; a mutable dict field would
    let any holder rewrite recorded provenance and have it reach to_record()."""
    manifest = _manifest()
    with pytest.raises(TypeError):
        manifest.settings_snapshot["log_level"] = "MUTATED"  # type: ignore[index]
    assert manifest.to_record()["settings_snapshot"]["log_level"] == "INFO"


def test_editing_a_serialized_record_does_not_reach_back_into_the_manifest() -> None:
    manifest = _manifest()
    record = manifest.to_record()
    record["settings_snapshot"]["log_level"] = "MUTATED"
    assert manifest.to_record()["settings_snapshot"]["log_level"] == "INFO"


# --- the execution prohibition, spelled every way an operator might spell it ------


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "YES", "on", "t", "y"])
def test_every_truthy_spelling_of_execution_enabled_is_rejected(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """ADR-0007 forbids execution; a case or synonym gap would be a silent bypass."""
    monkeypatch.setenv("ARGOS_EXECUTION_ENABLED", value)
    with pytest.raises(ExecutionProhibitedError) as caught:
        load_settings()
    assert caught.value.code == "argos.execution_prohibited"


@pytest.mark.parametrize("value", ["false", "False", "FALSE", "0", "no", "off", "f", "n"])
def test_falsy_spellings_of_execution_enabled_load_normally(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("ARGOS_EXECUTION_ENABLED", value)
    assert load_settings().execution_enabled is False


def test_a_nonsense_execution_flag_fails_rather_than_defaulting_to_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``maybe`` must not be quietly coerced; an unparseable flag is a config error."""
    monkeypatch.setenv("ARGOS_EXECUTION_ENABLED", "maybe")
    with pytest.raises(ConfigurationError):
        load_settings()


def test_execution_enabled_is_rejected_when_passed_as_an_override() -> None:
    with pytest.raises(ExecutionProhibitedError):
        load_settings(execution_enabled=True)


# --- unknown / misspelled variables ----------------------------------------------


@pytest.mark.parametrize("name", ["ARGOS_PRIVATE_KEY", "argos_private_key", "Argos_Log_Levl"])
def test_unknown_variables_are_detected_whatever_their_case(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Environment lookup is case-insensitive, so the guard must be too."""
    monkeypatch.setenv(name, "x")
    with pytest.raises(ConfigurationError) as caught:
        load_settings()
    assert name in caught.value.context["variables"]  # type: ignore[operator]


@pytest.mark.parametrize("name", ["argos_log_level", "Argos_Log_Level", "ARGOS_LOG_LEVEL"])
def test_a_known_field_in_any_case_is_accepted_and_applied(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "warning")
    assert unknown_environment_keys({name: "warning"}) == []
    assert load_settings().log_level == "WARNING"


def test_a_non_argos_variable_is_left_alone() -> None:
    assert unknown_environment_keys({"PATH": "/usr/bin", "ARGOSAURUS": "1"}) == []


def test_unknown_overrides_are_reported_as_configuration_errors() -> None:
    with pytest.raises(ConfigurationError) as caught:
        load_settings(bogus_field=1)
    assert caught.value.code == "argos.configuration"


def test_env_example_names_only_real_settings_fields() -> None:
    """A drifted example file hands operators a config that fails to load."""
    lines = (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    declared = {
        line.split("=", 1)[0].strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }
    assert declared, "the example file should document the ARGOS_* surface"
    assert unknown_environment_keys(dict.fromkeys(declared, "")) == []


# --- boundary validation ----------------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"http_timeout_seconds": 0},
        {"http_timeout_seconds": -1.0},
        {"http_max_attempts": 0},
        {"http_max_attempts": -3},
        {"log_level": "LOUD"},
        {"gamma_base_url": "gamma-api.polymarket.com"},
        {"gamma_base_url": "wss://gamma-api.polymarket.com"},
        {"clob_market_ws_url": "https://ws-subscriptions-clob.polymarket.com/ws/market"},
        {"clob_base_url": "https://"},
    ],
)
def test_invalid_values_are_refused_at_the_boundary(override: dict[str, object]) -> None:
    with pytest.raises(ConfigurationError):
        load_settings(**override)


@pytest.mark.parametrize("attempts", [1, 2, 10])
def test_every_legal_retry_count_is_accepted(attempts: int) -> None:
    assert Settings(http_max_attempts=attempts).http_max_attempts == attempts


@pytest.mark.parametrize("override", [{"http_max_attempts": 11}, {"http_timeout_seconds": 121.0}])
def test_retry_and_timeout_ceilings_are_enforced(override: dict[str, object]) -> None:
    """docs/09_SECURITY.md caps retry rates: no floor without a ceiling."""
    with pytest.raises(ConfigurationError):
        load_settings(**override)


# --- fingerprint reproducibility --------------------------------------------------


def test_fingerprint_ignores_how_a_value_arrived(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env-sourced and argument-sourced configuration are the same experiment."""
    monkeypatch.setenv("ARGOS_LOG_LEVEL", "debug")
    assert load_settings().fingerprint() == Settings(log_level="DEBUG").fingerprint()


def test_fingerprint_ignores_a_cosmetic_trailing_slash() -> None:
    assert (
        Settings(gamma_base_url="https://gamma-api.polymarket.com/").fingerprint()
        == Settings().fingerprint()
    )


def test_fingerprint_is_identical_in_a_fresh_process() -> None:
    """A fingerprint that depended on hash seeding or dict order could not be cited."""
    program = "from argos.config import Settings; print(Settings().fingerprint())"
    seen = set()
    for seed in ("0", "1", "524287"):
        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_ROOT,
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHONHASHSEED": seed,
                "PYTHONPATH": str(REPO_ROOT / "src"),
            },
            timeout=60,
        )
        seen.add(result.stdout.strip())
    assert seen == {Settings().fingerprint()}


def test_snapshot_is_canonical_json_and_hides_nothing_from_the_manifest() -> None:
    snapshot = Settings().snapshot()
    assert orjson.loads(orjson.dumps(snapshot)) == snapshot
    assert set(snapshot) == set(Settings.model_fields)
    assert snapshot["data_dir"] == ".data"


# --- record contracts -------------------------------------------------------------


def test_from_record_neither_mutates_nor_consumes_its_input() -> None:
    """A reader that popped the version out of the caller's dict would break re-reads."""
    record = _manifest().to_record()
    original = dict(record)
    first = RunManifest.from_record(record)
    assert record == original
    assert RunManifest.from_record(record) == first


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda r: r.update(unexpected_field="x"), id="extra-key"),
        pytest.param(lambda r: r.pop("run_id"), id="missing-key"),
        pytest.param(lambda r: r.update(runId=r.pop("run_id")), id="renamed-key"),
        pytest.param(lambda r: r.update(created_at="not-a-timestamp"), id="bad-timestamp"),
        pytest.param(lambda r: r.update(settings_snapshot="not-a-mapping"), id="bad-snapshot"),
    ],
)
def test_a_drifted_record_is_refused_rather_than_partially_loaded(
    mutate: Any,
) -> None:
    record = _manifest().to_record()
    mutate(record)
    with pytest.raises((ValidationError, SchemaVersionError)):
        RunManifest.from_record(record)


@pytest.mark.parametrize("version", [None, "", 1, "market_definition.v1", "run_manifest.v0"])
def test_a_record_without_a_usable_schema_version_is_refused(version: object) -> None:
    record = _manifest().to_record()
    if version is None:
        record.pop("schema_version")
    else:
        record["schema_version"] = version
    with pytest.raises(SchemaVersionError) as caught:
        RunManifest.from_record(record)
    assert caught.value.code == "argos.schema_version"


def test_the_schema_version_cannot_be_smuggled_in_as_a_field() -> None:
    """It describes the class; accepting it as data would let a record relabel itself."""
    with pytest.raises(ValidationError):
        RunManifest(
            run_id="run-1",
            mode="replay",
            created_at=START,
            argos_version="0.0.0",
            config_fingerprint="f",
            settings_snapshot={},
            schema_version="run_manifest.v9",
        )


def test_serializing_a_record_twice_produces_identical_bytes() -> None:
    manifest = _manifest()
    canonical = orjson.dumps(manifest.to_record(), option=orjson.OPT_SORT_KEYS)
    assert canonical == orjson.dumps(manifest.to_record(), option=orjson.OPT_SORT_KEYS)
    assert canonical == orjson.dumps(
        RunManifest.from_record(manifest.to_record()).to_record(), option=orjson.OPT_SORT_KEYS
    )


def test_a_versioned_model_must_declare_its_own_version() -> None:
    with pytest.raises(SchemaVersionError):

        class Unversioned(VersionedModel):
            pass


def test_an_empty_schema_version_is_refused_at_class_definition() -> None:
    with pytest.raises(SchemaVersionError):

        class Blank(VersionedModel):
            schema_version: ClassVar[str] = ""


# --- timestamp anchoring (regressions for defects found in the M0 review) -----------


def test_manifest_refuses_a_naive_created_at() -> None:
    """The model contract, not just build_run_manifest, must anchor the timestamp:
    a manifest read back from disk has to be as trustworthy as one just built."""
    with pytest.raises(NaiveDatetimeError):
        RunManifest(
            run_id="run-1",
            mode="replay",
            created_at=datetime(2026, 1, 1, 12, 0),
            argos_version="0.0.0",
            config_fingerprint="f",
            settings_snapshot={},
        )


def test_manifest_normalizes_a_non_utc_created_at() -> None:
    """Two manifests describing the same instant must serialize identically, or
    M3's "identical input produces identical output hash" cannot hold."""
    offset = datetime(2026, 1, 1, 13, 0, tzinfo=timezone(timedelta(hours=1)))
    manifest = RunManifest(
        run_id="run-1",
        mode="replay",
        created_at=offset,
        argos_version="0.0.0",
        config_fingerprint="f",
        settings_snapshot={},
    )
    assert manifest.to_record()["created_at"] == "2026-01-01T12:00:00Z"


def test_a_subclass_cannot_silently_reuse_its_parents_schema_version() -> None:
    """A subclass with different fields must not write records labelled with the
    parent's schema version — readers would accept them and the drift is invisible."""
    with pytest.raises(SchemaVersionError):

        class ExtendedManifest(RunManifest):
            extra_note: str = ""
