"""Contract tests for the public Gamma adapter.

No test here touches the network: respx intercepts at the transport layer, and
the recorded fixture supplies the payload shape. Backoff and the overall
deadline run on a :class:`RecordingPacer`, so a retry test records the
durations tenacity asked for without spending real time (ADR-0009).
"""

from __future__ import annotations

import hashlib
import random
import socket
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
from argos.errors import (
    InvalidDurationError,
    SourceProtocolError,
    SourceTimeoutError,
    SourceUnavailableError,
)
from argos.sources import GammaClient
from argos.sources.gamma import MAX_RESPONSE_BYTES, SourceHealth

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "gamma" / "markets_list.raw.json"
BASE = "https://gamma-api.polymarket.com"
START = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


class RecordingPacer:
    """Test fake: records requested wait durations instead of spending them.

    A cancellation aimed at the request deadline is not merely simulated: this
    wraps a real `anyio.CancelScope` and cancels it once the recorded waits
    accumulate past the budget passed to `move_on_after`, so `wait` still
    passes control back to the event loop via `anyio.lowlevel.checkpoint()`
    exactly as `RealPacer` does, and `scope.cancelled_caught` behaves exactly
    as it would under real elapsed time (ADR-0009). This is what finally makes
    the deadline-during-backoff path testable without spending real seconds.

    Both methods run the same `_require_duration` guard `RealPacer` runs, and
    at the same point in the call (eagerly, not lazily on `__enter__`): a fake
    that accepted a duration the real pacer would refuse would let a defect
    (a negative or non-finite wait) sail through every test built on this fake
    while `RealPacer` would raise in production. A more forgiving fake is
    worse than no fake.
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
    """Reproduce `_SeededExponentialJitter` exactly, so a test asserts against
    the real formula and a real seed rather than a hardcoded magic float."""
    rng = random.Random(seed)
    waits = []
    for attempt_number in range(1, attempts + 1):
        jitter = rng.uniform(0, 1.0)
        exp = 2 ** (attempt_number - 1)
        waits.append(max(0.0, min(0.5 * exp + jitter, 10.0)))
    return waits


def _stream(chunks: Iterator[bytes] | AsyncIterator[bytes]) -> httpx.AsyncByteStream:
    """Wrap an iterator as a response body httpx will consume chunk by chunk."""

    class _Stream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            if isinstance(chunks, AsyncIterator):
                async for chunk in chunks:
                    yield chunk
            else:
                for chunk in chunks:
                    yield chunk

    return _Stream()


@pytest.fixture(name="raw_page")
def raw_page_fixture() -> bytes:
    return FIXTURE.read_bytes()


@pytest.fixture(name="clock")
def clock_fixture() -> ReplayClock:
    return ReplayClock(START)


@pytest.fixture(name="pacer")
def pacer_fixture() -> RecordingPacer:
    return RecordingPacer()


@pytest.fixture(name="client")
async def client_fixture(clock: ReplayClock, pacer: RecordingPacer) -> GammaClient:
    settings = Settings(http_max_attempts=3, http_timeout_seconds=5.0)
    return GammaClient(settings, clock, pacer=pacer)


# --- RecordingPacer fidelity: the fake must not be more forgiving than RealPacer ----


async def test_recording_pacer_wait_rejects_invalid_durations_like_real_pacer(
    pacer: RecordingPacer,
) -> None:
    """If the fake accepted a duration `RealPacer` would refuse, a defect that
    computed a negative or non-finite wait would sail through every retry test
    built on this fake while production raised (ADR-0009's own warning about a
    pacer that is more forgiving than the real thing)."""
    with pytest.raises(InvalidDurationError):
        await pacer.wait(-1)
    with pytest.raises(InvalidDurationError):
        await pacer.wait(float("nan"))
    assert pacer.waits == [], "a rejected wait must not be recorded as if it happened"


def test_recording_pacer_move_on_after_rejects_invalid_durations_before_entering(
    pacer: RecordingPacer,
) -> None:
    """`RealPacer.move_on_after` raises at call time, not lazily on `__enter__` —
    the fake must fail at the same point, or a caller inspecting/discarding the
    context manager before entering it would see divergent behaviour live versus
    under test."""
    with pytest.raises(InvalidDurationError):
        pacer.move_on_after(-1)
    with pytest.raises(InvalidDurationError):
        pacer.move_on_after(float("nan"))


# --- the happy path -----------------------------------------------------------------


@respx.mock
async def test_a_page_of_markets_is_returned_with_its_provenance(
    client: GammaClient, raw_page: bytes
) -> None:
    route = respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, content=raw_page))
    async with client:
        response = await client.list_markets(limit=3)

    assert route.called
    assert response.raw == raw_page
    assert response.provenance.source == "gamma"
    assert response.provenance.http_status == 200
    assert response.provenance.raw_sha256 == hashlib.sha256(raw_page).hexdigest()
    assert response.provenance.byte_length == len(raw_page)
    assert response.provenance.matches(raw_page)
    assert isinstance(response.payload, list)


@respx.mock
async def test_the_retrieval_time_comes_from_the_injected_clock(
    client: GammaClient, raw_page: bytes
) -> None:
    respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, content=raw_page))
    async with client:
        response = await client.list_markets()
    assert response.provenance.retrieved_at == START


@respx.mock
async def test_query_parameters_are_explicit(client: GammaClient, raw_page: bytes) -> None:
    route = respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, content=raw_page))
    async with client:
        await client.list_markets(limit=25, offset=50, closed=False, order="volumeNum")

    request_url = route.calls.last.request.url
    assert request_url.params["limit"] == "25"
    assert request_url.params["offset"] == "50"
    assert request_url.params["closed"] == "false"
    assert request_url.params["order"] == "volumeNum"


@respx.mock
async def test_a_single_market_can_be_fetched(client: GammaClient) -> None:
    body = (FIXTURE.parent / "market_by_id.raw.json").read_bytes()
    respx.get(f"{BASE}/markets/2063134").mock(return_value=httpx.Response(200, content=body))
    async with client:
        response = await client.get_market("2063134")
    assert response.payload["id"] == "2063134"


@respx.mock
async def test_no_credential_is_ever_sent(client: GammaClient, raw_page: bytes) -> None:
    """The research core has no credentials; sending one would be a defect (ADR-0007)."""
    route = respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, content=raw_page))
    async with client:
        await client.list_markets()

    headers = route.calls.last.request.headers
    for forbidden in ("authorization", "poly_api_key", "poly-api-key", "cookie"):
        assert forbidden not in headers


# --- retries ------------------------------------------------------------------------


@respx.mock
async def test_a_server_error_is_retried_and_then_succeeds(
    client: GammaClient, pacer: RecordingPacer, raw_page: bytes
) -> None:
    respx.get(f"{BASE}/markets").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, content=raw_page),
        ]
    )
    async with client:
        response = await client.list_markets()

    assert response.provenance.http_status == 200
    assert client.health.requests == 1
    # The wait duration is RNG-derived; with the pacer's own seeded generator
    # (`source_jitter_seed` defaults to 0) it is exact, not merely "some backoff
    # happened" — and it must come from the pacer, never from the injected clock.
    assert pacer.waits == pytest.approx(_seeded_backoff_waits(seed=0, attempts=1))


@respx.mock
async def test_the_same_seed_reproduces_the_same_backoff_sequence(clock: ReplayClock) -> None:
    """Closes ADR-0009 defect 2 on the client's own construction path: seeding is
    configuration attached to one client, not a draw from shared process state, so
    building the same client twice must retry with exactly the same waits."""
    settings = Settings(http_max_attempts=3, http_timeout_seconds=5.0, source_jitter_seed=7)
    respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(500))

    waits: list[list[float]] = []
    for _ in range(2):
        pacer = RecordingPacer()
        client = GammaClient(settings, clock, pacer=pacer)
        async with client:
            with pytest.raises(SourceUnavailableError):
                await client.list_markets()
        waits.append(pacer.waits)

    assert waits[0] == waits[1] != []
    assert waits[0] == pytest.approx(_seeded_backoff_waits(seed=7, attempts=2))


@respx.mock
async def test_a_different_seed_yields_a_different_backoff_sequence(clock: ReplayClock) -> None:
    respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(500))

    async def waits_for(seed: int) -> list[float]:
        pacer = RecordingPacer()
        settings = Settings(http_max_attempts=3, http_timeout_seconds=5.0, source_jitter_seed=seed)
        client = GammaClient(settings, clock, pacer=pacer)
        async with client:
            with pytest.raises(SourceUnavailableError):
                await client.list_markets()
        return pacer.waits

    assert await waits_for(0) != await waits_for(1)


@respx.mock
async def test_two_clients_with_the_same_seed_do_not_share_hidden_state(
    clock: ReplayClock,
) -> None:
    """Regression guard for ADR-0009 defect 2: tenacity's own `wait_exponential_jitter`
    draws from module-level `random.uniform`, so in a capture loop the backoff one
    market receives would depend on how many sibling markets retried before it.
    Draining one client's retries first must not perturb a second, independently
    constructed client seeded identically — each owns its own generator."""
    settings = Settings(http_max_attempts=3, http_timeout_seconds=5.0, source_jitter_seed=3)
    respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(500))

    busy_pacer = RecordingPacer()
    busy_client = GammaClient(settings, clock, pacer=busy_pacer)
    async with busy_client:
        with pytest.raises(SourceUnavailableError):
            await busy_client.list_markets()
        with pytest.raises(SourceUnavailableError):
            await busy_client.list_markets()  # consumes more of any shared generator

    fresh_pacer = RecordingPacer()
    fresh_client = GammaClient(settings, clock, pacer=fresh_pacer)
    async with fresh_client:
        with pytest.raises(SourceUnavailableError):
            await fresh_client.list_markets()

    assert fresh_pacer.waits == pytest.approx(_seeded_backoff_waits(seed=3, attempts=2))


@respx.mock
async def test_rate_limiting_is_retried(client: GammaClient, raw_page: bytes) -> None:
    respx.get(f"{BASE}/markets").mock(
        side_effect=[httpx.Response(429), httpx.Response(200, content=raw_page)]
    )
    async with client:
        assert (await client.list_markets()).provenance.http_status == 200


@respx.mock
async def test_the_retry_budget_is_bounded(client: GammaClient) -> None:
    route = respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(500))
    async with client:
        with pytest.raises(SourceUnavailableError) as caught:
            await client.list_markets()

    assert route.call_count == 3, "http_max_attempts=3 must mean exactly three attempts"
    assert caught.value.context["attempts"] == 3
    assert caught.value.context["last_status"] == 500
    assert client.health.failures == 1
    assert client.health.retries == 2


@respx.mock
async def test_the_overall_deadline_can_fire_during_backoff(
    clock: ReplayClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for ADR-0009 defect 4: the overall deadline previously read
    the event loop's real monotonic clock while backoff ran on the injected clock,
    so five virtual hours of `ReplayClock.sleep` inside a 0.05s deadline scope never
    tripped it (`cancelled_caught` stayed `False`). Because backoff and the deadline
    now share one pacer, a `RecordingPacer` proves this deterministically and without
    spending real time — with `http_max_attempts > 1`, unlike
    `test_a_slow_drip_response_cannot_hang_the_client_forever`, whose own docstring
    concedes a virtual clock could not demonstrate this property on the retry path.
    """
    settings = Settings(http_max_attempts=5, http_timeout_seconds=5.0)
    pacer = RecordingPacer()
    client = GammaClient(settings, clock, pacer=pacer)
    # Shrink the deadline well below the retry budget so it trips mid-backoff
    # rather than after the retries are exhausted.
    monkeypatch.setattr(GammaClient, "_deadline_seconds", property(lambda self: 2.5))

    route = respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(503))
    async with client:
        with pytest.raises(SourceTimeoutError) as caught:
            await client.list_markets()

    assert caught.value.context["deadline_seconds"] == 2.5
    assert route.call_count == 2, "the deadline must cut the call short of its 5-attempt budget"
    assert pacer.waits == pytest.approx(_seeded_backoff_waits(seed=0, attempts=2)), (
        "the deadline must be reached by an actual backoff wait, not skip straight to it"
    )
    assert client.health.retries == 1
    assert client.health.failures == 1


