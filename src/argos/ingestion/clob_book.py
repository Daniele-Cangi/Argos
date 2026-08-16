"""Normalize a public CLOB `/book` response into an `ObservationEnvelopeV1`.

This is the ingestion-layer counterpart to :mod:`argos.domain.orderbook`, the
same relationship :mod:`argos.ingestion.gamma_markets` has to
:mod:`argos.domain.market`: the domain payload model raises plain
``ValueError`` from its validators, and this module is the layer that catches
it and turns it into a :class:`~argos.domain.observation.RejectedObservationV1`
with a reason (core invariant 14 — errors are data, never a silent drop).

Two decisions this module makes beyond what
:func:`argos.domain.orderbook.parse_order_book_snapshot` already does, both
measured against ``docs/research/m2-clob-rest-book.md`` rather than assumed:

1. **`timestamp` becomes `event_time`, never `received_time`.**
   ``.claude/rules/data-integrity.md`` forbids substituting the current time
   for a missing or unparseable source timestamp. The research note's own
   three-poll timing experiment established that the wire `timestamp` tracks
   the book's *last change*, not the response instant, so it is a genuine
   source-assigned event time, not a response timestamp masquerading as one.
   :func:`argos.ingestion.wire.parse_event_time` never raises and never falls
   back to a clock: a present-and-parseable value becomes
   ``EventTimeStatus.PRESENT``, a present value that will not parse becomes
   ``EventTimeStatus.UNPARSEABLE`` with the source's own text preserved
   verbatim in ``event_time_raw`` (through the envelope's own sanitizer), and
   an absent value becomes ``EventTimeStatus.MISSING``. Shared with
   :mod:`argos.ingestion.clob_price_change`, whose WebSocket frames carry the
   identical wire shape — see that module's docstring.
2. **`hash` is optional, its type is not.** The research note never observed
   the field missing, but this module does not assume a source guarantee it
   never measured. A `/book` response with no `hash` key at all is accepted
   with ``source_hash=None`` — the source simply did not send one this time,
   which is honestly represented rather than treated as malformed. A `hash`
   key present with a non-string value is a different, more suspicious
   signal — the response schema is not what this module was built against —
   and is refused as :attr:`~argos.errors.RejectionReason.MALFORMED_PAYLOAD`.

`ingest_sequence` is a caller-supplied parameter, not allocated here: this
module is a pure, synchronous normalization step, not the capture loop.
Sequence allocation, raw-payload archiving orchestration (this module only
*forwards* an already-archived ``raw_payload_location``, matching
``build_observation_envelope``'s own parameter, exactly as
``argos.cli.markets_discover`` archives Gamma responses outside
``normalize_markets``), and manifest bookkeeping are all left to that later
M2 slice on purpose (see the task instructions this module was built from).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Final

from argos.domain.observation import (
    DEFAULT_CLOCK_SKEW_TOLERANCE,
    MAX_IDENTIFIER_LENGTH,
    MAX_PAYLOAD_CANONICAL_BYTES,
    ObservationEnvelopeV1,
    ObservationSource,
    RejectedObservationV1,
    build_observation_envelope,
    build_rejected_observation,
)
from argos.domain.orderbook import parse_order_book_snapshot
from argos.domain.provenance import SourceProvenanceV1
from argos.domain.text import is_clean_identifier
from argos.errors import RejectionReason
from argos.ingestion.wire import parse_event_time

MAX_NORMALIZABLE_BYTES: Final = MAX_PAYLOAD_CANONICAL_BYTES
"""Largest response this module will read text out of.

