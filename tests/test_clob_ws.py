"""Contract tests for the public CLOB market WebSocket transport.

No test here opens a real socket: `tests/conftest.py::_no_outbound_network`
blocks outbound IP connects structurally, and every test drives a fake
`WebSocketConnector`/`MarketWebSocket` (`argos.sources.clob_ws.MarketWebSocket`,
`WebSocketConnector`) instead of `websockets.connect`.

`GatedPacer` below is the key testing device: heartbeat waits
(`HEARTBEAT_INTERVAL_SECONDS`) and reconnect backoff waits share one
`Pacer`, so a naive "wait() returns instantly" fake would let the heartbeat
loop spin unboundedly fast while a test's fake connection sits idle,
producing a nondeterministic ping count. `GatedPacer` instead blocks each
`wait()` call until the test explicitly releases that specific duration,
so heartbeat and backoff are driven independently and deterministically.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, datetime

import anyio
import orjson
import pytest
from anyio import CancelScope

from argos.clock import ReplayClock
from argos.config import Settings
from argos.sources.clob_ws import (
    HEARTBEAT_INTERVAL_SECONDS,
    MAX_BACKOFF_SECONDS,
    MAX_FRAME_BYTES,
    ClobMarketWsClient,
    ClobWsHealth,
    MarketFrame,
    WebsocketsConnector,
)

START = datetime(2026, 8, 10, 18, 47, 42, tzinfo=UTC)
TOKEN = "34691510069031117755834214800869745291092253295564665516660269316660628637961"
SIBLING_TOKEN = "95561234567890123456789012345678901234567890123456789012345677699"


class ConnectionDropped(Exception):
    """Test-only stand-in for whatever `MarketWebSocket.recv()` may raise."""


class GatedPacer:
    """Test fake: `wait()` blocks until the test releases that exact duration.

    See the module docstring for why this is more than convenience: the
    heartbeat loop and the reconnect-backoff loop share one `Pacer`, and a
    "wait resolves instantly" fake makes the heartbeat spin unboundedly fast
    while a test's fake connection is otherwise idle.
    """

    def __init__(self) -> None:
        self.waits: list[float] = []
        self._permits: dict[float, int] = {}
        self._pending: dict[float, list[anyio.Event]] = {}

    async def wait(self, seconds: float) -> None:
        self.waits.append(seconds)
        if self._permits.get(seconds, 0) > 0:
            self._permits[seconds] -= 1
            await anyio.lowlevel.checkpoint()
            return
        event = anyio.Event()
        self._pending.setdefault(seconds, []).append(event)
        await event.wait()

    def release(self, seconds: float) -> None:
        """Let one `wait(seconds)` call proceed, now or the next time it is made."""
        pending = self._pending.get(seconds)
        if pending:
            pending.pop(0).set()
            return
        self._permits[seconds] = self._permits.get(seconds, 0) + 1

    def move_on_after(self, seconds: float) -> AbstractContextManager[CancelScope, bool]:
        raise NotImplementedError("ClobMarketWsClient never calls move_on_after")


class FakeMarketWebSocket:
    """Test fake for `MarketWebSocket`: fed and dropped under explicit test control."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False
        self._send_stream, self._receive_stream = anyio.create_memory_object_stream[str](8)

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def recv(self) -> str:
        try:
            return await self._receive_stream.receive()
        except anyio.EndOfStream:
            raise ConnectionDropped("fake connection closed") from None

    async def close(self) -> None:
        self.closed = True

    async def feed(self, text: str) -> None:
        await self._send_stream.send(text)

    async def drop(self) -> None:
        await self._send_stream.aclose()


