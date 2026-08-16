"""Contract tests for the public CLOB REST adapter (`ClobClient`).

No test here touches the network: respx intercepts at the transport layer,
and the recorded fixture (`tests/fixtures/clob/book_yes.raw.json`, a verbatim
copy of `docs/research/fixtures/clob-book-yes-2026-08-10T181007Z.json`)
supplies the payload shape. Backoff and the overall deadline run on a
`RecordingPacer`, mirroring `tests/test_gamma_client.py` exactly (ADR-0009).
"""

from __future__ import annotations

import random
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import anyio
import httpx
import orjson
import pytest
import respx
from anyio import CancelScope

from argos.clock import RealPacer, ReplayClock
from argos.clock.base import _require_duration
from argos.config import Settings
from argos.errors import SourceProtocolError, SourceTimeoutError, SourceUnavailableError
from argos.sources import ClobClient
from argos.sources.clob import (
    MAX_RESPONSE_BYTES,
    ClobBookBadRequestError,
    ClobBookNotFoundError,
    ClobHealth,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "clob" / "book_yes.raw.json"
BASE = "https://clob.polymarket.com"
START = datetime(2026, 8, 10, 18, 10, 7, tzinfo=UTC)
TOKEN_ID = "63842529068710005716169325380315470359047749786610778647370693404952498013178"


class RecordingPacer:
    """Test fake: records requested wait durations instead of spending them.

    Copied from `tests/test_gamma_client.py` deliberately, not imported: test
    modules are not a shared library in this repository (no `tests/__init__.py`),
    and `src/argos/sources/clob.py` duplicates the pacer-consuming retry
    machinery it mirrors for the same reason (ADR-0009 — no shared module a
    future refactor could route back through `random`'s global state).
    """

    def __init__(self) -> None:
        self.waits: list[float] = []
        self._scope: CancelScope | None = None
        self._budget = 0.0
        self._elapsed = 0.0

    async def wait(self, seconds: float) -> None:
        _require_duration(seconds)
        self.waits.append(seconds)
        if self._scope is not None:
            self._elapsed += seconds
            if self._elapsed >= self._budget:
                self._scope.cancel()
        await anyio.lowlevel.checkpoint()

    def move_on_after(self, seconds: float) -> Iterator[CancelScope]:
        _require_duration(seconds)
        return self._scoped(seconds)

    @contextmanager
    def _scoped(self, seconds: float) -> Iterator[CancelScope]:
        scope = CancelScope()
        previous = (self._scope, self._budget, self._elapsed)
        self._scope, self._budget, self._elapsed = scope, seconds, 0.0
        try:
            with scope:
                yield scope
        finally:
            self._scope, self._budget, self._elapsed = previous


def _seeded_backoff_waits(seed: int, attempts: int) -> list[float]:
    rng = random.Random(seed)
    waits = []
    for attempt_number in range(1, attempts + 1):
        jitter = rng.uniform(0, 1.0)
        exp = 2 ** (attempt_number - 1)
        waits.append(max(0.0, min(0.5 * exp + jitter, 10.0)))
    return waits


@pytest.fixture(name="raw_book")
def raw_book_fixture() -> bytes:
    return FIXTURE.read_bytes()


def _stream(chunks: AsyncIterator[bytes]) -> httpx.AsyncByteStream:
    """Wrap an async iterator as a body httpx consumes chunk by chunk."""

    class _Stream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            async for chunk in chunks:
                yield chunk

    return _Stream()


@pytest.fixture(name="clock")
def clock_fixture() -> ReplayClock:
    return ReplayClock(START)


@pytest.fixture(name="pacer")
def pacer_fixture() -> RecordingPacer:
    return RecordingPacer()


@pytest.fixture(name="client")
async def client_fixture(clock: ReplayClock, pacer: RecordingPacer) -> ClobClient:
    settings = Settings(http_max_attempts=3, http_timeout_seconds=5.0)
    return ClobClient(settings, clock, pacer=pacer)


# --- the happy path -----------------------------------------------------------------


@respx.mock
async def test_a_book_snapshot_is_returned_with_its_provenance(
    client: ClobClient, raw_book: bytes
) -> None:
    route = respx.get(f"{BASE}/book").mock(return_value=httpx.Response(200, content=raw_book))
    async with client:
        response = await client.get_book(TOKEN_ID)

    assert route.called
    assert route.calls.last.request.url.params["token_id"] == TOKEN_ID
    assert response.raw == raw_book
    assert response.provenance.source == "clob_rest"
    assert response.provenance.http_status == 200
    assert response.provenance.matches(raw_book)
    assert response.payload["asset_id"] == TOKEN_ID
    assert response.provenance.retrieved_at == START


@respx.mock
async def test_the_counter_intuitive_wire_order_is_not_hidden_by_the_client(
    client: ClobClient, raw_book: bytes
) -> None:
    """The client returns the payload verbatim; a naive `[0]` read of either side
    would get the *worst* price on this fixture, not the best, per
    docs/research/m2-clob-rest-book.md. This pins that the wire shape survives
    unmodified through this layer — sorting into "best first" is
    `OrderBookSnapshotV1`'s job (`argos.domain.orderbook`), not this client's."""
    respx.get(f"{BASE}/book").mock(return_value=httpx.Response(200, content=raw_book))
    async with client:
        response = await client.get_book(TOKEN_ID)

    bids = response.payload["bids"]
    asks = response.payload["asks"]
    naive_best_bid = bids[0]["price"]
    naive_best_ask = asks[0]["price"]
    true_best_bid = bids[-1]["price"]
    true_best_ask = asks[-1]["price"]
    assert naive_best_bid == "0.01", "bids are ascending on the wire: [0] is the worst bid"
    assert naive_best_ask == "0.99", "asks are descending on the wire: [0] is the worst ask"
    assert true_best_bid == "0.42"
    assert true_best_ask == "0.43"


@respx.mock
async def test_no_credential_is_ever_sent(client: ClobClient, raw_book: bytes) -> None:
    route = respx.get(f"{BASE}/book").mock(return_value=httpx.Response(200, content=raw_book))
    async with client:
        await client.get_book(TOKEN_ID)

    headers = route.calls.last.request.headers
    for forbidden in ("authorization", "poly_api_key", "poly-api-key", "cookie"):
        assert forbidden not in headers


@respx.mock
async def test_an_explicit_user_agent_is_always_sent(client: ClobClient, raw_book: bytes) -> None:
    """docs/research/m2-clob-websocket.md: REST `/book` returned 403 for Python's
    default urllib User-Agent while curl's default and a browser-like UA both
    succeeded (UNVERIFIED as a general rule, but the adapter must not omit one)."""
    route = respx.get(f"{BASE}/book").mock(return_value=httpx.Response(200, content=raw_book))
    async with client:
        await client.get_book(TOKEN_ID)

    user_agent = route.calls.last.request.headers["user-agent"]
    assert "python-urllib" not in user_agent.lower()
    assert user_agent == "argos-research (public read-only market data)"


# --- 404 vs 400: distinguishable and separately counted -----------------------------


@respx.mock
async def test_a_404_is_reported_as_ambiguous_not_as_market_closed(client: ClobClient) -> None:
    route = respx.get(f"{BASE}/book").mock(
        return_value=httpx.Response(
            404, json={"error": "No orderbook exists for the requested token id"}
        )
    )
    async with client:
        with pytest.raises(ClobBookNotFoundError) as caught:
            await client.get_book(TOKEN_ID)

    assert route.call_count == 1, "a 404 will not become a 200 by asking again"
    assert caught.value.context["status"] == 404
    assert "market closed" not in caught.value.message.lower()
    assert client.health.not_found == 1
    assert client.health.failures == 0, "an ambiguous-but-expected outcome is not a source failure"
    assert client.health.bad_request == 0


@respx.mock
async def test_a_400_is_a_distinguishable_outcome_from_a_404(client: ClobClient) -> None:
    route = respx.get(f"{BASE}/book").mock(
        return_value=httpx.Response(400, json={"error": "Invalid token id"})
    )
    async with client:
        with pytest.raises(ClobBookBadRequestError) as caught:
            await client.get_book(TOKEN_ID)

    assert route.call_count == 1
    assert caught.value.context["status"] == 400
    assert client.health.bad_request == 1
    assert client.health.not_found == 0
    assert client.health.failures == 0


@respx.mock
async def test_404_and_400_are_never_the_same_exception_type(client: ClobClient) -> None:
    assert not issubclass(ClobBookNotFoundError, ClobBookBadRequestError)
    assert not issubclass(ClobBookBadRequestError, ClobBookNotFoundError)
    assert issubclass(ClobBookNotFoundError, SourceProtocolError)
    assert issubclass(ClobBookBadRequestError, SourceProtocolError)


# --- retries and the overall deadline ------------------------------------------------


@respx.mock
async def test_a_server_error_is_retried_and_then_succeeds(
    client: ClobClient, pacer: RecordingPacer, raw_book: bytes
) -> None:
    respx.get(f"{BASE}/book").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, content=raw_book)]
    )
    async with client:
        response = await client.get_book(TOKEN_ID)

    assert response.provenance.http_status == 200
    assert client.health.requests == 1
    assert client.health.retries == 1
    assert pacer.waits == pytest.approx(_seeded_backoff_waits(seed=0, attempts=1))