Aligned with the envelope's canonical-payload cap rather than the client's
32 MiB response cap: a response too large to become a payload cannot become an
accepted observation anyway, so doing the work first and discarding it is pure
cost. The largest book actually accepted is ~127,000 levels (~4.7 MB on the
wire), against a client that accepts 32 MiB -- roughly 7x of headroom in which
normalization did full work and threw it away.
"""

CLOB_BOOK_NORMALIZER_VERSION: Final = "clob-book-normalizer/1"

CLOB_REST_BOOK_EVENT_TYPE: Final = "book_snapshot"
"""ARGOS's own label for this message kind. The REST `/book` response carries
no `event_type` field of its own (unlike the WebSocket market channel), so
this is assigned rather than read from the wire. Named distinctly from the
WebSocket channel's own `"book"` `event_type` on purpose: both are full
snapshots, but they arrive over different transports with different
delivery/ordering guarantees (docs/research/m2-clob-websocket.md), and
conflating their `source_event_type` label would erase that distinction for a
reader filtering by it. `ObservationEnvelopeV1.source` (`clob_rest` vs
`clob_market_ws`) already disambiguates at the identity level; this label
keeps it legible at a glance too."""

# The millisecond wire timestamp parser (`_TIMESTAMP_MS_PATTERN` and the
# function below) moved to `argos.ingestion.wire.parse_event_time`: the
# WebSocket market-channel `price_change` frame carries the identical wire
# shape (`docs/research/m2-clob-websocket.md`), and duplicating the parser
# would have been a third instance of the decimal-normalization defect class
# `docs/STATUS.md` already records twice — same class, different field.


def normalize_clob_book(
    *,
    payload: Any,
    provenance: SourceProvenanceV1,
    requested_token_id: str,
    received_time: datetime,
    rejected_at: datetime,
    ingest_sequence: int,
    capture_run_id: str,
    raw_payload_location: str | None = None,
    clock_skew_tolerance: timedelta = DEFAULT_CLOCK_SKEW_TOLERANCE,
) -> ObservationEnvelopeV1 | RejectedObservationV1:
    """Normalize one already-JSON-decoded `/book` response body.

    Always returns a record — an accepted envelope or a rejection — and never
    raises for a malformed *payload*: every failure
    :func:`~argos.domain.orderbook.parse_order_book_snapshot` can raise, plus
    this module's own asset-id cross-check and `hash`-type check, is caught
    and represented as a :class:`~argos.domain.observation.RejectedObservationV1`
    with :attr:`~argos.errors.RejectionReason.MALFORMED_PAYLOAD`. A caller
    error (an invalid ``provenance``, for example) still raises, the same
    contract :func:`~argos.domain.observation.build_observation_envelope`
    itself has.
    """
    if provenance.byte_length > MAX_NORMALIZABLE_BYTES:
        # Refused before any text is read out of the payload, because the cost
        # this bounds is incurred by the reading itself. Security review
        # measured the gap between the client's 32 MiB response cap and the
        # envelope's 4 MiB canonical-payload cap: a 31 MiB `timestamp` string
        # cost 21.18 s CPU and was *accepted*; a 31 MiB `market` string on a
        # rejected payload cost 32.37 s and 571 MiB RSS; 900,000 book levels
        # peaked at 1,266 MiB before being refused. `neutralize_and_bound`
        # bounds the stored value but neutralizes the whole input first, and
        # both the accepted and the rejected path run it twice.
        #
        # A Pacer cannot bound any of it -- a cancel scope cannot interrupt
        # synchronous CPU work, re-measured here at 14.21 s elapsed against a
        # 0.50 s deadline with cancelled_caught False. This is the same class
        # the earlier M2 review closed at build_observation_envelope,
        # relocated one layer upstream where no cap applied.
        #
        # A rejection rather than a raise: the bytes really did arrive, and a
        # refusal that leaves no ledger entry is the silent drop invariant 14
        # forbids.
        return build_rejected_observation(
            reason=RejectionReason.MALFORMED_PAYLOAD,
            detail=(
                f"response of {provenance.byte_length} bytes exceeds the "
                f"{MAX_NORMALIZABLE_BYTES}-byte normalization budget"
            ),
            source=ObservationSource.CLOB_REST,
            source_event_type=CLOB_REST_BOOK_EVENT_TYPE,
            token_id=requested_token_id,
            provenance=provenance,
            received_time=received_time,
            rejected_at=rejected_at,
            capture_run_id=capture_run_id,
        )

    condition_id_hint = _best_effort_text(payload, "market")

    try:
        if not isinstance(payload, Mapping):
            raise ValueError(
                f"order book response must be a JSON object, got {type(payload).__name__}"
            )
        snapshot = parse_order_book_snapshot(payload)
        if snapshot.asset_id != requested_token_id:
            raise ValueError(
                f"response asset_id {snapshot.asset_id!r} does not match the requested "
                f"token id {requested_token_id!r}"
            )
        source_hash = _extract_source_hash(payload)
    except ValueError as error:
        return build_rejected_observation(
            reason=RejectionReason.MALFORMED_PAYLOAD,
            detail=str(error),
            source=ObservationSource.CLOB_REST,
            source_event_type=CLOB_REST_BOOK_EVENT_TYPE,
            condition_id=condition_id_hint,
            token_id=requested_token_id,
            provenance=provenance,
            received_time=received_time,
            rejected_at=rejected_at,
            capture_run_id=capture_run_id,
        )

    event_time, event_time_raw = parse_event_time(payload.get("timestamp"))

    return build_observation_envelope(
        source=ObservationSource.CLOB_REST,
        source_event_type=CLOB_REST_BOOK_EVENT_TYPE,
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
        parser_version=CLOB_BOOK_NORMALIZER_VERSION,
        capture_run_id=capture_run_id,
        clock_skew_tolerance=clock_skew_tolerance,
    )


def _best_effort_text(payload: Any, key: str) -> str | None:
    """Read a diagnostic-only string field without validating its shape.

    Used only to label a rejection record when the payload could not be
    trusted enough to build a real :class:`OrderBookSnapshotV1` from — the
    rejection ledger's own identifiers are neutralized and bounded, not
    refused (``argos.domain.observation.RejectedObservationV1``), so no
    further validation is owed here.
    """
    if not isinstance(payload, Mapping):
        return None
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _extract_source_hash(payload: Mapping[str, Any]) -> str | None:
    """Read the book's content hash, tolerant of absence, strict about type.

    See the module docstring, point 2: absence is a fact about this response
    ("the source did not send one"); a present-but-wrong-typed value is
    evidence the response does not match the schema this module was built
    against, and is refused rather than silently coerced or dropped.
    """
    if "hash" not in payload:
        return None
    value = payload["hash"]
    if not isinstance(value, str) or not value:
        raise ValueError(f"'hash' must be a non-empty string when present, got {value!r}")

    # Validated here, inside the caller's try, rather than left to
    # ObservationEnvelopeV1._validate_identifier. Security review reproduced
    # both escapes on a 3.7 KB payload: a 257-character hash and a hash
    # containing a newline each raised a raw pydantic ValidationError out of
    # normalize_clob_book, which this module's own docstring promises never
    # happens for a malformed payload -- and, worse, produced no ledger entry
    # at all. That is the silent drop core invariant 14 exists to prevent, and
    # the same shape as the M1 "read_raw_payload leaked JSONDecodeError"
    # finding. Refusing here turns both into a counted rejection with a reason.
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(
            f"'hash' exceeds {MAX_IDENTIFIER_LENGTH} characters ({len(value)} supplied)"
        )
    if not is_clean_identifier(value):
        raise ValueError("'hash' contains a display-control character")
    return value
