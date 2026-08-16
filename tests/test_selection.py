"""Market selection policy.

Selection decides the research sample, so the property that matters is not
"the right markets came through" — it is that nothing left without a reason.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from argos.domain.market import MarketDefinitionV1
from argos.domain.selection import (
    ExclusionReason,
    MarketSelectionPolicy,
    select_markets,
)
from argos.ingestion import normalize_market

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gamma"
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
DIGEST = "b" * 64


def _raw(**overrides: Any) -> dict[str, Any]:
    page = json.loads((FIXTURES / "markets_list.raw.json").read_text(encoding="utf-8"))
    payload = deepcopy(page[0])
    payload.update(
        {
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["111", "222"]',
            "active": True,
            "closed": False,
            "archived": False,
            "endDate": (NOW + timedelta(days=30)).isoformat().replace("+00:00", "Z"),
        }
    )
    payload.update(overrides)
    return payload


def _market(**overrides: Any) -> MarketDefinitionV1:
    return normalize_market(_raw(**overrides), raw_payload_sha256=DIGEST, normalized_at=NOW)


def test_a_standard_binary_market_is_selected() -> None:
    result = select_markets([_market()], MarketSelectionPolicy(), as_of=NOW)
    assert len(result.selected) == 1
    assert result.excluded == ()


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"outcomes": '["A", "B", "C"]', "clobTokenIds": '["1", "2", "3"]'},
            ExclusionReason.NOT_BINARY,
        ),
        (
            {"outcomes": '["Up", "Down"]', "clobTokenIds": '["1", "2"]'},
            ExclusionReason.NONSTANDARD_OUTCOMES,
        ),
        ({"active": False}, ExclusionReason.NOT_ACTIVE),
        ({"closed": True}, ExclusionReason.CLOSED),
        ({"archived": True}, ExclusionReason.ARCHIVED),
        ({"endDate": None}, ExclusionReason.NO_END_TIME),
        ({"endDate": "2026-01-01T00:00:00Z"}, ExclusionReason.ALREADY_ENDED),
    ],
)
def test_each_clause_excludes_with_its_own_reason(
    overrides: dict[str, Any], reason: ExclusionReason
) -> None:
    result = select_markets([_market(**overrides)], MarketSelectionPolicy(), as_of=NOW)
    assert result.selected == ()
    assert result.excluded[0].reason is reason
    assert result.excluded[0].detail


def test_thin_markets_can_be_excluded_by_liquidity() -> None:
    policy = MarketSelectionPolicy(min_liquidity=Decimal("10000"))
    result = select_markets([_market(liquidity="500")], policy, as_of=NOW)
    assert result.excluded[0].reason is ExclusionReason.BELOW_MIN_LIQUIDITY


def test_a_missing_liquidity_is_treated_as_zero_not_as_unknown_pass() -> None:
    """Absent evidence must not sneak a market past a liquidity floor."""
    policy = MarketSelectionPolicy(min_liquidity=Decimal("1"))
    result = select_markets([_market(liquidity=None)], policy, as_of=NOW)
    assert result.excluded[0].reason is ExclusionReason.BELOW_MIN_LIQUIDITY


def test_markets_ending_too_soon_can_be_excluded() -> None:
    policy = MarketSelectionPolicy(min_hours_to_end=48)
    soon = (NOW + timedelta(hours=5)).isoformat().replace("+00:00", "Z")
    result = select_markets([_market(endDate=soon)], policy, as_of=NOW)
    assert result.excluded[0].reason is ExclusionReason.ENDS_TOO_SOON


def test_policy_clauses_can_be_relaxed() -> None:
    permissive = MarketSelectionPolicy(
        require_binary=False,
        require_standard_outcome_labels=False,
        require_active=False,
        exclude_closed=False,
        exclude_archived=False,
        require_end_time=False,
    )
    market = _market(
        outcomes='["A", "B", "C"]',
        clobTokenIds='["1", "2", "3"]',
        active=False,
        closed=True,
        endDate=None,
    )
    assert len(select_markets([market], permissive).selected) == 1


def test_every_market_is_accounted_for() -> None:
    markets = [_market(id="1"), _market(id="2", closed=True), _market(id="3", active=False)]
    result = select_markets(markets, MarketSelectionPolicy(), as_of=NOW)
    assert result.total == 3
    assert result.counts_by_reason() == {"closed": 1, "not_active": 1}


def test_the_reported_reason_is_deterministic_when_several_clauses_fail() -> None:
    """A market failing three clauses must always report the same first one."""
    market = _market(active=False, closed=True, archived=True)
    reasons = {
        select_markets([market], MarketSelectionPolicy(), as_of=NOW).excluded[0].reason
        for _ in range(5)
    }
    assert reasons == {ExclusionReason.NOT_ACTIVE}


def test_selection_preserves_input_order() -> None:
    markets = [_market(id=str(index)) for index in range(5)]
    result = select_markets(markets, MarketSelectionPolicy(), as_of=NOW)
    assert [m.market_id for m in result.selected] == ["0", "1", "2", "3", "4"]


def test_time_clauses_are_skipped_without_an_as_of() -> None:
    """The policy never invents a clock; time filters simply do not apply."""
    ended = _market(endDate="2026-01-01T00:00:00Z")
    assert len(select_markets([ended], MarketSelectionPolicy()).selected) == 1


def test_an_empty_input_is_an_empty_result_not_an_error() -> None:
    result = select_markets([], MarketSelectionPolicy(), as_of=NOW)
    assert result.total == 0
    assert result.counts_by_reason() == {}


# --- boundaries ---------------------------------------------------------------------


def test_a_market_exactly_at_the_liquidity_floor_is_kept() -> None:
    """`min_liquidity` is a floor, not a strict threshold."""
    policy = MarketSelectionPolicy(min_liquidity=Decimal("500"))
    assert select_markets([_market(liquidity="500")], policy, as_of=NOW).selected


def test_a_market_one_unit_below_the_liquidity_floor_is_excluded() -> None:
    policy = MarketSelectionPolicy(min_liquidity=Decimal("500"))
    result = select_markets([_market(liquidity="499.999999999")], policy, as_of=NOW)
    assert result.excluded[0].reason is ExclusionReason.BELOW_MIN_LIQUIDITY


def test_a_liquidity_floor_is_compared_exactly_not_through_a_float() -> None:
    """0.1 + 0.2 != 0.3 in binary; a market at the floor must not be lost to that."""
    policy = MarketSelectionPolicy(min_liquidity=Decimal("0.3"))
    assert select_markets([_market(liquidity="0.30")], policy, as_of=NOW).selected


def test_a_market_ending_exactly_at_as_of_has_already_ended() -> None:
    ends_now = NOW.isoformat().replace("+00:00", "Z")
    result = select_markets([_market(endDate=ends_now)], MarketSelectionPolicy(), as_of=NOW)
    assert result.excluded[0].reason is ExclusionReason.ALREADY_ENDED


def test_a_market_ending_one_microsecond_after_as_of_is_still_live() -> None:
    later = (NOW + timedelta(microseconds=1)).isoformat().replace("+00:00", "Z")
    assert select_markets([_market(endDate=later)], MarketSelectionPolicy(), as_of=NOW).selected


def test_a_market_at_exactly_min_hours_to_end_is_kept() -> None:
    policy = MarketSelectionPolicy(min_hours_to_end=48)
    exactly = (NOW + timedelta(hours=48)).isoformat().replace("+00:00", "Z")
    assert select_markets([_market(endDate=exactly)], policy, as_of=NOW).selected


def test_a_zero_hour_floor_still_excludes_a_market_that_has_ended() -> None:
    """`min_hours_to_end=0` is a real configuration, not a synonym for `None`."""
    policy = MarketSelectionPolicy(min_hours_to_end=0)
    past = (NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    result = select_markets([_market(endDate=past)], policy, as_of=NOW)
    assert result.excluded[0].reason is ExclusionReason.ALREADY_ENDED


def test_a_negative_hour_floor_is_rejected_by_the_policy_itself() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError
        MarketSelectionPolicy(min_hours_to_end=-1)


# --- accounting ---------------------------------------------------------------------


def test_selection_is_idempotent_and_free_of_shared_state() -> None:
    """Core invariant: no hidden global state may change output with processing order."""
    markets = [_market(id="1"), _market(id="2", closed=True), _market(id="3")]
    policy = MarketSelectionPolicy()
    first = select_markets(markets, policy, as_of=NOW)
    second = select_markets(markets, policy, as_of=NOW)
    third = select_markets(list(reversed(markets)), policy, as_of=NOW)
    assert first == second
    assert first.counts_by_reason() == third.counts_by_reason()
    assert {m.market_id for m in first.selected} == {m.market_id for m in third.selected}


def test_a_duplicate_market_is_selected_twice_rather_than_deduplicated() -> None:
    """M1 has no dedup; the accounting must still add up so M2 can see the duplicate."""
    duplicate = _market(id="7")
    result = select_markets([duplicate, duplicate], MarketSelectionPolicy(), as_of=NOW)
    assert result.total == 2
    assert len(result.selected) == 2


def test_every_exclusion_names_a_market_that_was_offered() -> None:
    markets = [_market(id=str(index), closed=index % 2 == 0) for index in range(6)]
    result = select_markets(markets, MarketSelectionPolicy(), as_of=NOW)
    offered = {market.market_id for market in markets}
    assert {exclusion.market_id for exclusion in result.excluded} <= offered
    assert {market.market_id for market in result.selected} <= offered
    assert result.total == len(markets)


def test_the_policy_records_every_clause_including_the_inactive_ones() -> None:
    """A sample is uninterpretable without the policy that produced it.

    Replaces a `describe()` summary that was unused in `src/` and dropped
    `min_hours_to_end=0` and `min_liquidity=Decimal(0)`, both of which compare
    equal to `False`. The full dump is what discovery records, and it cannot
    silently omit a clause that was applied.
    """
    recorded = MarketSelectionPolicy(min_liquidity=Decimal("0")).model_dump(mode="json")
    assert set(recorded) == set(MarketSelectionPolicy.model_fields)
    assert recorded["min_liquidity"] == "0", "a zero floor is a declared clause, not an absence"
    assert recorded["min_volume"] is None
    assert recorded["exclude_ended"] is True


def test_the_policy_is_immutable_and_serializable() -> None:
    policy = MarketSelectionPolicy(min_liquidity=Decimal("100"))
    with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError
        policy.require_binary = False  # type: ignore[misc]
    assert policy.model_dump(mode="json")["min_liquidity"] == "100"