@respx.mock
async def test_the_retry_budget_is_bounded(client: ClobClient) -> None:
    route = respx.get(f"{BASE}/book").mock(return_value=httpx.Response(500))
    async with client:
        with pytest.raises(SourceUnavailableError) as caught:
            await client.get_book(TOKEN_ID)

    assert route.call_count == 3, "http_max_attempts=3 must mean exactly three attempts"
    assert caught.value.context["attempts"] == 3
    assert client.health.failures == 1
    assert client.health.retries == 2


@respx.mock
async def test_a_persistent_timeout_is_reported_as_a_timeout(client: ClobClient) -> None:
    respx.get(f"{BASE}/book").mock(side_effect=httpx.ReadTimeout("slow"))
    async with client:
        with pytest.raises(SourceTimeoutError) as caught:
            await client.get_book(TOKEN_ID)
    assert caught.value.context["timeout_seconds"] == 5.0
    assert client.health.failures == 1


# --- terminal failures ----------------------------------------------------------------


@respx.mock
async def test_a_non_json_body_is_reported_rather_than_guessed(client: ClobClient) -> None:
    respx.get(f"{BASE}/book").mock(return_value=httpx.Response(200, content=b"<html>down</html>"))
    async with client:
        with pytest.raises(SourceProtocolError) as caught:
            await client.get_book(TOKEN_ID)
    assert "JSON" in caught.value.message


