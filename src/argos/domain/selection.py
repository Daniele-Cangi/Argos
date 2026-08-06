"""Which markets ARGOS is willing to study.

Selection is policy, not truth: a market excluded here is not defective, it is
outside the configured research scope. Every exclusion is recorded with a reason
so a discovery run can explain its own sample — a silently narrowed sample is how
an evaluation ends up measuring the filter instead of the forecast.

The policy is pure and deterministic: no clock, no network, no global state, and
the same inputs always produce the same ordering.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from argos.domain.market import MarketDefinitionV1

BINARY_OUTCOME_LABELS: frozenset[str] = frozenset({"Yes", "No"})


class ExclusionReason(StrEnum):
    """Why a market fell outside the configured scope."""

    NOT_BINARY = "not_binary"
    NONSTANDARD_OUTCOMES = "nonstandard_outcomes"
    NOT_ACTIVE = "not_active"
    CLOSED = "closed"
    ARCHIVED = "archived"
    BELOW_MIN_LIQUIDITY = "below_min_liquidity"
    BELOW_MIN_VOLUME = "below_min_volume"
    NO_END_TIME = "no_end_time"
    ENDS_TOO_SOON = "ends_too_soon"
    ALREADY_ENDED = "already_ended"


class MarketSelectionPolicy(BaseModel):
    """The configured scope of a discovery run.

    Recorded alongside results: core invariant 13 makes configuration part of the
    experiment, and the selection policy is the most consequential configuration
    a research run has.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    require_binary: bool = True
    require_standard_outcome_labels: bool = True
    require_active: bool = True
    exclude_closed: bool = True
    exclude_archived: bool = True

    min_liquidity: Decimal | None = None
    min_volume: Decimal | None = None

    require_end_time: bool = True
    min_hours_to_end: int | None = Field(default=None, ge=0)

    def describe(self) -> str:
        """Return a one-line human summary for report headers."""
        clauses = [
            name for name, value in sorted(self.model_dump().items()) if value not in (None, False)
        ]
        return ", ".join(clauses) or "no constraints"


class MarketExclusion(BaseModel):
    """One market, one reason it is out of scope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    market_id: str
    slug: str
    reason: ExclusionReason
    detail: str = ""


class SelectionResult(BaseModel):
    """Selected markets plus a full account of what was left out."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    selected: tuple[MarketDefinitionV1, ...] = ()
    excluded: tuple[MarketExclusion, ...] = ()

    considered: ClassVar[str] = "selected + excluded"

    @property
    def total(self) -> int:
        return len(self.selected) + len(self.excluded)

    def counts_by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for exclusion in self.excluded:
            counts[exclusion.reason.value] = counts.get(exclusion.reason.value, 0) + 1
        return counts


def select_markets(
    markets: Sequence[MarketDefinitionV1],
    policy: MarketSelectionPolicy,
    *,
    as_of: datetime | None = None,
) -> SelectionResult:
    """Apply ``policy`` to ``markets``.

    ``as_of`` is required only by the time-based clauses, and is supplied by the
    caller rather than read from a clock so the result stays reproducible.
    """
    selected: list[MarketDefinitionV1] = []
    excluded: list[MarketExclusion] = []

    for market in markets:
        reason = _first_failure(market, policy, as_of)
        if reason is None:
            selected.append(market)
        else:
            excluded.append(
                MarketExclusion(
                    market_id=market.market_id,
                    slug=market.slug,
                    reason=reason[0],
                    detail=reason[1],
                )
            )
    return SelectionResult(selected=tuple(selected), excluded=tuple(excluded))


def _first_failure(
    market: MarketDefinitionV1,
    policy: MarketSelectionPolicy,
    as_of: datetime | None,
) -> tuple[ExclusionReason, str] | None:
    """Return the first clause the market fails, so the reason is deterministic."""
    if policy.require_binary and not market.is_binary:
        return ExclusionReason.NOT_BINARY, f"{len(market.outcomes)} outcomes"
    if policy.require_standard_outcome_labels and set(market.outcomes) != BINARY_OUTCOME_LABELS:
        return ExclusionReason.NONSTANDARD_OUTCOMES, f"outcomes {list(market.outcomes)}"
    if policy.require_active and not market.active:
        return ExclusionReason.NOT_ACTIVE, "market is not active"
    if policy.exclude_closed and market.closed:
        return ExclusionReason.CLOSED, "market is closed"
    if policy.exclude_archived and market.archived:
        return ExclusionReason.ARCHIVED, "market is archived"

    if policy.min_liquidity is not None:
        liquidity = market.liquidity if market.liquidity is not None else Decimal(0)
        if liquidity < policy.min_liquidity:
            return ExclusionReason.BELOW_MIN_LIQUIDITY, f"liquidity {liquidity}"
    if policy.min_volume is not None:
        volume = market.volume if market.volume is not None else Decimal(0)
        if volume < policy.min_volume:
            return ExclusionReason.BELOW_MIN_VOLUME, f"volume {volume}"

    if policy.require_end_time and market.end_time is None:
        return ExclusionReason.NO_END_TIME, "market declares no end time"

    if as_of is not None and market.end_time is not None:
        if market.end_time <= as_of:
            return ExclusionReason.ALREADY_ENDED, f"ended at {market.end_time.isoformat()}"
        if policy.min_hours_to_end is not None:
            remaining_hours = (market.end_time - as_of).total_seconds() / 3600
            if remaining_hours < policy.min_hours_to_end:
                return (
                    ExclusionReason.ENDS_TOO_SOON,
                    f"{remaining_hours:.1f}h remaining",
                )
    return None