@respx.mock
async def test_a_transport_error_is_retried_then_reported(client: GammaClient) -> None:
    respx.get(f"{BASE}/markets").mock(side_effect=httpx.ConnectError("refused"))
    async with client:
        with pytest.raises(SourceUnavailableError):
            await client.list_markets()


@respx.mock
async def test_a_persistent_timeout_is_reported_as_a_timeout(client: GammaClient) -> None:
    """A timeout and an outage are different operational stories."""
    respx.get(f"{BASE}/markets").mock(side_effect=httpx.ReadTimeout("slow"))
    async with client:
        with pytest.raises(SourceTimeoutError) as caught:
            await client.list_markets()
    assert caught.value.context["timeout_seconds"] == 5.0


@respx.mock
async def test_a_timeout_that_clears_is_not_an_error(client: GammaClient, raw_page: bytes) -> None:
    respx.get(f"{BASE}/markets").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, content=raw_page)]
    )
    async with client:
        assert (await client.list_markets()).provenance.http_status == 200


# --- terminal failures ----------------------------------------------------------------


@respx.mock
async def test_a_missing_market_is_not_retried(client: GammaClient) -> None:
    route = respx.get(f"{BASE}/markets/0").mock(return_value=httpx.Response(404))
    async with client:
        with pytest.raises(SourceProtocolError) as caught:
            await client.get_market("0")

    assert route.call_count == 1, "a 404 will not become a 200 by asking again"
    assert caught.value.context["status"] == 404


