"""Public CLOB market-channel WebSocket transport.

Scope: the wire-level connection to the public, unauthenticated market
channel only (``.claude/rules/no-execution.md``) — connect, subscribe,
heartbeat, reconnect, and yield raw frames. Everything this module knows
about the wire protocol comes from live capture, not documentation alone
(``docs/research/m2-clob-websocket.md``): the connect URL, the exact
subscribe frame shape, and the client-initiated ``PING``/server ``PONG``
plain-text heartbeat at a 10-second interval are all confirmed there against
real traffic, not merely read off ``docs.polymarket.com``.

**No JSON decoding happens here.** Core invariant 7 (raw data is immutable;
normalization is a separate, versioned step) already has its normalization
step for this source: :func:`argos.ingestion.clob_price_change.normalize_clob_price_change`
takes one already-JSON-decoded frame and is explicit that "no transport code
lives here." This module is that transport: it hands the caller one raw
:class:`MarketFrame` — text plus provenance — never a parsed mapping, and
never filters or routes by token. The research note found a frame can carry
``price_change`` entries for an *unsubscribed* sibling token, so filtering by
token id belongs to a layer that knows about subscriptions as a *domain*
concept, not to this frame-in/frame-out transport.

**What this slice deliberately does NOT build.** No ``ingest_sequence``
allocation, no capture manifest, no capture CLI. ``ingest_sequence`` is
caller-supplied throughout :mod:`argos.ingestion`, on the reasoning that a
frame can carry entries for an unsubscribed sibling token, so sequence
numbers assigned before subscription/token filtering would depend on
connection topology — that decision belongs to the capture-loop slice, not
this one. **This module therefore does not close the M2 exit criterion
"reconnect does not reset ingest sequence or silently lose manifest
state"** — there is no sequence and no manifest here to reset or lose. It
closes two narrower, transport-level deliverables instead: a public market
WebSocket adapter with subscription updates, and reconnect with bounded
exponential backoff and jitter.

**Reconnect is not proven gap-free, and this module never claims it is.**
The research note confirmed, by direct observation across two live captures,
that no sequence number exists on this channel, and recorded server
reconnect behaviour itself as UNVERIFIED — no live reconnect was ever
exercised there. This client resubscribes on every reconnect (the research
note confirms subscribing yields a fresh ``book`` snapshot on a fresh
connection) and counts every reconnect on :attr:`ClobWsHealth.reconnects`,
but a message dropped during the gap between disconnect and resubscribe is
not detectable from this channel alone — there is no sequence field to
notice the hole. Gap detection, if it exists at all for this source, has to
be built on ``(timestamp, hash)`` reconciliation against REST ``/book``, a
capture-loop concern outside this module.

**Testable without a socket.** :class:`MarketWebSocket` and
:class:`WebSocketConnector` are narrow protocols; :class:`ClobMarketWsClient`
takes a connector by injection, the same "adapters behind protocols" rule
:class:`argos.sources.clob.ClobClient` follows for HTTP.
:class:`WebsocketsConnector` wraps ``websockets.connect`` as the one
production implementation; every test in ``tests/test_clob_ws.py`` drives a
fake.

**Why ``__aenter__``/``__aexit__`` plus a plain :meth:`ClobMarketWsClient.frames`
generator, rather than one generator that owns the task group.** An earlier
draft put the heartbeat/receive task group directly inside the generator
that yields frames (``async with anyio.create_task_group(): ... yield
frame``). Reproduced directly: closing that generator early (``break`` plus
``contextlib.aclosing``, or any other explicit ``aclose()``) throws
``GeneratorExit`` into the generator while it is suspended inside the task
group's ``async with`` block, and unwinding that block requires awaiting the
task group's own internal cancel-scope exit — which anyio's asyncio backend
runs as a *separate* implicit task, tripping
``RuntimeError: Attempted to exit cancel scope in a different task than it
was entered in``. The task group's lifetime is therefore owned by
``__aenter__``/``__aexit__`` instead (a plain coroutine call chain, never
suspended at a bare ``yield``), and :meth:`frames` only ever awaits
``receive_stream.receive()`` — a plain, non-task-scoped operation — so
closing it early is always safe. Callers must drive :class:`ClobMarketWsClient`
as an async context manager (``async with client: async for frame in
client.frames(): ...``), always from the same task, matching the ordinary
contract of any async context manager.

Follows :mod:`argos.sources.clob` deliberately closely: injected ``Clock``
and ``Pacer`` (ADR-0009), an adapter-owned seeded ``random.Random`` for
backoff jitter (never the ``random`` module's shared global state), a frozen
health-counter dataclass, and no direct ``anyio.sleep``/``anyio.move_on_after``
calls (enforced by
``tests/test_boundaries.py::test_no_direct_anyio_pacing_calls``).
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator, Iterable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from types import TracebackType
from typing import Final, Protocol, Self

import anyio
import orjson
import websockets
import websockets.asyncio.client
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from argos.clock import Clock, Pacer
from argos.config import Settings
from argos.domain.market import TOKEN_ID_PATTERN
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.logging import get_logger

SOURCE_NAME: Final = "clob_market_ws"

# The library's own long-standing default (`websockets.connect`'s `max_size`
# parameter also defaults to 1 MiB), set here EXPLICITLY rather than relied
# on implicitly, per the task brief. Every real frame observed in
# `docs/research/m2-clob-websocket.md`'s two live captures — full `book`
# snapshots included — was on the order of tens of kilobytes; 1 MiB gives
# roughly one to two orders of magnitude of headroom above anything actually
# observed, while staying far below the REST adapter's 32 MiB cap, which
# bounds a fundamentally different failure mode (a compressed HTTP body, not
# a single WebSocket text frame). Cheap to raise later if a real capture ever
# shows a legitimate frame this size refused.
MAX_FRAME_BYTES: Final = 1 * 1024 * 1024

HEARTBEAT_INTERVAL_SECONDS: Final = 10.0
"""Client-initiated `PING` cadence, confirmed against live traffic — four
sends, four `PONG` replies, exactly 10 seconds apart, in a 45-second capture
(`docs/research/m2-clob-websocket.md`, "Priority question 4")."""

MAX_BACKOFF_SECONDS: Final = 10.0
_INITIAL_BACKOFF_SECONDS: Final = 0.5
_BACKOFF_JITTER_SECONDS: Final = 1.0

# Bounded, not unbounded and not zero: a stalled downstream consumer must
# apply real backpressure rather than let this client buffer frames without
# limit. Once the buffer is full, `_receive_loop`'s `send_stream.send()`
# blocks, which means `ws.recv()` is not called again until the consumer
# catches up — the client stops reading off the socket, so backpressure
# propagates all the way to the TCP receive buffer. The heartbeat task is
# independent and keeps sending `PING` even while backpressured, so a slow
# consumer does not, by itself, look like a dead connection to the source.
_FRAME_BUFFER: Final = 64

_PONG_TEXT: Final = "PONG"
_SUBSCRIBE_TYPE: Final = "market"

_logger = get_logger(__name__)


class MarketWebSocket(Protocol):
    """A single open connection to the market channel: send/recv/close only.

    Narrow by design — this is the entire surface :class:`ClobMarketWsClient`
    needs, so a test fake can implement it with no real networking, and the
    one production implementation (:class:`_WebsocketsConnection`) is a thin
    adapter over ``websockets``, not a leaky abstraction the client reaches
    through.
    """

    async def send(self, text: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self) -> None: ...


class WebSocketConnector(Protocol):
    """Opens one :class:`MarketWebSocket`. Injected so tests never open a real socket."""

    async def __call__(self) -> MarketWebSocket: ...


class _WebsocketsConnection:
    """Adapts a ``websockets`` client connection to :class:`MarketWebSocket`."""

    def __init__(self, connection: websockets.asyncio.client.ClientConnection) -> None:
        self._connection = connection

    async def send(self, text: str) -> None:
        await self._connection.send(text)

    async def recv(self) -> str:
        message = await self._connection.recv()
        return message if isinstance(message, str) else message.decode("utf-8")

    async def close(self) -> None:
        await self._connection.close()


class WebsocketsConnector:
    """Default :class:`WebSocketConnector`: wraps ``websockets.connect``.

    ``max_size`` is set explicitly rather than left at the library default —
    see :data:`MAX_FRAME_BYTES` for the reasoning and the number. The
    library's own protocol-level ping/pong (``ping_interval``/``ping_timeout``)
    is disabled: the market channel's heartbeat is the application-level,
    plain-text ``PING``/``PONG`` this client sends itself
    (``docs/research/m2-clob-websocket.md``), and running both mechanisms at
    once would be two independent liveness checks with no evidence the
    second one is wanted, or even tolerated, by the source. No credential,
    API key, or auth header is ever sent — the market channel is public.
    """

    def __init__(self, url: str, *, max_size: int = MAX_FRAME_BYTES) -> None:
        self._url = url
        self._max_size = max_size

    async def __call__(self) -> MarketWebSocket:
        connection = await websockets.connect(
            self._url,
            max_size=self._max_size,
            ping_interval=None,
            ping_timeout=None,
            user_agent_header="argos-research (public read-only market data)",
        )
        return _WebsocketsConnection(connection)


@dataclass(frozen=True)
class MarketFrame:
    """One raw text frame from the market channel, plus its provenance.

    Deliberately not decoded (see the module docstring): ``text`` is exactly
    the wire text, with no JSON parsing, no ``event_type`` inspection beyond
    the ``PONG`` heartbeat check, and no token filtering.
    """

    text: str
    received_time: datetime
    provenance: SourceProvenanceV1


@dataclass(frozen=True)
class ClobWsHealth:
    """Counters an operator can read to judge whether a capture is trustworthy.

    ``oversized_frames_refused`` is a transport-level refusal with **no
    rejection-ledger row**: no ingestion layer has seen these bytes at all,
    so there is nothing yet to build a ``RejectedObservationV1`` from. This
    is a known, documented gap the capture-loop slice must close
    (``.claude/rules/data-integrity.md`` wants every drop counted *and*
    reasoned; this counter is the "counted" half only, recorded honestly
    rather than papered over with a ledger entry this module has no
    ``capture_run_id``/``ingest_sequence`` to write one under).

    ``connection_failures`` counts every time opening a connection itself
    failed (including the very first attempt); ``reconnects`` counts every
    time this client goes around the loop again *after* having connected
    successfully at least once before — a connect failure that happens
    partway through a capture increments both.
    """

    frames_received: int = 0
    bytes_received: int = 0
    pings_sent: int = 0
    pongs_received: int = 0
    reconnects: int = 0
    oversized_frames_refused: int = 0
    connection_failures: int = 0


class _SeededBackoff:
    """Bounded exponential backoff with jitter from an adapter-owned RNG.

    Same formula as ``argos.sources.clob._SeededExponentialJitter``, not
    imported: ADR-0009's own reasoning for that duplication (a shared helper
    module would invite a future refactor back toward ``random``'s global
    state) applies identically here, and this client does not use tenacity
    at all — the reconnect loop is not "retry one call", it runs
    indefinitely alongside a concurrently scheduled heartbeat.
    """

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng

    def seconds(self, attempt: int) -> float:
        jitter = self._rng.uniform(0, _BACKOFF_JITTER_SECONDS)
        try:
            exp = float(2 ** (attempt - 1))
            result = _INITIAL_BACKOFF_SECONDS * exp + jitter
        except OverflowError:
            result = MAX_BACKOFF_SECONDS
        return max(0.0, min(result, MAX_BACKOFF_SECONDS))


class ClobMarketWsClient:
    """Async client for the public CLOB market-channel WebSocket.

    Timekeeping and pacing are separate ports (ADR-0009): ``clock`` supplies
    ``received_time``/``retrieved_at`` timestamps, and ``pacer`` supplies the
    heartbeat interval and reconnect backoff.

    Usage::

        async with ClobMarketWsClient(settings, clock, pacer=pacer,
                                       connector=connector,
                                       token_ids=["123..."]) as client:
            async for frame in client.frames():
                ...

    See the module docstring for why the async-context-manager-plus-generator
    shape is load-bearing, not stylistic.
    """

    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        *,
        pacer: Pacer,
        connector: WebSocketConnector,
        token_ids: Iterable[str] = (),
    ) -> None:
        token_id_set = frozenset(token_ids)
        for token_id in token_id_set:
            _validate_token_id(token_id)
        self._settings = settings
        self._clock = clock
        self._pacer = pacer
        self._connector = connector
        self._endpoint = settings.clob_market_ws_url
        self._token_ids: set[str] = set(token_id_set)
        self._rng = random.Random(settings.source_jitter_seed)
        self._backoff = _SeededBackoff(self._rng)
        self._health = ClobWsHealth()
        self._active_ws: MarketWebSocket | None = None
        self._task_group: TaskGroup | None = None
        self._receive_stream: MemoryObjectReceiveStream[MarketFrame] | None = None

    @property
    def health(self) -> ClobWsHealth:
        return self._health

    @property
    def subscribed_token_ids(self) -> frozenset[str]:
        return frozenset(self._token_ids)

    async def __aenter__(self) -> Self:
        send_stream, receive_stream = anyio.create_memory_object_stream[MarketFrame](_FRAME_BUFFER)
        self._receive_stream = receive_stream
        self._task_group = anyio.create_task_group()
        await self._task_group.__aenter__()
        self._task_group.start_soon(self._supervise, send_stream)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        assert self._task_group is not None
        self._task_group.cancel_scope.cancel()
        await self._task_group.__aexit__(exc_type, exc, traceback)
        if self._receive_stream is not None:
            await self._receive_stream.aclose()

    async def frames(self) -> AsyncIterator[MarketFrame]:
        """Yield raw market-channel frames until the client is closed.

        Must be called inside ``async with client:`` — see the module
        docstring for why the task group lives in ``__aenter__``/``__aexit__``
        rather than here. Safe to stop consuming early (``break``, an
        exception, or an explicit ``aclose()``): this generator holds no
        task group or cancel scope of its own.
        """
        if self._receive_stream is None:
            raise RuntimeError(
                "ClobMarketWsClient.frames() must be called inside `async with client:`"
            )
        async for frame in self._receive_stream:
            yield frame

    async def subscribe(self, token_ids: Iterable[str]) -> None:
        """Add token ids to the subscription set.

        If a connection is currently open, sends
        ``{"assets_ids": [...], "type": "market", "operation": "subscribe"}``
        immediately. ``docs/research/m2-clob-websocket.md`` describes this
        frame shape from documentation but never exercised it live — this
        behaviour is **UNVERIFIED on the wire**, recorded rather than
        assumed. New ids also join the set resent in full on every future
        (re)connect's initial subscribe frame, so a token added while
        disconnected is still picked up on the next connection.
        """
        new_ids = frozenset(token_ids)
        for token_id in new_ids:
            _validate_token_id(token_id)
        added = new_ids - self._token_ids
        self._token_ids |= new_ids
        if self._active_ws is not None and added:
            await self._send_operation(self._active_ws, added, operation="subscribe")

    async def unsubscribe(self, token_ids: Iterable[str]) -> None:
        """Remove token ids from the subscription set.

        Same UNVERIFIED-on-the-wire caveat as :meth:`subscribe`.
        """
        removed = frozenset(token_ids) & self._token_ids
        self._token_ids -= removed
        if self._active_ws is not None and removed:
            await self._send_operation(self._active_ws, removed, operation="unsubscribe")

    # --- connection lifecycle: no `yield` anywhere below this point ----------------
    #
    # Everything from here down runs as a plain coroutine inside the task group
    # opened by `__aenter__`, never inside `frames()`'s own frame. See the module
    # docstring for why that separation is load-bearing.

    async def _supervise(self, send_stream: MemoryObjectSendStream[MarketFrame]) -> None:
        connected_before = False
        backoff_attempt = 0
        async with send_stream:
            while True:
                try:
                    ws = await self._connector()
                except Exception as error:
                    self._count(connection_failures=1)
                    if connected_before:
                        self._count(reconnects=1)
                    _logger.warning(
                        "clob market ws failed to connect",
                        backoff_attempt=backoff_attempt,
                        error=str(error),
                    )
                    backoff_attempt += 1
                    await self._pacer.wait(self._backoff.seconds(backoff_attempt))
                    continue

                if connected_before:
                    self._count(reconnects=1)
                connected_before = True
                backoff_attempt = 0
                try:
                    await self._pump_cycle(ws, send_stream)
                except Exception as error:
                    _logger.warning("clob market ws connection dropped", error=str(error))
                backoff_attempt += 1
                await self._pacer.wait(self._backoff.seconds(backoff_attempt))

    async def _pump_cycle(
        self, ws: MarketWebSocket, send_stream: MemoryObjectSendStream[MarketFrame]
    ) -> None:
        self._active_ws = ws
        try:
            await self._send_subscribe(ws)
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._heartbeat_loop, ws)
                tg.start_soon(self._receive_loop, ws, send_stream.clone())
        finally:
            self._active_ws = None
            with suppress(Exception):
                await ws.close()

    async def _heartbeat_loop(self, ws: MarketWebSocket) -> None:
        while True:
            await self._pacer.wait(HEARTBEAT_INTERVAL_SECONDS)
            await ws.send("PING")
            self._count(pings_sent=1)

    async def _receive_loop(
        self, ws: MarketWebSocket, send_stream: MemoryObjectSendStream[MarketFrame]
    ) -> None:
        async with send_stream:
            while True:
                text = await ws.recv()
                frame = self._classify(text)
                if frame is not None:
                    await send_stream.send(frame)

    def _classify(self, text: str) -> MarketFrame | None:
        """Bound, then heartbeat-filter, a raw received frame.

        Order matters: the size bound applies to literally everything
        received, heartbeat traffic included, before any content is
        inspected. ``PONG`` is consumed and counted, never yielded as market
        data and never silently discarded (`.claude/rules/data-integrity.md`).
        Anything else — including a hypothetical undocumented shape — is
        handed back verbatim as raw evidence; this transport does not decode
        or judge content beyond the one documented heartbeat reply.
        """
        byte_length = len(text.encode("utf-8"))
        if byte_length > MAX_FRAME_BYTES:
            self._count(oversized_frames_refused=1)
            _logger.warning(
                "clob market ws frame exceeds the accepted size",
                byte_length=byte_length,
                limit=MAX_FRAME_BYTES,
            )
            return None
        if text == _PONG_TEXT:
            self._count(pongs_received=1)
            return None

        now = self._clock.now()
        raw = text.encode("utf-8")
        self._count(frames_received=1, bytes_received=byte_length)
        return MarketFrame(
            text=text,
            received_time=now,
            provenance=SourceProvenanceV1(
                source=SOURCE_NAME,
                endpoint=self._endpoint,
                http_status=None,
                retrieved_at=now,
                raw_sha256=sha256_hex(raw),
                byte_length=byte_length,
            ),
        )

    async def _send_subscribe(self, ws: MarketWebSocket) -> None:
        frame = {"assets_ids": sorted(self._token_ids), "type": _SUBSCRIBE_TYPE}
        await ws.send(orjson.dumps(frame).decode())

    async def _send_operation(
        self, ws: MarketWebSocket, token_ids: Iterable[str], *, operation: str
    ) -> None:
        frame = {
            "assets_ids": sorted(token_ids),
            "type": _SUBSCRIBE_TYPE,
            "operation": operation,
        }
        await ws.send(orjson.dumps(frame).decode())

    def _count(self, **deltas: int) -> None:
        self._health = replace(
            self._health,
            **{name: getattr(self._health, name) + value for name, value in deltas.items()},
        )


def _validate_token_id(token_id: str) -> None:
    if not TOKEN_ID_PATTERN.fullmatch(token_id):
        raise ValueError(f"token_id must match {TOKEN_ID_PATTERN.pattern}, got {token_id!r}")
