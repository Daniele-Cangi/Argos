"""Offline, versioned declaration for the first asynchronous M4 cohort.

This module has no clock, network, or storage access. A live owner must persist
the protocol and its receipt before selecting the first target.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from itertools import pairwise
from typing import Any, ClassVar
from urllib.parse import urlsplit

import orjson
from pydantic import Field, field_validator, model_validator

from argos.clock import ensure_utc
from argos.compiler import compile_market_contract
from argos.domain.market import MarketDefinitionV1
from argos.domain.versioning import VersionedModel, ensure_supported_version
from argos.evaluation.prospective import (
    CutoffBasis,
    ProspectiveExperimentProtocolV1,
)


class CohortBlockV1(VersionedModel):
    """One predeclared selection/capture window, not a mutable target list."""

    schema_version: ClassVar[str] = "m4_cohort_block.v1"

    ordinal: int = Field(ge=1)
    start: datetime
    end: datetime
    intended_targets: int = Field(gt=0)

    @field_validator("start", "end")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _positive_window(self) -> CohortBlockV1:
        if self.start >= self.end:
            raise ValueError("cohort block needs a positive UTC window")
        return self


class GammaSelectionV1(VersionedModel):
    """Exact public discovery query and deterministic eligibility thresholds."""

    schema_version: ClassVar[str] = "m4_gamma_selection.v1"

    base_url: str = Field(min_length=1)
    query: tuple[tuple[str, str], ...]
    minimum_liquidity: Decimal = Field(ge=0)
    minimum_outcome_price: Decimal = Field(ge=0, le=1)
    maximum_outcome_price: Decimal = Field(ge=0, le=1)
    target_end_min: datetime
    target_end_max: datetime

    @field_validator("target_end_min", "target_end_max")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("query")
    @classmethod
    def _canonical_query(cls, value: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        if not value or any(not key or not isinstance(item, str) for key, item in value):
            raise ValueError("Gamma query needs nonempty string parameters")
        if tuple(sorted(value)) != value or len({key for key, _ in value}) != len(value):
            raise ValueError("Gamma query must be sorted and contain unique keys")
        return value

    @model_validator(mode="after")
    def _selection_is_public_and_bounded(self) -> GammaSelectionV1:
        url = urlsplit(self.base_url)
        if (
            url.scheme != "https"
            or url.hostname != "gamma-api.polymarket.com"
            or url.path not in ("", "/")
            or url.port is not None
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
        ):
            raise ValueError("Gamma discovery must use the public HTTPS base URL")
        if self.minimum_outcome_price >= self.maximum_outcome_price:
            raise ValueError("outcome price bounds must be increasing")
        if self.target_end_min >= self.target_end_max:
            raise ValueError("target end-time bounds must be increasing")
        return self


class AsynchronousCohortProtocolV1(ProspectiveExperimentProtocolV1):
    """First 4x4 cohort declaration; no 48-hour finality cutoff is implied."""

    schema_version: ClassVar[str] = "m4_asynchronous_cohort_protocol.v1"

    blocks: tuple[CohortBlockV1, ...]
    selection: GammaSelectionV1
    capture_max_seconds_per_target: int = Field(gt=0, le=120)
    capture_max_frames_per_target: int = Field(gt=0, le=500)
    capture_separate_database_per_target: bool
    capture_subscribe_both_tokens: bool
    capture_raw_archive: bool
    distinct_event_identity: bool
    replacement_allowed: bool
    reject_short_block_before_forecast: bool
    pending_retained_in_denominator: bool
    operational_review_at: datetime
    initial_poll_interval_seconds: int = Field(gt=0)
    post_review_poll_interval_seconds: int = Field(gt=0)

    @field_validator("operational_review_at")
    @classmethod
    def _utc_review(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _cohort_is_coherent(self) -> AsynchronousCohortProtocolV1:
        if len(self.blocks) != 4 or any(
            block.ordinal != index or block.intended_targets != 4
            for index, block in enumerate(self.blocks, start=1)
        ):
            raise ValueError("first M4 cohort requires four ordered blocks of four targets")
        if self.blocks[0].start != self.observation_window_start or (
            self.blocks[-1].end != self.observation_window_end
        ):
            raise ValueError("observation window must exactly enclose the cohort blocks")
        if any(left.end > right.start for left, right in pairwise(self.blocks)):
            raise ValueError("cohort blocks must not overlap")
        if self.declared_at >= self.blocks[0].start:
            raise ValueError("cohort protocol must precede first selection block")
        if any(
            (block.end - block.start).total_seconds()
            < block.intended_targets * self.capture_max_seconds_per_target
            for block in self.blocks
        ):
            raise ValueError("block window cannot fit its bounded separate captures")
        if self.minimum_intended_resolved_target_count != 16:
            raise ValueError("first M4 cohort denominator must retain all 16 intended targets")
        if self.scientific_minimum_resolved_target_count < 30:
            raise ValueError("16 targets cannot lower the calibration sufficiency floor")
        if self.cutoff_basis is not CutoffBasis.FIRST_OBSERVED_FINAL_SETTLEMENT:
            raise ValueError("asynchronous finality requires first-observed-final cutoff")
        if (
            not all(
                (
                    self.capture_separate_database_per_target,
                    self.capture_subscribe_both_tokens,
                    self.capture_raw_archive,
                    self.distinct_event_identity,
                    self.reject_short_block_before_forecast,
                    self.pending_retained_in_denominator,
                )
            )
            or self.replacement_allowed
        ):
            raise ValueError("cohort capture, independence and no-replacement guards are required")
        if self.minimum_category_count_for_calibration < 2 or (
            self.minimum_yes_outcomes_for_calibration < 5
            or self.minimum_no_outcomes_for_calibration < 5
        ):
            raise ValueError("cohort cannot weaken calibration category or outcome dispersion")
        if self.operational_review_at <= self.observation_window_end:
            raise ValueError("operational review must follow the final capture block")
        if self.post_review_poll_interval_seconds < self.initial_poll_interval_seconds:
            raise ValueError("post-review cadence must not increase polling frequency")
        return self

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        record["blocks"] = [block.to_record() for block in self.blocks]
        record["selection"] = self.selection.to_record()
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> AsynchronousCohortProtocolV1:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["blocks"] = tuple(
            CohortBlockV1.from_record(dict(item)) for item in payload["blocks"]
        )
        payload["selection"] = GammaSelectionV1.from_record(dict(payload["selection"]))
        return cls.model_validate(payload)


def validate_block_admission(
    protocol: AsynchronousCohortProtocolV1,
    *,
    block_ordinal: int,
    selected_at: datetime,
    event_ids: tuple[str, ...],
    earlier_event_ids: frozenset[str],
) -> None:
    """Reject a partial/replacement block before any forecast is made.

    The caller must persist the public source bytes, contract and target receipts
    separately. This pure guard cannot establish their availability by itself.
    """
    if not 1 <= block_ordinal <= len(protocol.blocks):
        raise ValueError("unknown cohort block")
    block = protocol.blocks[block_ordinal - 1]
    expected_earlier = sum(item.intended_targets for item in protocol.blocks[: block_ordinal - 1])
    if len(earlier_event_ids) != expected_earlier:
        raise ValueError("cohort blocks must proceed in order without missing predecessors")
    at = ensure_utc(selected_at)
    if not block.start <= at < block.end:
        raise ValueError("target selection falls outside frozen block")
    if len(event_ids) != block.intended_targets:
        raise ValueError("short cohort block must be rejected before forecast")
    if any(not event_id.strip() for event_id in event_ids):
        raise ValueError("cohort targets need explicit Gamma event identities")
    if len(set(event_ids)) != len(event_ids) or set(event_ids) & earlier_event_ids:
        raise ValueError("cohort target event identity was selected more than once")


def select_block_candidates(
    protocol: AsynchronousCohortProtocolV1,
    *,
    block_ordinal: int,
    selected_at: datetime,
    markets: tuple[MarketDefinitionV1, ...],
    raw_by_market_id: dict[str, dict[str, Any]],
    earlier_event_ids: frozenset[str],
) -> tuple[MarketDefinitionV1, ...]:
    """Rank an archived Gamma page; reject the whole block on shortfall.

    This does not fetch, persist, capture or forecast. The future live adapter
    must bind the exact raw page and source retrieval before calling it.
    """
    at = ensure_utc(selected_at)
    if not 1 <= block_ordinal <= len(protocol.blocks):
        raise ValueError("unknown cohort block")
    selection = protocol.selection
    eligible: list[MarketDefinitionV1] = []
    for market in markets:
        raw = raw_by_market_id.get(market.market_id)
        if (
            raw is None
            or not market.event_id
            or not market.market_id.isdecimal()
            or market.normalized_at > at
        ):
            continue
        if not (
            market.active
            and not market.closed
            and not market.archived
            and market.outcomes == ("Yes", "No")
            and market.end_time is not None
            and selection.target_end_min <= market.end_time <= selection.target_end_max
            and (market.liquidity or Decimal(0)) >= selection.minimum_liquidity
            and raw.get("enableOrderBook") is True
            and raw.get("acceptingOrders") is True
        ):
            continue
        prices = _outcome_prices(raw.get("outcomePrices"))
        if (
            prices is None
            or len(prices) != 2
            or not all(
                selection.minimum_outcome_price <= price <= selection.maximum_outcome_price
                for price in prices
            )
        ):
            continue
        compile_market_contract(market, compiled_at=at)
        eligible.append(market)
    eligible.sort(key=lambda market: (-(market.liquidity or Decimal(0)), int(market.market_id)))
    chosen: list[MarketDefinitionV1] = []
    seen = set(earlier_event_ids)
    seen_markets: set[str] = set()
    required = protocol.blocks[block_ordinal - 1].intended_targets
    for market in eligible:
        assert market.event_id is not None
        if market.event_id in seen or market.market_id in seen_markets:
            continue
        chosen.append(market)
        seen.add(market.event_id)
        seen_markets.add(market.market_id)
        if len(chosen) == required:
            break
    validate_block_admission(
        protocol,
        block_ordinal=block_ordinal,
        selected_at=at,
        event_ids=tuple(market.event_id or "" for market in chosen),
        earlier_event_ids=earlier_event_ids,
    )
    return tuple(chosen)


def _outcome_prices(raw: Any) -> tuple[Decimal, ...] | None:
    try:
        values = orjson.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(values, list):
            return None
        prices = tuple(Decimal(str(value)) for value in values)
        return prices if all(price.is_finite() for price in prices) else None
    except (orjson.JSONDecodeError, ValueError, TypeError):
        return None
