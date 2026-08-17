"""End-to-end tests for `argos capture market`, the last M2 deliverable.

No test opens a real socket or a real database file outside `tmp_path`:
`tests/conftest.py::_no_outbound_network` blocks outbound IP connects
structurally, and every test drives `argos.cli` through a fake
`WebSocketConnector`/`MarketWebSocket`, injected via `_CAPTURE_CONNECTOR_FACTORY`
-- the narrowest seam `cli.capture_market` exposes (see its own module
comment: `WebSocketConnector` is a `Protocol`, which Typer cannot turn into a
CLI parameter, so a fake cannot enter through the command's own signature the
way `--min-liquidity` enters `markets_discover`'s).

`ScriptedWebSocket`/`SingleConnectionConnector` below mirror
`tests/test_clob_ws.py`'s own fakes in spirit but are deliberately simpler: a
capture-CLI test does not need `test_clob_ws.py`'s reconnect/backoff
machinery, only "one connection, a fixed script of frames, then idle" --
`ClobMarketWsClient` itself, and its already-tested reconnect/heartbeat
behaviour, are exercised for real underneath. Every price_change event mirrors
`tests/test_capture_loop.py`'s own `_price_change_event` fixture (same
`TOKEN_YES`/`CONDITION_ID` values), itself built from the real recorded
WebSocket capture (`docs/research/fixtures/clob-ws-market-2026-08-10T184742Z.json`)
rather than invented from scratch.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import anyio
import orjson
import pytest
from typer.testing import CliRunner

import argos.cli as cli
from argos.sources.clob_ws import MarketWebSocket, WebSocketConnector
from argos.store import CompletionStatus, open_sqlite_event_store

runner = CliRunner()

TOKEN_YES = "34691510069031117755834214800869745291092253295564665516660269316660628637961"
CONDITION_ID = "0x94a39addec8bd24a2d03deeaa43bdee6a2b11eca403cd12495eb24132cc23173"


def _price_change_text(
    *,
    timestamp: str = "1786387666174",
    price: str = "0.49",
    entry_hash: str = "5ce704dea0a2123a388f1d8b432aad0058f5c479",
    asset_id: str = TOKEN_YES,
) -> str:
    """One well-formed `price_change` frame body, mirroring `test_capture_loop.py`."""
    event = {
        "event_type": "price_change",
        "market": CONDITION_ID,
        "timestamp": timestamp,
        "price_changes": [
            {
                "asset_id": asset_id,
                "price": price,
                "size": "636",
                "side": "SELL",
                "hash": entry_hash,
                "best_bid": "0.28",
                "best_ask": "0.29",
            }
        ],
    }
    return orjson.dumps(event).decode()


class ScriptedWebSocket:
    """Test fake `MarketWebSocket`: replays a fixed script, then idles forever.

    Idling (rather than raising `ConnectionClosedError`/`StopAsyncIteration`
    style) matches a real, quiet socket: nothing distinguishes "the market
    went quiet" from "the script ran out" from this class's own perspective,
    which is exactly the point -- a bound must be able to stop consumption
    even when the source itself never says "done".
    """

    def __init__(self, script: Sequence[str]) -> None:
        self._script = list(script)
        self._index = 0
        self.sent: list[str] = []
        self.closed = False

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def recv(self) -> str:
        if self._index < len(self._script):
            text = self._script[self._index]
            self._index += 1
            await anyio.lowlevel.checkpoint()
            return text
        await anyio.Event().wait()
        raise AssertionError("unreachable: the idle event is never set")  # pragma: no cover

    async def close(self) -> None:
        self.closed = True


class SingleConnectionConnector:
    """Test fake `WebSocketConnector`: always returns the same connection, never fails."""

    def __init__(self, socket: ScriptedWebSocket) -> None:
        self.socket = socket
        self.call_count = 0

    async def __call__(self) -> MarketWebSocket:
        self.call_count += 1
        return self.socket


class RefusingConnector:
    """Test fake: fails the test outright if a connection is ever attempted."""

    async def __call__(self) -> MarketWebSocket:
        raise AssertionError("a socket must not be opened for a refused command")


def _install_connector(monkeypatch: pytest.MonkeyPatch, connector: WebSocketConnector) -> None:
    monkeypatch.setattr(cli, "_CAPTURE_CONNECTOR_FACTORY", lambda settings: connector)


# --- validation happens before anything is opened -----------------------------------


def test_missing_token_id_fails_before_anything_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_connector(monkeypatch, RefusingConnector())
    db_path = tmp_path / "events.sqlite3"
    result = runner.invoke(
        cli.app, ["capture", "market", "--max-frames", "1", "--db", str(db_path)]
    )
    assert result.exit_code == 2
    assert "--token-id" in result.output
    assert not db_path.exists()


def test_neither_bound_given_fails_with_a_clear_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_connector(monkeypatch, RefusingConnector())
    db_path = tmp_path / "events.sqlite3"
    result = runner.invoke(
        cli.app, ["capture", "market", "--token-id", TOKEN_YES, "--db", str(db_path)]
    )
    assert result.exit_code == 2
    assert "--max-seconds" in result.output
    assert "--max-frames" in result.output
    assert not db_path.exists()


def test_an_invalid_token_id_is_refused_before_any_socket_or_database_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_connector(monkeypatch, RefusingConnector())
    db_path = tmp_path / "events.sqlite3"
    result = runner.invoke(
        cli.app,
        [
            "capture",
            "market",
            "--token-id",
            "not-a-token-id",
            "--max-frames",
            "1",
            "--db",
            str(db_path),
        ],
    )
    assert result.exit_code == 2
    assert "not-a-token-id" in result.output
    assert not db_path.exists()


def test_a_non_positive_bound_is_refused_as_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_connector(monkeypatch, RefusingConnector())
    db_path = tmp_path / "events.sqlite3"
    result = runner.invoke(
        cli.app,
        ["capture", "market", "--token-id", TOKEN_YES, "--max-frames", "0", "--db", str(db_path)],
    )
    assert result.exit_code == 2
    assert "--max-frames" in result.output
    assert not db_path.exists()


# --- a successful, bounded run --------------------------------------------------------


def test_a_successful_run_writes_observations_closes_the_run_and_emits_a_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = [
        _price_change_text(),  # accepted (new)
        _price_change_text(timestamp="1786387666999", entry_hash="deadbeef00"),  # accepted (new)
        _price_change_text(),  # redelivery of the first state: duplicate
    ]
    socket = ScriptedWebSocket(script)
    connector = SingleConnectionConnector(socket)
    _install_connector(monkeypatch, connector)

    db_path = tmp_path / "events.sqlite3"
    result = runner.invoke(
        cli.app,
        [
            "capture",
            "market",
            "--token-id",
            TOKEN_YES,
            "--max-frames",
            "3",
            "--db",
            str(db_path),
            "--capture-run-id",
            "test-run-success",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    report = orjson.loads(result.stdout)

    assert report["capture_run_id"] == "test-run-success"
    assert report["interrupted"] is False
    assert report["store_counts"] == {"accepted": 2, "duplicate": 1, "rejected": 0}
    assert report["loop_health"]["accepted"] == 2
    assert report["loop_health"]["duplicate"] == 1
    assert report["loop_health"]["frames_consumed"] == 3

    # A socket really was opened, and the subscribe frame was really sent --
    # this is the one place these tests distinguish "wired end to end" from
    # "the store alone was exercised".
    assert connector.call_count == 1
    assert socket.sent, "no subscribe frame was ever sent over the fake connection"

    store = open_sqlite_event_store(db_path)
    try:
        run_record = store.get_capture_run("test-run-success")
        assert run_record is not None
        assert not run_record.is_open
        assert run_record.completion_status is CompletionStatus.COMPLETED
        counts = store.counts_for_capture_run("test-run-success")
        assert (counts.accepted, counts.duplicate, counts.rejected) == (2, 1, 0)
    finally:
        store.close()

    manifest_path = Path(report["manifest_path"])
    assert manifest_path.exists()
    manifest_record: dict[str, Any] = orjson.loads(manifest_path.read_bytes())
    assert manifest_record["schema_version"] == "run_manifest.v4"
    assert manifest_record["mode"] == "capture"
    assert manifest_record["capture_run_id"] == "test-run-success"
    assert "code_revision" in manifest_record
    assert set(manifest_record["schema_versions"]) >= {
        "price_change.v1",
        "observation_envelope.v1",
        "rejected_observation.v1",
    }
    assert manifest_record["input_provenance"] == []


def test_human_readable_output_names_the_run_db_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = ScriptedWebSocket([_price_change_text()])
    _install_connector(monkeypatch, SingleConnectionConnector(socket))
    db_path = tmp_path / "events.sqlite3"
    result = runner.invoke(
        cli.app,
        ["capture", "market", "--token-id", TOKEN_YES, "--max-frames", "1", "--db", str(db_path)],
    )
    assert result.exit_code == 0, result.output
    assert "capture_run_id" in result.output
    assert str(db_path) in result.output


# --- an idle source is still bounded by real elapsed time ----------------------------


def test_a_max_seconds_bound_completes_the_run_even_against_an_idle_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No frame ever arrives; only `--max-seconds` can end this run.

    Spends a small amount of real wall-clock time on purpose, the same
    trade-off `tests/test_gamma_client.py::test_a_slow_drip_response_cannot_hang_the_client_forever`
    already makes: a virtual clock cannot demonstrate a real-elapsed-time
    bound firing.
    """
    socket = ScriptedWebSocket([])  # idles immediately
    _install_connector(monkeypatch, SingleConnectionConnector(socket))
    db_path = tmp_path / "events.sqlite3"
    result = runner.invoke(
        cli.app,
        [
            "capture",
            "market",
            "--token-id",
            TOKEN_YES,
            "--max-seconds",
            "0.2",
            "--db",
            str(db_path),
            "--capture-run-id",
            "test-run-idle",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    report = orjson.loads(result.stdout)
    assert report["store_counts"] == {"accepted": 0, "duplicate": 0, "rejected": 0}
    assert report["loop_health"]["frames_consumed"] == 0

    store = open_sqlite_event_store(db_path)
    try:
        run_record = store.get_capture_run("test-run-idle")
        assert run_record is not None
        assert run_record.completion_status is CompletionStatus.COMPLETED
    finally:
        store.close()


# --- idempotent capture_run_id --------------------------------------------------------


def test_reusing_an_existing_capture_run_id_fails_rather_than_corrupting_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "events.sqlite3"
    common_args = [
        "capture",
        "market",
        "--token-id",
        TOKEN_YES,
        "--max-frames",
        "1",
        "--db",
        str(db_path),
        "--capture-run-id",
        "reused-run",
    ]

    _install_connector(
        monkeypatch, SingleConnectionConnector(ScriptedWebSocket([_price_change_text()]))
    )
    first = runner.invoke(cli.app, common_args)
    assert first.exit_code == 0, first.output

    _install_connector(
        monkeypatch,
        SingleConnectionConnector(ScriptedWebSocket([_price_change_text(entry_hash="second00")])),
    )
    second = runner.invoke(cli.app, common_args)
    assert second.exit_code == 1
    assert "argos.storage" in second.output

    store = open_sqlite_event_store(db_path)
    try:
        # Exactly the first run's one observation: the second, refused
        # invocation must not have corrupted or duplicated anything.
        counts = store.counts_for_capture_run("reused-run")
        assert (counts.accepted, counts.duplicate, counts.rejected) == (1, 0, 0)
    finally:
        store.close()


# --- Ctrl-C closes the run cleanly, rather than leaving it dangling ------------------


def test_a_keyboard_interrupt_is_reported_rather_than_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercises `capture_market`'s own `except KeyboardInterrupt` branch directly.

    A true SIGINT-delivery test would be flaky and platform-dependent (real
    signal timing, not this command's own logic) and is exactly the kind of
    live-timing test `docs/13_TEST_STRATEGY.md` forbids. What *is* this
    command's own responsibility, and what this test actually exercises, is
    the reaction once `asyncio.run(...)` raises `KeyboardInterrupt` -- which
    is genuinely what a real Ctrl-C produces one layer up (Python's
    `asyncio.Runner` cancels the running task cooperatively on the first
    interrupt and only re-raises `KeyboardInterrupt` to `asyncio.run`'s own
    caller once that cancellation has been absorbed). `run_capture` itself
    already closes `capture_run_id` for any exception that reaches its own
    frame loop -- covered by `tests/test_capture_loop.py`, not re-proven here
    -- so this test lets the scripted, bounded capture actually run to a real
    completion first (so the row exists and is closed for a concrete,
    inspectable reason), then raises `KeyboardInterrupt` in place of
    `asyncio.run`'s normal return, to isolate this command's own handling of
    that outcome from `run_capture`'s already-tested one.
    """
    socket = ScriptedWebSocket([_price_change_text()])
    _install_connector(monkeypatch, SingleConnectionConnector(socket))

    real_asyncio_run = asyncio.run

    def _run_then_interrupt(coro: Any) -> Any:
        real_asyncio_run(coro)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.asyncio, "run", _run_then_interrupt)

    db_path = tmp_path / "events.sqlite3"
    result = runner.invoke(
        cli.app,
        [
            "capture",
            "market",
            "--token-id",
            TOKEN_YES,
            "--max-frames",
            "1",
            "--db",
            str(db_path),
            "--capture-run-id",
            "test-run-interrupted",
            "--json",
        ],
    )
    assert result.exit_code == 130
    report = orjson.loads(result.stdout)
    assert report["interrupted"] is True
    assert report["loop_health"] is None, "the coroutine never returned a CaptureHealth"

    store = open_sqlite_event_store(db_path)
    try:
        run_record = store.get_capture_run("test-run-interrupted")
        assert run_record is not None
        assert not run_record.is_open, "an interrupted run must be closed, not dangling"
    finally:
        store.close()

    manifest_path = Path(report["manifest_path"])
    assert manifest_path.exists(), "a manifest is still written for an interrupted run"