@respx.mock
async def test_a_non_json_body_is_reported_rather_than_guessed(client: GammaClient) -> None:
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, content=b"<html>maintenance</html>")
    )
    async with client:
        with pytest.raises(SourceProtocolError) as caught:
            await client.list_markets()
    assert "JSON" in caught.value.message


@respx.mock
async def test_an_oversized_body_is_refused(
    client: GammaClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unbounded response would be a memory-exhaustion path on a public endpoint."""
    monkeypatch.setattr("argos.sources.gamma.MAX_RESPONSE_BYTES", 128)
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, content=orjson.dumps([{"x": "y" * 500}]))
    )
    async with client:
        with pytest.raises(SourceProtocolError) as caught:
            await client.list_markets()
    assert caught.value.context["limit"] == 128


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [("list_markets", {"limit": 0}), ("list_markets", {"offset": -1}), ("get_market", {})],
)
async def test_nonsense_arguments_are_refused_before_any_request(
    client: GammaClient, method: str, kwargs: dict[str, object]
) -> None:
    async with client:
        with pytest.raises(ValueError):
            if method == "get_market":
                await client.get_market("   ")
            else:
                await client.list_markets(**kwargs)  # type: ignore[arg-type]


def test_the_response_ceiling_is_declared() -> None:
    assert 0 < MAX_RESPONSE_BYTES <= 64 * 1024 * 1024


@respx.mock
async def test_the_body_is_abandoned_mid_stream_rather_than_buffered_whole(
    client: GammaClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checking the size after reading `.content` reports memory exhaustion, it does
    not prevent it: httpx buffers and transparently decompresses the whole body
    first, so a small compressed response can expand by orders of magnitude."""
    monkeypatch.setattr("argos.sources.gamma.MAX_RESPONSE_BYTES", 1024)
    delivered = 0

    def endless() -> Iterator[bytes]:
        nonlocal delivered
        for _ in range(10_000):
            delivered += 1
            yield b"x" * 512

    respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, stream=_stream(endless())))
    async with client:
        with pytest.raises(SourceProtocolError) as caught:
            await client.list_markets()

    assert caught.value.context["limit"] == 1024
    assert delivered < 10, f"the client kept reading past its limit ({delivered} chunks)"