class FakeConnector:
    """Test fake `WebSocketConnector`: returns pre-seeded connections/errors in order."""

    def __init__(self, outcomes: list[FakeMarketWebSocket | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.connections: list[FakeMarketWebSocket] = []
        self.call_count = 0

    async def __call__(self) -> FakeMarketWebSocket:
        self.call_count += 1
        if not self._outcomes:
            raise AssertionError("FakeConnector exhausted: the test did not seed enough outcomes")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        self.connections.append(outcome)
        return outcome


def _subscribe_json(*token_ids: str) -> str:
    return orjson.dumps({"assets_ids": sorted(token_ids), "type": "market"}).decode()


def _make_client(
    settings: Settings,
    clock: ReplayClock,
    pacer: GatedPacer,
    connector: FakeConnector,
    *,
    token_ids: list[str] | None = None,
) -> ClobMarketWsClient:
    return ClobMarketWsClient(
        settings,
        clock,
        pacer=pacer,
        connector=connector,
        token_ids=token_ids if token_ids is not None else [TOKEN],
    )


async def _next_frame(client: ClobMarketWsClient) -> MarketFrame:
    async for frame in client.frames():
        return frame
    raise AssertionError("frames() ended without yielding")


@pytest.fixture(name="clock")
def clock_fixture() -> ReplayClock:
    return ReplayClock(START)


@pytest.fixture(name="pacer")
def pacer_fixture() -> GatedPacer:
    return GatedPacer()


@pytest.fixture(name="settings")
def settings_fixture() -> Settings:
    return Settings()


# --- subscribe on connect and on reconnect ------------------------------------------


async def test_the_subscribe_frame_is_sent_exactly_as_specified_on_connect(
    settings: Settings, clock: ReplayClock, pacer: GatedPacer
) -> None:
    ws = FakeMarketWebSocket()
    connector = FakeConnector([ws])
    client = _make_client(settings, clock, pacer, connector)

    async with client:
        await anyio.lowlevel.checkpoint()
        assert ws.sent == [_subscribe_json(TOKEN)]


async def test_the_subscribe_frame_is_sent_again_after_a_reconnect(
    settings: Settings, clock: ReplayClock, pacer: GatedPacer
) -> None:
    ws1 = FakeMarketWebSocket()
    ws2 = FakeMarketWebSocket()
    connector = FakeConnector([ws1, ws2])
    client = _make_client(settings, clock, pacer, connector)

    async with client:
        await anyio.lowlevel.checkpoint()
        assert ws1.sent == [_subscribe_json(TOKEN)]

        await ws1.drop()
        # The dropped connection surfaces inside `_supervise` as a caught
        # exception, which then calls `pacer.wait(...)` for backoff. Release
        # whatever duration it asks for -- the reproducibility test below
        # pins the exact formula; this test only cares that a reconnect
        # actually happens and resubscribes.
        await _release_next_backoff_wait(pacer)
        await anyio.lowlevel.checkpoint()

        assert ws2.sent == [_subscribe_json(TOKEN)]
        assert ws1.closed is True
        assert client.health.reconnects == 1
        assert client.health.connection_failures == 0


async def _release_next_backoff_wait(pacer: GatedPacer, *, tries: int = 20) -> float:
    """Release whichever single backoff duration `_supervise` is about to wait on.

    `_SeededBackoff.seconds` is deterministic but its exact value depends on
    the RNG draw, so this polls for the wait to appear (via `pacer.waits`)
    rather than hardcoding the formula -- the formula itself is pinned
    separately by the reproducibility test below.
    """
    seen = len(pacer.waits)
    for _ in range(tries):
        if len(pacer.waits) > seen:
            seconds = pacer.waits[-1]
            if seconds != HEARTBEAT_INTERVAL_SECONDS:
                pacer.release(seconds)
                return seconds
        await anyio.lowlevel.checkpoint()
    raise AssertionError("no backoff wait() call observed")


# --- heartbeat: PING sent, PONG counted and never yielded ---------------------------


async def test_ping_is_sent_on_the_heartbeat_interval_via_the_injected_pacer(
    settings: Settings, clock: ReplayClock, pacer: GatedPacer
) -> None:
    ws = FakeMarketWebSocket()
    connector = FakeConnector([ws])
    client = _make_client(settings, clock, pacer, connector)

    async with client:
        await anyio.lowlevel.checkpoint()
        assert client.health.pings_sent == 0
        assert "PING" not in ws.sent

        pacer.release(HEARTBEAT_INTERVAL_SECONDS)
        await anyio.lowlevel.checkpoint()
        await anyio.lowlevel.checkpoint()

        assert ws.sent[-1] == "PING"
        assert client.health.pings_sent == 1
        assert HEARTBEAT_INTERVAL_SECONDS in pacer.waits


async def test_a_pong_reply_is_counted_and_never_yielded_as_market_data(
    settings: Settings, clock: ReplayClock, pacer: GatedPacer
) -> None:
    ws = FakeMarketWebSocket()
    connector = FakeConnector([ws])
    client = _make_client(settings, clock, pacer, connector)

    async with client:
        await anyio.lowlevel.checkpoint()
        await ws.feed("PONG")
        await ws.feed('{"event_type": "book", "market": "0x1"}')

        frame = await _next_frame(client)

    assert frame.text == '{"event_type": "book", "market": "0x1"}'
    assert client.health.pongs_received == 1
    assert client.health.frames_received == 1


# --- a normal frame is yielded with correct provenance -------------------------------


async def test_a_normal_frame_is_yielded_with_correct_provenance(
    settings: Settings, clock: ReplayClock, pacer: GatedPacer
) -> None:
    ws = FakeMarketWebSocket()
    connector = FakeConnector([ws])
    client = _make_client(settings, clock, pacer, connector)
    text = '{"event_type": "price_change", "market": "0x1", "price_changes": []}'

    async with client:
        await anyio.lowlevel.checkpoint()
        await ws.feed(text)
        frame = await _next_frame(client)

    assert frame.text == text
    assert frame.received_time == START
    assert frame.provenance.source == "clob_market_ws"
    assert frame.provenance.endpoint == settings.clob_market_ws_url
    assert frame.provenance.http_status is None
    assert frame.provenance.retrieved_at == START
    assert frame.provenance.byte_length == len(text.encode("utf-8"))
    assert frame.provenance.matches(text.encode("utf-8"))


# --- oversized frames are counted, never crash, never yielded ------------------------


async def test_an_oversized_frame_is_counted_and_does_not_crash(
    settings: Settings,
    clock: ReplayClock,
    pacer: GatedPacer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("argos.sources.clob_ws.MAX_FRAME_BYTES", 16)
    ws = FakeMarketWebSocket()
    connector = FakeConnector([ws])
    client = _make_client(settings, clock, pacer, connector)

    async with client:
        await anyio.lowlevel.checkpoint()
        await ws.feed("x" * 500)
        await ws.feed("small")
        frame = await _next_frame(client)

    assert frame.text == "small"
    assert client.health.oversized_frames_refused == 1
    assert client.health.frames_received == 1


def test_the_frame_size_ceiling_is_declared() -> None:
    assert 0 < MAX_FRAME_BYTES <= 8 * 1024 * 1024


# --- reconnect: bounded, seeded, reproducible backoff --------------------------------


async def test_repeated_initial_connection_failures_back_off_with_a_bounded_seeded_wait(
    settings: Settings, clock: ReplayClock, pacer: GatedPacer
) -> None:
    ws = FakeMarketWebSocket()
    connector = FakeConnector([ConnectionRefusedError("no"), ConnectionRefusedError("no"), ws])
    client = _make_client(settings, clock, pacer, connector)

    async with client:
        first = await _release_next_backoff_wait(pacer)
        second = await _release_next_backoff_wait(pacer)
        await anyio.lowlevel.checkpoint()
        assert ws.sent == [_subscribe_json(TOKEN)]

    # Both failures happen before the *first* successful connect, so neither
    # is a "re"-connect yet -- see `test_connection_failures_and_reconnects_are_distinct_counters`.
    assert client.health.connection_failures == 2
    assert client.health.reconnects == 0
    assert 0.0 <= first <= MAX_BACKOFF_SECONDS
    assert 0.0 <= second <= MAX_BACKOFF_SECONDS


async def test_a_connect_failure_after_a_prior_success_counts_as_both(
    settings: Settings, clock: ReplayClock, pacer: GatedPacer
) -> None:
    """A connect failure that happens *after* a connection already succeeded
    once is a genuine reconnect attempt, unlike the all-initial-failure case
    above -- both counters move together here."""
    ws1 = FakeMarketWebSocket()
    ws2 = FakeMarketWebSocket()
    connector = FakeConnector([ws1, ConnectionRefusedError("no"), ws2])
    client = _make_client(settings, clock, pacer, connector)

    async with client:
        await anyio.lowlevel.checkpoint()
        await ws1.drop()
        await _release_next_backoff_wait(pacer)  # backoff after the drop itself
        await _release_next_backoff_wait(pacer)  # backoff after the failed reconnect attempt
        await anyio.lowlevel.checkpoint()
        assert ws2.sent == [_subscribe_json(TOKEN)]

    assert client.health.connection_failures == 1
    # The drop and the failed retry are each a genuine reconnect attempt.
    assert client.health.reconnects == 2


async def test_backoff_is_reproducible_across_two_clients_with_the_same_seed(
    clock: ReplayClock,
) -> None:
    async def collect_four_failure_waits(settings: Settings) -> list[float]:
        pacer = GatedPacer()
        connector = FakeConnector(
            [
                ConnectionRefusedError("no"),
                ConnectionRefusedError("no"),
                ConnectionRefusedError("no"),
                ConnectionRefusedError("no"),
                FakeMarketWebSocket(),
            ]
        )
        client = ClobMarketWsClient(
            settings, clock, pacer=pacer, connector=connector, token_ids=[TOKEN]
        )
        waits: list[float] = []
        async with client:
            for _ in range(4):
                waits.append(await _release_next_backoff_wait(pacer))
            await anyio.lowlevel.checkpoint()
        return waits

    first_run = await collect_four_failure_waits(Settings(source_jitter_seed=7))
    second_run = await collect_four_failure_waits(Settings(source_jitter_seed=7))

    assert first_run == pytest.approx(second_run)
    assert all(0.0 <= seconds <= MAX_BACKOFF_SECONDS for seconds in first_run)
    assert len(first_run) == 4


async def test_a_different_seed_produces_a_different_backoff_sequence(
    clock: ReplayClock,
) -> None:
    async def collect_two_failure_waits(settings: Settings) -> list[float]:
        pacer = GatedPacer()
        connector = FakeConnector(
            [ConnectionRefusedError("no"), ConnectionRefusedError("no"), FakeMarketWebSocket()]
        )
        client = ClobMarketWsClient(
            settings, clock, pacer=pacer, connector=connector, token_ids=[TOKEN]
        )
        waits: list[float] = []
        async with client:
            for _ in range(2):
                waits.append(await _release_next_backoff_wait(pacer))
            await anyio.lowlevel.checkpoint()
        return waits

    seed_a = await collect_two_failure_waits(Settings(source_jitter_seed=1))
    seed_b = await collect_two_failure_waits(Settings(source_jitter_seed=2))

    assert seed_a != seed_b


# --- health counters -------------------------------------------------------------------


async def test_health_starts_at_zero() -> None:
    assert ClobWsHealth() == ClobWsHealth(0, 0, 0, 0, 0, 0, 0)


async def test_connection_failures_and_reconnects_are_distinct_counters(
    settings: Settings, clock: ReplayClock, pacer: GatedPacer
) -> None:
    """The very first connect failing must not be counted as a reconnect --
    there was nothing to reconnect *from* yet."""
    connector = FakeConnector([ConnectionRefusedError("no"), FakeMarketWebSocket()])
    client = _make_client(settings, clock, pacer, connector)

    async with client:
        await _release_next_backoff_wait(pacer)
        await anyio.lowlevel.checkpoint()

    assert client.health.connection_failures == 1
    assert client.health.reconnects == 0


# --- subscription updates --------------------------------------------------------------


async def test_subscribe_adds_a_token_and_sends_an_operation_frame_when_connected(
    settings: Settings, clock: ReplayClock, pacer: GatedPacer
) -> None:
    ws = FakeMarketWebSocket()
    connector = FakeConnector([ws])
    client = _make_client(settings, clock, pacer, connector)

    async with client:
        await anyio.lowlevel.checkpoint()
        await client.subscribe([SIBLING_TOKEN])

        assert client.subscribed_token_ids == frozenset({TOKEN, SIBLING_TOKEN})
        sent = orjson.loads(ws.sent[-1])
        assert sent == {
            "assets_ids": [SIBLING_TOKEN],
            "type": "market",
            "operation": "subscribe",
        }


async def test_unsubscribe_removes_a_token_and_sends_an_operation_frame(
    settings: Settings, clock: ReplayClock, pacer: GatedPacer
) -> None:
    ws = FakeMarketWebSocket()
    connector = FakeConnector([ws])
    client = ClobMarketWsClient(
        settings, clock, pacer=pacer, connector=connector, token_ids=[TOKEN, SIBLING_TOKEN]
    )

    async with client:
        await anyio.lowlevel.checkpoint()
        await client.unsubscribe([SIBLING_TOKEN])

        assert client.subscribed_token_ids == frozenset({TOKEN})
        sent = orjson.loads(ws.sent[-1])
        assert sent == {
            "assets_ids": [SIBLING_TOKEN],
            "type": "market",
            "operation": "unsubscribe",
        }


async def test_a_hostile_token_id_is_refused_by_the_constructor(
    settings: Settings, clock: ReplayClock, pacer: GatedPacer
) -> None:
    connector = FakeConnector([])
    with pytest.raises(ValueError):
        ClobMarketWsClient(
            settings, clock, pacer=pacer, connector=connector, token_ids=["../../admin"]
        )


async def test_a_hostile_token_id_is_refused_by_subscribe(
    settings: Settings, clock: ReplayClock, pacer: GatedPacer
) -> None:
    connector = FakeConnector([FakeMarketWebSocket()])
    client = _make_client(settings, clock, pacer, connector)
    async with client:
        with pytest.raises(ValueError):
            await client.subscribe(["not-a-token"])


async def test_frames_raises_at_first_iteration_when_not_entered() -> None:
    """Calling `frames()` itself only creates the generator object (ordinary
    Python generator semantics); the `RuntimeError` fires on first iteration."""
    settings = Settings()
    clock = ReplayClock(START)
    pacer = GatedPacer()
    connector = FakeConnector([])
    client = _make_client(settings, clock, pacer, connector)
    with pytest.raises(RuntimeError):
        async for _ in client.frames():
            pass


# --- no execution/credential surface ----------------------------------------------------


async def test_no_credential_is_ever_sent(
    settings: Settings, clock: ReplayClock, pacer: GatedPacer
) -> None:
    ws = FakeMarketWebSocket()
    connector = FakeConnector([ws])
    client = _make_client(settings, clock, pacer, connector)

    async with client:
        await anyio.lowlevel.checkpoint()
        pacer.release(HEARTBEAT_INTERVAL_SECONDS)
        await anyio.lowlevel.checkpoint()
        await anyio.lowlevel.checkpoint()

    forbidden = ("authorization", "api_key", "api-key", "cookie", "poly_api_key", "secret")
    for sent in ws.sent:
        lowered = sent.lower()
        for token in forbidden:
            assert token not in lowered


async def test_websockets_connector_sends_no_auth_header(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    class _StubConnection:
        async def close(self) -> None:
            pass

    async def fake_connect(uri: str, **kwargs: object) -> _StubConnection:
        calls.append({"uri": uri, **kwargs})
        return _StubConnection()

    monkeypatch.setattr("argos.sources.clob_ws.websockets.connect", fake_connect)
    connector = WebsocketsConnector("wss://ws-subscriptions-clob.polymarket.com/ws/market")
    await connector()

    assert len(calls) == 1
    call = calls[0]
    assert "additional_headers" not in call
    assert call["max_size"] == MAX_FRAME_BYTES
    assert call["ping_interval"] is None
    assert call["ping_timeout"] is None
    user_agent = str(call["user_agent_header"])
    assert "python" not in user_agent.lower()
    for forbidden in ("authorization", "api_key", "api-key", "cookie", "secret"):
        assert forbidden not in orjson.dumps({k: str(v) for k, v in call.items()}).decode().lower()


async def test_websockets_connector_sets_max_size_explicitly() -> None:
    connector = WebsocketsConnector("wss://example.polymarket.com/ws/market", max_size=12345)
    assert connector._max_size == 12345
