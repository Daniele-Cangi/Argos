"""Public Gamma REST adapter for market and event metadata.

Read-only by construction: the client sends no credentials, exposes only market
metadata endpoints, and has no code path that could place, cancel, or sign
anything (ADR-0007, ``.claude/rules/no-execution.md``).

Every response is returned as the exact bytes received plus a
:class:`~argos.domain.provenance.SourceProvenanceV1` describing where and when
they were retrieved and what they hashed to. Parsing happens downstream, so a
schema surprise never destroys the evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from types import TracebackType
from typing import Any, Final, Self

import anyio
import httpx
import orjson
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt
from tenacity.wait import wait_exponential_jitter

from argos.clock import Clock
from argos.config import Settings
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.errors import SourceProtocolError, SourceTimeoutError, SourceUnavailableError
from argos.logging import get_logger

SOURCE_NAME: Final = "gamma"
MAX_RESPONSE_BYTES: Final = 32 * 1024 * 1024
MAX_BACKOFF_SECONDS: Final = 10.0
RETRYABLE_STATUSES: Final = frozenset({408, 425, 429, 500, 502, 503, 504})
MARKET_ID_PATTERN: Final = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")

_logger = get_logger(__name__)


@dataclass(frozen=True)
class GammaResponse:
    """Raw bytes, their provenance, and the decoded JSON payload."""

    provenance: SourceProvenanceV1
    raw: bytes
    payload: Any


@dataclass(frozen=True)
class SourceHealth:
    """Counters an operator can read to judge whether a capture is trustworthy."""

    requests: int = 0
    retries: int = 0
    failures: int = 0
    bytes_received: int = 0


class _RetryableResponse(Exception):
    """Internal signal: this attempt failed in a way worth retrying."""

    def __init__(self, detail: str, *, status: int | None = None, timed_out: bool = False) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.timed_out = timed_out


class GammaClient:
    """Async client for the public Gamma API.

    Backoff sleeps go through the injected clock, so a test can pass a
    :class:`~argos.clock.ReplayClock` and pay no wall-clock seconds for a retry
    path.

    **Do not hand this client the replay scheduler's clock.** ``ReplayClock.sleep``
    advances virtual time, and the jitter in the backoff is not seeded — so an
    adapter retrying under the scheduler's clock would move replay time by a
    nondeterministic amount and break the M3 requirement that identical input
    produce an identical output hash. Separating pacing from timekeeping is an
    open decision recorded in ``docs/BACKLOG.md`` against M2.
    """

    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._health = SourceHealth()
        self._client = httpx.AsyncClient(
            base_url=settings.gamma_base_url,
            timeout=httpx.Timeout(settings.http_timeout_seconds),
            transport=transport,
            headers={
                "accept": "application/json",
                "user-agent": "argos-research (public read-only market data)",
            },
            follow_redirects=False,
        )

    @property
    def health(self) -> SourceHealth:
        return self._health

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_markets(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        closed: bool | None = False,
        order: str | None = "volumeNum",
        ascending: bool = False,
    ) -> GammaResponse:
        """Fetch a page of markets."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if offset < 0:
            raise ValueError("offset must not be negative")
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if closed is not None:
            params["closed"] = _as_query_bool(closed)
        if order is not None:
            params["order"] = order
            params["ascending"] = _as_query_bool(ascending)
        return await self._get("/markets", params)

    async def get_market(self, market_id: str) -> GammaResponse:
        """Fetch a single market by its Gamma id."""
        if not MARKET_ID_PATTERN.match(market_id):
            # httpx normalizes dot segments during base-URL merge, so an id like
            # "../../admin" would silently retarget the path and misreport the
            # endpoint in the provenance record.
            raise ValueError(f"market_id must match {MARKET_ID_PATTERN.pattern}, got {market_id!r}")
        return await self._get(f"/markets/{market_id}", {})

    @property
    def _deadline_seconds(self) -> float:
        """An upper bound on one logical request, retries and backoff included.

        The per-request timeout is httpx's *read* timeout, which is per chunk: a
        server dripping one byte per interval keeps a request alive forever. This
        deadline is the thing that actually bounds the call.
        """
        attempts = self._settings.http_max_attempts
        return self._settings.http_timeout_seconds * attempts + MAX_BACKOFF_SECONDS * (attempts - 1)

    async def _get(self, path: str, params: dict[str, Any]) -> GammaResponse:
        try:
            with anyio.fail_after(self._deadline_seconds):
                return await self._get_within_deadline(path, params)
        except TimeoutError as error:
            self._count(failures=1)
            raise SourceTimeoutError(
                "gamma exceeded the overall deadline for this request",
                endpoint=path,
                deadline_seconds=self._deadline_seconds,
            ) from error

    async def _get_within_deadline(self, path: str, params: dict[str, Any]) -> GammaResponse:
        attempts = 0
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._settings.http_max_attempts),
                wait=wait_exponential_jitter(initial=0.5, max=MAX_BACKOFF_SECONDS),
                retry=retry_if_exception_type(_RetryableResponse),
                sleep=self._clock.sleep,
                reraise=True,
            ):
                with attempt:
                    if attempts:
                        # Counted here, not only on exhaustion: a capture that
                        # recovered after two retries still ran against a degraded
                        # endpoint, and the operator needs to see that.
                        self._count(retries=1)
                    attempts += 1
                    return await self._attempt(path, params)
        except _RetryableResponse as error:
            self._count(failures=1)
            _logger.warning(
                "gamma request exhausted its retries",
                path=path,
                attempts=attempts,
                status=error.status,
                timed_out=error.timed_out,
            )
            if error.timed_out:
                raise SourceTimeoutError(
                    "gamma timed out on every attempt",
                    endpoint=path,
                    attempts=attempts,
                    timeout_seconds=self._settings.http_timeout_seconds,
                ) from error
            raise SourceUnavailableError(
                "gamma did not answer successfully within its retry budget",
                endpoint=path,
                attempts=attempts,
                last_status=error.status,
            ) from error
        except Exception:
            self._count(failures=1)
            raise
        raise AssertionError("unreachable: AsyncRetrying always returns or raises")

    async def _attempt(self, path: str, params: dict[str, Any]) -> GammaResponse:
        try:
            async with self._client.stream("GET", path, params=params) as response:
                if response.status_code in RETRYABLE_STATUSES:
                    raise _RetryableResponse(
                        f"gamma answered {response.status_code}", status=response.status_code
                    )
                if response.status_code >= 400:
                    raise SourceProtocolError(
                        "gamma rejected the request",
                        endpoint=path,
                        status=response.status_code,
                    )
                raw = await self._read_bounded(response, path)
                request_url = str(response.request.url)
                status = response.status_code
        except httpx.TimeoutException as error:
            # A timeout is transient, so it is retried under the same bounded
            # budget; only an exhausted budget surfaces as SourceTimeoutError.
            raise _RetryableResponse(f"timeout: {type(error).__name__}", timed_out=True) from error
        except httpx.TransportError as error:
            raise _RetryableResponse(f"transport error: {type(error).__name__}") from error

        try:
            payload = orjson.loads(raw)
        except orjson.JSONDecodeError as error:
            raise SourceProtocolError(
                "gamma response is not valid JSON",
                endpoint=path,
                status=status,
                byte_length=len(raw),
            ) from error

        self._count(requests=1, bytes_received=len(raw))
        return GammaResponse(
            provenance=SourceProvenanceV1(
                source=SOURCE_NAME,
                endpoint=request_url,
                http_status=status,
                retrieved_at=self._clock.now(),
                raw_sha256=sha256_hex(raw),
                byte_length=len(raw),
            ),
            raw=raw,
            payload=payload,
        )

    async def _read_bounded(self, response: httpx.Response, path: str) -> bytes:
        """Read the body, aborting as soon as it exceeds the accepted size.

        Reading ``response.content`` would buffer and transparently *decompress*
        the whole body before any size check could run: a small compressed
        response can expand by orders of magnitude, so a check after the fact
        reports memory exhaustion rather than preventing it.
        """
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
            raise SourceProtocolError(
                "gamma declared a response larger than the accepted size",
                endpoint=path,
                declared_length=int(declared),
                limit=MAX_RESPONSE_BYTES,
            )

        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise SourceProtocolError(
                    "gamma response exceeds the accepted size",
                    endpoint=path,
                    byte_length_so_far=total,
                    limit=MAX_RESPONSE_BYTES,
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def _count(self, **deltas: int) -> None:
        self._health = replace(
            self._health,
            **{name: getattr(self._health, name) + value for name, value in deltas.items()},
        )


def _as_query_bool(value: bool) -> str:
    return "true" if value else "false"
