"""Resolution from the CLOB market record, where the source states it outright.

The Gamma path (:mod:`argos.resolution.gamma_resolution`) infers an outcome from
``outcomePrices``, and has to refuse most of what it sees because that field is
usually a last price. This source does not require inference: the CLOB market
record carries an explicit **``winner``** boolean per token.

Two measured facts make this the primary path rather than a second option
(``docs/research/m4-gamma-resolution.md``):

- **It is stated, not derived.** ``{"outcome": "Iga Swiatek", "price": 1,
  "winner": true}`` is the source saying who won. Reading `1.0` out of a price
  array and calling it a settlement is ARGOS deciding; reading ``winner`` is
  ARGOS recording.
- **Gamma does not cover the same markets.** For the exact market ARGOS
  captured on 2026-08-10 — the one every replay test in this repository runs on
  — Gamma's ``condition_ids`` query returns **nothing at all**, while the CLOB
  returns a complete resolved record. A resolution pipeline built only on Gamma
  would have had zero coverage of ARGOS's own capture.

The price field is still cross-checked against the winner flag rather than
ignored. They agreed on every observed record, and a disagreement would mean
the source contradicts itself about a settlement — which must surface as a
refusal rather than as a silent choice between two answers.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from argos.resolution.gamma_resolution import (
    ResolutionRefusal,
    ResolutionRefusalReason,
    ResolutionStatus,
    ResolutionV1,
    WinningOutcome,
)

__all__ = ["CLOB_RESOLUTION_NORMALIZER_VERSION", "normalize_clob_resolution"]

CLOB_RESOLUTION_NORMALIZER_VERSION = "clob-resolution-normalizer/1"


def normalize_clob_resolution(
    payload: Mapping[str, Any],
    *,
    source_payload_sha256: str,
    normalized_at: datetime,
    yes_token_id: str | None = None,
) -> ResolutionV1 | ResolutionRefusal:
    """Turn one CLOB market record into a resolution, or say why not.

    ``yes_token_id`` names which of the two tokens is the market's "YES" side.
    It has to be supplied because this source does not label one: its outcomes
    are ``"Diana Shnaider"`` and ``"Iga Swiatek"``, not ``"Yes"`` and ``"No"``,
    and picking the first token as YES would silently make the outcome depend on
    array order. When it is omitted, the record still resolves and reports the
    **winning token**, with :attr:`WinningOutcome.YES` meaning "the first listed
    token won" — stated here because that is exactly the kind of convention that
    corrupts an evaluation when it is assumed rather than read.
    """
    condition_id = _text(payload.get("condition_id"))
    if not condition_id:
        return ResolutionRefusal(
            reason=ResolutionRefusalReason.MALFORMED_PAYLOAD,
            detail="payload carries no usable 'condition_id'",
        )
    if payload.get("closed") is not True:
        return ResolutionRefusal(
            reason=ResolutionRefusalReason.NOT_CLOSED,
            detail=f"market is not closed (closed={payload.get('closed')!r})",
            condition_id=condition_id,
        )

    tokens = payload.get("tokens")
    if not isinstance(tokens, list) or len(tokens) != 2:
        found = len(tokens) if isinstance(tokens, list) else type(tokens).__name__
        return ResolutionRefusal(
            reason=ResolutionRefusalReason.NOT_A_BINARY_MARKET,
            detail=f"expected exactly two tokens, got {found}",
            condition_id=condition_id,
        )

    winners = [token for token in tokens if _is_mapping(token) and token.get("winner") is True]
    if not winners:
        return ResolutionRefusal(
            reason=ResolutionRefusalReason.NO_DETERMINABLE_OUTCOME,
            detail="the market is closed and no token is flagged as the winner",
            condition_id=condition_id,
        )
    if len(winners) > 1:
        return ResolutionRefusal(
            reason=ResolutionRefusalReason.MALFORMED_PAYLOAD,
            detail="more than one token is flagged as the winner",
            condition_id=condition_id,
        )

    winner = winners[0]
    winning_token = _text(winner.get("token_id"))
    if not winning_token:
        return ResolutionRefusal(
            reason=ResolutionRefusalReason.MALFORMED_PAYLOAD,
            detail="the winning token carries no usable 'token_id'",
            condition_id=condition_id,
        )

    disagreement = _price_disagreement(tokens)
    if disagreement is not None:
        return ResolutionRefusal(
            reason=ResolutionRefusalReason.MALFORMED_PAYLOAD,
            detail=(
                f"the source contradicts itself about the settlement: {disagreement}. "
                "Refused rather than resolved by preferring one field, because a "
                "settlement two fields disagree about is not a settlement"
            ),
            condition_id=condition_id,
        )

    first_token = _text(tokens[0].get("token_id")) if _is_mapping(tokens[0]) else None
    reference = yes_token_id if yes_token_id is not None else first_token
    outcome = WinningOutcome.YES if winning_token == reference else WinningOutcome.NO

    return ResolutionV1(
        resolution_id=f"resolution-{condition_id}",
        # This source has no Gamma market id; the condition id is the identifier
        # it does carry, and inventing one would make two records for one market
        # look like records for two.
        market_id=condition_id,
        condition_id=condition_id,
        resolved_at=None,
        winning_outcome=outcome,
        winning_token_id=winning_token,
        resolution_source="",
        source_payload_sha256=source_payload_sha256,
        clarification_present=False,
        # The CLOB record states the settlement rather than a UMA trail, so
        # there is no proposal history to read. FINAL is the honest reading of
        # an explicit winner flag on a closed market -- and the model requires a
        # winning token for it, which this path always has.
        resolution_status=ResolutionStatus.FINAL,
        normalizer_version=CLOB_RESOLUTION_NORMALIZER_VERSION,
        normalized_at=normalized_at,
    )


def _price_disagreement(tokens: list[Any]) -> str | None:
    """Cross-check each token's ``price`` against its ``winner`` flag.

    Observed to agree on every record (winner at 1, loser at 0). The check
    exists because the two fields *can* disagree, and a pipeline that preferred
    one silently would record a settlement the source did not unambiguously
    state.
    """
    for token in tokens:
        if not _is_mapping(token):
            continue
        price = _decimal(token.get("price"))
        if price is None:
            continue
        is_winner = token.get("winner") is True
        if is_winner and price != 1:
            return f"token flagged winner carries price {price}, not 1"
        if not is_winner and price != 0:
            return f"token not flagged winner carries price {price}, not 0"
    return None


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _decimal(raw: Any) -> Decimal | None:
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def _text(raw: Any) -> str | None:
    return raw if isinstance(raw, str) and raw else None
