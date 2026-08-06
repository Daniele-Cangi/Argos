"""Operator commands only. No domain logic lives here."""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import orjson
import typer

from argos import __version__
from argos.clock import LiveClock
from argos.config import Settings, build_run_manifest, load_settings
from argos.errors import ArgosError
from argos.logging import configure_logging

app = typer.Typer(no_args_is_help=True, help="ARGOS research CLI")

REPO_ROOT = Path(__file__).resolve().parents[2]


@app.callback()
def main() -> None:
    """ARGOS probability intelligence research commands."""


@app.command()
def status() -> None:
    """Print the repository milestone status."""
    status_path = REPO_ROOT / "docs" / "STATUS.md"
    if status_path.exists():
        typer.echo(status_path.read_text(encoding="utf-8"))
    else:
        typer.echo("STATUS.md not found")


@app.command()
def version() -> None:
    """Print the ARGOS version."""
    typer.echo(f"ARGOS {__version__}")


@app.command()
def config() -> None:
    """Validate the current configuration and print it with its fingerprint."""
    settings = _load_or_exit()
    payload = settings.snapshot()
    payload["config_fingerprint"] = settings.fingerprint()
    typer.echo(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS).decode())


@app.command()
def manifest(mode: str = typer.Option("inspect", help="Label for this run.")) -> None:
    """Emit a run manifest for the current configuration."""
    settings = _load_or_exit()
    configure_logging(settings.log_level)
    run_manifest = build_run_manifest(
        settings=settings,
        clock=LiveClock(),
        run_id=uuid4().hex,
        mode=mode,
        code_revision=_code_revision(),
    )
    typer.echo(
        orjson.dumps(
            run_manifest.to_record(), option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS
        ).decode()
    )


def _load_or_exit() -> Settings:
    try:
        return load_settings()
    except ArgosError as exc:
        typer.echo(orjson.dumps(exc.as_record()).decode(), err=True)
        raise typer.Exit(code=2) from exc


def _code_revision() -> str | None:
    """Return the current git revision, or None when it cannot be determined."""
    try:
        result = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


if __name__ == "__main__":
    app()
