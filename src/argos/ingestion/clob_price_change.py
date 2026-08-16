"""Normalize a public CLOB market-channel `price_change` frame into an `ObservationEnvelopeV1`.

This is the ingestion-layer counterpart to :mod:`argos.domain.pricechange`,
the same relationship :mod:`argos.ingestion.clob_book` has to
:mod:`argos.domain.orderbook`: the domain payload model
(:func:`argos.domain.pricechange.parse_price_change_group`) raises plain
``ValueError`` from its validators, and this module is the layer that catches
it and turns it into a :class:`~argos.domain.observation.RejectedObservationV1`
with a reason (core invariant 14 -- errors are data, never a silent drop).

**No transport code lives here.** This module takes one already-JSON-decoded
frame; opening the WebSocket connection, subscribing, reconnecting, and
dispatching received text to this function are a later M2 slice
(``.claude/rules/no-execution.md`` scopes this repository to public read
adapters only, and the transport is out of scope for this slice regardless).

Two things this module does that are genuinely different from
:mod:`argos.ingestion.clob_book`, both measured against
``docs/research/m2-clob-websocket.md`` rather than assumed:

1. **`source_event_type` is read from the wire, not assigned by ARGOS.**
   The REST `/book` response carries no `event_type` field of its own, so
   ``clob_book.py`` had to invent ``"book_snapshot"``. The WebSocket
   market-channel frame carries its own `event_type: "price_change"`, so this
   module uses the source's own name for the message kind
   (:data:`CLOB_WS_PRICE_CHANGE_EVENT_TYPE`) rather than inventing a second
   label for the same concept.
2. **A frame with no entry for the requested token is a counted non-event,
   not a rejection.** See :func:`normalize_clob_price_change`'s docstring,
   the ``None`` return, for the full reasoning: the research note found a
   frame can legitimately carry entries for an unsubscribed binary sibling
   token and nothing for the token this call was made on behalf of, and that
   is normal traffic, not a defect.

Two backlog constraints from the ``price_change.v1`` payload slice
(``docs/BACKLOG.md``, "Constraints the WebSocket ingestion slice must close,
with measurements") are closed here, both reusing patterns already proven at
:mod:`argos.ingestion.clob_book`:

- **A byte cap checked before any parsing.**
  :func:`~argos.domain.pricechange.parse_price_change_group` bounds neither
  the ``price_changes`` array length nor any field size. Measured on the
  parse path alone (not yet the more expensive envelope-build path this
  module also runs): 100,000 entries cost 16.15 s CPU / 81.2 MiB; 900,000
  entries cost 146.87 s / 731.7 MiB. No ``Pacer`` can bound this -- a cancel
  scope cannot interrupt synchronous CPU work. :data:`MAX_NORMALIZABLE_BYTES`
  (imported from ``clob_book.py``, not redefined -- see its own docstring for
  why the bound is the envelope's canonical-payload cap rather than a new
  number) is checked against ``provenance.byte_length`` before any key is
  read out of ``event``, and the refusal is a rejection, not a raise: the
  bytes really did arrive, and a refusal that leaves no ledger entry is the
  silent drop invariant 14 forbids.
- **`entry_hash` validated inside this module's own `try`.** Measured on the
  pre-fix code: a newline, an OSC 52 sequence, a 257-character value and a
  20,000,000-character value all parse successfully out of
  :func:`~argos.domain.pricechange.parse_price_change_group` (it deliberately
  leaves ``entry_hash`` unsanitized -- see that module's docstring) and are
  then refused by ``ObservationEnvelopeV1._validate_identifier`` when
  ``source_hash`` reaches the envelope constructor -- but that refusal sits
  *outside* any ``try`` this module owns, so a raw pydantic
  ``ValidationError`` propagates out of :func:`normalize_clob_price_change`
  with **no ledger entry at all**. This is the third appearance of the same
  MEDIUM finding ``clob_book._extract_source_hash`` already closed once
  (length check against ``MAX_IDENTIFIER_LENGTH`` plus ``is_clean_identifier``,
  inside the module's own ``try``); :func:`_validate_entry_hash` reproduces
  that check rather than the gap.

``timestamp`` -> ``event_time`` reuses
:func:`argos.ingestion.wire.parse_event_time`, the REST adapter's own parser,
moved to a shared module rather than duplicated -- see that module's
docstring and ``docs/STATUS.md``: this is the second wire field (after
``Decimal`` scale) where this repository has deliberately guarded against
reimplementing normalization logic that already exists once.

`ingest_sequence` is a caller-supplied parameter, not allocated here, for the
same reason ``clob_book.py`` documents: this module is a pure, synchronous
normalization step, not the capture loop. The WebSocket research note found a
frame can carry entries for an *unsubscribed* sibling token, so assigning
sequence numbers before subscription/token filtering would make
``ingest_sequence`` depend on connection topology -- deliberately left to the
capture-loop slice, per ``docs/BACKLOG.md``.
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
from argos.domain.pricechange import NoEntriesForToken, parse_price_change_group
from argos.domain.provenance import SourceProvenanceV1
from argos.domain.text import is_clean_identifier
from argos.errors import RejectionReason
from argos.ingestion.clob_book import MAX_NORMALIZABLE_BYTES
from argos.ingestion.wire import parse_event_time

CLOB_PRICE_CHANGE_NORMALIZER_VERSION: Final = "clob-price-change-normalizer/1"

CLOB_WS_PRICE_CHANGE_EVENT_TYPE: Final = "price_change"
"""The source's own `event_type` for this message kind, read verbatim rather
than assigned by ARGOS -- unlike :data:`argos.ingestion.clob_book.CLOB_REST_BOOK_EVENT_TYPE`,
where the REST `/book` response carries no `event_type` field of its own and
one had to be invented. See the module docstring, point 1."""


def normalize_clob_price_change(
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
    """Normalize one already-JSON-decoded `price_change` WebSocket frame for one token.

    Returns one of three outcomes:

    - An :class:`~argos.domain.observation.ObservationEnvelopeV1` when the
      frame carries at least one well-formed ``price_changes`` entry for
      ``requested_token_id``.
    - A :class:`~argos.domain.observation.RejectedObservationV1` when the
      frame **was** about this token (or could not even be inspected well
      enough to tell) but could not be honestly represented -- every
      ``ValueError`` :func:`~argos.domain.pricechange.parse_price_change_group`
      can raise, plus this module's own ``entry_hash`` validation, is caught
      and represented with :attr:`~argos.errors.RejectionReason.MALFORMED_PAYLOAD`.
      This function never raises for a malformed *payload*; a caller error
      (an invalid ``provenance``, for example) still raises, the same
      contract :func:`~argos.domain.observation.build_observation_envelope`
      itself has.
    - ``None`` when the frame carries **no entry at all for
      `requested_token_id`**. This is deliberately *not* a rejection.
      ``docs/research/m2-clob-websocket.md`` established, by direct
      observation on live traffic, that subscribing to one token delivers
      `price_change` entries for its unsubscribed binary sibling in the same
      frame -- a frame naming only the sibling is normal, healthy traffic
      about a token nobody asked this call about, not a defect in the frame
      or in this token's subscription. Turning it into a rejection would
      flood the ledger with rows describing perfectly healthy traffic and
      destroy the ledger's signal value.

      **The caller MUST count a `None` return.** Per
      ``.claude/rules/data-integrity.md`` ("every drop, duplicate, parse
      failure, gap, late event, and fallback is counted and reasoned"), this
      is a counted non-event, not a silent drop -- it is the capture loop's
      job to count it (for example as a per-token "frame not applicable"
      health counter), because this function has no health-counter object to
      write to and no ledger row is the correct representation of "this frame
      was never about this token" (see the docstring point above and the
      distinction from an actual rejection: a rejection means the frame *was*
      about this token and something else about it could not be honestly
      represented; ``None`` means it never was).
    """
    if provenance.byte_length > MAX_NORMALIZABLE_BYTES:
        # See the module docstring, "A byte cap checked before any parsing."
        # Refused before a single key is read out of `event`, because the
        # cost this bounds -- constructing one PriceLevelChangeV1 per entry
        # -- is incurred by the reading itself and no Pacer deadline can
        # interrupt synchronous CPU work once it has started.
        return build_rejected_observation(
            reason=RejectionReason.MALFORMED_PAYLOAD,
            detail=(
                f"frame of {provenance.byte_length} bytes exceeds the "
                f"{MAX_NORMALIZABLE_BYTES}-byte normalization budget"
            ),
            source=ObservationSource.CLOB_MARKET_WS,
            source_event_type=CLOB_WS_PRICE_CHANGE_EVENT_TYPE,
            token_id=requested_token_id,
            provenance=provenance,
            received_time=received_time,
            rejected_at=rejected_at,
            capture_run_id=capture_run_id,
        )

    condition_id_hint = _best_effort_text(event, "market")

    try:
        group = parse_price_change_group(event, asset_id=requested_token_id)
        entry_hash = _validate_entry_hash(group.entry_hash)
    except NoEntriesForToken:
        # Not a malformation -- see the docstring's `None` case. The frame
        # passed every other check `parse_price_change_group` runs (non-object
        # event/entry, event_type, top-level and entry-level key shape) before
        # it ever reaches this specific one, which is the last that function
        # performs -- so this branch is reached only once every malformation
        # the domain knows how to detect has already been ruled out for the
        # *whole* frame, not merely for this token.
        #
        # `NoEntriesForToken` subclasses `ValueError`, so this clause must
        # stay above the general one below.
        return None
    except ValueError as error:
        return build_rejected_observation(
            reason=RejectionReason.MALFORMED_PAYLOAD,
            detail=str(error),
            source=ObservationSource.CLOB_MARKET_WS,
            source_event_type=CLOB_WS_PRICE_CHANGE_EVENT_TYPE,
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
        source_event_type=CLOB_WS_PRICE_CHANGE_EVENT_TYPE,
        condition_id=group.payload.condition_id,
        token_id=group.payload.asset_id,
        event_time=event_time,
        event_time_raw=event_time_raw,
        received_time=received_time,
        ingest_sequence=ingest_sequence,
        source_hash=entry_hash,
        payload=group.payload,
        provenance=provenance,
        raw_payload_location=raw_payload_location,
        parser_version=CLOB_PRICE_CHANGE_NORMALIZER_VERSION,
        capture_run_id=capture_run_id,
        clock_skew_tolerance=clock_skew_tolerance,
    )


def _best_effort_text(event: Any, key: str) -> str | None:
    """Read a diagnostic-only string field without validating its shape.

    Mirrors :func:`argos.ingestion.clob_book._best_effort_text` exactly (not
    imported from there: it is a private name, and this one small,
    non-normalizing diagnostic helper is not the class of duplication
    ``docs/STATUS.md`` warns against -- that class is `Decimal`/timestamp
    *normalization* logic, which this module reuses rather than
    reimplements; see :func:`argos.ingestion.wire.parse_event_time`). Used
    only to label a rejection record when the frame could not be trusted
    enough to build a real :class:`~argos.domain.pricechange.PriceChangeV1`
    from -- the rejection ledger's own identifiers are neutralized and
    bounded, not refused
    (:class:`~argos.domain.observation.RejectedObservationV1`), so no
    further validation is owed here.
    """
    if not isinstance(event, Mapping):
        return None
    value = event.get(key)
    return value if isinstance(value, str) and value else None


def _validate_entry_hash(value: str) -> str:
    """Validate the group's own content hash before it can reach `source_hash`.

    See the module docstring, "`entry_hash` validated inside this module's
    own `try`." :func:`~argos.domain.pricechange.parse_price_change_group`
    already guarantees ``entry_hash`` is a non-empty ``str`` -- only length
    and content need checking here, the same two checks
    ``clob_book._extract_source_hash`` already applies to the REST book's
    own ``hash`` field.
    """
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(
            f"'hash' exceeds {MAX_IDENTIFIER_LENGTH} characters ({len(value)} supplied)"
        )
    if not is_clean_identifier(value):
        raise ValueError("'hash' contains a display-control character")
    return value