@respx.mock
async def test_an_oversized_body_is_refused(
    client: ClobClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("argos.sources.clob.MAX_RESPONSE_BYTES", 128)
    respx.get(f"{BASE}/book").mock(
        return_value=httpx.Response(200, content=orjson.dumps({"x": "y" * 500}))
    )
    async with client:
        with pytest.raises(SourceProtocolError) as caught:
            await client.get_book(TOKEN_ID)
    assert caught.value.context["limit"] == 128


def test_the_response_ceiling_is_declared() -> None:
    assert 0 < MAX_RESPONSE_BYTES <= 64 * 1024 * 1024


@pytest.mark.parametrize(
    "hostile", ["../../admin", "1?closed=true", "not-a-token", "-5", "5.0", ""]
)
async def test_a_token_id_that_is_not_a_token_id_never_reaches_the_wire(
    client: ClobClient, hostile: str
) -> None:
    """httpx normalizes dot segments during base-URL merge, the same M1
    precedent `GammaClient.get_market` closed, and a malformed id wastes a
    request the source answers 400 for anyway."""
    async with client:
        with pytest.raises(ValueError):
            await client.get_book(hostile)
    assert client.health == ClobHealth()


# --- health ---------------------------------------------------------------------------


@respx.mock
async def test_health_counters_track_the_capture(client: ClobClient, raw_book: bytes) -> None:
    respx.get(f"{BASE}/book").mock(return_value=httpx.Response(200, content=raw_book))
    async with client:
        await client.get_book(TOKEN_ID)
        await client.get_book(TOKEN_ID)

    assert client.health.requests == 2
    assert client.health.failures == 0
    assert client.health.bytes_received == 2 * len(raw_book)


@respx.mock
async def test_success_not_found_and_bad_request_are_counted_independently(
    client: ClobClient, raw_book: bytes
) -> None:
    async with client:
        respx.get(f"{BASE}/book").mock(return_value=httpx.Response(200, content=raw_book))
        await client.get_book(TOKEN_ID)
        respx.get(f"{BASE}/book").mock(return_value=httpx.Response(404))
        with pytest.raises(ClobBookNotFoundError):
            await client.get_book(TOKEN_ID)
        respx.get(f"{BASE}/book").mock(return_value=httpx.Response(400))
        with pytest.raises(ClobBookBadRequestError):
            await client.get_book(TOKEN_ID)

    assert client.health.requests == 1
    assert client.health.not_found == 1
    assert client.health.bad_request == 1
    assert client.health.failures == 0


# --- regressions the M1 HIGH properties needed and did not have -------------------
#
# Security review confirmed by measurement that both M1 HIGH classes are inherited
# by this adapter as real properties (gzip bomb refused at 174.7 MiB peak RSS in
# 0.186 s, indistinguishable from gamma.py; the overall deadline fires inside a
# real backoff sleep). What it also found is that nothing here *pinned* them, so a
# future edit to clob.py could regress them silently. These are those pins.


@respx.mock
async def test_the_overall_deadline_can_fire_during_backoff(
    clock: ReplayClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-M2 pacing slice found the M1 deadline fix was only ever exercised
    on the zero-retry path, because the regression test pinned
    `http_max_attempts=1` and `anyio.move_on_after` read the event loop's clock
    rather than the injected one. This is the retry path, deterministically."""
    settings = Settings(http_max_attempts=5, http_timeout_seconds=5.0)
    pacer = RecordingPacer()
    client = ClobClient(settings, clock, pacer=pacer)
    monkeypatch.setattr(ClobClient, "_deadline_seconds", property(lambda self: 2.5))

    route = respx.get(f"{BASE}/book").mock(return_value=httpx.Response(503))
    async with client:
        with pytest.raises(SourceTimeoutError) as caught:
            await client.get_book(TOKEN_ID)

    assert caught.value.context["deadline_seconds"] == 2.5
    assert route.call_count == 2, "the deadline must cut the call short of its 5-attempt budget"
    assert pacer.waits == pytest.approx(_seeded_backoff_waits(seed=0, attempts=2)), (
        "the deadline must be reached by an actual backoff wait, not skip straight to it"
    )


@respx.mock
async def test_a_slow_drip_response_cannot_hang_the_client_forever(clock: ReplayClock) -> None:
    """httpx's read timeout is per *chunk*, so a server emitting a byte just often
    enough keeps one request alive indefinitely and the retry budget never applies.
    This spends real time on purpose — a virtual clock cannot demonstrate it."""
    settings = Settings(http_max_attempts=1, http_timeout_seconds=0.2)
    client = ClobClient(settings, clock, pacer=RealPacer())

    async def drip() -> AsyncIterator[bytes]:
        while True:
            await anyio.sleep(0.02)  # comfortably inside the per-chunk read timeout
            yield b"x"

    respx.get(f"{BASE}/book").mock(return_value=httpx.Response(200, stream=_stream(drip())))
    started = time.monotonic()
    async with client:
        with pytest.raises(SourceTimeoutError):
            await client.get_book(TOKEN_ID)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"the deadline did not bound the request ({elapsed:.1f}s)"


@respx.mock
async def test_a_redirect_is_never_followed(client: ClobClient) -> None:
    """Following a redirect would let the source move ARGOS to an unvetted host."""
    route = respx.get(f"{BASE}/book").mock(
        return_value=httpx.Response(302, headers={"location": "http://evil.example/book"})
    )
    async with client:
        with pytest.raises(SourceProtocolError):
            await client.get_book(TOKEN_ID)
    assert route.call_count == 1
    assert all("evil.example" not in str(call.request.url) for call in route.calls)


@respx.mock
async def test_two_clients_with_the_same_seed_do_not_share_hidden_state(
    clock: ReplayClock,
) -> None:
    """ADR-0009 excised exactly this defect once already: backoff drawn from a
    module-global RNG made one market's retry timing depend on how many siblings
    had already retried — the CLAUDE.md-prohibited hidden global state that
    varies with processing order. Seeding the global between runs must change
    nothing."""
    waits: list[list[float]] = []
    for global_seed in (12345, 999):
        random.seed(global_seed)
        pacer = RecordingPacer()
        client = ClobClient(Settings(http_max_attempts=3), clock, pacer=pacer)
        respx.get(f"{BASE}/book").mock(return_value=httpx.Response(503))
        async with client:
            with pytest.raises(SourceUnavailableError):
                await client.get_book(TOKEN_ID)
        waits.append(pacer.waits)

    assert waits[0] == pytest.approx(waits[1]), (
        "backoff must come from the adapter's own seeded RNG, not the module global"
    )
