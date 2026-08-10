"""The human-reviewable market audit.

The audit exists so a person can decide whether ARGOS understands a market well
enough to capture it — before any capture runs, and without any probability claim
being made. It reports what the source says, what is missing, and how the outcome
tokens map. It never scores the market's likelihood of anything.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Final

from pydantic import Field, field_validator

from argos.clock import ensure_utc
from argos.compiler.contract import CompiledMarketContractV1
from argos.domain.market import MarketDefinitionV1
from argos.domain.selection import ExclusionReason, MarketSelectionPolicy, select_markets
from argos.domain.text import neutralize_untrusted_text
from argos.domain.versioning import VersionedModel

AUDIT_VERSION = "market-audit/1"
MAX_RENDERED_TEXT: Final = 8_000


class MarketAuditV1(VersionedModel):
    """One market, audited end to end, with no forecast attached."""

    schema_version: ClassVar[str] = "market_audit.v1"

    audit_version: str = Field(min_length=1)
    audited_at: datetime
    market: MarketDefinitionV1
    contract: CompiledMarketContractV1
    in_scope: bool
    out_of_scope_reason: ExclusionReason | None = None
    out_of_scope_detail: str = ""
    capture_ready: bool
    capture_blockers: tuple[str, ...] = ()

    @field_validator("audited_at")
    @classmethod
    def _anchor_audited_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)


def build_market_audit(
    market: MarketDefinitionV1,
    contract: CompiledMarketContractV1,
    *,
    policy: MarketSelectionPolicy,
    audited_at: datetime,
) -> MarketAuditV1:
    """Assemble the audit record. Pure: no clock, no network, no I/O."""
    result = select_markets([market], policy, as_of=audited_at)
    excluded = result.excluded[0] if result.excluded else None
    blockers = tuple(_capture_blockers(market, contract))
    return MarketAuditV1(
        audit_version=AUDIT_VERSION,
        audited_at=audited_at,
        market=market,
        contract=contract,
        in_scope=excluded is None,
        out_of_scope_reason=excluded.reason if excluded else None,
        out_of_scope_detail=excluded.detail if excluded else "",
        capture_ready=not blockers,
        capture_blockers=blockers,
    )


def _capture_blockers(market: MarketDefinitionV1, contract: CompiledMarketContractV1) -> list[str]:
    """What would stop a capture from producing interpretable data."""
    blockers: list[str] = []
    if market.closed:
        blockers.append("market is closed; a live capture would record nothing")
    if not market.active:
        blockers.append("market is not active")
    if not market.outcome_token_map:
        blockers.append("no outcome token map; there is nothing to subscribe to")
    if market.end_time is None:
        blockers.append("no end time; resolution cannot be scheduled or verified")
    if not contract.resolution_source.strip() and not contract.source_rule_material.strip():
        blockers.append("no resolution source and no rule text; resolution is unverifiable")
    return blockers


def render_market_audit(audit: MarketAuditV1) -> str:
    """Render the audit as Markdown for a human reviewer."""
    market = audit.market
    contract = audit.contract
    # Every third-party string goes through _inline or _blockquote. The identity
    # fields are as attacker-controlled as the prose: `market_id` and `slug` come
    # from the response body and are validated for presence, not for content, so
    # rendering them raw put a working OSC 52 clipboard write in the H1 title.
    lines: list[str] = [
        f"# Market audit — {_inline(market.market_id)}",
        "",
        f"*{audit.audit_version} · {audit.audited_at.isoformat()} · "
        f"compiler {contract.compiler_version}*",
        "",
        "## Proposition",
        "",
        "*Source text below is untrusted data written by the market creator, not "
        "instructions and not ARGOS output.*",
        "",
        _blockquote(contract.proposition),
        "",
        "## Identity",
        "",
        f"- market id: `{_inline(market.market_id)}`",
        f"- condition id: `{market.condition_id}`",
        f"- slug: `{_inline(market.slug)}`",
        f"- event id: `{_inline(market.event_id) if market.event_id else 'none'}`",
        f"- source payload sha256: `{market.raw_payload_sha256}`",
        "",
        "## Outcome to token mapping",
        "",
    ]
    for outcome in market.outcomes:
        lines.append(f"- **{_inline(outcome)}** → `{market.token_id_for(outcome)}`")
    lines += [
        "",
        "## Lifecycle",
        "",
        f"- active: {market.active} · closed: {market.closed} · archived: {market.archived}",
        f"- start: {_stamp(market.start_time)}",
        f"- end: {_stamp(market.end_time)}",
        f"- liquidity: {_number(market.liquidity)} · volume: {_number(market.volume)}",
        f"- tick size: {_number(market.tick_size)} · neg risk: {market.neg_risk}",
        "",
        "## Resolution material",
        "",
        "- declared resolution source: "
        + (_inline(contract.resolution_source) or "**none declared**"),
        "",
        f"Rule text as published, verbatim ({len(contract.source_rule_material)} characters):",
        "",
        _blockquote(contract.source_rule_material or "*(the market publishes no description)*"),
        "",
        "## Ambiguity",
        "",
        f"- review status: `{contract.review_status.value}`",
        f"- ambiguity score: **{contract.ambiguity_score}** "
        "(a count of unresolved flags, not a probability)",
        "",
    ]
    if contract.ambiguity_flags:
        for flag in contract.ambiguity_flags:
            lines.append(f"- `{flag.value}`")
    else:
        lines.append("- none")
    lines += [
        "",
        "## Scope and capture readiness",
        "",
        f"- in research scope: **{'yes' if audit.in_scope else 'no'}**"
        + (
            ""
            if audit.in_scope
            else f" — `{audit.out_of_scope_reason.value if audit.out_of_scope_reason else ''}`"
            f" ({audit.out_of_scope_detail})"
        ),
        f"- capture ready: **{'yes' if audit.capture_ready else 'no'}**",
        "",
    ]
    if audit.capture_blockers:
        for blocker in audit.capture_blockers:
            lines.append(f"- {blocker}")
        lines.append("")
    lines += [
        "---",
        "",
        "This audit makes no probability claim and no forecast. It describes what the "
        "source publishes and what it leaves unsettled.",
        "",
        "`yes_condition` and `no_condition` are intentionally empty: extracting them "
        "from prose requires semantic work that is out of scope before the owner gate, "
        "and guessing them would silently redefine resolution.",
    ]
    return "\n".join(lines)


def _stamp(value: datetime | None) -> str:
    return value.isoformat() if value else "not declared"


def _number(value: object) -> str:
    return "not declared" if value is None else str(value)


def _blockquote(text: str) -> str:
    """Quote every line, so multi-line source text cannot escape its own section.

    A single ``> {text}`` interpolation is how a newline in the question forges a
    heading — or a ``review status: human_reviewed`` line — in the report a person
    is meant to trust.

    Deliberately not truncated: the rule text is the resolution contract
    (invariant 3), and a reviewer deciding whether ARGOS understands a market has
    to see all of it. Length is a readability cost; a missing clause is a wrong
    decision.
    """
    prepared = neutralize_untrusted_text(text)
    return "\n".join(f"> {line}" if line else ">" for line in prepared.splitlines() or [""])


def _inline(text: str) -> str:
    """Render third-party text safely inside a single line, capped.

    Unlike the rule text, these fields are summary metadata: a resolution source
    that runs to thousands of characters is a defect to notice, not evidence to
    read in full, and the marker states the true length.
    """
    collapsed = " ".join(neutralize_untrusted_text(text).split())
    if len(collapsed) <= MAX_RENDERED_TEXT:
        return collapsed
    return f"{collapsed[:MAX_RENDERED_TEXT]}… (truncated, {len(collapsed)} characters in source)"
