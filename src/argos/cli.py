"""Operator commands only. No domain logic lives here."""

from __future__ import annotations

import asyncio
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

import orjson
import typer

from argos import __version__
from argos.clock import LiveClock
from argos.compiler import build_market_audit, compile_market_contract, render_market_audit
from argos.config import Settings, build_run_manifest, load_settings
from argos.domain.market import MarketDefinitionV1
from argos.domain.selection import MarketSelectionPolicy, select_markets
from argos.errors import ArgosError
from argos.ingestion import normalize_market, normalize_markets
from argos.logging import configure_logging
from argos.sources import GammaClient
from argos.store import write_raw_payload

app = typer.Typer(no_args_is_help=True, help="ARGOS research CLI")
markets_app = typer.Typer(no_args_is_help=True, help="Public market discovery and audit")
app.add_typer(markets_app, name="markets")

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


@markets_app.command("discover")
def markets_discover(
    limit: int = typer.Option(50, help="Markets to request from Gamma."),
    min_liquidity: str | None = typer.Option(
        None, help="Exclude thinner markets. Parsed as an exact decimal, never a float."
    ),
    min_hours_to_end: int | None = typer.Option(None, help="Exclude markets ending sooner."),
    save_raw: bool = typer.Option(
        False, "--save-raw", help="Archive the raw payload under the configured data dir."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit the report as JSON."),
) -> None:
    """Discover public markets, normalize them, and report what was selected.

    Prints a full account: accepted, quarantined, and excluded, each with a
    reason. A discovery run that silently narrowed its own sample would make
    every downstream evaluation unreadable.
    """
    settings = _load_or_exit()
    configure_logging(settings.log_level)
    clock = LiveClock()
    policy = MarketSelectionPolicy(
        min_liquidity=_decimal_option(min_liquidity, "--min-liquidity"),
        min_hours_to_end=min_hours_to_end,
    )

    async def run() -> tuple[Any, Any]:
        async with GammaClient(settings, clock) as client:
            return (await client.list_markets(limit=limit), clock.now())

    response, observed_at = _run_or_exit(run())
    archived: str | None = None
    if save_raw:
        try:
            archived = str(
                write_raw_payload(
                    settings.data_dir, raw=response.raw, provenance=response.provenance
                )
            )
        except ArgosError as exc:
            _fail(exc)

    report = normalize_markets(
        response.payload,
        raw_payload_sha256=response.provenance.raw_sha256,
        normalized_at=observed_at,
    )
    selection = select_markets(report.accepted, policy, as_of=observed_at)

    payload: dict[str, Any] = {
        "retrieved_at": response.provenance.retrieved_at.isoformat(),
        "endpoint": response.provenance.endpoint,
        "raw_sha256": response.provenance.raw_sha256,
        "raw_archive_path": archived,
        "policy": policy.model_dump(mode="json"),
        "returned": report.total,
        "normalized": len(report.accepted),
        "quarantined": len(report.quarantined),
        "quarantine_reasons": report.counts_by_reason(),
        # Identities, not only counts: an operator cannot act on "2 quarantined".
        "quarantined_markets": [
            {"market_id": record.market_id, "slug": record.slug, "reason": record.reason.value}
            for record in report.quarantined
        ],
        "selected": len(selection.selected),
        "exclusion_reasons": selection.counts_by_reason(),
        "markets": [
            {"market_id": m.market_id, "slug": m.slug, "question": m.question}
            for m in selection.selected
        ],
    }
    if as_json:
        typer.echo(_json(payload))
        return

    typer.echo(f"Gamma returned {payload['returned']} markets at {payload['retrieved_at']}")
    typer.echo(f"  normalized  {payload['normalized']}")
    typer.echo(f"  quarantined {payload['quarantined']} {payload['quarantine_reasons'] or ''}")
    typer.echo(f"  selected    {payload['selected']} {payload['exclusion_reasons'] or ''}")
    typer.echo("")
    for market in selection.selected:
        typer.echo(f"  {market.market_id:>10}  {market.question}")


@markets_app.command("audit")
def markets_audit(
    market_id: str = typer.Argument(help="Gamma market id."),
    as_json: bool = typer.Option(False, "--json", help="Emit the audit record as JSON."),
) -> None:
    """Audit one market end to end, without making any probability claim."""
    settings = _load_or_exit()
    configure_logging(settings.log_level)
    clock = LiveClock()

    async def run() -> tuple[Any, Any]:
        async with GammaClient(settings, clock) as client:
            return (await client.get_market(market_id), clock.now())

    response, observed_at = _run_or_exit(run())
    market = _normalize_or_exit(response, observed_at)
    contract = compile_market_contract(market, compiled_at=observed_at)
    audit = build_market_audit(
        market,
        contract,
        policy=MarketSelectionPolicy(),
        audited_at=observed_at,
    )
    typer.echo(_json(audit.to_record()) if as_json else render_market_audit(audit))


def _normalize_or_exit(response: Any, observed_at: Any) -> MarketDefinitionV1:
    try:
        return normalize_market(
            response.payload,
            raw_payload_sha256=response.provenance.raw_sha256,
            normalized_at=observed_at,
        )
    except ArgosError as exc:
        _fail(exc)


def _run_or_exit(coroutine: Any) -> Any:
    try:
        return asyncio.run(coroutine)
    except ArgosError as exc:
        _fail(exc)
    except ValueError as exc:
        # An adapter argument the operator got wrong (`--limit 0`) is a usage
        # error, not a crash. Without this it reaches the user as a traceback.
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


def _fail(exc: ArgosError) -> NoReturn:
    typer.echo(orjson.dumps(exc.as_record()).decode(), err=True)
    raise typer.Exit(code=1) from exc


def _decimal_option(value: str | None, flag: str) -> Decimal | None:
    """Parse a money-like option exactly. Going through float would round it."""
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        typer.echo(f"{flag} must be a decimal number, got {value!r}", err=True)
        raise typer.Exit(code=2) from None


def _json(payload: Any) -> str:
    return orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS).decode()


def _load_or_exit() -> Settings:
    try:
        return load_settings()
    except ArgosError as exc:
        typer.echo(orjson.dumps(exc.as_record()).decode(), err=True)
        raise typer.Exit(code=2) from exc


def _code_revision() -> str | None:
    """Return this repository's git revision, or None when it cannot be proven.

    git discovers repositories upward from ``cwd``. Installed as a wheel,
    ``REPO_ROOT`` is inside the virtualenv, so a bare ``rev-parse HEAD`` would
    happily return the HEAD of whatever repository happens to contain it — and
    stamp a manifest with provenance for code that never produced the run.
    The toplevel is therefore verified before the revision is trusted.
    """
    try:
        result = subprocess.run(
            ("git", "-c", "core.fsmonitor=false", "rev-parse", "--show-toplevel", "HEAD"),
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    lines = result.stdout.split()
    if len(lines) != 2:
        return None
    toplevel, revision = lines
    if Path(toplevel).resolve() != REPO_ROOT:
        return None
    if len(revision) != 40 or not all(char in "0123456789abcdef" for char in revision):
        return None
    return revision


if __name__ == "__main__":
    app()
