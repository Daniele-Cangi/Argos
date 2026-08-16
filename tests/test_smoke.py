from datetime import UTC
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from argos import __version__, cli
from argos.cli import app
from argos.config import RunManifest, Settings

runner = CliRunner()


def test_status_command() -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "ARGOS" in result.stdout


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "ARGOS" in result.stdout


def test_config_command_reports_a_fingerprint() -> None:
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    payload = orjson.loads(result.stdout)
    assert payload["execution_enabled"] is False
    assert len(payload["config_fingerprint"]) == 64


def test_config_command_fails_loudly_on_bad_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARGOS_EXECUTION_ENABLED", "true")
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 2
    assert "argos.execution_prohibited" in result.output


def test_manifest_command_emits_a_versioned_record() -> None:
    result = runner.invoke(app, ["manifest", "--mode", "inspect"])
    assert result.exit_code == 0
    payload = orjson.loads(result.stdout)
    assert payload["schema_version"] == "run_manifest.v3"
    assert payload["mode"] == "inspect"


def test_no_arguments_prints_help_instead_of_acting() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code != 0
    assert "Usage" in result.output


def test_version_command_reports_the_installed_version() -> None:
    result = runner.invoke(app, ["version"])
    assert __version__ in result.stdout


def test_status_command_survives_a_missing_status_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "not found" in result.stdout


def test_config_command_prints_exactly_the_snapshot_plus_its_fingerprint() -> None:
    result = runner.invoke(app, ["config"])
    payload = orjson.loads(result.stdout)
    assert set(payload) == set(Settings.model_fields) | {"config_fingerprint"}
    assert payload["config_fingerprint"] == Settings().fingerprint()
    assert list(payload) == sorted(payload), "operator output must be canonically ordered"


def test_manifest_command_output_is_readable_by_the_record_reader() -> None:
    """The CLI and the reader must agree on the wire format, not merely on key names."""
    result = runner.invoke(app, ["manifest", "--mode", "replay"])
    payload = orjson.loads(result.stdout)
    manifest = RunManifest.from_record(payload)
    assert manifest.mode == "replay"
    assert manifest.created_at.tzinfo is not None
    assert manifest.created_at.utcoffset() == UTC.utcoffset(None)
    assert manifest.config_fingerprint == Settings().fingerprint()
    assert manifest.settings_snapshot["execution_enabled"] is False
    assert manifest.describe().startswith(manifest.run_id)


def test_manifest_command_run_ids_are_unique_per_invocation() -> None:
    first = orjson.loads(runner.invoke(app, ["manifest"]).stdout)["run_id"]
    second = orjson.loads(runner.invoke(app, ["manifest"]).stdout)["run_id"]
    assert first != second


@pytest.mark.parametrize("command", ["config", "manifest"])
def test_a_misspelled_variable_stops_the_command_with_a_coded_error(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.setenv("ARGOS_LOG_LEVL", "DEBUG")
    result = runner.invoke(app, [command])
    assert result.exit_code == 2
    record = orjson.loads(result.output.strip().splitlines()[-1])
    assert record["error_code"] == "argos.configuration"
    assert record["context"]["variables"] == ["ARGOS_LOG_LEVL"]


def test_a_failing_command_prints_no_configuration_to_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARGOS_EXECUTION_ENABLED", "true")
    result = runner.invoke(app, ["manifest"])
    assert result.exit_code == 2
    assert "config_fingerprint" not in result.output
