"""The canonical `ObservationEnvelope` and its rejection-ledger counterpart.

``docs/02_ARCHITECTURE.md`` names this the boundary every source adapter writes
to and the event store reads from: "Canonical ObservationEnvelope". Two
contracts live here:

- :class:`ObservationEnvelopeV1` — one accepted source observation, with the
  three times kept separate (core invariant 6) and a deterministic identity so
  the store can recognize a re-delivered source event without accepting it
  twice (M2 exit criterion: "duplicate source event does not create a second
  accepted observation").
- :class:`RejectedObservationV1` — the rejection ledger record for input
  ARGOS refuses (M2 exit criterion: "invalid messages enter a rejection ledger
  with reason and raw hash"). Core invariant 14: errors are data, never a
  silent drop.

Neither carries typed per-event-type fields (order book, trade, price change,
...): those payload shapes are still being researched in a later M2 slice.
Instead the envelope stores the normalized payload as an opaque, frozen
container plus ``payload_schema_version`` naming the :class:`VersionedModel`
that actually describes it. :func:`read_payload` is the typed accessor for a
caller that knows which model to expect; the envelope itself, the store, and
the replay reader never need to.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, ClassVar, cast

from pydantic import Field, field_serializer, field_validator, model_validator

from argos.clock import ensure_utc
from argos.domain.provenance import SHA256_LENGTH, SourceProvenanceV1
from argos.domain.text import (
    is_clean_identifier,
    neutralize_and_bound,
    neutralize_identifier_and_bound,
)
from argos.domain.versioning import (
    SCHEMA_VERSION_KEY,
    VersionedModel,
    freeze,
    resolve_schema,
    thaw,
)
from argos.errors import ContractViolationError, RejectionReason, SchemaVersionError

# Untrusted source text is neutralized, never dropped outright, and bounded so a
# hostile or malformed source cannot grow a ledger record without limit.
MAX_DETAIL_LENGTH = 4_000
MAX_EVENT_TIME_RAW_LENGTH = 500
MAX_SOURCE_EVENT_TYPE_LENGTH = 200
MAX_IDENTIFIER_LENGTH = 256
"""Cap on every machine identifier a source supplies. Security review accepted a
20,000,000-character ``market_id`` on a rejection record whose ``detail`` was
capped at 4,043 — the bound the module claimed applied to one field beside five
that had none. A condition id is 66 characters and a uint256 token id is 78."""

MAX_PAYLOAD_CANONICAL_BYTES = 4 * 1024 * 1024
"""Cap on the canonical serialization of a normalized payload.

