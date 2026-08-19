"""Normalizing a Gamma market payload into a resolution, or refusing to.

The M4 exit criterion this module exists for is **"unresolved markets are not
scored as negatives"**, and the obvious implementation breaks it silently.
"`closed == true` means resolved, read the winner out of `outcomePrices`" is
wrong on the majority of closed markets, measured rather than suspected
(``docs/research/m4-gamma-resolution.md``):

| `outcomePrices` shape | Oldest-first, n = 900 | Most recently ended, n = 500 |
|---|---|---|
| exactly 1/0 | 0.4% | 100% |
| fractional, summing to 1 | 93.2% | 0 |
| ``["0","0"]`` | 5.1% | 0 |

The fractional ones are last prices. Market 40 is *"Will Trump win the 2020 U.S.
presidential election"*, closed, at `0.0000000436` / `0.9999999` — a question
whose real-world outcome is not in doubt and whose payload does not encode it.
An evaluator that read those as resolutions would score 93% of an id-ordered
sample against a price.

So this module refuses far more than it accepts, and every refusal is a
**counted, reasoned outcome** rather than an exception (core invariant 14): the
caller gets a :class:`ResolutionRefusal` naming which shape it saw, and a
report can say how many markets were unusable and why.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import Field, field_validator, model_validator

from argos.clock import ensure_utc
from argos.domain.provenance import SHA256_LENGTH
from argos.domain.text import neutralize_and_bound
from argos.domain.versioning import VersionedModel

__all__ = [
    "GAMMA_RESOLUTION_NORMALIZER_VERSION",
    "ResolutionRefusal",
    "ResolutionRefusalReason",
    "ResolutionStatus",
    "ResolutionV1",
    "WinningOutcome",
    "normalize_gamma_resolution",
]

GAMMA_RESOLUTION_NORMALIZER_VERSION = "gamma-resolution-normalizer/2"

_MAX_TEXT = 200


class WinningOutcome(StrEnum):
    """Which side of a binary market won.

    Only two members, and no ``UNKNOWN``: a resolution record exists **only**
    when the outcome is determined. "Undetermined" is the absence of one of
    these records, reported as a :class:`ResolutionRefusal`, so a scoring loop
    cannot accidentally iterate over an ``UNKNOWN`` outcome and treat it as a
    negative.
    """

    YES = "yes"
    NO = "no"


class ResolutionStatus(StrEnum):
    """How settled the resolution is, from the source's own UMA trail.

    ``docs/04_DATA_CONTRACTS.md`` specifies ``proposed | disputed | final |
    unknown``. The observed vocabulary is ``proposed | disputed | resolved``,
    so ``resolved`` maps to ``final`` and the mapping is recorded here rather
    than left for a reader to infer.

    ``UNKNOWN`` is a real, frequent state and not a parse failure: 8 of 500
    recently-closed markets carry an exact 1/0 outcome with **no UMA trail at
    all**. The outcome is determined and the status is not recorded, which is
    exactly why the two are separate fields.
    """

    PROPOSED = "proposed"
    DISPUTED = "disputed"
    FINAL = "final"
    UNKNOWN = "unknown"


class ResolutionRefusalReason(StrEnum):
    """Why a payload did not yield a resolution. Every value is observed."""

    NOT_CLOSED = "not_closed"
    """The market is still open. Necessary but far from sufficient on its own."""

    PRICES_ARE_NOT_A_RESOLUTION = "prices_are_not_a_resolution"
    """``outcomePrices`` are fractional — a last price, not a settlement.
    93.2% of an id-ordered closed sample."""

    NO_DETERMINABLE_OUTCOME = "no_determinable_outcome"
    """``outcomePrices`` are ``["0","0"]``: closed with nothing determinable.
    5.1% of the same sample."""

    NOT_A_BINARY_MARKET = "not_a_binary_market"
    """The payload does not carry exactly two outcomes."""

    MALFORMED_PAYLOAD = "malformed_payload"
    """A required field is missing, or is not the shape the source documents."""


@dataclass(frozen=True, slots=True)
class ResolutionRefusal:
    """A payload that carries no usable resolution, and why.

    Returned rather than raised. A normalization sweep over a thousand markets
    meets these constantly — they are the *common* case — and an exception per
    market would turn the ordinary shape of this data into control flow.
    """

    reason: ResolutionRefusalReason
    detail: str
    market_id: str | None = None
    condition_id: str | None = None


class ResolutionV1(VersionedModel):
    """A determined binary outcome for one market."""

    schema_version: ClassVar[str] = "resolution.v1"

    resolution_id: str = Field(min_length=1)
    market_id: str = Field(min_length=1)
    condition_id: str = Field(min_length=1)

    resolved_at: datetime | None = None
    """When the source last updated the market, which is the closest thing the
    payload offers to a settlement time. ``None`` when absent rather than
    filled in from the clock — the same rule
    ``.claude/rules/data-integrity.md`` applies to every other timestamp."""

    winning_outcome: WinningOutcome
    winning_token_id: str | None = Field(default=None, min_length=1)

    resolution_source: str = ""
    """The source's own ``resolutionSource``, verbatim and neutralized. Often
    empty on this source, which is recorded as empty rather than as absent."""

    source_payload_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    clarification_present: bool = False
    resolution_status: ResolutionStatus
    normalizer_version: str = Field(min_length=1)
    normalized_at: datetime

    @field_validator("resolution_source")
    @classmethod
    def _sanitize(cls, value: str) -> str:
        return neutralize_and_bound(value, _MAX_TEXT)

    @field_validator("resolved_at", "normalized_at")
    @classmethod
    def _anchor(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @model_validator(mode="after")
    def _a_final_status_is_never_assumed(self) -> ResolutionV1:
        """Guard the direction that matters.

        An unknown status is safe: it says less than the truth. A `final` status
        invented for a market whose trail did not say so would be a stronger
        claim than the source made, on the record that decides whether a
        forecast gets scored.
        """
        if self.resolution_status is ResolutionStatus.FINAL and not self.winning_token_id:
            raise ValueError(
                "a final resolution must name the winning token; without it the "
                "settlement cannot be checked against the market's own token map"
            )
        return self


def normalize_gamma_resolution(
    payload: Mapping[str, Any],
    *,
    source_payload_sha256: str,
    normalized_at: datetime,
) -> ResolutionV1 | ResolutionRefusal:
    """Turn one Gamma market payload into a resolution, or say why not.

    Accepts **only** an exact ``{1, 0}`` pair in ``outcomePrices``. Everything
    else is refused with a reason, including shapes that look nearly resolved:
    `0.0000000436 / 0.9999999` is a price that rounds to a resolution and is not
    one, and rounding it would be ARGOS deciding an outcome the source did not
    state.
    """
    market_id = _text(payload.get("id"))
    condition_id = _text(payload.get("conditionId"))

    if not market_id or not condition_id:
        return ResolutionRefusal(
            reason=ResolutionRefusalReason.MALFORMED_PAYLOAD,
            detail="payload carries no usable 'id' or 'conditionId'",
            market_id=market_id,
            condition_id=condition_id,
        )
    if payload.get("closed") is not True:
        return ResolutionRefusal(
            reason=ResolutionRefusalReason.NOT_CLOSED,
            detail=f"market is not closed (closed={payload.get('closed')!r})",
            market_id=market_id,
            condition_id=condition_id,
        )

    prices = _decimal_list(payload.get("outcomePrices"))
    if prices is None:
        return ResolutionRefusal(
            reason=ResolutionRefusalReason.MALFORMED_PAYLOAD,
            detail="outcomePrices is absent or is not a JSON array of decimal strings",
            market_id=market_id,
            condition_id=condition_id,
        )
    if len(prices) != 2:
        return ResolutionRefusal(
            reason=ResolutionRefusalReason.NOT_A_BINARY_MARKET,
            detail=f"outcomePrices carries {len(prices)} outcomes, not 2",
            market_id=market_id,
            condition_id=condition_id,
        )

    yes_price, no_price = prices
    if yes_price == 0 and no_price == 0:
        return ResolutionRefusal(
            reason=ResolutionRefusalReason.NO_DETERMINABLE_OUTCOME,
            detail='outcomePrices are ["0","0"]: closed with no determinable outcome',
            market_id=market_id,
            condition_id=condition_id,
        )
    if {yes_price, no_price} != {Decimal(1), Decimal(0)}:
        return ResolutionRefusal(
            reason=ResolutionRefusalReason.PRICES_ARE_NOT_A_RESOLUTION,
            detail=(
                f"outcomePrices are {yes_price}/{no_price}, a price rather than a "
                "settlement; only an exact 1/0 pair determines an outcome"
            ),
            market_id=market_id,
            condition_id=condition_id,
        )

    winner = WinningOutcome.YES if yes_price == 1 else WinningOutcome.NO
    tokens = _string_list(payload.get("clobTokenIds")) or []
    winning_token = None
    if len(tokens) == 2:
        winning_token = tokens[0] if winner is WinningOutcome.YES else tokens[1]

    status = _status(payload.get("umaResolutionStatuses"))
    if status is ResolutionStatus.FINAL and not winning_token:
        # A `final` record must name its winning token (see the model validator).
        # Rather than raise, degrade the claim: the outcome is still determined,
        # and the payload simply did not carry a usable token map.
        status = ResolutionStatus.UNKNOWN

    return ResolutionV1(
        resolution_id=_resolution_id(
            condition_id=condition_id,
            source_payload_sha256=source_payload_sha256,
            winning_outcome=winner,
            winning_token_id=winning_token,
            resolution_status=status,
        ),
        market_id=market_id,
        condition_id=condition_id,
        resolved_at=_timestamp(payload.get("updatedAt")),
        winning_outcome=winner,
        winning_token_id=winning_token,
        resolution_source=_text(payload.get("resolutionSource")) or "",
        source_payload_sha256=source_payload_sha256,
        clarification_present=bool(_text(payload.get("clarification"))),
        resolution_status=status,
        normalizer_version=GAMMA_RESOLUTION_NORMALIZER_VERSION,
        normalized_at=normalized_at,
    )


def _status(raw: Any) -> ResolutionStatus:
    """Read the *last* element of the UMA trail, which is the current status.

    The field is a history, not a status: a disputed market carries
    ``["proposed","disputed","proposed","resolved"]``, and reading its first
    element would report a settled market as merely proposed. An unrecognized
    value degrades to ``UNKNOWN`` rather than being guessed at — nothing
    establishes that the three observed values are the whole vocabulary, and
    the safe direction is to claim less.
    """
    statuses = _string_list(raw)
    if not statuses:
        return ResolutionStatus.UNKNOWN
    latest = statuses[-1].strip().lower()
    if latest == "resolved":
        return ResolutionStatus.FINAL
    if latest == "proposed":
        return ResolutionStatus.PROPOSED
    if latest == "disputed":
        return ResolutionStatus.DISPUTED
    return ResolutionStatus.UNKNOWN


def _resolution_id(
    *,
    condition_id: str,
    source_payload_sha256: str,
    winning_outcome: WinningOutcome,
    winning_token_id: str | None,
    resolution_status: ResolutionStatus,
) -> str:
    parts = (
        "gamma_resolution_identity.v2",
        condition_id,
        source_payload_sha256,
        winning_outcome.value,
        winning_token_id or "",
        resolution_status.value,
    )
    encoded = "|".join(f"{len(part)}:{part}" for part in parts)
    return f"resolution-{hashlib.sha256(encoded.encode()).hexdigest()[:32]}"


def _decimal_list(raw: Any) -> list[Decimal] | None:
    values = _string_list(raw)
    if values is None:
        return None
    try:
        return [Decimal(value) for value in values]
    except (InvalidOperation, ValueError):
        return None


def _string_list(raw: Any) -> list[str] | None:
    """Gamma encodes these arrays as JSON *strings*, not as JSON arrays.

    Both shapes are accepted because the encoding is the source's choice and
    could change, and a normalizer that only understood one would turn a source
    formatting change into a total refusal storm — the same reasoning that keeps
    a format off `hash` elsewhere in this repository.
    """
    if isinstance(raw, list):
        return (
            [item for item in raw if isinstance(item, str)]
            if all(isinstance(item, str) for item in raw)
            else None
        )
    if not isinstance(raw, str):
        return None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        return None
    return list(decoded)


def _text(raw: Any) -> str | None:
    return raw if isinstance(raw, str) and raw else None


def _timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None
