"""The market-rule contract compiler and the human-reviewable audit.

The compiler's job is to be honest about what the source does not say. These
tests are mostly about what it refuses to do.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from argos.compiler import (
    AmbiguityFlag,
    CompiledMarketContractV1,
    MarketAuditV1,
    ReviewStatus,
    build_market_audit,
    compile_market_contract,
    render_market_audit,
)
from argos.domain.market import MarketDefinitionV1
from argos.domain.selection import MarketSelectionPolicy
from argos.ingestion import normalize_market

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gamma"
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
DIGEST = "c" * 64


def _raw(**overrides: Any) -> dict[str, Any]:
    page = json.loads((FIXTURES / "markets_list.raw.json").read_text(encoding="utf-8"))
    payload = deepcopy(page[0])
    payload.update(
        {
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["111", "222"]',
            "endDate": (NOW + timedelta(days=30)).isoformat().replace("+00:00", "Z"),
        }
    )
    payload.update(overrides)
    return payload


def _market(**overrides: Any) -> MarketDefinitionV1:
    return normalize_market(_raw(**overrides), raw_payload_sha256=DIGEST, normalized_at=NOW)


def _contract(**overrides: Any) -> CompiledMarketContractV1:
    return compile_market_contract(_market(**overrides), compiled_at=NOW)


# --- what the compiler preserves ------------------------------------------------------


def test_the_question_is_carried_verbatim() -> None:
    market = _market()
    assert _contract().proposition == market.question


def test_the_rule_text_is_carried_verbatim() -> None:
    """Core invariant 3: the rule material is evidence, not something to summarize."""
    market = _market()
    assert _contract().source_rule_material == market.description


def test_the_contract_links_back_to_the_raw_payload() -> None:
    contract = _contract()
    assert contract.source_market_hash == DIGEST
    assert contract.condition_id == _market().condition_id


def test_compilation_is_deterministic() -> None:
    assert _contract().to_record() == _contract().to_record()


def test_the_contract_id_is_derived_not_generated() -> None:
    """Recompiling the same evidence must not invent a new identity."""
    assert _contract().contract_id == _contract().contract_id
    assert _contract(id="other").contract_id != _contract().contract_id


def test_the_contract_round_trips_through_storage() -> None:
    contract = _contract()
    assert CompiledMarketContractV1.from_record(contract.to_record()) == contract


# --- what the compiler refuses to invent ----------------------------------------------


def test_the_yes_and_no_conditions_are_never_guessed() -> None:
    """Turning prose into conditions is semantic work, gated behind owner review."""
    contract = _contract()
    assert contract.yes_condition is None
    assert contract.no_condition is None
    assert AmbiguityFlag.CONDITIONS_NOT_EXTRACTED in contract.ambiguity_flags


def test_edge_cases_and_clarifications_stay_empty() -> None:
    contract = _contract()
    assert contract.edge_cases == ()
    assert contract.clarifications == ()


def test_the_compiler_never_marks_a_contract_human_reviewed() -> None:
    """M1 exit criterion, and core invariant 15."""
    assert _contract().review_status is not ReviewStatus.HUMAN_REVIEWED
    assert _contract(resolutionSource="Official results").review_status is not (
        ReviewStatus.HUMAN_REVIEWED
    )


def test_human_reviewed_cannot_even_be_constructed_by_code() -> None:
    contract = _contract()
    with pytest.raises(ValidationError):
        CompiledMarketContractV1(
            **{**contract.model_dump(), "review_status": ReviewStatus.HUMAN_REVIEWED}
        )


# --- ambiguity ------------------------------------------------------------------------


def test_a_missing_resolution_source_is_flagged() -> None:
    """Gamma very often publishes an empty resolutionSource."""
    contract = _contract(resolutionSource="")
    assert AmbiguityFlag.RESOLUTION_SOURCE_MISSING in contract.ambiguity_flags


def test_a_missing_description_is_flagged() -> None:
    assert AmbiguityFlag.DESCRIPTION_MISSING in _contract(description="").ambiguity_flags


def test_a_missing_end_boundary_is_flagged() -> None:
    assert AmbiguityFlag.END_BOUNDARY_MISSING in _contract(endDate=None).ambiguity_flags


def test_an_end_before_its_start_is_flagged() -> None:
    contract = _contract(
        startDate="2026-09-01T00:00:00Z",
        endDate="2026-08-01T00:00:00Z",
    )
    assert AmbiguityFlag.END_BEFORE_START in contract.ambiguity_flags


def test_non_binary_outcomes_are_flagged() -> None:
    contract = _contract(outcomes='["A", "B", "C"]', clobTokenIds='["1", "2", "3"]')
    assert AmbiguityFlag.NON_BINARY_OUTCOMES in contract.ambiguity_flags


def test_nonstandard_binary_labels_are_flagged() -> None:
    contract = _contract(outcomes='["Up", "Down"]', clobTokenIds='["1", "2"]')
    assert AmbiguityFlag.NONSTANDARD_OUTCOME_LABELS in contract.ambiguity_flags


def test_the_ambiguity_score_is_a_count_not_a_probability() -> None:
    contract = _contract(resolutionSource="", description="", endDate=None)
    assert contract.ambiguity_score == len(contract.ambiguity_flags)
    assert contract.ambiguity_score > 1, "a score above 1 cannot be mistaken for a probability"


def test_flags_are_reported_in_a_stable_order() -> None:
    first = _contract(resolutionSource="", description="", endDate=None).ambiguity_flags
    second = _contract(resolutionSource="", description="", endDate=None).ambiguity_flags
    assert first == second


def test_a_more_ambiguous_market_scores_higher() -> None:
    clear = _contract(resolutionSource="Official election results")
    murky = _contract(resolutionSource="", description="", endDate=None)
    assert murky.ambiguity_score > clear.ambiguity_score


# --- the audit ------------------------------------------------------------------------


def _audit(**overrides: Any) -> MarketAuditV1:
    market = _market(**overrides)
    return build_market_audit(
        market,
        compile_market_contract(market, compiled_at=NOW),
        policy=MarketSelectionPolicy(),
        audited_at=NOW,
    )


def test_an_audit_reports_scope_and_capture_readiness() -> None:
    audit = _audit(active=True, closed=False, archived=False)
    assert audit.in_scope
    assert audit.capture_ready
    assert audit.capture_blockers == ()


def test_a_closed_market_is_not_capture_ready_and_says_why() -> None:
    audit = _audit(closed=True)
    assert not audit.capture_ready
    assert any("closed" in blocker for blocker in audit.capture_blockers)
    assert not audit.in_scope
    assert audit.out_of_scope_reason is not None


def test_a_market_without_an_end_time_cannot_be_verified_later() -> None:
    audit = _audit(endDate=None)
    assert any("end time" in blocker for blocker in audit.capture_blockers)


def test_the_audit_record_round_trips() -> None:
    audit = _audit()
    assert MarketAuditV1.from_record(audit.to_record()) == audit


def test_the_rendered_audit_is_reviewable_markdown() -> None:
    rendered = render_market_audit(_audit())
    market = _market()
    assert rendered.startswith("# Market audit")
    assert market.question in rendered
    assert market.condition_id in rendered
    for outcome in market.outcomes:
        assert f"**{outcome}**" in rendered
        assert market.token_id_for(outcome) in rendered


def test_the_rendered_audit_makes_no_probability_claim() -> None:
    """The audit describes evidence. Nothing in it may read as a forecast."""
    rendered = render_market_audit(_audit()).lower()
    assert "no probability claim" in rendered
    for forbidden in ("p_yes", "predicted", "our estimate", "likelihood of yes"):
        assert forbidden not in rendered


def test_the_rendered_audit_labels_the_score_as_a_score() -> None:
    rendered = render_market_audit(_audit())
    assert "not a probability" in rendered


def test_the_rendered_audit_says_the_conditions_are_deliberately_empty() -> None:
    assert "intentionally empty" in render_market_audit(_audit())


def test_a_market_with_no_rules_at_all_is_flagged_as_unverifiable() -> None:
    audit = _audit(resolutionSource="", description="")
    assert any("unverifiable" in blocker for blocker in audit.capture_blockers)
