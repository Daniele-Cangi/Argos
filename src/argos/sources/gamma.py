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

from dataclasses import dataclass, replace
from types import TracebackType
from typing import Any, Final, Self

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
RETRYABLE_STATUSES: Final = frozenset({408, 425, 429, 500, 502, 503, 504})

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

    Backoff sleeps go through the injected clock, so tests and replay runs never
    wait in real time. Retry timing is deliberately not part of any output hash.
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
        if not market_id.strip():
            raise ValueError("market_id must not be empty")
        return await self._get(f"/markets/{market_id}", {})

    async def _get(self, path: str, params: dict[str, Any]) -> GammaResponse:
        attempts = 0
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._settings.http_max_attempts),
                wait=wait_exponential_jitter(initial=0.5, max=10.0),
                retry=retry_if_exception_type(_RetryableResponse),
                sleep=self._clock.sleep,
                reraise=True,
            ):
                with attempt:
                    attempts += 1
                    return await self._attempt(path, params)
        except _RetryableResponse as error:
            self._count(failures=1, retries=attempts - 1)
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
            self._count(failures=1, retries=max(attempts - 1, 0))
            raise
        raise AssertionError("unreachable: AsyncRetrying always returns or raises")

    async def _attempt(self, path: str, params: dict[str, Any]) -> GammaResponse:
        try:
            response = await self._client.get(path, params=params)
        except httpx.TimeoutException as error:
            # A timeout is transient, so it is retried under the same bounded
            # budget; only an exhausted budget surfaces as SourceTimeoutError.
            raise _RetryableResponse(f"timeout: {type(error).__name__}", timed_out=True) from error
        except httpx.TransportError as error:
            raise _RetryableResponse(f"transport error: {type(error).__name__}") from error

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

        raw = response.content
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SourceProtocolError(
                "gamma response exceeds the accepted size",
                endpoint=path,
                byte_length=len(raw),
                limit=MAX_RESPONSE_BYTES,
            )

        try:
            payload = orjson.loads(raw)
        except orjson.JSONDecodeError as error:
            raise SourceProtocolError(
                "gamma response is not valid JSON",
                endpoint=path,
                status=response.status_code,
                byte_length=len(raw),
            ) from error

        self._count(requests=1, bytes_received=len(raw))
        return GammaResponse(
            provenance=SourceProvenanceV1(
                source=SOURCE_NAME,
                endpoint=str(response.request.url),
                http_status=response.status_code,
                retrieved_at=self._clock.now(),
                raw_sha256=sha256_hex(raw),
                byte_length=len(raw),
            ),
            raw=raw,
            payload=payload,
        )

    def _count(self, **deltas: int) -> None:
        self._health = replace(
            self._health,
            **{name: getattr(self._health, name) + value for name, value in deltas.items()},
        )


def _as_query_bool(value: bool) -> str:
    return "true" if value else "false"
