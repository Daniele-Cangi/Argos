import orjson
import pytest
from typer.testing import CliRunner

from argos.cli import app

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
    assert payload["schema_version"] == "run_manifest.v1"
    assert payload["mode"] == "inspect"