Security review measured a 31.8 MiB payload — legal under the source client's
own 32 MiB response cap — costing 2.84 s of CPU and 750 MiB of RSS to build one
envelope, because building traverses the payload four times. The ``Pacer``
deadline cannot bound it: ``move_on_after`` is a cancel scope and cannot
interrupt synchronous CPU work, measured at 1.03 s elapsed against a 0.50 s
deadline with ``cancelled_caught`` false. This is the M1 gzip-bomb class
relocated downstream of the byte cap that fixed it, so the bound has to exist
here too."""

DEFAULT_CLOCK_SKEW_TOLERANCE = timedelta(seconds=2)
"""How far a source's event time may lead the moment ARGOS received it before the
observation is flagged. ``docs/04_DATA_CONTRACTS.md`` requires a configured
tolerance and a quality flag; a small non-zero default reflects that a source
clock and ours are never exactly aligned, while an event time that leads by more
than this is evidence about the source, not about the market."""


class ObservationSource(StrEnum):
    """Where an observation originated.

    ``docs/04_DATA_CONTRACTS.md`` lists these "at least"; a genuinely new
    source adapter adds its own member here when it is actually built, the
    same convention ``RunMode`` uses — membership tracks real need, not
    anticipated milestones.
    """

    GAMMA = "gamma"
    CLOB_REST = "clob_rest"
    CLOB_MARKET_WS = "clob_market_ws"


class EventTimeStatus(StrEnum):
    """Whether the source's own event time could be used at all.

    Core invariant 6 and ``.claude/rules/data-integrity.md`` forbid quietly
    substituting ``received_time`` for a missing or unparseable ``event_time``.
    This is the explicit reason that absence is represented instead of
    papered over: a projection reading ``event_time`` must be able to tell
    "the source never sent one" apart from "the source sent something ARGOS
    could not parse", because the second case is evidence of a parser or
    source-schema defect and the first is not.
    """

    PRESENT = "present"
    MISSING = "missing"
    UNPARSEABLE = "unparseable"


class ObservationQualityFlag(StrEnum):
    """A defect in an accepted observation that a consumer must be able to see.

    Accepting an observation and trusting every field of it are different
    things. A flag records the second kind of doubt without discarding the
    evidence, which is what core invariant 14 asks for: the anomaly is counted
    and reasoned, not dropped and not silently smoothed over.
    """

    EVENT_TIME_AHEAD_OF_RECEIPT = "event_time_ahead_of_receipt"
    """The source's event time leads ``received_time`` by more than the
    configured clock-skew tolerance. Required by ``docs/04_DATA_CONTRACTS.md``.
    The observation is still accepted — a source clock running fast is a fact
    about the source — but an event-time window must be able to tell that this
    timestamp was never corroborated."""


class ObservationEnvelopeV1(VersionedModel):
    """One accepted source observation, normalized but not yet interpreted.

    ``observation_id`` is deterministic (see :func:`_observation_identity`) so
    that redelivering the exact same source event twice — a websocket resend
    after reconnect, a REST poll returning an unchanged snapshot — produces the
    same identity both times, and the event store's insert can be idempotent
    rather than accumulating duplicates.
    """

    schema_version: ClassVar[str] = "observation_envelope.v1"

    observation_id: str = Field(min_length=1)
    source: ObservationSource
    source_event_type: str = Field(min_length=1)
    """The source's own name for this message kind (for example a CLOB
    websocket ``event_type`` such as ``"book"`` or ``"price_change"``). Doubles
    as the logical-contract's ``payload_type``: a second, ARGOS-facing field
    with the same meaning would only invite the two to silently disagree."""

    market_id: str | None = Field(default=None, min_length=1)
    condition_id: str | None = Field(default=None, min_length=1)
    token_id: str | None = Field(default=None, min_length=1)
    """All three are optional: some source messages are lifecycle-wide (for
    example a connection-level heartbeat) and name no specific market."""

    event_time_status: EventTimeStatus
    event_time: datetime | None = None
    event_time_raw: str | None = Field(default=None, min_length=1)
    """Set only when ``event_time_status`` is ``UNPARSEABLE``: the source's own
    text, neutralized and bounded, kept so a human can diagnose *why* parsing
    failed without ARGOS ever guessing a substitute time."""

    received_time: datetime
    ingest_sequence: int = Field(gt=0)
    """Assigned by ingestion, not generated here — the domain never invents its
    own notion of arrival order (``docs/02_ARCHITECTURE.md``, "Temporal
    model")."""

    source_sequence: str | None = Field(default=None, min_length=1)
    source_hash: str | None = Field(default=None, min_length=1)

    quality_flags: tuple[ObservationQualityFlag, ...] = ()
    """Defects detected while accepting this observation. Empty is the normal
    case and means "no anomaly detected", never "not checked"."""

    payload_schema_version: str = Field(min_length=1)
    """Names the :class:`VersionedModel` subclass that describes ``payload``
    (for example ``"order_book_snapshot.v1"``), so a reader can dispatch on
    type without the envelope itself being generic. See :func:`read_payload`."""

    payload: Mapping[str, Any]
    """The normalized payload in its **JSON-canonical form**, frozen on
    validation (core invariant 7: a new representation, never a replacement for
    the raw bytes below).

    Canonical form, not the live Python objects, because the envelope has to
    survive a round trip through storage unchanged. ``to_record`` serializes in
    JSON mode, so a ``Decimal('0.5')`` held here would reload as ``'0.5'``: the
    replayed envelope would differ from the live one while carrying the same
    ``observation_id``, which is invariant 5 broken and M3's identical-hash
    criterion with it. Storing the canonical form makes live and replayed
    objects identical by construction. Typed access restores the real types —
    see :func:`read_payload`, which validates this mapping back into the model
    named by ``payload_schema_version``."""

    raw_payload_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    raw_payload_location: str | None = Field(default=None, min_length=1)
    provenance: SourceProvenanceV1

    parser_version: str = Field(min_length=1)
    capture_run_id: str = Field(min_length=1)

    @field_validator(
        "source_event_type",
        "market_id",
        "condition_id",
        "token_id",
        "source_sequence",
        "source_hash",
        "raw_payload_location",
        "parser_version",
        "capture_run_id",
    )
    @classmethod
    def _validate_identifier(cls, value: str | None) -> str | None:
        """Refuse a hostile or unbounded identifier instead of neutralizing it.

        These are machine identifiers, not prose. Security review found them
        stored verbatim — ESC, OSC 52, RLO and newlines all survived in
        ``market_id`` while ``detail`` beside it was correctly sanitized — and
        unbounded, and this is the M1 finding recurring: ``compiler/audit.py``
        says in its own comment that ``market_id`` is "validated for presence,
        not for content" and that rendering it raw put a working clipboard write
        in the report's title.

        Refusal rather than neutralization is deliberate. Neutralizing is lossy,
        and these fields are inside the observation identity, so two different
        hostile ids would collapse to one stored value and take two genuinely
        different observations with them. An accepted observation must have clean
        identifiers; a payload that does not have them belongs in the rejection
        ledger, which is built to hold exactly that.
        """
        if value is None:
            return None
        if len(value) > MAX_IDENTIFIER_LENGTH:
            raise ValueError(
                f"identifier exceeds {MAX_IDENTIFIER_LENGTH} characters "
                f"({len(value)} supplied); refused rather than truncated because "
                "the value is part of the observation identity"
            )
        if not is_clean_identifier(value):
            raise ValueError(
                "identifier contains a display-control character; refused rather "
                "than neutralized because neutralization is lossy and this value "
                "is part of the observation identity"
            )
        return value

    @field_validator("event_time_raw")
    @classmethod
    def _sanitize_event_time_raw(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return neutralize_and_bound(value, MAX_EVENT_TIME_RAW_LENGTH)

    @field_validator("event_time", "received_time")
    @classmethod
    def _anchor_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @field_validator("payload")
    @classmethod
    def _freeze_payload(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], freeze(value))

    @field_serializer("payload")
    def _serialize_payload(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], thaw(value))

    @model_validator(mode="after")
    def _validate_event_time_shape(self) -> ObservationEnvelopeV1:
        status = self.event_time_status
        if status is EventTimeStatus.PRESENT:
            if self.event_time is None:
                raise ValueError("event_time_status is present but event_time is unset")
            if self.event_time_raw is not None:
                raise ValueError("event_time_status is present; event_time_raw must be unset")
        elif status is EventTimeStatus.MISSING:
            if self.event_time is not None:
                raise ValueError("event_time_status is missing; event_time must be unset")
            if self.event_time_raw is not None:
                raise ValueError(
                    "event_time_status is missing; there is nothing to capture in "
                    "event_time_raw (use UNPARSEABLE if the source sent something)"
                )
        else:  # UNPARSEABLE
            if self.event_time is not None:
                raise ValueError("event_time_status is unparseable; event_time must be unset")
            if self.event_time_raw is None:
                raise ValueError(
                    "event_time_status is unparseable but no raw source text was captured"
                )
        return self

    @model_validator(mode="after")
    def _validate_provenance_matches_raw_hash(self) -> ObservationEnvelopeV1:
        if self.raw_payload_sha256 != self.provenance.raw_sha256:
            raise ValueError(
                "raw_payload_sha256 must match provenance.raw_sha256: "
                f"{self.raw_payload_sha256!r} vs {self.provenance.raw_sha256!r}"
            )
        return self

    @model_validator(mode="after")
    def _validate_provenance_names_the_same_source(self) -> ObservationEnvelopeV1:
        """An envelope must not attribute itself to a different source than its bytes.

        ``provenance.source`` also builds the raw archive path, so a mismatch
        does not merely mislabel the record: it points the envelope at the wrong
        archive directory, and the raw payload it claims to be linked to cannot
        be found where the envelope says it is.
        """
        if self.provenance.source != self.source.value:
            raise ValueError(
                "provenance.source must name the same source as the envelope: "
                f"{self.provenance.source!r} vs {self.source.value!r}"
            )
        return self


