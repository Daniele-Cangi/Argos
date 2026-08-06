from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True, help="ARGOS research CLI")


@app.callback()
def main() -> None:
    """ARGOS probability intelligence research commands."""


@app.command()
def status() -> None:
    """Print the repository milestone status."""
    status_path = Path(__file__).resolve().parents[2] / "docs" / "STATUS.md"
    if status_path.exists():
        typer.echo(status_path.read_text(encoding="utf-8"))
    else:
        typer.echo("STATUS.md not found")


if __name__ == "__main__":
    app()
