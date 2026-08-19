"""Typed auxiliary evidence for a standalone CLOB last_trade_price event.

The schema is pinned to the first byte-for-byte real sample captured by the
prospective M4 pilot on 2026-08-19. A trade is not a resting order and must
therefore never participate in book_state_digest or state_hash.v1.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar, Final

from pydantic import Field, field_validator

from argos.domain.market import CONDITION_ID_PATTERN, TOKEN_ID_PATTERN
from argos.domain.orderbook import MAX_PRICE, MIN_PRICE, parse_wire_decimal
from argos.domain.versioning import VersionedModel

EXPECTED_EVENT_KEYS: Final = frozenset(
    {
        "market",
        "asset_id",
        "price",
        "size",
        "fee_rate_bps",
        "side",
        "timestamp",
        "event_type",
        "transaction_hash",
    }
)
TRANSACTION_HASH_PATTERN: Final = re.compile(r"0x[0-9a-f]{64}")


class TradeSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class LastTradePriceV1(VersionedModel):
    """One executed trade reported by the public CLOB market channel."""

    schema_version: ClassVar[str] = "last_trade_price.v1"

    condition_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    price: Decimal = Field(ge=MIN_PRICE, le=MAX_PRICE)
    size: Decimal = Field(gt=0)
    fee_rate_bps: Decimal = Field(ge=0)
    side: TradeSide
    transaction_hash: str = Field(min_length=66, max_length=66)

    @field_validator("condition_id")
    @classmethod
    def _validate_condition_id(cls, value: str) -> str:
        lowered = value.lower()
        if not CONDITION_ID_PATTERN.fullmatch(lowered):
            raise ValueError(f"condition_id must be a 0x-prefixed 32-byte hash, got {value!r}")
        return lowered

    @field_validator("asset_id")
    @classmethod
    def _validate_asset_id(cls, value: str) -> str:
        if not TOKEN_ID_PATTERN.fullmatch(value):
            raise ValueError(f"asset_id must be a decimal token id string, got {value!r}")
        return value

    @field_validator("price", "size", "fee_rate_bps", mode="before")
    @classmethod
    def _parse_decimal(cls, value: Any) -> Decimal:
        return parse_wire_decimal(value)

    @field_validator("transaction_hash")
    @classmethod
    def _validate_transaction_hash(cls, value: str) -> str:
        lowered = value.lower()
        if not TRANSACTION_HASH_PATTERN.fullmatch(lowered):
            raise ValueError(
                f"transaction_hash must be a 0x-prefixed 32-byte hexadecimal hash, got {value!r}"
            )
        return lowered


def parse_last_trade_price(event: Mapping[str, Any]) -> LastTradePriceV1:
    """Parse exactly the observed standalone wire shape, refusing schema drift."""
    if not isinstance(event, Mapping):
        raise ValueError(
            f"last_trade_price event must be a JSON object, got {type(event).__name__}"
        )
    event_type = event.get("event_type")
    if event_type != "last_trade_price":
        raise ValueError(f"expected event_type 'last_trade_price', got {event_type!r}")
    missing = EXPECTED_EVENT_KEYS - set(event)
    if missing:
        raise ValueError(f"last_trade_price event is missing fields: {sorted(missing)}")
    unexpected = set(event) - EXPECTED_EVENT_KEYS
    if unexpected:
        raise ValueError(
            f"last_trade_price event has unexpected top-level fields: {sorted(unexpected)}"
        )
    return LastTradePriceV1(
        condition_id=event["market"],
        asset_id=event["asset_id"],
        price=event["price"],
        size=event["size"],
        fee_rate_bps=event["fee_rate_bps"],
        side=event["side"],
        transaction_hash=event["transaction_hash"],
    )
