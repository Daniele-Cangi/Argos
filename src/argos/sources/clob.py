"""Public CLOB REST adapter for order-book snapshots.

Read-only by construction, per ``.claude/rules/no-execution.md``: the client
sends no credentials, exposes only the public ``GET /book`` endpoint, and has
no code path that could place, cancel, or sign anything (ADR-0007). It follows
:class:`argos.sources.gamma.GammaClient`'s patterns deliberately closely —
streamed body with an incremental size cap and a ``content-length``
pre-check, one overall deadline via ``Pacer.move_on_after`` bounding retries
included, seeded jitter via an adapter-owned :class:`random.Random`, a health
counter set, and id validation before URL interpolation — because those exist
to close real defects the M1 security review found, not by convention.

Every response is returned as the exact bytes received plus a
:class:`~argos.domain.provenance.SourceProvenanceV1`. Parsing into
:class:`~argos.domain.orderbook.OrderBookSnapshotV1` happens in
:mod:`argos.ingestion.clob_book`, not here, for the same reason
``GammaClient`` never parses a market: a schema surprise must not destroy the
evidence this adapter already has in hand.

Findings from ``docs/research/m2-clob-rest-book.md`` this module is built
against, not merely aware of:

- **404 is ambiguous by construction** — a closed market, an unknown token
  id, and a syntactically valid token id that never had a book are
  indistinguishable from the response alone. This adapter never infers
  "market closed" from a 404; :class:`ClobBookNotFoundError` is a distinct,
  counted outcome, and lifecycle determination stays with the Gamma metadata
  M1 already captures.
- **400 is a separate, distinguishable outcome** (``:class:`ClobBookBadRequestError```),
  raised before any book lookup happens on the source's side. ``token_id`` is
  validated against :data:`argos.domain.market.TOKEN_ID_PATTERN` before it
  reaches the wire, so this is expected to be rare, not absent — the source is
  the final authority on what it accepts.
- The REST 403-without-explicit-User-Agent finding from
  ``docs/research/m2-clob-websocket.md`` (Python's default ``urllib`` UA was
  refused; ``curl``'s default and a browser-like UA both succeeded) is
  UNVERIFIED as a general rule, but this client sets an explicit,
  descriptive User-Agent regardless, matching ``GammaClient``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from types import TracebackType
from typing import Any, Final, Self

import httpx
import orjson
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type, stop_after_attempt
from tenacity.wait import wait_base

from argos.clock import Clock, Pacer
from argos.config import Settings
from argos.domain.market import TOKEN_ID_PATTERN
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.errors import SourceProtocolError, SourceTimeoutError, SourceUnavailableError
from argos.logging import get_logger

SOURCE_NAME: Final = "clob_rest"
MAX_RESPONSE_BYTES: Final = 32 * 1024 * 1024
MAX_BACKOFF_SECONDS: Final = 10.0
RETRYABLE_STATUSES: Final = frozenset({408, 425, 429, 500, 502, 503, 504})

_logger = get_logger(__name__)


class ClobBookNotFoundError(SourceProtocolError):
    """No orderbook exists for the requested token id.

    404 is ambiguous by construction (``docs/research/m2-clob-rest-book.md``,
    "Errors and unknown tokens"): a closed market, an unknown token id, and a
    syntactically valid token id that never had a book all answer this way.
    This type exists so a caller can distinguish "no book" from every other
    protocol failure without inferring market lifecycle from an HTTP status —
    that determination belongs to the Gamma metadata M1 already captures, not
    to this adapter.
    """

    code = "argos.clob_book_not_found"


class ClobBookBadRequestError(SourceProtocolError):
    """The request itself was rejected as malformed before any book lookup.

    Distinguishable from :class:`ClobBookNotFoundError` on purpose: a 400
    means the source refused the request shape, not that it looked for a book
    and found none (``docs/research/m2-clob-rest-book.md``: ``/book`` with the
    ``token_id`` parameter omitted answers 400 with a different body than a
    404). ``token_id`` is validated against
    :data:`argos.domain.market.TOKEN_ID_PATTERN` before this client ever sends
    a request, so this is expected to be rare in practice, not absent.
    """

    code = "argos.clob_book_bad_request"


@dataclass(frozen=True)
class ClobBookResponse:
    """Raw bytes, their provenance, and the decoded JSON payload of a ``/book`` response."""

    provenance: SourceProvenanceV1
    raw: bytes
    payload: Any


@dataclass(frozen=True)
class ClobHealth:
    """Counters an operator can read to judge whether a capture is trustworthy.

    ``not_found`` and ``bad_request`` are deliberately separate from
    ``failures``: a 404 is an ambiguous but expected outcome on this source
    (docs/research/m2-clob-rest-book.md), not evidence the adapter itself is
    unhealthy, so folding it into ``failures`` would make a quiet, ordinary
    capture session look like a degraded one.
    """

    requests: int = 0
    retries: int = 0
    failures: int = 0
    bytes_received: int = 0
    not_found: int = 0
    bad_request: int = 0


class _SeededExponentialJitter(wait_base):
    """Exponential backoff with jitter drawn from an adapter-owned generator.

    Identical formula to ``argos.sources.gamma._SeededExponentialJitter``, and
    deliberately not imported from it: tenacity's own
    ``wait_exponential_jitter`` draws from module-level ``random.uniform`` with
    no injection hook, and a shared helper module for this one class would
    invite a future refactor to reach for that same module-level state instead
    of an adapter-owned ``random.Random`` (ADR-0009).
    """

    def __init__(
        self, rng: random.Random, *, initial: float, max: float, jitter: float = 1.0
    ) -> None:
        self._rng = rng
        self._initial = initial
        self._max = max
        self._jitter = jitter

    def __call__(self, retry_state: RetryCallState) -> float:
        jitter = self._rng.uniform(0, self._jitter)
        try:
            attempt: int = int(retry_state.attempt_number)
            exp = float(2 ** (attempt - 1))
            result = self._initial * exp + jitter
        except OverflowError:
            result = self._max
        return max(0.0, min(result, self._max))


class _RetryableResponse(Exception):
    """Internal signal: this attempt failed in a way worth retrying."""

    def __init__(self, detail: str, *, status: int | None = None, timed_out: bool = False) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.timed_out = timed_out


class ClobClient:
    """Async client for the public CLOB REST order-book endpoint.

    Timekeeping and pacing are separate ports (ADR-0009): ``clock`` supplies
    ``retrieved_at`` timestamps, and ``pacer`` supplies retry backoff and the
    overall request deadline.
    """

    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        *,
        pacer: Pacer,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._pacer = pacer
        self._rng = random.Random(settings.source_jitter_seed)
        self._health = ClobHealth()
        self._client = httpx.AsyncClient(
            base_url=settings.clob_base_url,
            timeout=httpx.Timeout(settings.http_timeout_seconds),
            transport=transport,
            headers={
                "accept": "application/json",
                "user-agent": "argos-research (public read-only market data)",
            },
            follow_redirects=False,
        )

    @property
    def health(self) -> ClobHealth:
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

    async def get_book(self, token_id: str) -> ClobBookResponse:
        """Fetch the public order-book snapshot for one token.

        ``token_id`` is validated against
        :data:`argos.domain.market.TOKEN_ID_PATTERN` before it reaches the
        wire: httpx normalizes dot segments during base-URL merge (the same
        M1 precedent ``GammaClient.get_market`` closed), and a malformed id
        would otherwise cost a wasted request the source answers 400 anyway
        (``docs/research/m2-clob-rest-book.md``).
        """
        if not TOKEN_ID_PATTERN.fullmatch(token_id):
            raise ValueError(f"token_id must match {TOKEN_ID_PATTERN.pattern}, got {token_id!r}")
        return await self._get("/book", {"token_id": token_id})

    @property
    def _deadline_seconds(self) -> float:
        """An upper bound on one logical request, retries and backoff included.

        See ``argos.sources.gamma.GammaClient._deadline_seconds`` for the full
        reasoning: httpx's read timeout is per chunk, so only a pacer-bound
        deadline actually bounds the call.
        """
        attempts = self._settings.http_max_attempts
        return self._settings.http_timeout_seconds * attempts + MAX_BACKOFF_SECONDS * (attempts - 1)

    async def _get(self, path: str, params: dict[str, Any]) -> ClobBookResponse:
        with self._pacer.move_on_after(self._deadline_seconds) as scope:
            return await self._get_within_deadline(path, params)

        if scope.cancelled_caught:
            self._count(failures=1)
            raise SourceTimeoutError(
                "clob exceeded the overall deadline for this request",
                endpoint=path,
                deadline_seconds=self._deadline_seconds,
            )
        raise AssertionError("unreachable: the scope either returns or is cancelled")

    async def _get_within_deadline(self, path: str, params: dict[str, Any]) -> ClobBookResponse:
        attempts = 0
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._settings.http_max_attempts),
                wait=_SeededExponentialJitter(self._rng, initial=0.5, max=MAX_BACKOFF_SECONDS),
                retry=retry_if_exception_type(_RetryableResponse),
                sleep=self._pacer.wait,
                reraise=True,
            ):
                with attempt:
                    if attempts:
                        self._count(retries=1)
                    attempts += 1
                    return await self._attempt(path, params)
        except (ClobBookNotFoundError, ClobBookBadRequestError):
            # Neither is a source failure: both are reasoned, counted outcomes
            # already recorded at the point they were raised in `_attempt`.
            # Folding them into `failures` here would conflate "the book
            # answer was ambiguous/malformed" with "the source was down".
            raise
        except _RetryableResponse as error:
            self._count(failures=1)
            _logger.warning(
                "clob request exhausted its retries",
                path=path,
                attempts=attempts,
                status=error.status,
                timed_out=error.timed_out,
            )
            if error.timed_out:
                raise SourceTimeoutError(
                    "clob timed out on every attempt",
                    endpoint=path,
                    attempts=attempts,
                    timeout_seconds=self._settings.http_timeout_seconds,
                ) from error
            raise SourceUnavailableError(
                "clob did not answer successfully within its retry budget",
                endpoint=path,
                attempts=attempts,
                last_status=error.status,
            ) from error
        except Exception:
            self._count(failures=1)
            raise
        raise AssertionError("unreachable: AsyncRetrying always returns or raises")

    async def _attempt(self, path: str, params: dict[str, Any]) -> ClobBookResponse:
        try:
            async with self._client.stream("GET", path, params=params) as response:
                if response.status_code in RETRYABLE_STATUSES:
                    raise _RetryableResponse(
                        f"clob answered {response.status_code}", status=response.status_code
                    )
                if response.status_code == 404:
                    self._count(not_found=1)
                    raise ClobBookNotFoundError(
                        "clob reports no orderbook for the requested token id; ambiguous "
                        "across a closed market, an unknown token, and a token that never "
                        "had a book (docs/research/m2-clob-rest-book.md)",
                        endpoint=path,
                        status=404,
                    )
                if response.status_code == 400:
                    self._count(bad_request=1)
                    raise ClobBookBadRequestError(
                        "clob rejected the request as malformed", endpoint=path, status=400
                    )
                if response.status_code >= 400:
                    raise SourceProtocolError(
                        "clob rejected the request",
                        endpoint=path,
                        status=response.status_code,
                    )
                raw = await self._read_bounded(response, path)
                request_url = str(response.request.url)
                status = response.status_code
        except httpx.TimeoutException as error:
            raise _RetryableResponse(f"timeout: {type(error).__name__}", timed_out=True) from error
        except httpx.TransportError as error:
            raise _RetryableResponse(f"transport error: {type(error).__name__}") from error

        try:
            payload = orjson.loads(raw)
        except orjson.JSONDecodeError as error:
            raise SourceProtocolError(
                "clob response is not valid JSON",
                endpoint=path,
                status=status,
                byte_length=len(raw),
            ) from error

        self._count(requests=1, bytes_received=len(raw))
        return ClobBookResponse(
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

        See ``argos.sources.gamma.GammaClient._read_bounded``: reading
        ``response.content`` would buffer and transparently decompress the
        whole body before any size check could run.
        """
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
            raise SourceProtocolError(
                "clob declared a response larger than the accepted size",
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
                    "clob response exceeds the accepted size",
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