@respx.mock
async def test_a_declared_oversize_is_refused_before_a_byte_is_read(
    client: GammaClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("argos.sources.gamma.MAX_RESPONSE_BYTES", 1024)
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, headers={"content-length": "999999"}, content=b"{}")
    )
    async with client:
        with pytest.raises(SourceProtocolError) as caught:
            await client.list_markets()
    assert caught.value.context["declared_length"] == 999999


@respx.mock
async def test_a_slow_drip_response_cannot_hang_the_client_forever(
    clock: ReplayClock,
) -> None:
    """httpx's read timeout is per *chunk*, so a server emitting a byte just often
    enough keeps one request alive indefinitely and the retry budget never applies.
    The overall deadline is the only thing that bounds it, so this test spends real
    time on purpose — a virtual clock could not demonstrate the property.
    """
    settings = Settings(http_max_attempts=1, http_timeout_seconds=0.2)
    client = GammaClient(settings, clock, pacer=RealPacer())
    assert settings.http_timeout_seconds == 0.2, "deadline is 0.2s: attempts=1, no backoff"

    async def drip() -> AsyncIterator[bytes]:
        while True:
            await anyio.sleep(0.02)  # comfortably inside the per-chunk read timeout
            yield b"x"

    respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, stream=_stream(drip())))
    started = time.monotonic()
    async with client:
        with pytest.raises(SourceTimeoutError) as caught:
            await client.list_markets()
    elapsed = time.monotonic() - started

    assert caught.value.context["deadline_seconds"] == pytest.approx(0.2)
    assert elapsed < 5.0, f"the deadline did not bound the request ({elapsed:.1f}s)"


@pytest.mark.parametrize("hostile", ["../../admin", "1?closed=true", "../..//evil.example/x", ""])
async def test_a_market_id_that_is_not_an_id_never_reaches_the_wire(
    client: GammaClient, hostile: str
) -> None:
    """httpx normalizes dot segments during base-URL merge, so `../../admin` would
    retarget the path and record the escaped URL as this record's provenance."""
    async with client:
        with pytest.raises(ValueError):
            await client.get_market(hostile)
    assert client.health == SourceHealth()


