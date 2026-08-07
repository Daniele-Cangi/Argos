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


def test_one_market_has_one_contract_id_whichever_endpoint_it_came_from() -> None:
    """`raw_payload_sha256` is the hash of the whole *page*, so deriving identity
    from it gave the same market two ids — one from /markets, one from /markets/{id}
    — and minted a new one whenever an unrelated sibling market's volume ticked."""
    page = json.loads((FIXTURES / "markets_list.raw.json").read_text(encoding="utf-8"))
    single = json.loads((FIXTURES / "market_by_id.raw.json").read_text(encoding="utf-8"))
    from_page = next(entry for entry in page if entry["id"] == single["id"])

    listed = normalize_market(from_page, raw_payload_sha256="1" * 64, normalized_at=NOW)
    fetched = normalize_market(single, raw_payload_sha256="2" * 64, normalized_at=NOW)
    assert listed.raw_payload_sha256 != fetched.raw_payload_sha256, "different pages"
    assert (
        compile_market_contract(listed, compiled_at=NOW).contract_id
        == compile_market_contract(fetched, compiled_at=NOW).contract_id
    )


def test_a_changed_rule_changes_the_contract_id() -> None:
    """Identity must still move when the material it describes moves."""
    base = _contract().contract_id
    assert _contract(description="Different rules entirely.").contract_id != base
    assert _contract(resolutionSource="A named authority").contract_id != base
    assert _contract(clobTokenIds='["999", "888"]').contract_id != base


def test_the_provenance_link_survives_even_though_identity_does_not_use_it() -> None:
    listed = normalize_market(
        json.loads((FIXTURES / "market_by_id.raw.json").read_text(encoding="utf-8")),
        raw_payload_sha256="3" * 64,
        normalized_at=NOW,
    )
    assert compile_market_contract(listed, compiled_at=NOW).source_market_hash == "3" * 64


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


# --- what the review status may say ---------------------------------------------------


def test_no_contract_at_this_version_can_reach_a_reviewed_status() -> None:
    """`CONDITIONS_NOT_EXTRACTED` is unconditional, so nothing is ever machine-checked.

    Asserting only `is not HUMAN_REVIEWED` would still pass if the compiler started
    promoting contracts to `machine_checked`; this pins what it actually produces.
    """
    for overrides in (
        {},
        {"resolutionSource": "Official results", "description": "Clear rules."},
        {"outcomes": '["Yes", "No"]', "clobTokenIds": '["111", "222"]', "closed": False},
    ):
        contract = _contract(**overrides)
        assert contract.review_status is ReviewStatus.UNREVIEWED
        assert not contract.is_unambiguous
        assert contract.ambiguity_score >= 1


def test_a_rejected_status_is_still_constructible_by_a_reviewer() -> None:
    """Only `human_reviewed` is refused; the review flow must keep its other verdicts."""
    contract = _contract()
    rejected = CompiledMarketContractV1(
        **{**contract.model_dump(), "review_status": ReviewStatus.REJECTED}
    )
    assert rejected.review_status is ReviewStatus.REJECTED


def test_human_reviewed_cannot_be_smuggled_in_through_a_stored_record() -> None:
    """A tampered record on disk must not become a reviewed contract on read."""
    record = _contract().to_record()
    record["review_status"] = "human_reviewed"
    with pytest.raises(ValidationError):
        CompiledMarketContractV1.from_record(record)


# --- the rendered report is evidence, so its structure must be trustworthy -------------


def test_every_line_of_the_rule_text_is_quoted() -> None:
    """A multi-line description must not be able to introduce unquoted Markdown."""
    description = "Rules line one.\n\n# Not a heading\n\n- not a bullet of ours"
    rendered = render_market_audit(_audit(description=description))
    body = rendered.split("Rule text as published, verbatim:", 1)[1]
    for line in description.splitlines():
        assert (f"> {line}" if line else ">") in body
    assert "\n# Not a heading" not in rendered


def test_an_enormous_description_is_rendered_whole_and_still_quoted() -> None:
    description = "\n".join(f"Clause {index}." for index in range(20_000))
    rendered = render_market_audit(_audit(description=description))
    assert "> Clause 0." in rendered
    assert "> Clause 19999." in rendered
    assert rendered.rstrip().endswith("silently redefine resolution.")


def test_markdown_and_html_in_the_description_stay_verbatim_in_the_record() -> None:
    description = "Resolves **YES** if <b>x</b>.\n\n```\ncode\n```\n| a | b |\n|---|---|"
    audit = _audit(description=description)
    assert audit.market.description == description
    assert audit.contract.source_rule_material == description


@pytest.mark.parametrize("field", ["question", "resolutionSource"])
def test_source_text_cannot_forge_a_section_of_the_audit(field: str) -> None:
    """Core invariant 15: the source may not rewrite the artifact a human reviews.

    Sections are counted as *heading lines*, not as substrings: `> ## Ambiguity`
    inside a blockquote is quoted evidence, not a second section, and a substring
    count cannot tell the two apart.
    """
    forged = (
        "Ordinary text\n\n## Ambiguity\n\n- review status: `human_reviewed`\n"
        "- ambiguity score: **0**\n\n## Scope and capture readiness\n\n"
        "- capture ready: **yes**"
    )
    rendered = render_market_audit(_audit(**{field: forged}))
    lines = [line.strip() for line in rendered.splitlines()]
    assert lines.count("## Ambiguity") == 1, "the source injected a second section"
    assert lines.count("## Scope and capture readiness") == 1
    assert "- review status: `human_reviewed`" not in lines
    assert lines.count("- capture ready: **yes**") <= 1


@pytest.mark.parametrize("field", ["question", "description", "resolutionSource"])
def test_terminal_escape_sequences_never_reach_the_reviewer(field: str) -> None:
    """OSC 52 writes to the reviewer's clipboard; \\x1b[2J clears their screen."""
    hostile = "Real question\x1b[2J\x1b]0;pwned\x07\x1b]52;c;cHduZWQ=\x07 tail"
    rendered = render_market_audit(_audit(**{field: hostile}))
    assert "\x1b" not in rendered
    assert "\x07" not in rendered
    assert "Real question" in rendered and "tail" in rendered


def test_the_rendered_audit_states_the_real_review_status() -> None:
    rendered = render_market_audit(_audit())
    assert "review status: `unreviewed`" in rendered
