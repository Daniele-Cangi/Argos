"""The capture loop: drives one `capture_run` from raw frames to durable storage.

This is the layer :mod:`argos.ingestion.clob_price_change` and
:mod:`argos.sources.clob_ws` both explicitly deferred to "the capture-loop
slice" -- see their module docstrings. Three decisions were deliberately left
unmade across four prior M2 slices; this module makes all three, and this
docstring is where each is justified, per the task brief that built it.

## Decision 1: `ingest_sequence` allocation

The sequence belongs to the **run**, not to the connection, and is
**allocated only when a record is actually produced** -- an accepted
observation or a rejection-ledger row, never a bare `None` ("this frame was
not about this token", see
:func:`argos.ingestion.clob_price_change.normalize_clob_price_change`'s own
docstring for why that is a counted non-event and not a rejection).

The mechanism is "peek, then commit": :meth:`_CaptureState.peek_sequence`
returns the value a record *would* get without consuming it;
:meth:`_CaptureState.commit_sequence` both returns and consumes it. Every
call site peeks, does the work that might produce a record, and only calls
`commit_sequence` in the branch that actually writes to the store. Because
this loop is single-threaded and fully synchronous between an `await` on the
next frame, no other code can observe or consume the peeked value first, so
`commit_sequence()` is guaranteed to return exactly what `peek_sequence()`
promised.

The alternative -- reserve a sequence number unconditionally before knowing
the outcome, then simply skip writing anything for a `None` result -- was
rejected. It would still satisfy "monotonic, never reset", but it would also
carve a permanent, silent gap into the sequence for every frame that turned
out to be about an unsubscribed sibling token, which on this source (see
`docs/research/m2-clob-websocket.md`) is *routine* traffic, not rare. A
reader of the delivery/rejection ledger would then have to independently
learn "gaps here are expected and mean nothing" instead of the sequence
simply having no gaps to begin with. Peek-then-commit gives a contiguous,
per-run sequence with zero interpretive burden on a future reader, at the
cost of one extra (cheap, synchronous, in-process) function call per
candidate token per frame -- and it is exactly what "allocate a sequence only
when a record is actually produced" asks for literally, not just in effect.

Because the frame source (:class:`FrameSource`) is consumed as one
uninterrupted async iterator for the whole lifetime of one `run_capture`
call, and `argos.sources.clob_ws.ClobMarketWsClient.frames()` already
resumes yielding transparently across its own internal reconnects (the
generator's identity, and this module's local `_CaptureState`, both survive
a reconnect untouched), the sequence counter is never reset by a reconnect
as a structural consequence of this design, not an extra check bolted on.
Only a full process restart starts a new `run_capture` call, and that
necessarily opens a new `capture_run_id` with its own fresh counter -- which
is correct: a genuinely new run is a genuinely new sequence space.

## Decision 2: cross-token fan-out

One `price_change` frame can carry entries for more than one token,
including an *unsubscribed* binary sibling
(`docs/research/m2-clob-websocket.md`, "Priority question 4", reproduced by
:class:`argos.domain.pricechange.NoEntriesForToken`'s own docstring). This
module never iterates "the tokens this frame happens to mention" -- it
iterates `sorted(subscribed_token_ids)`, the run's own configuration, and
calls :func:`~argos.ingestion.clob_price_change.normalize_clob_price_change`
once per configured token per event. A token not in that configured set is
never looked at, no matter what the frame contains for it. This is what
makes sequence allocation "a function of configuration and frame order only"
(the task brief's own phrase): the set of candidate records for one frame is
fixed before the frame's bytes are even decoded, and only shrinks (via a
`None` normalizer result) rather than depending on what the wire happened to
bundle. `sorted()` gives one deterministic candidate order across identical
runs, which the tests pin directly.

## Decision 3: what the "capture manifest" is

Nothing new. `RunManifest` (`run_manifest.v2`,
:mod:`argos.config.manifest`) plus the store's own append-only `capture_run`
rows (:mod:`argos.store.event_store`, ADR-0011) already are the capture
manifest -- this module's entire manifest-side responsibility is to open and
close exactly one `capture_run` row per invocation, which
:func:`run_capture` does. `docs/STATUS.md` records a hard constraint from the
pre-M2 pacing slice: an M2 capture manifest must **not** embed one
`SourceProvenanceV1` per ingested event, because event volume makes that
unbounded in a way M1 discovery's one-provenance-per-page never was. This
module honors that constraint by construction -- it writes no
`SourceProvenanceV1` anywhere itself; every `SourceProvenanceV1` it touches
is already embedded inside an `ObservationEnvelopeV1`/`RejectedObservationV1`
record the store persists once, keyed by `(capture_run_id, ingest_sequence)`,
never accumulated into a manifest list. A future capture CLI's
`RunManifest.input_provenance` should therefore reference this
`capture_run_id` (and, if it wants a manifest-level summary, query
`EventStore.counts_for_capture_run`), not enumerate one `SourceProvenanceV1`
per frame or per record -- that CLI slice inherits this constraint
deliberately rather than discovering it late, per the same pattern
`docs/STATUS.md` already used for the pre-M2 slice's own finding.

## The oversized-frame backlog item: judgement, not implementation

`docs/BACKLOG.md` lists, for this slice, closing the gap where
`argos.sources.clob_ws`'s oversized-frame refusal is counted
(`ClobWsHealth.oversized_frames_refused`) but produces no rejection-ledger
row. **This module does not close that gap, deliberately, and the owner
asked for that judgement to be argued rather than assumed:**

`WebsocketsConnector` passes `max_size=MAX_FRAME_BYTES` to
`websockets.connect` -- the *same* constant `MarketFrame._classify` checks
`text` against. The `websockets` library enforces `max_size` at the protocol
frame-reassembly layer: once the declared payload length of an incoming
message exceeds `max_size`, `websockets` raises `ConnectionClosedError`
(protocol close code 1009, "message too big") out of `ws.recv()` itself,
**before** the oversized payload is ever fully buffered into the Python
string `_classify` inspects. That is: for the one production transport this
module will ever be run against, `_classify`'s own size check is unreachable
dead code -- the library has already refused the frame two layers below it,
and `_pump_cycle`'s `except Exception` around `_receive_loop` converts that
`ConnectionClosedError` into a plain reconnect, counted only via
`ClobWsHealth.reconnects`/`connection_failures`, with **no frame object of
any kind, and no captured bytes, ever reaching this module or any other**.
There is no `MarketFrame`, no `raw_sha256`, and no `byte_length` to build a
rejection from, because ARGOS's process never received the oversized bytes
at all -- the TCP-level websocket framing was torn down mid-message by the
peer library before user code saw them. `RejectedObservationV1.raw_payload_sha256`
is a required field: writing a row for this case would mean inventing a hash
for content ARGOS never possessed, which is fabrication, not evidence, and
directly against core invariant 7 ("raw data is immutable") and
`.claude/rules/data-integrity.md` ("raw source payloads are immutable and
linked by hash") -- a rejection ledger entry pointing at a hash of nothing is
worse than the honest gap it would claim to close.

The only way `_classify`'s size check could ever fire on real bytes is a
future connector that sets a `max_size` larger than `MAX_FRAME_BYTES`, or one
that does not enforce `max_size` at all -- in which case `_classify` refuses
a **fully-received** oversized frame with the complete text in hand. Even
then, the frame is refused *inside the transport*, which returns `None` from
`_classify` and never yields a `MarketFrame` to any caller -- so this module,
sitting entirely downstream of `frame_source.frames()`, still never sees it,
regardless of what the connector does. Closing this gap for that hypothetical
would require changing `_classify` to yield a distinguishable oversized-frame
signal instead of silently returning `None` -- a transport-module change, out
of this slice's stated scope (`src/argos/sources/` is explicitly untouched
here). This module's judgement: **the backlog item as written is not
achievable without fabricating evidence for the real transport, and the one
case where it would be achievable is out of this slice's scope; recorded here
rather than silently worked around.**
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import Any, Final, Protocol

import orjson

from argos.clock import Clock
from argos.domain.observation import (
    DEFAULT_CLOCK_SKEW_TOLERANCE,
    ObservationEnvelopeV1,
    ObservationSource,
    RejectedObservationV1,
    build_rejected_observation,
)
from argos.errors import RejectionReason
from argos.ingestion.clob_price_change import (
    CLOB_WS_PRICE_CHANGE_EVENT_TYPE,
    normalize_clob_price_change,
)
from argos.ingestion.clob_ws_book import CLOB_WS_BOOK_EVENT_TYPE, normalize_clob_ws_book
from argos.sources.clob_ws import MarketFrame
from argos.store.event_store import CompletionStatus, Disposition, EventStore
from argos.store.raw_archive import write_raw_payload

__all__ = ["CaptureHealth", "FrameSource", "run_capture"]

_MISSING_EVENT_TYPE_DETAIL: Final = "event carries no usable 'event_type' field"


class FrameSource(Protocol):
    """Anything that yields raw market-channel frames, matching `ClobMarketWsClient.frames()`.

    Deliberately not the concrete `ClobMarketWsClient`: this loop depends only
    on this narrow shape, so a test drives a bare async generator with no
    socket, no task group, and no subscribe/heartbeat machinery, and an M3
    replay source can satisfy this exact protocol without this module
    changing at all -- core invariant 5, "live and replay use the same domain
    handlers; only the clock and event source may differ." Connecting,
    subscribing, and reconnecting are the transport's job
    (`argos.sources.clob_ws`), entered by the caller *before* `run_capture` is
    invoked; this module's own lifetime is exactly the lifetime of one
    `capture_run` row, no more.
    """

    def frames(self) -> AsyncIterator[MarketFrame]: ...


@dataclass(frozen=True, slots=True)
class CaptureHealth:
    """Structured counters an operator reads to judge one capture run.

    `accepted`/`duplicate`/`rejected` are kept in lockstep with what
    `EventStore.counts_for_capture_run` derives by query over the same run --
    every increment here corresponds to exactly one `store.append_observation`
    or `store.append_rejection` call, so the two must never disagree; tests
    pin this directly. `decode_failures` and `unknown_event_type` are reason
    subsets of `rejected` (a decode failure and an unknown event type both
    also increment `rejected`), kept as their own counters because an
    operator watching a live capture wants "how many frames were not even
    JSON" and "how many events had no wired payload model" as distinct
    signals, not just one undifferentiated rejection count. `not_applicable`
    is the `None`-return case from
    `argos.ingestion.clob_price_change.normalize_clob_price_change` -- a
    counted non-event, never a rejection; see this module's own docstring,
    Decision 1.
    """

    frames_consumed: int = 0
    decode_failures: int = 0
    events_seen: int = 0
    accepted: int = 0
    duplicate: int = 0
    rejected: int = 0
    not_applicable: int = 0
    unknown_event_type: int = 0


@dataclass(slots=True)
class _CaptureState:
    """Mutable, single-run, single-task state: the sequence counter and health.

    Not exported. `next_sequence` starts at 1 (`EventStore.append_rejection`
    refuses `ingest_sequence <= 0`) and is local to one `run_capture` call --
    see the module docstring, Decision 1, for why that is exactly what makes
    the sequence survive a transport-level reconnect untouched.
    """

    next_sequence: int = 1
    health: CaptureHealth = field(default_factory=CaptureHealth)

    current_raw_location: str | None = None
    """Where the frame currently being consumed was archived, or ``None`` when
    raw archiving is switched off.

    Set once per frame, before fan-out, so every record derived from one frame
    points at the same archived bytes — which is the truth: they all came from
    those bytes. Carried on the state object rather than threaded through five
    call signatures because it has exactly the same lifetime and scope as the
    sequence counter beside it, and splitting one per-frame fact across two
    mechanisms is how they drift apart."""

    def peek_sequence(self) -> int:
        """Return the sequence a record would get, without consuming it."""
        return self.next_sequence

    def commit_sequence(self) -> int:
        """Consume and return the next sequence. Only called when a record is written."""
        value = self.next_sequence
        self.next_sequence += 1
        return value

    def count(self, **deltas: int) -> None:
        self.health = replace(
            self.health,
            **{name: getattr(self.health, name) + value for name, value in deltas.items()},
        )


async def run_capture(
    *,
    frame_source: FrameSource,
    store: EventStore,
    clock: Clock,
    capture_run_id: str,
    subscribed_token_ids: Iterable[str],
    raw_archive_dir: Path | None = None,
    clock_skew_tolerance: timedelta = DEFAULT_CLOCK_SKEW_TOLERANCE,
) -> CaptureHealth:
    """Consume `frame_source` into `store` under one `capture_run`, until exhausted or failed.

    Opens `capture_run_id` before consuming a single frame and **always**
    closes it: `CompletionStatus.COMPLETED` if `frame_source.frames()` is
    exhausted cleanly, `CompletionStatus.FAILED` -- recorded, then re-raised
    -- if anything raises while consuming it. A process killed outright (a
    `SIGKILL`, a hard crash) runs neither branch and writes no closing row at
    all; `EventStore.iter_open_capture_runs` reporting that run as open is the
    intended signal for "an interrupted capture", not a gap this function
    should paper over (see `docs/adr/0011-sqlite-event-store-and-delivery-record.md`
    section 5 and `docs/STATUS.md`, "an interrupted capture closes or marks
    its manifest incomplete").

    `subscribed_token_ids` is read once, deduplicated, and sorted -- see the
    module docstring, Decision 2. Decoding one frame's JSON, and dispatching
    each event inside it, is entirely synchronous, single-threaded work; the
    only `await` in this function is the frame source's own `async for`.
    """
    tokens: Sequence[str] = sorted(frozenset(subscribed_token_ids))
    store.open_capture_run(capture_run_id, started_at=clock.now())
    state = _CaptureState()
    try:
        async for frame in frame_source.frames():
            state.count(frames_consumed=1)
            # Archived once per frame, before anything is normalized, because
            # CLAUDE.md's engineering rules require storing the raw payload
            # *plus* the normalized one, and core invariant 7 says normalization
            # never replaces the source payload. Until this existed, a live
            # capture kept `raw_payload_sha256` and threw the bytes away — a
            # hash of something nobody had, an unverifiable claim, and no way to
            # re-normalize a historical capture under a corrected parser, which
            # is the correction mechanism ADR-0004 requires.
            #
            # A failure here is deliberately fatal to the run rather than
            # counted: continuing would keep producing records that silently
            # cannot be reproduced, which is worse than stopping loudly.
            if raw_archive_dir is not None:
                state.current_raw_location = str(
                    write_raw_payload(
                        raw_archive_dir,
                        raw=frame.text.encode("utf-8"),
                        provenance=frame.provenance,
                    )
                )
            _consume_frame(
                frame,
                state=state,
                store=store,
                clock=clock,
                capture_run_id=capture_run_id,
                tokens=tokens,
                clock_skew_tolerance=clock_skew_tolerance,
            )
    except BaseException:
        store.close_capture_run(
            capture_run_id, ended_at=clock.now(), completion_status=CompletionStatus.FAILED
        )
        raise
    else:
        store.close_capture_run(
            capture_run_id, ended_at=clock.now(), completion_status=CompletionStatus.COMPLETED
        )
    return state.health


def _consume_frame(
    frame: MarketFrame,
    *,
    state: _CaptureState,
    store: EventStore,
    clock: Clock,
    capture_run_id: str,
    tokens: Sequence[str],
    clock_skew_tolerance: timedelta,
) -> None:
    """Decode one frame's JSON and dispatch each event it carries.

    Decoding happens here, and only here: `MarketFrame.text` is deliberately
    undecoded by the transport (`argos.sources.clob_ws`'s own module
    docstring). A frame may be a single event object or a JSON array of
    events -- both shapes were observed in the research capture
    (`docs/research/m2-clob-websocket.md`) -- and both are handled uniformly
    by treating a single object as a one-element list.
    """
    try:
        decoded = orjson.loads(frame.text)
    except orjson.JSONDecodeError as error:
        state.count(decode_failures=1)
        _append_rejection(
            reason=RejectionReason.MALFORMED_PAYLOAD,
            detail=f"frame text is not valid JSON: {error}",
            source_event_type=None,
            condition_id=None,
            token_id=None,
            frame=frame,
            state=state,
            store=store,
            clock=clock,
            capture_run_id=capture_run_id,
        )
        return

    if not isinstance(decoded, list | Mapping):
        state.count(decode_failures=1)
        _append_rejection(
            reason=RejectionReason.MALFORMED_PAYLOAD,
            detail=(
                f"decoded frame is neither a JSON object nor an array, got {type(decoded).__name__}"
            ),
            source_event_type=None,
            condition_id=None,
            token_id=None,
            frame=frame,
            state=state,
            store=store,
            clock=clock,
            capture_run_id=capture_run_id,
        )
        return

    events: list[Any] = decoded if isinstance(decoded, list) else [decoded]
    for event in events:
        state.count(events_seen=1)
        _consume_event(
            event,
            frame=frame,
            state=state,
            store=store,
            clock=clock,
            capture_run_id=capture_run_id,
            tokens=tokens,
            clock_skew_tolerance=clock_skew_tolerance,
        )


def _consume_event(
    event: Any,
    *,
    frame: MarketFrame,
    state: _CaptureState,
    store: EventStore,
    clock: Clock,
    capture_run_id: str,
    tokens: Sequence[str],
    clock_skew_tolerance: timedelta,
) -> None:
    """Dispatch one already-decoded event: fan out `price_change`, reject everything else."""
    if not isinstance(event, Mapping):
        _append_rejection(
            reason=RejectionReason.MALFORMED_PAYLOAD,
            detail=f"frame array element is not a JSON object, got {type(event).__name__}",
            source_event_type=None,
            condition_id=None,
            token_id=None,
            frame=frame,
            state=state,
            store=store,
            clock=clock,
            capture_run_id=capture_run_id,
        )
        return

    event_type = event.get("event_type")
    event_type_label = event_type if isinstance(event_type, str) and event_type else None

    if event_type_label == CLOB_WS_PRICE_CHANGE_EVENT_TYPE:
        for token_id in tokens:
            _consume_price_change_for_token(
                event,
                token_id=token_id,
                frame=frame,
                state=state,
                store=store,
                clock=clock,
                capture_run_id=capture_run_id,
                clock_skew_tolerance=clock_skew_tolerance,
            )
        return

    if event_type_label == CLOB_WS_BOOK_EVENT_TYPE:
        for token_id in tokens:
            _consume_ws_book_for_token(
                event,
                token_id=token_id,
                frame=frame,
                state=state,
                store=store,
                clock=clock,
                capture_run_id=capture_run_id,
                clock_skew_tolerance=clock_skew_tolerance,
            )
        return

    # Covers `last_trade_price`, `tick_size_change`, any other source-defined
    # event_type, and an absent/non-string event_type -- "anything unknown"
    # per the task brief. No payload model is wired for any of these here;
    # refusing rather than guessing at a model is the point (see the module
    # docstring, "Do not invent one").
    state.count(unknown_event_type=1)
    detail = (
        f"no payload model is wired for event_type {event_type_label!r} yet"
        if event_type_label is not None
        else _MISSING_EVENT_TYPE_DETAIL
    )
    _append_rejection(
        reason=RejectionReason.UNKNOWN_EVENT_TYPE,
        detail=detail,
        source_event_type=event_type_label,
        condition_id=_best_effort_text(event, "market"),
        token_id=_best_effort_text(event, "asset_id"),
        frame=frame,
        state=state,
        store=store,
        clock=clock,
        capture_run_id=capture_run_id,
    )


def _consume_price_change_for_token(
    event: Any,
    *,
    token_id: str,
    frame: MarketFrame,
    state: _CaptureState,
    store: EventStore,
    clock: Clock,
    capture_run_id: str,
    clock_skew_tolerance: timedelta,
) -> None:
    """Normalize one `price_change` event for one configured token; write if applicable.

    See the module docstring, Decision 1, for why the sequence is peeked
    before calling the normalizer and only committed in the two branches that
    actually write a row.
    """
    sequence = state.peek_sequence()
    result = normalize_clob_price_change(
        event=event,
        provenance=frame.provenance,
        raw_payload_location=state.current_raw_location,
        requested_token_id=token_id,
        received_time=frame.received_time,
        rejected_at=clock.now(),
        ingest_sequence=sequence,
        capture_run_id=capture_run_id,
        clock_skew_tolerance=clock_skew_tolerance,
    )
    if result is None:
        state.count(not_applicable=1)
        return

    committed = state.commit_sequence()
    assert committed == sequence, "sequence advanced between peek and commit"

    if isinstance(result, ObservationEnvelopeV1):
        delivery = store.append_observation(result)
        if delivery.disposition is Disposition.ACCEPTED_NEW:
            state.count(accepted=1)
        else:
            state.count(duplicate=1)
    else:
        store.append_rejection(result, ingest_sequence=sequence)
        state.count(rejected=1)


def _consume_ws_book_for_token(
    event: Any,
    *,
    token_id: str,
    frame: MarketFrame,
    state: _CaptureState,
    store: EventStore,
    clock: Clock,
    capture_run_id: str,
    clock_skew_tolerance: timedelta,
) -> None:
    """Normalize one `book` event for one configured token; write if applicable.

    Mirrors :func:`_consume_price_change_for_token` exactly -- same
    peek-then-commit sequence discipline (module docstring, Decision 1), same
    fan-out over the configured token set (Decision 2), same three-way
    ``ObservationEnvelopeV1`` / ``RejectedObservationV1`` / ``None`` outcome
    shape. A `book` event is one more record kind dispatched through the same
    machinery, not a new allocation rule.
    """
    sequence = state.peek_sequence()
    result = normalize_clob_ws_book(
        event=event,
        provenance=frame.provenance,
        raw_payload_location=state.current_raw_location,
        requested_token_id=token_id,
        received_time=frame.received_time,
        rejected_at=clock.now(),
        ingest_sequence=sequence,
        capture_run_id=capture_run_id,
        clock_skew_tolerance=clock_skew_tolerance,
    )
    if result is None:
        state.count(not_applicable=1)
        return

    committed = state.commit_sequence()
    assert committed == sequence, "sequence advanced between peek and commit"

    if isinstance(result, ObservationEnvelopeV1):
        delivery = store.append_observation(result)
        if delivery.disposition is Disposition.ACCEPTED_NEW:
            state.count(accepted=1)
        else:
            state.count(duplicate=1)
    else:
        store.append_rejection(result, ingest_sequence=sequence)
        state.count(rejected=1)


def _append_rejection(
    *,
    reason: RejectionReason,
    detail: str,
    source_event_type: str | None,
    condition_id: str | None,
    token_id: str | None,
    frame: MarketFrame,
    state: _CaptureState,
    store: EventStore,
    clock: Clock,
    capture_run_id: str,
) -> None:
    """Build and store one rejection-ledger row for a frame- or event-level defect."""
    sequence = state.commit_sequence()
    rejection: RejectedObservationV1 = build_rejected_observation(
        reason=reason,
        detail=detail,
        source=ObservationSource.CLOB_MARKET_WS,
        source_event_type=source_event_type,
        condition_id=condition_id,
        token_id=token_id,
        provenance=frame.provenance,
        received_time=frame.received_time,
        rejected_at=clock.now(),
        capture_run_id=capture_run_id,
    )
    store.append_rejection(rejection, ingest_sequence=sequence)
    state.count(rejected=1)


def _best_effort_text(event: Mapping[str, Any], key: str) -> str | None:
    """Read a diagnostic-only string field without validating its shape.

    Mirrors `argos.ingestion.clob_book._best_effort_text` and
    `argos.ingestion.clob_price_change._best_effort_text` exactly, not
    imported from either: both are private names in modules documenting the
    same reasoning for not sharing this one small, non-normalizing helper --
    the duplication class `docs/STATUS.md` actually warns against is
    `Decimal`/timestamp *normalization* logic, which this module never
    reimplements (it never touches a price, size, or timestamp field at
    all).
    """
    value = event.get(key)
    return value if isinstance(value, str) and value else None
