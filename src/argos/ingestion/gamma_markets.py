"""Normalize Gamma market payloads into ``MarketDefinitionV1``.

Two properties matter more than convenience here:

* **Determinism.** The same raw payload must always produce the same record, so
  nothing in this module reads a clock, a random source, or global state — the
  timestamp is passed in.
* **No silent coercion.** Gamma encodes ``outcomes``, ``clobTokenIds``, and
  ``outcomePrices`` as JSON *strings*, and ``resolutionSource`` is frequently
  empty. A payload this module cannot read becomes a
  :class:`~argos.domain.market.QuarantinedMarketV1` with a reason, never a
  half-filled record and never a skip.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import orjson

from argos.clock import ensure_utc
from argos.domain.market import MarketDefinitionV1, QuarantinedMarketV1
from argos.errors import IngestionError, RejectionReason

NORMALIZER_VERSION = "gamma-market-normalizer/1"


@dataclass(frozen=True)
class NormalizationReport:
    """The outcome of normalizing a page of markets.

    Accepted and quarantined records together account for every input: a caller
    can assert ``len(accepted) + len(quarantined) == len(payloads)``.
    """

    accepted: tuple[MarketDefinitionV1, ...] = ()
    quarantined: tuple[QuarantinedMarketV1, ...] = ()

    @property
    def total(self) -> int:
        return len(self.accepted) + len(self.quarantined)

    def counts_by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.quarantined:
            counts[record.reason.value] = counts.get(record.reason.value, 0) + 1
        return counts


@dataclass
class _Accumulator:
    accepted: list[MarketDefinitionV1] = field(default_factory=list)
    quarantined: list[QuarantinedMarketV1] = field(default_factory=list)


def normalize_markets(
    payloads: Iterable[Any],
    *,
    raw_payload_sha256: str,
    normalized_at: datetime,
) -> NormalizationReport:
    """Normalize a page of Gamma markets, quarantining the ones that do not parse."""
    accumulator = _Accumulator()
    for payload in payloads:
        try:
            accumulator.accepted.append(
                normalize_market(
                    payload,
                    raw_payload_sha256=raw_payload_sha256,
                    normalized_at=normalized_at,
                )
            )
        except IngestionError as error:
            accumulator.quarantined.append(
                _quarantine(payload, error, raw_payload_sha256, normalized_at)
            )
    return NormalizationReport(
        accepted=tuple(accumulator.accepted),
        quarantined=tuple(accumulator.quarantined),
    )


def normalize_market(
    payload: Any,
    *,
    raw_payload_sha256: str,
    normalized_at: datetime,
) -> MarketDefinitionV1:
    """Normalize one Gamma market payload.

    Raises :class:`~argos.errors.IngestionError` with a
    :class:`~argos.errors.RejectionReason` when the payload cannot be represented
    faithfully.
    """
    if not isinstance(payload, dict):
        raise IngestionError(
            f"expected a market object, got {type(payload).__name__}",
            reason=RejectionReason.MALFORMED_PAYLOAD,
        )

    market_id = _require_text(payload, "id")
    outcomes = tuple(_decode_string_list(payload, "outcomes"))
    token_ids = tuple(_decode_string_list(payload, "clobTokenIds"))

    if len(outcomes) != len(token_ids):
        raise IngestionError(
            f"{len(outcomes)} outcomes but {len(token_ids)} token ids",
            reason=RejectionReason.QUARANTINED_MAPPING,
            market_id=market_id,
            outcomes=list(outcomes),
        )
    if not outcomes:
        raise IngestionError(
            "market declares no outcomes",
            reason=RejectionReason.QUARANTINED_MAPPING,
            market_id=market_id,
        )

    try:
        return MarketDefinitionV1(
            market_id=market_id,
            condition_id=_require_text(payload, "conditionId"),
            slug=_require_text(payload, "slug"),
            event_id=_first_event_id(payload),
            question=_require_text(payload, "question"),
            description=_optional_text(payload, "description") or "",
            resolution_source=_optional_text(payload, "resolutionSource") or "",
            start_time=_optional_time(payload, "startDate", market_id),
            end_time=_optional_time(payload, "endDate", market_id),
            active=_require_flag(payload, "active"),
            closed=_require_flag(payload, "closed"),
            archived=_require_flag(payload, "archived"),
            restricted=_optional_flag(payload, "restricted"),
            category=_optional_text(payload, "category"),
            liquidity=_optional_decimal(payload, "liquidity", market_id),
            volume=_optional_decimal(payload, "volume", market_id),
            open_interest=_optional_decimal(payload, "openInterest", market_id),
            outcomes=outcomes,
            outcome_token_map=dict(zip(outcomes, token_ids, strict=True)),
            neg_risk=_optional_flag(payload, "negRisk"),
            tick_size=_optional_decimal(payload, "orderPriceMinTickSize", market_id),
            source_updated_time=_optional_time(payload, "updatedAt", market_id),
            raw_payload_sha256=raw_payload_sha256,
            normalized_at=normalized_at,
            normalizer_version=NORMALIZER_VERSION,
        )
    except ValueError as error:
        # A contract violation on a mapping is a quarantine case, not a crash: the
        # rest of the page must still normalize.
        raise IngestionError(
            str(error),
            reason=RejectionReason.QUARANTINED_MAPPING,
            market_id=market_id,
        ) from error


def _quarantine(
    payload: Any,
    error: IngestionError,
    raw_payload_sha256: str,
    normalized_at: datetime,
) -> QuarantinedMarketV1:
    identifiers = payload if isinstance(payload, dict) else {}
    return QuarantinedMarketV1(
        market_id=_optional_text(identifiers, "id"),
        slug=_optional_text(identifiers, "slug"),
        reason=error.reason,
        detail=error.message,
        raw_payload_sha256=raw_payload_sha256,
        quarantined_at=ensure_utc(normalized_at),
        normalizer_version=NORMALIZER_VERSION,
    )


# --- field readers ------------------------------------------------------------------


def _require_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    raise IngestionError(
        f"missing or empty required field {key!r}",
        reason=RejectionReason.MALFORMED_PAYLOAD,
        field=key,
    )


def _optional_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value != "" else None


def _require_flag(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    raise IngestionError(
        f"required boolean field {key!r} is {value!r}",
        reason=RejectionReason.MALFORMED_PAYLOAD,
        field=key,
    )


def _optional_flag(payload: dict[str, Any], key: str) -> bool | None:
    value = payload.get(key)
    return value if isinstance(value, bool) else None


def _optional_decimal(payload: dict[str, Any], key: str, market_id: str) -> Decimal | None:
    """Read a money-like field as ``Decimal``, via ``str`` so no float noise enters."""
    value = payload.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise IngestionError(
            f"field {key!r} is a boolean where a number was expected",
            reason=RejectionReason.MALFORMED_PAYLOAD,
            market_id=market_id,
            field=key,
        )
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise IngestionError(
            f"field {key!r} is not a number: {value!r}",
            reason=RejectionReason.MALFORMED_PAYLOAD,
            market_id=market_id,
            field=key,
        ) from error


def _optional_time(payload: dict[str, Any], key: str, market_id: str) -> datetime | None:
    """Parse a source timestamp, refusing to substitute the current time for a bad one.

    ``.claude/rules/data-integrity.md``: never replace an invalid source timestamp
    with current time silently.
    """
    value = payload.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise IngestionError(
            f"timestamp field {key!r} is {type(value).__name__}, expected a string",
            reason=RejectionReason.INVALID_TIMESTAMP,
            market_id=market_id,
            field=key,
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise IngestionError(
            f"timestamp field {key!r} is not ISO 8601: {value!r}",
            reason=RejectionReason.INVALID_TIMESTAMP,
            market_id=market_id,
            field=key,
        ) from error
    if parsed.tzinfo is None:
        raise IngestionError(
            f"timestamp field {key!r} carries no timezone: {value!r}",
            reason=RejectionReason.INVALID_TIMESTAMP,
            market_id=market_id,
            field=key,
        )
    return ensure_utc(parsed)


def _decode_string_list(payload: dict[str, Any], key: str) -> Sequence[str]:
    """Read a field Gamma may deliver as a list *or* as a JSON-encoded string."""
    value = payload.get(key)
    if isinstance(value, str):
        try:
            value = orjson.loads(value)
        except orjson.JSONDecodeError as error:
            raise IngestionError(
                f"field {key!r} is a string but not JSON: {value!r}",
                reason=RejectionReason.MALFORMED_PAYLOAD,
                field=key,
            ) from error
    if value is None:
        raise IngestionError(
            f"missing required field {key!r}",
            reason=RejectionReason.QUARANTINED_MAPPING,
            field=key,
        )
    if not isinstance(value, list):
        raise IngestionError(
            f"field {key!r} decoded to {type(value).__name__}, expected a list",
            reason=RejectionReason.MALFORMED_PAYLOAD,
            field=key,
        )
    decoded: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise IngestionError(
                f"field {key!r} contains a non-string entry: {item!r}",
                reason=RejectionReason.MALFORMED_PAYLOAD,
                field=key,
            )
        decoded.append(item)
    return decoded


def _first_event_id(payload: dict[str, Any]) -> str | None:
    events = payload.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        return _optional_text(events[0], "id")
    return None
