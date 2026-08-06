"""Contract tests for the public Gamma adapter.

No test here touches the network: respx intercepts at the transport layer, and
the recorded fixture supplies the payload shape. Backoff runs on a `ReplayClock`,
so a retry test costs virtual time rather than wall-clock seconds.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import httpx
import orjson
import pytest
import respx

from argos.clock import ReplayClock
from argos.config import Settings
from argos.errors import SourceProtocolError, SourceTimeoutError, SourceUnavailableError
from argos.sources import GammaClient
from argos.sources.gamma import MAX_RESPONSE_BYTES

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "gamma" / "markets_list.raw.json"
BASE = "https://gamma-api.polymarket.com"
START = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


@pytest.fixture(name="raw_page")
def raw_page_fixture() -> bytes:
    return FIXTURE.read_bytes()


@pytest.fixture(name="clock")
def clock_fixture() -> ReplayClock:
    return ReplayClock(START)


@pytest.fixture(name="client")
async def client_fixture(clock: ReplayClock) -> GammaClient:
    settings = Settings(http_max_attempts=3, http_timeout_seconds=5.0)
    return GammaClient(settings, clock)


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
    client: GammaClient, clock: ReplayClock, raw_page: bytes
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
    assert clock.now() > START, "backoff must run on the injected clock"


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