def test_the_suite_cannot_reach_the_internet_even_by_accident() -> None:
    """M1 exit criterion: unit tests do not require internet.

    `tests/conftest.py` refuses outbound IP connects for the whole session. This
    pins that guard so a future adapter test cannot quietly become a live smoke
    test. The address is RFC 5737 TEST-NET-1, so no name resolution is involved.
    """
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(RuntimeError, match="must not open a network connection"),
    ):
        sock.connect(("192.0.2.1", 80))


# --- health ---------------------------------------------------------------------------


@respx.mock
async def test_health_counters_track_the_capture(client: GammaClient, raw_page: bytes) -> None:
    respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, content=raw_page))
    async with client:
        await client.list_markets()
        await client.list_markets()

    assert client.health.requests == 2
    assert client.health.failures == 0
    assert client.health.bytes_received == 2 * len(raw_page)


@respx.mock
async def test_success_and_failure_are_counted_independently(
    client: GammaClient, raw_page: bytes
) -> None:
    """Interleaved outcomes must not overwrite one another's counters."""
    async with client:
        respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, content=raw_page))
        await client.list_markets()
        respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(500))
        with pytest.raises(SourceUnavailableError):
            await client.list_markets()
        respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, content=raw_page))
        await client.list_markets()
        respx.get(f"{BASE}/markets/0").mock(return_value=httpx.Response(404))
        with pytest.raises(SourceProtocolError):
            await client.get_market("0")

    assert client.health.requests == 2
    assert client.health.failures == 2
    assert client.health.bytes_received == 2 * len(raw_page)


@respx.mock
async def test_an_argument_error_is_not_recorded_as_a_source_failure(client: GammaClient) -> None:
    """A caller mistake is not evidence that Gamma is unhealthy."""
    async with client:
        with pytest.raises(ValueError):
            await client.list_markets(limit=0)
    assert client.health == SourceHealth()


@respx.mock
async def test_a_retry_that_eventually_succeeds_is_still_counted(
    client: GammaClient, raw_page: bytes
) -> None:
    respx.get(f"{BASE}/markets").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, content=raw_page),
        ]
    )
    async with client:
        await client.list_markets()

    assert client.health.requests == 1
    assert client.health.retries == 2


# --- lifecycle --------------------------------------------------------------------------


@respx.mock
async def test_closing_twice_is_safe(client: GammaClient, raw_page: bytes) -> None:
    respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, content=raw_page))
    await client.list_markets()
    await client.aclose()
    await client.aclose()


@respx.mock
async def test_closing_after_a_failure_is_safe(client: GammaClient) -> None:
    """A capture that dies mid-flight must still release its connections."""
    respx.get(f"{BASE}/markets").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(SourceUnavailableError):
        async with client:
            await client.list_markets()
    await client.aclose()


@respx.mock
async def test_a_closed_client_refuses_to_send_and_counts_the_failure(
    client: GammaClient, raw_page: bytes
) -> None:
    respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, content=raw_page))
    async with client:
        await client.list_markets()
    with pytest.raises(RuntimeError):
        await client.list_markets()
    assert client.health.failures == 1


# --- what the adapter must not do ---------------------------------------------------------


@respx.mock
async def test_a_redirect_is_never_followed(client: GammaClient) -> None:
    """Following a redirect would let the source move ARGOS to an unvetted host."""
    route = respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(302, headers={"location": "http://evil.example/markets"})
    )
    async with client:
        with pytest.raises(SourceProtocolError):
            await client.list_markets()
    assert route.call_count == 1
    assert all("evil.example" not in str(call.request.url) for call in route.calls)


@respx.mock
async def test_an_empty_page_is_a_valid_answer(client: GammaClient) -> None:
    """Past the last page Gamma answers `[]`; that is data, not a failure."""
    respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, content=b"[]"))
    async with client:
        response = await client.list_markets(offset=10_000)
    assert response.payload == []
    assert response.provenance.byte_length == 2
    assert client.health.requests == 1


@respx.mock
async def test_the_adapter_returns_an_unexpected_shape_verbatim(client: GammaClient) -> None:
    """Parsing belongs downstream: a schema surprise must not destroy the evidence."""
    body = b'{"data": [], "next_cursor": "abc"}'
    respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, content=body))
    async with client:
        response = await client.list_markets()
    assert response.raw == body
    assert response.payload == {"data": [], "next_cursor": "abc"}
    assert response.provenance.matches(body)
