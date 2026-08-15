"""Normalize a public CLOB market-channel `book` frame into an `ObservationEnvelopeV1`.

This is the ingestion-layer counterpart to :mod:`argos.domain.wsbook`, the
same relationship :mod:`argos.ingestion.clob_price_change` has to
:mod:`argos.domain.pricechange`: the domain payload model
(:func:`argos.domain.wsbook.parse_ws_book_snapshot`) raises plain
``ValueError`` from its validators, and this module is the layer that catches
it and turns it into a :class:`~argos.domain.observation.RejectedObservationV1`
with a reason (core invariant 14 — errors are data, never a silent drop).

**No transport code lives here.** This module takes one already-JSON-decoded
event; opening the WebSocket connection, subscribing, reconnecting, and
dispatching received text to this function are the capture loop's job
(``.claude/rules/no-execution.md`` scopes this repository to public read
adapters only regardless).

This module closes the sharpest remaining M2 gap `docs/BACKLOG.md` records:
before it, every `book` event became an `unknown_event_type` rejection, so a
stored capture could replay its `price_change.v1` deltas but had nothing in
the same capture to seed a projection from. With this module wired into
:mod:`argos.ingestion.capture`, a stored capture is self-sufficient: its own
`ws_book_snapshot.v1` observations seed
:class:`argos.projections.book.BookState` and its own `price_change.v1`
observations apply on top, with no REST call anywhere in the path.

Three things this module does, each reusing a pattern already proven on a
sibling adapter rather than inventing a new one:

1. **`source_event_type` is read from the wire, not assigned by ARGOS.**
   The `book` event carries its own `event_type: "book"`, so this module uses
   the source's own name (:data:`CLOB_WS_BOOK_EVENT_TYPE`), the same choice
   :mod:`argos.ingestion.clob_price_change` made for `price_change`.
2. **A `book` event naming a different token is a counted non-event, not a
   rejection.** A `book` event names exactly one `asset_id`; the research
   note's cross-token finding for `price_change` frames applies just as much
   here — a connection subscribed to one token can legitimately see traffic
   about its binary sibling. This module reuses
   :class:`argos.domain.pricechange.NoEntriesForToken` for the same outcome,
   rather than inventing a second type for what
   :func:`~argos.ingestion.clob_price_change.normalize_clob_price_change`'s
   own docstring already documents as one concept: "the frame was never about
   this token." A caller must count a ``None`` return exactly as it already
   does for the sibling normalizer — see this function's own docstring.
3. **A byte cap checked before any parsing, and `hash` validated inside this
   module's own `try`.** Both reuse the exact pattern
   :mod:`argos.ingestion.clob_price_change` already closed for the identical
   class of defect: :data:`~argos.ingestion.clob_book.MAX_NORMALIZABLE_BYTES`
   is checked against ``provenance.byte_length`` before a single key is read
   out of ``event``, and :func:`_extract_source_hash` (mirroring
   :func:`argos.ingestion.clob_book._extract_source_hash`: tolerant of
   absence, strict about type, length- and identifier-checked) runs inside
   this function's own ``try`` rather than leaving
   ``ObservationEnvelopeV1._validate_identifier`` as the only guard, which
   would let a hostile hash escape as a raw pydantic ``ValidationError`` with
   no ledger entry at all — the third appearance of that exact finding, after
   ``clob_book.py`` and ``clob_price_change.py``.

``timestamp`` -> ``event_time`` reuses
:func:`argos.ingestion.wire.parse_event_time`, the shared parser both sibling
adapters already use for the identical wire shape.

`ingest_sequence` is a caller-supplied parameter, not allocated here, for the
same reason both sibling normalizers document: this module is a pure,
synchronous normalization step, not the capture loop.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Final

from argos.domain.observation import (
    DEFAULT_CLOCK_SKEW_TOLERANCE,
    MAX_IDENTIFIER_LENGTH,
    ObservationEnvelopeV1,
    ObservationSource,
    RejectedObservationV1,
    build_observation_envelope,
    build_rejected_observation,
)
from argos.domain.pricechange import NoEntriesForToken
from argos.domain.provenance import SourceProvenanceV1
from argos.domain.text import is_clean_identifier
from argos.domain.wsbook import parse_ws_book_snapshot
from argos.errors import RejectionReason
from argos.ingestion.clob_book import MAX_NORMALIZABLE_BYTES
from argos.ingestion.wire import parse_event_time

CLOB_WS_BOOK_NORMALIZER_VERSION: Final = "clob-ws-book-normalizer/1"

CLOB_WS_BOOK_EVENT_TYPE: Final = "book"
"""The source's own `event_type` for this message kind, read verbatim rather
than assigned by ARGOS — matching
:data:`argos.ingestion.clob_price_change.CLOB_WS_PRICE_CHANGE_EVENT_TYPE`'s
own choice for the identical reason."""


def normalize_clob_ws_book(
    *,
    event: Any,
    provenance: SourceProvenanceV1,
    requested_token_id: str,
    received_time: datetime,
    rejected_at: datetime,
    ingest_sequence: int,
    capture_run_id: str,
    raw_payload_location: str | None = None,
    clock_skew_tolerance: timedelta = DEFAULT_CLOCK_SKEW_TOLERANCE,
) -> ObservationEnvelopeV1 | RejectedObservationV1 | None:
    """Normalize one already-JSON-decoded `book` WebSocket event for one token.

    Returns one of three outcomes:

    - An :class:`~argos.domain.observation.ObservationEnvelopeV1` when the
      event is a well-formed `book` snapshot naming ``requested_token_id``.
    - A :class:`~argos.domain.observation.RejectedObservationV1` when the
      event **was** about this token (or could not even be inspected well
      enough to tell) but could not be honestly represented — every
      ``ValueError`` :func:`~argos.domain.wsbook.parse_ws_book_snapshot` can
      raise, plus this module's own ``hash`` validation, is caught and
      represented with :attr:`~argos.errors.RejectionReason.MALFORMED_PAYLOAD`.
      This function never raises for a malformed *payload*; a caller error
      (an invalid ``provenance``, for example) still raises, the same
      contract :func:`~argos.domain.observation.build_observation_envelope`
      itself has.
    - ``None`` when the event names a different token than
      ``requested_token_id``. This is deliberately *not* a rejection — see
      the module docstring, point 2, and
      :func:`argos.ingestion.clob_price_change.normalize_clob_price_change`'s
      own docstring for the full reasoning it shares.

      **The caller MUST count a `None` return**, exactly as it already must
      for the sibling `price_change` normalizer
      (``.claude/rules/data-integrity.md``): this is a counted non-event, not
      a silent drop, and it is the capture loop's job to count it.
    """
    if provenance.byte_length > MAX_NORMALIZABLE_BYTES:
        # See the module docstring, point 3. Refused before a single key is
        # read out of `event`, because the cost this bounds -- constructing
        # up to two full sides of `OrderBookLevel`s -- is incurred by the
        # reading itself, and no `Pacer` deadline can interrupt synchronous
        # CPU work once it has started.
        return build_rejected_observation(
            reason=RejectionReason.MALFORMED_PAYLOAD,
            detail=(
                f"event of {provenance.byte_length} bytes exceeds the "
                f"{MAX_NORMALIZABLE_BYTES}-byte normalization budget"
            ),
            source=ObservationSource.CLOB_MARKET_WS,
            source_event_type=CLOB_WS_BOOK_EVENT_TYPE,
            token_id=requested_token_id,
            provenance=provenance,
            received_time=received_time,
            rejected_at=rejected_at,
            capture_run_id=capture_run_id,
        )

    condition_id_hint = _best_effort_text(event, "market")

    try:
        snapshot = parse_ws_book_snapshot(event)
        if snapshot.asset_id != requested_token_id:
            # See the module docstring, point 2 -- the frame was never about
            # this token, not a malformation of it.
            raise NoEntriesForToken(
                f"book event names asset_id {snapshot.asset_id!r}, not the requested "
                f"token id {requested_token_id!r}"
            )
        source_hash = _extract_source_hash(event)
    except NoEntriesForToken:
        return None
    except ValueError as error:
        return build_rejected_observation(
            reason=RejectionReason.MALFORMED_PAYLOAD,
            detail=str(error),
            source=ObservationSource.CLOB_MARKET_WS,
            source_event_type=CLOB_WS_BOOK_EVENT_TYPE,
            condition_id=condition_id_hint,
            token_id=requested_token_id,
            provenance=provenance,
            received_time=received_time,
            rejected_at=rejected_at,
            capture_run_id=capture_run_id,
        )

    event_time, event_time_raw = parse_event_time(event.get("timestamp"))

    return build_observation_envelope(
        source=ObservationSource.CLOB_MARKET_WS,
        source_event_type=CLOB_WS_BOOK_EVENT_TYPE,
        condition_id=snapshot.condition_id,
        token_id=snapshot.asset_id,
        event_time=event_time,
        event_time_raw=event_time_raw,
        received_time=received_time,
        ingest_sequence=ingest_sequence,
        source_hash=source_hash,
        payload=snapshot,
        provenance=provenance,
        raw_payload_location=raw_payload_location,
        parser_version=CLOB_WS_BOOK_NORMALIZER_VERSION,
        capture_run_id=capture_run_id,
        clock_skew_tolerance=clock_skew_tolerance,
    )


def _best_effort_text(event: Any, key: str) -> str | None:
    """Read a diagnostic-only string field without validating its shape.

    Mirrors :func:`argos.ingestion.clob_price_change._best_effort_text`
    exactly (not imported from there: it is a private name, and this one
    small, non-normalizing diagnostic helper is not the class of duplication
    ``docs/STATUS.md`` warns against). Used only to label a rejection record
    when the event could not be trusted enough to build a real
    :class:`~argos.domain.wsbook.WsBookSnapshotV1` from — the rejection
    ledger's own identifiers are neutralized and bounded, not refused, so no
    further validation is owed here.
    """
    if not isinstance(event, Mapping):
        return None
    value = event.get(key)
    return value if isinstance(value, str) and value else None


def _extract_source_hash(event: Mapping[str, Any]) -> str | None:
    """Read the book's content hash, tolerant of absence, strict about type.

    Mirrors :func:`argos.ingestion.clob_book._extract_source_hash`: absence is
    a fact about this event ("the source did not send one this time"); a
    present-but-wrong-typed value is refused rather than silently coerced or
    dropped. Every real `book` event in the recorded fixture carries a
    non-empty string `hash`, but this module does not assume a source
    guarantee it never measured.

    Validated here, inside the caller's own ``try`` — see the module
    docstring, point 3, for why leaving this to
    ``ObservationEnvelopeV1._validate_identifier`` alone would be the third
    appearance of an already-twice-fixed finding.
    """
    if "hash" not in event:
        return None
    value = event["hash"]
    if not isinstance(value, str) or not value:
        raise ValueError(f"'hash' must be a non-empty string when present, got {value!r}")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(
            f"'hash' exceeds {MAX_IDENTIFIER_LENGTH} characters ({len(value)} supplied)"
        )
    if not is_clean_identifier(value):
        raise ValueError("'hash' contains a display-control character")
    return value
