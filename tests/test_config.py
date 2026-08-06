from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from argos.clock import ReplayClock
from argos.config import RunManifest, Settings, build_run_manifest, load_settings
from argos.errors import ConfigurationError, ExecutionProhibitedError, SchemaVersionError

START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


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