class RejectedObservationV1(VersionedModel):
    """The rejection ledger record for input ARGOS refuses to accept.

    Deliberately holds no parsed/trusted payload: a record built from data
    ARGOS could not validate must not itself present as validated data. Any
    embedded source text goes through :func:`argos.domain.text.neutralize_and_bound`,
    the same shared sanitizer the M1 audit renderer uses for a market's own
    question and description, so a hostile payload cannot forge terminal output
    or another ledger entry through this record.
    """

    schema_version: ClassVar[str] = "rejected_observation.v1"

    rejection_id: str = Field(min_length=1)
    reason: RejectionReason
    detail: str = Field(min_length=1)

    source: ObservationSource
    source_event_type: str | None = Field(default=None, min_length=1)
    market_id: str | None = Field(default=None, min_length=1)
    condition_id: str | None = Field(default=None, min_length=1)
    token_id: str | None = Field(default=None, min_length=1)

    raw_payload_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    provenance: SourceProvenanceV1

    received_time: datetime
    rejected_at: datetime
    capture_run_id: str = Field(min_length=1)

    @field_validator("detail")
    @classmethod
    def _sanitize_detail(cls, value: str) -> str:
        return neutralize_and_bound(value, MAX_DETAIL_LENGTH)

    @field_validator("source_event_type", "market_id", "condition_id", "token_id")
    @classmethod
    def _sanitize_identifier(cls, value: str | None) -> str | None:
        """Neutralize and bound, rather than refuse.

        The opposite choice from the envelope's, and deliberately so. This record
        exists to hold input ARGOS could not validate, so refusing a hostile
        identifier here would mean the rejection itself could not be written —
        the input would vanish with no ledger entry, which is the silent drop
        core invariant 14 exists to prevent. Bounding matters independently:
        security review accepted a 20,000,000-character ``market_id`` on a record
        whose ``detail`` was capped at 4,043.
        """
        if value is None:
            return None
        return neutralize_identifier_and_bound(value, MAX_IDENTIFIER_LENGTH)

    @field_validator("received_time", "rejected_at")
    @classmethod
    def _anchor_timestamps(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _validate_provenance_names_the_same_source(self) -> RejectedObservationV1:
        """Same cross-check as the envelope: a ledger entry must not misattribute."""
        if self.provenance.source != self.source.value:
            raise ValueError(
                "provenance.source must name the same source as the rejection: "
                f"{self.provenance.source!r} vs {self.source.value!r}"
            )
        return self

    @model_validator(mode="after")
    def _validate_provenance_matches_raw_hash(self) -> RejectedObservationV1:
        if self.raw_payload_sha256 != self.provenance.raw_sha256:
            raise ValueError(
                "raw_payload_sha256 must match provenance.raw_sha256: "
                f"{self.raw_payload_sha256!r} vs {self.provenance.raw_sha256!r}"
            )
        return self


def build_observation_envelope(
    *,
    source: ObservationSource,
    source_event_type: str,
    market_id: str | None = None,
    condition_id: str | None = None,
    token_id: str | None = None,
    event_time: datetime | None,
    event_time_raw: str | None = None,
    received_time: datetime,
    ingest_sequence: int,
    source_sequence: str | None = None,
    source_hash: str | None = None,
    payload: VersionedModel,
    provenance: SourceProvenanceV1,
    raw_payload_location: str | None = None,
    parser_version: str,
    capture_run_id: str,
    clock_skew_tolerance: timedelta = DEFAULT_CLOCK_SKEW_TOLERANCE,
) -> ObservationEnvelopeV1:
    """Build an envelope from a *typed* payload, deriving identity from stable content.

    ``payload`` is the typed normalized event — an order-book snapshot, a price
    change — not a bare mapping. Taking the model rather than a mapping plus a
    version string closes two failures that an earlier draft had: a caller could
    label any mapping with any ``payload_schema_version``, and the mismatch only
    surfaced when :func:`read_payload` ran during *replay*, against a capture
    that can no longer be re-taken. Now the label cannot disagree with the
    content, because both come from the same object.

    ``event_time_status`` is inferred rather than asked for twice: pass
    ``event_time`` when the source's timestamp parsed, ``event_time_raw``
    (with ``event_time=None``) when the source sent something that did not
    parse, or neither when the source sent no event time at all.
    """
    if event_time is not None:
        status = EventTimeStatus.PRESENT
    elif event_time_raw is not None:
        status = EventTimeStatus.UNPARSEABLE
    else:
        status = EventTimeStatus.MISSING

    payload_schema_version = type(payload).schema_version
    canonical_payload = _canonical_payload(payload)
    canonical_json = _canonical_json(canonical_payload)
    if len(canonical_json.encode()) > MAX_PAYLOAD_CANONICAL_BYTES:
        raise ContractViolationError(
            "normalized payload exceeds the canonical size cap",
            payload_schema_version=payload_schema_version,
            canonical_bytes=len(canonical_json.encode()),
            cap=MAX_PAYLOAD_CANONICAL_BYTES,
        )

    flags: tuple[ObservationQualityFlag, ...] = ()
    if event_time is not None and ensure_utc(event_time) - ensure_utc(received_time) > (
        clock_skew_tolerance
    ):
        flags = (ObservationQualityFlag.EVENT_TIME_AHEAD_OF_RECEIPT,)

    observation_id = _observation_identity(
        source=source,
        source_event_type=neutralize_and_bound(source_event_type, MAX_SOURCE_EVENT_TYPE_LENGTH),
        market_id=market_id,
        condition_id=condition_id,
        token_id=token_id,
        payload_schema_version=payload_schema_version,
        payload_canonical_json=canonical_json,
        event_time_status=status,
        event_time=event_time,
        event_time_raw=(
            None
            if event_time_raw is None
            else neutralize_and_bound(event_time_raw, MAX_EVENT_TIME_RAW_LENGTH)
        ),
        source_sequence=source_sequence,
        source_hash=source_hash,
    )

    return ObservationEnvelopeV1(
        observation_id=observation_id,
        source=source,
        source_event_type=source_event_type,
        market_id=market_id,
        condition_id=condition_id,
        token_id=token_id,
        event_time_status=status,
        event_time=event_time,
        event_time_raw=event_time_raw,
        received_time=received_time,
        ingest_sequence=ingest_sequence,
        source_sequence=source_sequence,
        source_hash=source_hash,
        quality_flags=flags,
        payload_schema_version=payload_schema_version,
        payload=canonical_payload,
        raw_payload_sha256=provenance.raw_sha256,
        raw_payload_location=raw_payload_location,
        provenance=provenance,
        parser_version=parser_version,
        capture_run_id=capture_run_id,
    )


def build_rejected_observation(
    *,
    reason: RejectionReason,
    detail: str,
    source: ObservationSource,
    source_event_type: str | None = None,
    market_id: str | None = None,
    condition_id: str | None = None,
    token_id: str | None = None,
    provenance: SourceProvenanceV1,
    received_time: datetime,
    rejected_at: datetime,
    capture_run_id: str,
) -> RejectedObservationV1:
    """Build a rejection ledger record with a deterministic identity.

    Re-delivery of the exact same invalid bytes, refused for the same reason,
    collapses onto the same ``rejection_id`` — the same idempotency the
    accepted-observation path gets from ``observation_id``, for the same
    reason: a store must be able to insert a redelivered rejection without
    growing the ledger without bound.
    """
    rejection_id = _rejection_identity(
        reason=reason,
        source=source,
        source_event_type=_bound_identifier(source_event_type),
        market_id=_bound_identifier(market_id),
        condition_id=_bound_identifier(condition_id),
        token_id=_bound_identifier(token_id),
        raw_payload_sha256=provenance.raw_sha256,
    )
    return RejectedObservationV1(
        rejection_id=rejection_id,
        reason=reason,
        detail=detail,
        source=source,
        source_event_type=source_event_type,
        market_id=market_id,
        condition_id=condition_id,
        token_id=token_id,
        raw_payload_sha256=provenance.raw_sha256,
        provenance=provenance,
        received_time=received_time,
        rejected_at=rejected_at,
        capture_run_id=capture_run_id,
    )


def read_payload[PayloadT: VersionedModel](
    envelope: ObservationEnvelopeV1, model: type[PayloadT]
) -> PayloadT:
    """Validate ``envelope.payload`` into a caller-supplied typed model.

    Refuses when ``payload_schema_version`` does not name ``model``'s own
    schema version: reading one payload type's fields out of another's data
    would be exactly the silent coercion ``.claude/rules/data-integrity.md``
    forbids, even though both are "just a mapping" underneath.
    """
    if envelope.payload_schema_version != model.schema_version:
        raise SchemaVersionError(
            "envelope payload_schema_version does not match the requested model",
            found=envelope.payload_schema_version,
            supported=[model.schema_version],
        )
    return model.model_validate(thaw(envelope.payload))


def read_declared_payload(envelope: ObservationEnvelopeV1) -> VersionedModel:
    """Validate ``envelope.payload`` into whichever model its own version names.

    The counterpart to :func:`read_payload` for a reader that does *not* know
    the payload type in advance — an M3 replay dispatcher over a capture holding
    several payload kinds, or any future tool that walks a stored capture. It
    goes through :func:`argos.domain.versioning.resolve_schema`, so the model is
    looked up rather than selected by a chain of string comparisons, and so the
    lookup is backed by the uniqueness guarantee that makes it meaningful: two
    classes cannot both claim one version.

    Resolving is not the same as accepting. This returns the typed payload; a
    caller that only knows how to handle certain kinds must still check what it
    got and refuse the rest deliberately, with a count. Dispatching on whatever
    a stored record claims to be is exactly the silent coercion
    ``.claude/rules/data-integrity.md`` forbids.
    """
    model = resolve_schema(envelope.payload_schema_version)
    return read_payload(envelope, model)


def _observation_identity(
    *,
    source: ObservationSource,
    source_event_type: str,
    market_id: str | None,
    condition_id: str | None,
    token_id: str | None,
    payload_schema_version: str,
    payload_canonical_json: str,
    event_time_status: EventTimeStatus,
    event_time: datetime | None,
    event_time_raw: str | None,
    source_sequence: str | None,
    source_hash: str | None,
) -> str:
    """Derive a deterministic identity from source-stable, rule-bearing content.

    What is IN the identity: *every* stable discriminator the message carries —
    the source, its own message-type label, whichever market/condition/token it
    names, the payload's declared type, the event-time marker, the source's own
    sequence and content hash when it supplies them, and a canonical hash of the
    normalized payload. Cross-checking against ``docs/02_ARCHITECTURE.md``,
    "Persistence model": "Unique deterministic event ID where source fields
    permit it; otherwise content hash plus source key."

    They are combined, not ranked. An earlier draft of this function preferred
    the source hash, then the source sequence, and only fell back to payload
    content — which reads as "trust the strongest identifier available" but
    silently discards evidence in two reproduced cases:

    1. *State reversion.* A book that moves A → B → A carries the source's own
       content hash ``hash(A)`` on both the first and third poll, so the third
       observation collided with the first and would be refused as a duplicate.
       The fact that the book was back at A at 12:00:09 is a real observation,
       and erasing it corrupts every downstream time series.
    2. *Sequence reset.* A source sequence that restarts at 1 after a reconnect
       — which the M2 exit criteria explicitly anticipate — made two genuinely
       different events share one identity, so one was silently dropped.

    Both are the silent loss core invariant 14 forbids. Combining the
    discriminators closes both while keeping the M2 criterion intact: the
    criterion is about the *same delivery* arriving twice (a websocket resend
    after reconnect, a reprocessed frame), and every field above is identical in
    that case, so it still collides.

    The cost is deliberate and accepted: polling an unchanged book at a new
    source timestamp now yields a new observation rather than a duplicate. That
    is the honest reading — it is a distinct observation that the book was
    unchanged at that instant. Collapsing it is a projection's job, not an
    identity's.

    What is NOT in the identity, deliberately: ``ingest_sequence`` and
    ``received_time`` (both differ on every redelivery of the identical source
    event by construction, so including either would make a duplicate
    unable to collide — the M2 exit criterion would be unreachable) and
    ``capture_run_id`` (a second capture session observing the same live
    event is still the same event; scoping identity to the run would silently
    admit its bytes twice into the store, and "Duplicate insert is idempotent"
    in the persistence model is not qualified per run).

    M1 precedent (``argos.compiler.contract._contract_id``): deriving an
    identity from the *enclosing* payload rather than the entity's own content
    gave one market two identities depending on which endpoint returned it.
    The same failure mode here would be deriving ``observation_id`` from
    ``raw_payload_sha256`` — a REST snapshot response can bundle many
    observations behind one raw payload, and a websocket frame can too.

    Failure mode this derivation does NOT close: two source messages that are
    genuinely different events but share every field above — same scope, no
    source sequence or hash, identical normalized payload, and event times that
    are equal or both absent — remain indistinguishable and collide. That is
    irreducible without information the source did not send, and a source whose
    payload carries its own timestamp does not reach it. Recorded here rather
    than discovered silently later.
    """
    material = (
        source.value,
        source_event_type,
        market_id,
        condition_id,
        token_id,
        payload_schema_version,
        _event_time_marker(event_time_status, event_time, event_time_raw),
        source_sequence,
        source_hash,
        payload_canonical_json,
    )
    return f"observation-{_digest(material)[:32]}"


def _digest(material: tuple[str | None, ...]) -> str:
    """Hash an ordered field list under an injective encoding.

    Joining the fields with a separator — any separator — is not injective when
    the fields themselves are source-controlled. Reproduced against the first
    draft of this module, which joined on ``\\x1f``:

    ``source_event_type="book\\x1fmarket-1"`` with no ``market_id`` produced
    byte-identical material to ``source_event_type="book"`` with
    ``market_id="market-1\\x1f"``, so two genuinely different observations
    collided and an idempotent insert would drop one. Every one of
    ``market_id``, ``condition_id``, ``token_id``, ``source_sequence`` and
    ``source_hash`` is a free-form source-controlled string, so this was
    reachable from a hostile or merely eccentric payload.

    Length-prefixing each component removes the ambiguity: a decoder could walk
    the encoding back to exactly one field list. Absence is encoded as a marker
    that no present value can produce, because the same draft let
    ``source_sequence="absent"`` forge the identity of ``source_sequence=None``.
    """
    parts: list[str] = []
    for value in material:
        if value is None:
            parts.append("-")
        else:
            parts.append(f"{len(value)}:{value}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def recompute_observation_id(envelope: ObservationEnvelopeV1) -> str:
    """Re-derive ``observation_id`` from the envelope's own stored fields.

    An identity nobody can recompute is an identity nobody can audit. Because
    the derivation runs on the *validated* field values — the sanitized,
    bounded, UTC-anchored ones the record actually stores — this returns the
    stored ``observation_id`` for any envelope built by
    :func:`build_observation_envelope`, and a store or a reviewer can verify
    that claim without trusting the writer.
    """
    return _observation_identity(
        source=envelope.source,
        source_event_type=envelope.source_event_type,
        market_id=envelope.market_id,
        condition_id=envelope.condition_id,
        token_id=envelope.token_id,
        payload_schema_version=envelope.payload_schema_version,
        payload_canonical_json=_canonical_json(thaw(envelope.payload)),
        event_time_status=envelope.event_time_status,
        event_time=envelope.event_time,
        event_time_raw=envelope.event_time_raw,
        source_sequence=envelope.source_sequence,
        source_hash=envelope.source_hash,
    )


def _canonical_payload(payload: VersionedModel) -> dict[str, Any]:
    """Return the payload's JSON-canonical mapping, without its schema version.

    The version travels in the envelope's own ``payload_schema_version`` field;
    duplicating it inside the payload would create two copies that can disagree.
    """
    try:
        record = payload.to_record()
    except (TypeError, ValueError, RecursionError) as error:
        # pydantic raises a bare ValueError("Circular reference detected") past
        # nesting depth 254, and freeze/thaw raise RecursionError. Both are
        # trivially reachable from hostile JSON and both escaped the taxonomy, so
        # a capture loop would die on one instead of counting it.
        raise ContractViolationError(
            "normalized payload could not be serialized canonically",
            payload_schema_version=type(payload).schema_version,
            error_type=type(error).__name__,
        ) from error
    record.pop(SCHEMA_VERSION_KEY, None)
    return record


def _bound_identifier(value: str | None) -> str | None:
    """Apply the same neutralization the ledger record stores, so the identity is
    recomputable from the stored value rather than from the builder's argument."""
    return None if value is None else neutralize_identifier_and_bound(value, MAX_IDENTIFIER_LENGTH)


def recompute_rejection_id(rejection: RejectedObservationV1) -> str:
    """Re-derive ``rejection_id`` from the record's own stored fields.

    The observation identity has had this since review; the ledger — the record
    built from data ARGOS could *not* validate — had no equivalent, which is the
    wrong way round for the two.
    """
    return _rejection_identity(
        reason=rejection.reason,
        source=rejection.source,
        source_event_type=rejection.source_event_type,
        market_id=rejection.market_id,
        condition_id=rejection.condition_id,
        token_id=rejection.token_id,
        raw_payload_sha256=rejection.raw_payload_sha256,
    )


def _rejection_identity(
    *,
    reason: RejectionReason,
    source: ObservationSource,
    source_event_type: str | None,
    market_id: str | None,
    condition_id: str | None,
    token_id: str | None,
    raw_payload_sha256: str,
) -> str:
    """Derive a rejection identity from the same kind of stable content.

    ``capture_run_id`` is excluded for the same reason it is excluded from
    :func:`_observation_identity`: the same invalid bytes rejected for the
    same reason in two capture sessions are the same rejection, not two.
    """
    material = (
        reason.value,
        source.value,
        source_event_type,
        market_id,
        condition_id,
        token_id,
        raw_payload_sha256,
    )
    return f"rejection-{_digest(material)[:32]}"


def _event_time_marker(
    status: EventTimeStatus, event_time: datetime | None, event_time_raw: str | None
) -> str:
    if status is EventTimeStatus.PRESENT:
        assert event_time is not None  # enforced by _validate_event_time_shape at model level
        return f"present:{ensure_utc(event_time).isoformat()}"
    if status is EventTimeStatus.MISSING:
        return "missing"
    return f"unparseable:{event_time_raw or ''}"


def _canonical_json(value: Any) -> str:
    """Deterministic JSON for content hashing: sorted keys, no incidental whitespace.

    The input is always a payload already dumped in JSON mode by
    :func:`_canonical_payload`, so every value is JSON-native and every mapping
    key is a string. A failure here therefore means a caller bypassed the
    builder, which is a contract violation and not a datum: it is raised inside
    the ARGOS taxonomy rather than as a bare ``TypeError``, so a capture loop
    can count it instead of dying on it. M1 precedent: ``normalize_markets``
    raised a bare ``TypeError`` on a scalar body.
    """
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ContractViolationError(
            "observation payload is not canonically JSON-serializable",
            error_type=type(error).__name__,
        ) from error
