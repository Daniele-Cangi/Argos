from typer.testing import CliRunner

from argos.cli import app


def test_status_command() -> None:
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0
    assert "ARGOS" in result.stdout
