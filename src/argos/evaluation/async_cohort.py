"""Offline, versioned declaration for the first asynchronous M4 cohort.

This module has no clock, network, or storage access. A live owner must persist
the protocol and its receipt before selecting the first target.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from itertools import pairwise
from typing import Any, ClassVar
from urllib.parse import urlsplit

import orjson
from pydantic import Field, field_validator, model_validator

from argos.clock import ensure_utc
from argos.compiler import compile_market_contract
from argos.domain.market import MarketDefinitionV1
from argos.domain.provenance import SHA256_LENGTH, sha256_hex
from argos.domain.versioning import VersionedModel, ensure_supported_version
from argos.evaluation.prospective import (
    CutoffBasis,
    ProspectiveExperimentProtocolV1,
)
from argos.ingestion.gamma_markets import normalize_markets

MAX_MARKET_ID_DIGITS = 128


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
        if self.selection.target_end_min <= self.observation_window_end:
            raise ValueError(
                "target end-time lower bound must follow the cohort observation window"
            )
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


class CandidateExclusionReason(StrEnum):
    NORMALIZATION_REJECTED = "normalization_rejected"
    RAW_ENTRY_MISSING = "raw_entry_missing"
    EVENT_ID_MISSING = "event_id_missing"
    MARKET_ID_INVALID = "market_id_invalid"
    NORMALIZED_AFTER_SELECTION = "normalized_after_selection"
    NOT_ACTIVE = "not_active"
    CLOSED = "closed"
    ARCHIVED = "archived"
    NOT_BINARY_YES_NO = "not_binary_yes_no"
    END_TIME_OUT_OF_RANGE = "end_time_out_of_range"
    LIQUIDITY_BELOW_MINIMUM = "liquidity_below_minimum"
    ORDER_BOOK_UNAVAILABLE = "order_book_unavailable"
    MALFORMED_PRICES = "malformed_prices"
    PRICE_OUT_OF_RANGE = "price_out_of_range"
    EVENT_ID_REUSED = "event_id_reused"
    RANK_BELOW_CUTOFF = "rank_below_cutoff"
    BLOCK_SHORTFALL = "block_shortfall"
    CONTRACT_INVALID = "contract_invalid"


class CandidateExclusionV1(VersionedModel):
    """One accounted discovery entry; detail is source data, never an instruction."""

    schema_version: ClassVar[str] = "m4_candidate_exclusion.v1"

    market_id: str | None
    reason: CandidateExclusionReason
    detail: str | None = None


class BlockSelectionStatus(StrEnum):
    ADMITTED = "admitted"
    REJECTED_SHORT_BLOCK = "rejected_short_block"


class OfflineBlockSelectionV1(VersionedModel):
    """Complete partition of a normalized discovery page, not target evidence."""

    schema_version: ClassVar[str] = "m4_offline_block_selection.v1"

    experiment_id: str = Field(min_length=1)
    block_ordinal: int = Field(ge=1)
    selected_at: datetime
    status: BlockSelectionStatus
    protocol_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    predecessor_selection_sha256: str | None = Field(
        default=None, min_length=SHA256_LENGTH, max_length=SHA256_LENGTH
    )
    source_payload_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    source_retrieved_at: datetime
    source_candidate_count: int = Field(ge=0)
    selected_markets: tuple[MarketDefinitionV1, ...]
    exclusions: tuple[CandidateExclusionV1, ...]

    @field_validator("selected_at", "source_retrieved_at")
    @classmethod
    def _utc_selection(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _partition_is_complete(self) -> OfflineBlockSelectionV1:
        if self.source_retrieved_at > self.selected_at:
            raise ValueError("discovery source cannot be retrieved after block selection")
        if (self.block_ordinal == 1) != (self.predecessor_selection_sha256 is None):
            raise ValueError("offline selection predecessor digest must follow block ordinal")
        if len(self.selected_markets) + len(self.exclusions) != self.source_candidate_count:
            raise ValueError("offline selection must account for every discovery entry")
        if self.status is BlockSelectionStatus.ADMITTED and len(self.selected_markets) != 4:
            raise ValueError("admitted block needs exactly four selected markets")
        if self.status is BlockSelectionStatus.ADMITTED:
            market_ids = [market.market_id for market in self.selected_markets]
            event_ids = [market.event_id for market in self.selected_markets]
            if any(
                market.raw_payload_sha256 != self.source_payload_sha256
                for market in self.selected_markets
            ):
                raise ValueError("admitted markets must match the recorded discovery source digest")
            if any(not _canonical_market_id(market_id) for market_id in market_ids) or len(
                set(market_ids)
            ) != len(market_ids):
                raise ValueError("admitted markets need distinct canonical numeric IDs")
            if any(not event_id or not event_id.strip() for event_id in event_ids) or len(
                {_event_identity(event_id) for event_id in event_ids if event_id is not None}
            ) != len(event_ids):
                raise ValueError("admitted markets need distinct explicit event identities")
        if self.status is BlockSelectionStatus.REJECTED_SHORT_BLOCK and self.selected_markets:
            raise ValueError("rejected block cannot contain admitted targets")
        return self

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        record["selected_markets"] = [market.to_record() for market in self.selected_markets]
        record["exclusions"] = [exclusion.to_record() for exclusion in self.exclusions]
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> OfflineBlockSelectionV1:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["selected_markets"] = tuple(
            MarketDefinitionV1.from_record(dict(item)) for item in payload["selected_markets"]
        )
        payload["exclusions"] = tuple(
            CandidateExclusionV1.from_record(dict(item)) for item in payload["exclusions"]
        )
        return cls.model_validate(payload)


def validate_block_admission(
    protocol: AsynchronousCohortProtocolV1,
    *,
    block_ordinal: int,
    selected_at: datetime,
    event_ids: tuple[str, ...],
    prior_block_selections: tuple[OfflineBlockSelectionV1, ...],
) -> None:
    """Reject a partial/replacement block before any forecast is made.

    The caller must persist the public source bytes, contract and target receipts
    separately. This pure guard cannot establish their availability by itself.
    """
    block, earlier_event_ids, _ = _validate_block_readiness(
        protocol,
        block_ordinal=block_ordinal,
        selected_at=selected_at,
        prior_block_selections=prior_block_selections,
    )
    if len(event_ids) != block.intended_targets:
        raise ValueError("short cohort block must be rejected before forecast")
    if any(not event_id.strip() for event_id in event_ids):
        raise ValueError("cohort targets need explicit Gamma event identities")
    current_identities = {_event_identity(event_id) for event_id in event_ids}
    if len(current_identities) != len(event_ids) or current_identities & earlier_event_ids:
        raise ValueError("cohort target event identity was selected more than once")


def _validate_block_readiness(
    protocol: AsynchronousCohortProtocolV1,
    *,
    block_ordinal: int,
    selected_at: datetime,
    prior_block_selections: tuple[OfflineBlockSelectionV1, ...],
) -> tuple[CohortBlockV1, frozenset[str], str | None]:
    if not 1 <= block_ordinal <= len(protocol.blocks):
        raise ValueError("unknown cohort block")
    block = protocol.blocks[block_ordinal - 1]
    if len(prior_block_selections) != block_ordinal - 1:
        raise ValueError("cohort blocks must proceed in order without missing predecessors")
    protocol_digest = sha256_hex(orjson.dumps(protocol.to_record(), option=orjson.OPT_SORT_KEYS))
    earlier_event_ids: set[str] = set()
    predecessor_digest: str | None = None
    for ordinal, prior in enumerate(prior_block_selections, start=1):
        earlier_block = protocol.blocks[ordinal - 1]
        if (
            prior.experiment_id != protocol.experiment_id
            or prior.protocol_sha256 != protocol_digest
            or prior.block_ordinal != ordinal
            or prior.status is not BlockSelectionStatus.ADMITTED
            or prior.predecessor_selection_sha256 != predecessor_digest
            or not earlier_block.start <= prior.source_retrieved_at <= prior.selected_at
            or not prior.selected_at < earlier_block.end
            or (earlier_block.end - prior.selected_at).total_seconds()
            < earlier_block.intended_targets * protocol.capture_max_seconds_per_target
        ):
            raise ValueError("prior cohort blocks must form an admitted, hash-linked chain")
        for market in prior.selected_markets:
            assert market.event_id is not None
            identity = _event_identity(market.event_id)
            if identity in earlier_event_ids:
                raise ValueError("prior cohort target event identity was selected more than once")
            earlier_event_ids.add(identity)
        predecessor_digest = sha256_hex(
            orjson.dumps(prior.to_record(), option=orjson.OPT_SORT_KEYS)
        )
    at = ensure_utc(selected_at)
    if not block.start <= at < block.end:
        raise ValueError("target selection falls outside frozen block")
    remaining = block.end - at
    needed = timedelta(seconds=block.intended_targets * protocol.capture_max_seconds_per_target)
    if remaining < needed:
        raise ValueError("insufficient time remains for bounded captures in this block")
    return block, frozenset(earlier_event_ids), predecessor_digest


def select_block_candidates(
    protocol: AsynchronousCohortProtocolV1,
    *,
    block_ordinal: int,
    selected_at: datetime,
    source_payload_bytes: bytes,
    source_retrieved_at: datetime,
    prior_block_selections: tuple[OfflineBlockSelectionV1, ...],
) -> OfflineBlockSelectionV1:
    """Rank an archived Gamma page; reject the whole block on shortfall.

    This does not fetch, persist, capture or forecast. The future live adapter
    must archive the exact raw page and its retrieval before calling it.
    """
    at = ensure_utc(selected_at)
    retrieved_at = ensure_utc(source_retrieved_at)
    if retrieved_at > at:
        raise ValueError("discovery source cannot be retrieved after block selection")
    block, earlier_event_ids, predecessor_digest = _validate_block_readiness(
        protocol,
        block_ordinal=block_ordinal,
        selected_at=at,
        prior_block_selections=prior_block_selections,
    )
    if retrieved_at < block.start:
        raise ValueError("discovery source must be retrieved inside the selected block")
    try:
        page = orjson.loads(source_payload_bytes)
    except orjson.JSONDecodeError as error:
        raise ValueError("discovery source is not valid JSON") from error
    if not isinstance(page, list):
        raise ValueError("discovery source must be a market page")
    raw_ids = [
        str(entry["id"])
        for entry in page
        if isinstance(entry, dict)
        and isinstance(entry.get("id"), str | int)
        and not isinstance(entry["id"], bool)
    ]
    numeric_ids = [
        market_id.lstrip("0") or "0" for market_id in raw_ids if _bounded_digits(market_id)
    ]
    duplicates = sorted(market_id for market_id, count in Counter(numeric_ids).items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate market IDs in discovery page: {duplicates}")
    source_payload_sha256 = sha256_hex(source_payload_bytes)
    normalization = normalize_markets(
        page, raw_payload_sha256=source_payload_sha256, normalized_at=retrieved_at
    )
    raw_by_market_id: dict[str, dict[str, Any]] = {}
    for entry in page:
        if isinstance(entry, dict):
            market_id = entry.get("id")
            if isinstance(market_id, str | int) and not isinstance(market_id, bool):
                raw_by_market_id[str(market_id)] = entry
    selection = protocol.selection
    exclusions = [
        CandidateExclusionV1(
            market_id=item.market_id,
            reason=CandidateExclusionReason.NORMALIZATION_REJECTED,
            detail=f"{item.reason.value}: {item.detail}",
        )
        for item in normalization.quarantined
    ]
    eligible: list[MarketDefinitionV1] = []
    for market in normalization.accepted:
        raw = raw_by_market_id.get(market.market_id)
        reason = _candidate_rejection(market, raw, selection, selected_at=at)
        if reason is not None:
            exclusions.append(CandidateExclusionV1(market_id=market.market_id, reason=reason))
            continue
        try:
            compile_market_contract(market, compiled_at=at)
        except ValueError as error:
            exclusions.append(
                CandidateExclusionV1(
                    market_id=market.market_id,
                    reason=CandidateExclusionReason.CONTRACT_INVALID,
                    detail=str(error),
                )
            )
            continue
        eligible.append(market)
    eligible.sort(key=lambda market: (-(market.liquidity or Decimal(0)), int(market.market_id)))
    chosen: list[MarketDefinitionV1] = []
    seen = set(earlier_event_ids)
    required = block.intended_targets
    for market in eligible:
        assert market.event_id is not None
        identity = _event_identity(market.event_id)
        if identity in seen:
            exclusions.append(
                CandidateExclusionV1(
                    market_id=market.market_id, reason=CandidateExclusionReason.EVENT_ID_REUSED
                )
            )
            continue
        if len(chosen) == required:
            exclusions.append(
                CandidateExclusionV1(
                    market_id=market.market_id, reason=CandidateExclusionReason.RANK_BELOW_CUTOFF
                )
            )
            continue
        chosen.append(market)
        seen.add(identity)
    if len(chosen) < required:
        exclusions.extend(
            CandidateExclusionV1(
                market_id=market.market_id,
                reason=CandidateExclusionReason.BLOCK_SHORTFALL,
            )
            for market in chosen
        )
        chosen = []
        status = BlockSelectionStatus.REJECTED_SHORT_BLOCK
    else:
        validate_block_admission(
            protocol,
            block_ordinal=block_ordinal,
            selected_at=at,
            event_ids=tuple(market.event_id or "" for market in chosen),
            prior_block_selections=prior_block_selections,
        )
        status = BlockSelectionStatus.ADMITTED
    return OfflineBlockSelectionV1(
        experiment_id=protocol.experiment_id,
        block_ordinal=block_ordinal,
        selected_at=at,
        status=status,
        protocol_sha256=sha256_hex(orjson.dumps(protocol.to_record(), option=orjson.OPT_SORT_KEYS)),
        predecessor_selection_sha256=predecessor_digest,
        source_payload_sha256=source_payload_sha256,
        source_retrieved_at=retrieved_at,
        source_candidate_count=len(page),
        selected_markets=tuple(chosen),
        exclusions=tuple(exclusions),
    )


def _candidate_rejection(
    market: MarketDefinitionV1,
    raw: dict[str, Any] | None,
    selection: GammaSelectionV1,
    *,
    selected_at: datetime,
) -> CandidateExclusionReason | None:
    if raw is None:
        return CandidateExclusionReason.RAW_ENTRY_MISSING
    if not market.event_id or not market.event_id.strip():
        return CandidateExclusionReason.EVENT_ID_MISSING
    if not _canonical_market_id(market.market_id):
        return CandidateExclusionReason.MARKET_ID_INVALID
    if market.normalized_at > selected_at:
        return CandidateExclusionReason.NORMALIZED_AFTER_SELECTION
    if not market.active:
        return CandidateExclusionReason.NOT_ACTIVE
    if market.closed:
        return CandidateExclusionReason.CLOSED
    if market.archived:
        return CandidateExclusionReason.ARCHIVED
    if market.outcomes != ("Yes", "No"):
        return CandidateExclusionReason.NOT_BINARY_YES_NO
    if market.end_time is None or not (
        selection.target_end_min <= market.end_time <= selection.target_end_max
    ):
        return CandidateExclusionReason.END_TIME_OUT_OF_RANGE
    if (market.liquidity or Decimal(0)) < selection.minimum_liquidity:
        return CandidateExclusionReason.LIQUIDITY_BELOW_MINIMUM
    if raw.get("enableOrderBook") is not True or raw.get("acceptingOrders") is not True:
        return CandidateExclusionReason.ORDER_BOOK_UNAVAILABLE
    prices = _outcome_prices(raw.get("outcomePrices"))
    if prices is None or len(prices) != 2:
        return CandidateExclusionReason.MALFORMED_PRICES
    if not all(
        selection.minimum_outcome_price <= price <= selection.maximum_outcome_price
        for price in prices
    ):
        return CandidateExclusionReason.PRICE_OUT_OF_RANGE
    return None


def _bounded_digits(market_id: str) -> bool:
    return (
        0 < len(market_id) <= MAX_MARKET_ID_DIGITS and market_id.isascii() and market_id.isdecimal()
    )


def _canonical_market_id(market_id: str) -> bool:
    return _bounded_digits(market_id) and (len(market_id) == 1 or market_id[0] != "0")


def _event_identity(event_id: str) -> str:
    if event_id.isascii() and event_id.isdecimal():
        return f"numeric:{event_id.lstrip('0') or '0'}"
    return f"text:{event_id}"


def _outcome_prices(raw: Any) -> tuple[Decimal, ...] | None:
    try:
        values = orjson.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(values, list):
            return None
        prices = tuple(Decimal(str(value)) for value in values)
        return prices if all(price.is_finite() for price in prices) else None
    except (orjson.JSONDecodeError, InvalidOperation, ValueError, TypeError):
        return None
