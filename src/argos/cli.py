"""Operator commands only. No domain logic lives here."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, NoReturn
from uuid import uuid4

import orjson
import typer

from argos import __version__
from argos.clock import Clock, LiveClock, Pacer, RealPacer
from argos.compiler import build_market_audit, compile_market_contract, render_market_audit
from argos.config import RunMode, Settings, WorkingTreeStatus, build_run_manifest, load_settings
from argos.domain.market import TOKEN_ID_PATTERN, MarketDefinitionV1
from argos.domain.observation import ObservationEnvelopeV1, RejectedObservationV1
from argos.domain.pricechange import PriceChangeV1
from argos.domain.selection import MarketSelectionPolicy, select_markets
from argos.errors import ArgosError
from argos.evaluation import evaluate_capture
from argos.ingestion import (
    CaptureHealth,
    FrameSource,
    normalize_market,
    normalize_markets,
    run_capture,
)
from argos.logging import configure_logging
from argos.replay import RealTimePacer, ReplayMode, VirtualPacer, replay_capture
from argos.resolution import (
    ResolutionRefusal,
    ResolutionV1,
    normalize_clob_resolution,
    normalize_gamma_resolution,
)
from argos.sources import (
    ClobMarketWsClient,
    GammaClient,
    MarketFrame,
    WebSocketConnector,
    WebsocketsConnector,
)
from argos.store import EventStore, open_sqlite_event_store, write_raw_payload

app = typer.Typer(no_args_is_help=True, help="ARGOS research CLI")
markets_app = typer.Typer(no_args_is_help=True, help="Public market discovery and audit")
capture_app = typer.Typer(no_args_is_help=True, help="Bounded live capture against a public source")
replay_app = typer.Typer(no_args_is_help=True, help="Deterministic replay of a stored capture")
evaluate_app = typer.Typer(
    no_args_is_help=True, help="Score market baselines against a recorded resolution"
)
app.add_typer(markets_app, name="markets")
app.add_typer(capture_app, name="capture")
app.add_typer(replay_app, name="replay")
app.add_typer(evaluate_app, name="evaluate")

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


# A module-level singleton, not an inline call: ruff (B008) only recognizes
# builtin literal types (str/int/bool) as safe inline `typer.Option` defaults,
# not a custom enum, and constructing one per invocation would be pointless
# anyway since it is immutable.
_MODE_OPTION = typer.Option(RunMode.INSPECT, help="Kind of run this manifest describes.")


@app.command()
def manifest(mode: RunMode = _MODE_OPTION) -> None:
    """Emit a run manifest for the current configuration."""
    settings = _load_or_exit()
    configure_logging(settings.log_level)
    code_revision, working_tree = _code_revision()
    run_manifest = build_run_manifest(
        settings=settings,
        clock=LiveClock(),
        run_id=uuid4().hex,
        mode=mode,
        code_revision=code_revision,
        working_tree=working_tree,
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
    pacer = RealPacer()
    policy = MarketSelectionPolicy(
        min_liquidity=_decimal_option(min_liquidity, "--min-liquidity"),
        min_hours_to_end=min_hours_to_end,
    )

    async def run() -> tuple[Any, Any]:
        async with GammaClient(settings, clock, pacer=pacer) as client:
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
    pacer = RealPacer()

    async def run() -> tuple[Any, Any]:
        async with GammaClient(settings, clock, pacer=pacer) as client:
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


# --- capture ------------------------------------------------------------------------
#
# The last M2 deliverable: everything above this point evidences its exit
# criteria only by replaying recorded frames. This command is what lets a
# capture actually run against live traffic.

# Every record schema one `capture market` run can read or write, for
# `RunManifest.schema_versions` (core invariant 13). Not `run_capture`'s own
# module-scoped constants because that module deliberately names none of
# these -- see its own docstring, Decision 3: the manifest is this command's
# responsibility, not the loop's.
_CAPTURE_SCHEMA_VERSIONS: Final = (
    ObservationEnvelopeV1.schema_version,
    RejectedObservationV1.schema_version,
    PriceChangeV1.schema_version,
)


def _default_connector(settings: Settings) -> WebSocketConnector:
    return WebsocketsConnector(settings.clob_market_ws_url)


# The narrowest injection seam available, not the widest: `WebSocketConnector`
# is a `Protocol`, and Typer has no way to turn a protocol-typed parameter into
# a CLI option, so a fake cannot enter through `capture_market`'s own
# signature the way `--min-liquidity` enters `markets_discover`'s. A plain
# module attribute a test can monkeypatch
# (`monkeypatch.setattr(cli, "_CAPTURE_CONNECTOR_FACTORY", ...)`) is the
# smallest seam that still lets `CliRunner` drive the real command end to end
# with no socket opened. `capture_market` itself never changes shape because
# of this; it always calls whichever factory this name is currently bound to.
_CAPTURE_CONNECTOR_FACTORY: Callable[[Settings], WebSocketConnector] = _default_connector


def _default_capture_run_id(clock: Clock) -> str:
    """A unique, human-legible id: readable in a directory listing, sortable by start time."""
    return f"clobws-{clock.now():%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"


class _BoundedFrameSource:
    """Wraps a `FrameSource`, stopping cleanly once a configured bound is reached.

    Reaching `--max-frames`/`--max-seconds` is a **deliberate operator stop,
    not a failure**: this class stops by *returning* from its own generator —
    ordinary async-generator exhaustion — which `run_capture` closes as
    `CompletionStatus.COMPLETED`. It never stops by raising, which
    `run_capture` would (correctly) close as `CompletionStatus.FAILED`, the
    outcome reserved for a capture that broke while genuinely running
    (`argos.ingestion.capture.run_capture`'s own module docstring). The
    "interrupted capture" signal ADR-0011 defines — a `capture_run` row with
    no closing row at all — means a process that died outright (`SIGKILL`, a
    hard crash); by construction such a process never runs any of this code,
    so a bound reached here is never that signal.

    Ctrl-C needs no special handling in this class: `run_capture` already
    closes `capture_run_id` (as `FAILED`, not dangling) for *any* exception
    that reaches its own frame-consumption loop, cancellation included, so
    the run this command started is never left open either way — only the
    completion status differs from the two bounds above, and that is the
    correct distinction (an operator's Ctrl-C is not "the same outcome as
    reaching a configured limit", even though neither is "the process died").

    `--max-seconds` is measured against `clock`, not against `pacer`'s own
    notion of time: the deadline computed here is only bookkeeping across
    loop iterations, never the thing that actually interrupts a stuck wait
    for the next frame -- `pacer.move_on_after` does that, opened and closed
    entirely within one loop iteration so no cancel scope ever spans a
    suspended `yield`. Spanning one is exactly the hazard
    `argos.sources.clob_ws`'s own module docstring documents reproducing for
    a task group; a plain `CancelScope` does not spawn child tasks the way a
    task group does, but this class does not lean on that distinction --
    entering and exiting inside one iteration is safe regardless of which
    kind of scope it is.
    """

    def __init__(
        self,
        inner: FrameSource,
        *,
        clock: Clock,
        pacer: Pacer,
        max_frames: int | None,
        max_seconds: float | None,
    ) -> None:
        self._inner = inner
        self._clock = clock
        self._pacer = pacer
        self._max_frames = max_frames
        self._max_seconds = max_seconds

    async def frames(self) -> AsyncIterator[MarketFrame]:
        count = 0
        inner_iter = self._inner.frames()
        deadline: datetime | None = None
        if self._max_seconds is not None:
            deadline = self._clock.now() + timedelta(seconds=self._max_seconds)
        while True:
            if self._max_frames is not None and count >= self._max_frames:
                return
            if deadline is not None:
                remaining = (deadline - self._clock.now()).total_seconds()
                if remaining <= 0:
                    return
                with self._pacer.move_on_after(remaining) as scope:
                    try:
                        frame = await inner_iter.__anext__()
                    except StopAsyncIteration:
                        return
                if scope.cancelled_caught:
                    return
            else:
                try:
                    frame = await inner_iter.__anext__()
                except StopAsyncIteration:
                    return
            count += 1
            yield frame


def _open_store_or_exit(path: Path) -> EventStore:
    try:
        return open_sqlite_event_store(path)
    except ArgosError as exc:
        _fail(exc)


# Module-level singletons, like `_MODE_OPTION` above: ruff (B008) does not
# recognize `list[str]`/`Path` as safe inline `typer.Option` default types the
# way it does `str`/`int`/`bool`/`None`.
_TOKEN_ID_OPTION: Final = typer.Option(
    None, "--token-id", help="CLOB token id to subscribe to. Repeatable; required at least once."
)
_DB_OPTION: Final = typer.Option(
    None, "--db", help="SQLite event-store path. Defaults under the configured data dir."
)


@capture_app.command("market")
def capture_market(
    token_id: list[str] | None = _TOKEN_ID_OPTION,
    max_seconds: float | None = typer.Option(
        None, help="Stop the capture after this many seconds of real elapsed time."
    ),
    max_frames: int | None = typer.Option(
        None, help="Stop the capture after this many raw frames have been consumed."
    ),
    db: Path | None = _DB_OPTION,
    raw_archive: bool = typer.Option(
        True,
        "--raw-archive/--no-raw-archive",
        help=(
            "Archive every raw frame beside the database. On by default: "
            "CLAUDE.md requires storing the raw payload alongside the normalized "
            "one. Disabling it produces a capture that CANNOT be re-normalized "
            "under a corrected parser and whose stored raw_payload_sha256 can "
            "never be checked against anything."
        ),
    ),
    capture_run_id: str | None = typer.Option(
        None, help="Override the generated capture_run_id. Mainly for tests and resumption."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit the final report as JSON."),
) -> None:
    """Capture the public CLOB market-channel WebSocket for a small, explicit token set.

    Runs until `--max-seconds` and/or `--max-frames` is reached, or until the
    operator stops it with Ctrl-C -- at least one of the two bounds is
    required outright: a research CLI must not start an unbounded live-
    traffic run by accident. Wires `argos.sources.clob_ws.ClobMarketWsClient`
    through `argos.ingestion.capture.run_capture` into a `SQLiteEventStore`
    (ADR-0011), on an injected `LiveClock`/`RealPacer`, matching every other
    live command in this module.

    See `_BoundedFrameSource` for why reaching a configured bound closes the
    capture run `CompletionStatus.COMPLETED` (a deliberate operator stop),
    while Ctrl-C still closes it, just as `FAILED` rather than dangling --
    "interrupted", the state ADR-0011's `iter_open_capture_runs` reports, is
    reserved for a process that dies outright and therefore never reaches
    any closing code at all.
    """
    token_ids: list[str] = list(token_id) if token_id else []
    if not token_ids:
        typer.echo("--token-id is required at least once", err=True)
        raise typer.Exit(code=2)
    for token in token_ids:
        if not TOKEN_ID_PATTERN.fullmatch(token):
            typer.echo(f"--token-id {token!r} does not match {TOKEN_ID_PATTERN.pattern}", err=True)
            raise typer.Exit(code=2)
    if max_seconds is None and max_frames is None:
        typer.echo(
            "one of --max-seconds or --max-frames is required; a research capture "
            "must not start an unbounded run by accident",
            err=True,
        )
        raise typer.Exit(code=2)
    if max_seconds is not None and max_seconds <= 0:
        typer.echo("--max-seconds must be positive", err=True)
        raise typer.Exit(code=2)
    if max_frames is not None and max_frames <= 0:
        typer.echo("--max-frames must be positive", err=True)
        raise typer.Exit(code=2)

    settings = _load_or_exit()
    configure_logging(settings.log_level)
    clock = LiveClock()
    pacer = RealPacer()
    code_revision, working_tree = _code_revision()

    run_id = capture_run_id or _default_capture_run_id(clock)
    db_path = db if db is not None else settings.data_dir / "capture" / "events.sqlite3"
    # Beside the database, not inside the data dir root: a capture's raw frames
    # and its event store are one artifact and should move or be deleted
    # together. Content-addressed by hash inside `write_raw_payload`, so a
    # redelivered frame -- which really happens, one arrived in a 60-second live
    # capture -- costs nothing extra.
    raw_archive_dir = db_path.parent / "raw" if raw_archive else None
    db_path.parent.mkdir(parents=True, exist_ok=True)

    store = _open_store_or_exit(db_path)
    connector = _CAPTURE_CONNECTOR_FACTORY(settings)

    async def run() -> CaptureHealth:
        async with ClobMarketWsClient(
            settings, clock, pacer=pacer, connector=connector, token_ids=token_ids
        ) as client:
            bounded = _BoundedFrameSource(
                client,
                clock=clock,
                pacer=pacer,
                max_frames=max_frames,
                max_seconds=max_seconds,
            )
            return await run_capture(
                raw_archive_dir=raw_archive_dir,
                frame_source=bounded,
                store=store,
                clock=clock,
                capture_run_id=run_id,
                subscribed_token_ids=token_ids,
            )

    interrupted = False
    health: CaptureHealth | None = None
    manifest_path: Path | None = None
    try:
        try:
            health = asyncio.run(run())
        except KeyboardInterrupt:
            # `run_capture` has already closed `run_id` -- as `FAILED`, not
            # dangling -- for this exception the same way it would for any
            # other (see `_BoundedFrameSource`'s docstring). There is nothing
            # left to await; report what the store itself now says instead of
            # the `CaptureHealth` this coroutine never returned.
            interrupted = True
        except BaseExceptionGroup as group:
            # `run_capture` runs inside an `anyio` task group, so an
            # `ArgosError` raised in it arrives wrapped and would sail past the
            # handler below — reproduced: reusing an existing `capture_run_id`,
            # an ordinary operator mistake the store deliberately refuses,
            # printed **nothing at all** and crashed with a traceback instead of
            # the refusal's own reason. Same unwrapping as `_run_or_exit`.
            argos_errors = _flatten_argos_errors(group)
            if argos_errors:
                _fail(argos_errors[0])
            raise
        except ArgosError as exc:
            _fail(exc)

        counts = store.counts_for_capture_run(run_id)
        run_manifest = build_run_manifest(
            settings=settings,
            clock=clock,
            run_id=run_id,
            mode=RunMode.CAPTURE,
            code_revision=code_revision,
            working_tree=working_tree,
            capture_run_id=run_id,
            schema_versions=_CAPTURE_SCHEMA_VERSIONS,
            # The operator's own arguments, which are not settings and were in
            # no durable artifact before `run_parameters` existed (core
            # invariant 13). `subscribed_token_ids` is recorded in the form the
            # loop actually fanned out over -- deduplicated and sorted, matching
            # `run_capture`'s own `sorted(frozenset(...))` -- because that set is
            # what determines `ingest_sequence` allocation, and the order the
            # flags happened to appear in determines nothing.
            run_parameters={
                "subscribed_token_ids": sorted(frozenset(token_ids)),
                "max_seconds": max_seconds,
                "max_frames": max_frames,
                "raw_archive": raw_archive,
            },
        )
        manifest_path = db_path.parent / f"{run_id}.manifest.json"
        manifest_path.write_bytes(
            orjson.dumps(
                run_manifest.to_record(), option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS
            )
        )
    finally:
        store.close()

    payload: dict[str, Any] = {
        "capture_run_id": run_id,
        "interrupted": interrupted,
        "db_path": str(db_path),
        "manifest_path": str(manifest_path),
        "token_ids": token_ids,
        # Both are printed deliberately: the loop's own counters and the
        # store's independently derived counts must never disagree, and a
        # disagreement between them is exactly the kind of defect that must
        # be visible, not averaged away.
        "loop_health": asdict(health) if health is not None else None,
        "store_counts": {
            "accepted": counts.accepted,
            "duplicate": counts.duplicate,
            "rejected": counts.rejected,
        },
    }
    if as_json:
        typer.echo(_json(payload))
    else:
        typer.echo(f"capture_run_id {run_id}")
        typer.echo(f"  db       {db_path}")
        typer.echo(f"  manifest {manifest_path}")
        if interrupted:
            typer.echo("  interrupted by the operator; capture_run is closed, not dangling")
        typer.echo(f"  loop counters  {payload['loop_health']}")
        typer.echo(
            "  store counts   accepted={accepted} duplicate={duplicate} rejected={rejected}".format(
                **payload["store_counts"]
            )
        )
    if interrupted:
        raise typer.Exit(code=130)


# --- replay ---------------------------------------------------------------------------
#
# M3's operator surface. Everything this command does is `argos.replay`'s; the
# CLI's own job is exactly the three things a CLI should do -- read arguments,
# choose adapters, render a result.

_REPLAY_DB_OPTION: Final = typer.Option(
    ..., "--db", help="SQLite event store holding the capture to replay."
)
# Module-level singletons: ruff (B008) does not recognize `Path` as a safe
# inline `typer.Option` default the way it does `str`/`int`/`bool`/`None`.
_REPLAY_MANIFEST_OPTION: Final = typer.Option(
    None, "--manifest-out", help="Where to write the replay manifest. Defaults beside the db."
)
_REPLAY_MODE_OPTION: Final = typer.Option(
    ReplayMode.ACCELERATED,
    "--mode",
    help=(
        "Pacing only. It cannot change the output hash -- that is a tested "
        "property, not a claim (ADR-0012 section 7)."
    ),
)


@replay_app.command("capture")
def replay_capture_command(
    capture_run_id: str = typer.Argument(help="The capture_run_id to replay."),
    db: Path = _REPLAY_DB_OPTION,
    mode: ReplayMode = _REPLAY_MODE_OPTION,
    allowed_lateness_ms: int = typer.Option(
        0,
        help=(
            "Watermark tolerance in milliseconds. Zero is the default because no "
            "capture in this repository contains an out-of-order arrival, so zero "
            "flags nothing yet observed while flagging any genuine regression."
        ),
    ),
    manifest_out: Path | None = _REPLAY_MANIFEST_OPTION,
    as_json: bool = typer.Option(False, "--json", help="Emit the result as JSON."),
) -> None:
    """Replay one stored capture run and report its state hash and counts.

    Reads only; writes nothing but the manifest. The event store is opened,
    walked in `ingest_sequence` order, and left exactly as it was found -- a
    replay that could modify its own input would not be a replay.
    """
    if allowed_lateness_ms < 0:
        typer.echo("--allowed-lateness-ms must not be negative", err=True)
        raise typer.Exit(code=2)

    settings = _load_or_exit()
    configure_logging(settings.log_level)
    clock = LiveClock()
    code_revision, working_tree = _code_revision()
    run_id = f"replay-{clock.now():%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"

    store = _open_store_or_exit(db)
    try:
        result = replay_capture(
            store=store,
            capture_run_id=capture_run_id,
            settings=settings,
            wall_clock=clock,
            replay_run_id=run_id,
            mode=mode,
            allowed_lateness=timedelta(milliseconds=allowed_lateness_ms),
            # Real pacing only when the operator asked to watch it happen; every
            # other mode records the waits it would have made and makes none.
            pacer=RealTimePacer() if mode is ReplayMode.ORIGINAL_ARRIVAL else VirtualPacer(),
            code_revision=code_revision,
            working_tree=working_tree,
        )
    except ArgosError as exc:
        _fail(exc)
    finally:
        store.close()

    destination = manifest_out or db.parent / f"{run_id}.replay-manifest.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(
        orjson.dumps(result.manifest.to_record(), option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
    )

    payload: dict[str, Any] = {
        "replay_run_id": run_id,
        "source_capture_run_id": capture_run_id,
        "mode": mode.value,
        "manifest_path": str(destination),
        "state_hash": result.state_hash,
        "state_hash_version": result.manifest.state_hash_version,
        "counts": result.manifest.to_record()["output_record_counts"],
        "projections": [
            {"condition_id": condition_id, "token_id": token_id, "digest": projection.digest()}
            for (condition_id, token_id), projection in sorted(
                result.dispatcher.projections.items()
            )
        ],
    }
    if as_json:
        typer.echo(_json(payload))
        return

    typer.echo(result.manifest.describe())
    typer.echo(f"  manifest {destination}")
    typer.echo(f"  state    {result.state_hash} ({result.manifest.state_hash_version})")
    typer.echo(f"  arrivals {payload['counts']['arrivals']}")
    typer.echo(f"  dispatch {payload['counts']['dispatch']}")
    for entry in payload["projections"]:
        typer.echo(f"  book     {entry['token_id']} -> {entry['digest'][:16]}")


# --- evaluate ---------------------------------------------------------------------
#
# M4's operator surface. Both inputs are records -- a stored capture and a
# recorded source payload -- and nothing is fetched here: an evaluation that
# reached the network could give a different answer tomorrow from the same
# arguments, which is the opposite of "reproducible from stored records".

_EVAL_DB_OPTION: Final = typer.Option(
    ..., "--db", help="SQLite event store holding the capture to evaluate."
)
_EVAL_RESOLUTION_OPTION: Final = typer.Option(
    ...,
    "--resolution",
    help=(
        "Path to a recorded market payload carrying the settlement. A file, not a "
        "URL: the evaluation must give the same answer tomorrow."
    ),
)
_EVAL_OUT_OPTION: Final = typer.Option(
    None, "--report-out", help="Where to write the evaluation report. Defaults beside the db."
)


def _load_resolution(path: Path, token_id: str, clock: Clock) -> ResolutionV1:
    """Read a recorded payload and normalize it, trying the CLOB shape first.

    Two shapes rather than one because the two sources genuinely differ, and the
    difference is not cosmetic: the CLOB record *states* the winner, while the
    Gamma record leaves it to be inferred from `outcomePrices` -- and for the one
    market this repository has captured, Gamma returns nothing at all
    (`docs/research/m4-gamma-resolution.md`).
    """
    try:
        raw = path.read_bytes()
    except OSError as error:
        typer.echo(f"cannot read {path}: {error}", err=True)
        raise typer.Exit(code=2) from error
    try:
        decoded = orjson.loads(raw)
    except orjson.JSONDecodeError as error:
        typer.echo(f"{path} is not valid JSON", err=True)
        raise typer.Exit(code=2) from error

    payload = decoded[0] if isinstance(decoded, list) and decoded else decoded
    if not isinstance(payload, dict):
        typer.echo(f"{path} does not contain a market object", err=True)
        raise typer.Exit(code=2)

    digest = hashlib.sha256(raw).hexdigest()
    now = clock.now()
    attempts: list[ResolutionRefusal] = []
    candidates: list[ResolutionV1 | ResolutionRefusal] = [
        normalize_clob_resolution(
            payload, source_payload_sha256=digest, normalized_at=now, yes_token_id=token_id
        ),
        normalize_gamma_resolution(payload, source_payload_sha256=digest, normalized_at=now),
    ]
    for result in candidates:
        if isinstance(result, ResolutionV1):
            return result
        attempts.append(result)

    # Both refused. Report *both* reasons: "this is not a CLOB record" and "this
    # is not a resolved Gamma record" are different problems with different
    # fixes, and printing one would send the operator down the wrong path.
    for refusal in attempts:
        typer.echo(f"{refusal.reason.value}: {refusal.detail}", err=True)
    raise typer.Exit(code=1)


@evaluate_app.command("baseline")
def evaluate_baseline(
    capture_run_id: str = typer.Argument(help="The capture_run_id to evaluate."),
    token_id: str = typer.Option(..., "--token-id", help="The token whose side is forecast."),
    db: Path = _EVAL_DB_OPTION,
    resolution_path: Path = _EVAL_RESOLUTION_OPTION,
    report_out: Path | None = _EVAL_OUT_OPTION,
    as_json: bool = typer.Option(False, "--json", help="Emit the report as JSON."),
) -> None:
    """Score market baselines from a stored capture against a recorded settlement.

    Makes no edge claim and cannot: the report's `limitations` field is required
    to be non-empty, and it always states that the scores are uncalibrated market
    baselines rather than ARGOS probabilities (ADR-0006).
    """
    if not TOKEN_ID_PATTERN.fullmatch(token_id):
        typer.echo(f"--token-id {token_id!r} does not match {TOKEN_ID_PATTERN.pattern}", err=True)
        raise typer.Exit(code=2)

    settings = _load_or_exit()
    configure_logging(settings.log_level)
    clock = LiveClock()
    code_revision, _working_tree = _code_revision()
    resolution = _load_resolution(resolution_path, token_id, clock)
    run_id = f"eval-{clock.now():%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"

    store = _open_store_or_exit(db)
    try:
        result = evaluate_capture(
            store=store,
            capture_run_id=capture_run_id,
            resolution=resolution,
            token_id=token_id,
            settings=settings,
            clock=clock,
            evaluation_run_id=run_id,
            code_revision=code_revision,
        )
    except ArgosError as exc:
        _fail(exc)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    finally:
        store.close()

    destination = report_out or db.parent / f"{run_id}.evaluation-report.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(
        orjson.dumps(result.report.to_record(), option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
    )

    if as_json:
        typer.echo(_json(result.report.to_record()))
        return

    report = result.report
    typer.echo(report.describe())
    typer.echo(f"  report     {destination}")
    typer.echo(f"  resolution {resolution.winning_outcome.value} for token {token_id}")
    typer.echo(f"  state      {report.source_state_hash}")
    for method, summary in sorted(report.calibration.items()):
        typer.echo(
            f"  {method:<12} n={summary['sample_count']} "
            f"brier={summary['mean_brier']} log_loss={summary['mean_log_loss']} "
            f"clipped={summary['clipped_count']} ece={summary['expected_calibration_error']}"
        )
    typer.echo("  limitations:")
    for limitation in report.limitations:
        typer.echo(f"    - {limitation}")


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
    except BaseExceptionGroup as group:
        # An `ArgosError` raised inside an `anyio` task group arrives wrapped in
        # an `ExceptionGroup`, which is not an `ArgosError`, so it used to sail
        # past the handler below and reach the operator as a bare traceback with
        # **no output at all**. Reproduced on the capture command: reusing an
        # existing `capture_run_id` — an ordinary, expected operator mistake
        # that the store deliberately refuses — printed an empty message and a
        # crash instead of the refusal's own reason.
        #
        # This is the "escapes the error taxonomy" class the M2 slices closed
        # three times inside the library, arriving at the CLI boundary, where
        # the consequence is not a missing ledger row but an operator told
        # nothing. Only the first matching error is reported; the rest are
        # carried in its context by the group itself.
        argos_errors = _flatten_argos_errors(group)
        if argos_errors:
            _fail(argos_errors[0])
        raise
    except ArgosError as exc:
        _fail(exc)
    except ValueError as exc:
        # An adapter argument the operator got wrong (`--limit 0`) is a usage
        # error, not a crash. Without this it reaches the user as a traceback.
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


def _flatten_argos_errors(group: BaseException) -> list[ArgosError]:
    """Collect every `ArgosError` inside an arbitrarily nested exception group.

    Task groups nest: a group can contain groups. Recursing rather than
    inspecting one level keeps the CLI honest about a failure raised two
    scopes down, which is exactly where a store or adapter error is raised
    during a capture.
    """
    found: list[ArgosError] = []
    if isinstance(group, BaseExceptionGroup):
        for inner in group.exceptions:
            found.extend(_flatten_argos_errors(inner))
    elif isinstance(group, ArgosError):
        found.append(group)
    return found


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


# git reads its own behaviour from the environment, so a provenance probe that
# inherits the ambient environment is not probing what it thinks it is.
# `GIT_DIR` alone makes `--show-toplevel` report the cwd while `HEAD` comes from
# a foreign repository, defeating the toplevel check; `GIT_CONFIG_COUNT` and
# friends inject arbitrary config, including the `status.showUntrackedFiles=no`
# that turns a dirty tree clean. All of it is stripped before either call.
_GIT_ENV_OVERRIDES: Final = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
)

# `-c` beats a config file but loses to `GIT_CONFIG_*`, so the environment is
# scrubbed above as well as pinned here. `--no-optional-locks` keeps a
# read-only provenance probe from rewriting `.git/index` mtimes.
_GIT_BASE_ARGV: Final = ("git", "--no-optional-locks", "-c", "core.fsmonitor=false")


def _git_env() -> dict[str, str]:
    scrubbed = {
        key: value
        for key, value in os.environ.items()
        if key not in _GIT_ENV_OVERRIDES and not key.startswith("GIT_CONFIG")
    }
    # A repository owned by another uid would otherwise abort with
    # "dubious ownership"; the toplevel check below is what establishes trust.
    scrubbed["GIT_TERMINAL_PROMPT"] = "0"
    return scrubbed


def _run_git(*arguments: str) -> subprocess.CompletedProcess[str] | None:
    """Run a git command from ``REPO_ROOT``, or return None if it cannot be trusted."""
    try:
        completed = subprocess.run(
            (*_GIT_BASE_ARGV, *arguments),
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        # Includes TimeoutExpired. stderr is deliberately never logged: it can
        # carry absolute paths from a foreign checkout.
        return None
    return completed if completed.returncode == 0 else None


def _code_revision() -> tuple[str | None, WorkingTreeStatus]:
    """Return this repository's git revision and working-tree cleanliness.

    git discovers repositories upward from ``cwd``. Installed as a wheel,
    ``REPO_ROOT`` is inside the virtualenv, so a bare ``rev-parse HEAD`` would
    happily return the HEAD of whatever repository happens to contain it — and
    stamp a manifest with provenance for code that never produced the run.
    The toplevel is therefore verified before the revision is trusted, and a
    dirty tree is reported explicitly rather than silently attributed to
    ``HEAD`` alone: a run built from uncommitted changes is not reproducible
    from the revision string by itself.

    ``CLEAN`` is a positive claim that the code on disk is the code at ``HEAD``,
    so every way of *not knowing* degrades to ``UNKNOWN`` rather than to
    ``CLEAN``. Three ways to hold a genuinely modified tree while
    ``git status`` reports nothing were reproduced against this function, and
    each is closed here: untracked files suppressed by
    ``status.showUntrackedFiles=no`` (pinned on the command line and scrubbed
    from the environment), and the two index bits — ``assume-unchanged`` and
    ``skip-worktree`` — which make git ignore edits to a tracked file
    altogether and are therefore treated as unknowable rather than clean.
    """
    result = _run_git("rev-parse", "--show-toplevel", "HEAD")
    if result is None:
        return None, WorkingTreeStatus.UNKNOWN

    lines = result.stdout.split()
    if len(lines) != 2:
        return None, WorkingTreeStatus.UNKNOWN
    toplevel, revision = lines
    if Path(toplevel).resolve() != REPO_ROOT:
        return None, WorkingTreeStatus.UNKNOWN
    if len(revision) != 40 or not all(char in "0123456789abcdef" for char in revision):
        return None, WorkingTreeStatus.UNKNOWN

    return revision, _working_tree_status()


def _working_tree_status() -> WorkingTreeStatus:
    """Classify the working tree, degrading to ``UNKNOWN`` whenever git may be blind."""
    listed = _run_git("ls-files", "-v")
    if listed is None:
        return WorkingTreeStatus.UNKNOWN
    for line in listed.stdout.splitlines():
        if not line:
            continue
        tag = line[0]
        # Lowercase marks assume-unchanged; 'S' marks skip-worktree. Either way
        # git will not notice an edit to that file, so "clean" is unprovable.
        if tag.islower() or tag == "S":
            return WorkingTreeStatus.UNKNOWN

    status = _run_git(
        "status",
        "--porcelain",
        "--untracked-files=normal",
        "--ignore-submodules=none",
    )
    if status is None:
        return WorkingTreeStatus.UNKNOWN
    return WorkingTreeStatus.DIRTY if status.stdout.strip() else WorkingTreeStatus.CLEAN


if __name__ == "__main__":
    app()
