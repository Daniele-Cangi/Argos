"""Resolution normalization, driven by the four recorded real payloads.

Every shape asserted here was recorded from the live public Gamma endpoint on
2026-08-18 and is committed with a provenance sidecar
(`docs/research/m4-gamma-resolution.md`). That matters more here than anywhere
else in this repository: the naive implementation of this module —
"`closed == true` means resolved, read the winner out of `outcomePrices`" — is
wrong on 93% of an id-ordered sample of closed markets, and no amount of
hand-built fixtures would have shown it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from argos.resolution import (
    ResolutionRefusal,
    ResolutionRefusalReason,
    ResolutionStatus,
    ResolutionV1,
    WinningOutcome,
    normalize_gamma_resolution,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gamma"
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _payload(name: str) -> tuple[dict[str, Any], str]:
    raw = (FIXTURES / f"{name}.raw.json").read_bytes()
    decoded = json.loads(raw.decode("utf-8"))
    market = decoded[0] if isinstance(decoded, list) else decoded
    return market, hashlib.sha256(raw).hexdigest()


def _normalize(name: str) -> ResolutionV1 | ResolutionRefusal:
    market, digest = _payload(name)
    return normalize_gamma_resolution(market, source_payload_sha256=digest, normalized_at=NOW)


# --- what is accepted -------------------------------------------------------------


def test_a_really_resolved_market_normalizes() -> None:
    result = _normalize("market_resolved")
    assert isinstance(result, ResolutionV1)
    assert result.winning_outcome in set(WinningOutcome)
    assert result.resolution_status is ResolutionStatus.FINAL
    assert result.winning_token_id
    assert result.normalizer_version == "gamma-resolution-normalizer/1"
    assert result.source_payload_sha256 == _payload("market_resolved")[1]


def test_the_current_status_is_the_last_element_of_the_uma_trail() -> None:
    """`umaResolutionStatuses` is a history, not a status.

    The recorded payload carries `["proposed","disputed","proposed","resolved"]`.
    Reading its first element would report a settled market as merely proposed,
    and reading "is 'disputed' present" would report a settled market as
    disputed forever.
    """
    market, _ = _payload("market_resolved_after_dispute")
    assert json.loads(market["umaResolutionStatuses"]) == [
        "proposed",
        "disputed",
        "proposed",
        "resolved",
    ]
    result = _normalize("market_resolved_after_dispute")
    assert isinstance(result, ResolutionV1)
    assert result.resolution_status is ResolutionStatus.FINAL


def test_the_winning_token_matches_the_winning_side_of_the_token_map() -> None:
    market, _ = _payload("market_resolved")
    result = _normalize("market_resolved")
    assert isinstance(result, ResolutionV1)
    tokens = json.loads(market["clobTokenIds"])
    expected = tokens[0] if result.winning_outcome is WinningOutcome.YES else tokens[1]
    assert result.winning_token_id == expected


# --- what is refused, which is most of it -----------------------------------------


def test_a_closed_market_with_a_price_is_refused_not_rounded() -> None:
    """The finding this module exists for.

    Market 40 is "Will Trump win the 2020 U.S. presidential election", closed,
    at 0.0000000436 / 0.9999999. It rounds to a resolution and is not one, and
    rounding it would be ARGOS deciding an outcome the source did not state.
    93.2% of an id-ordered closed sample looks like this.
    """
    result = _normalize("market_closed_with_a_price_not_a_resolution")
    assert isinstance(result, ResolutionRefusal)
    assert result.reason is ResolutionRefusalReason.PRICES_ARE_NOT_A_RESOLUTION


def test_a_closed_market_with_no_determinable_outcome_is_refused() -> None:
    result = _normalize("market_closed_without_outcome")
    assert isinstance(result, ResolutionRefusal)
    assert result.reason is ResolutionRefusalReason.NO_DETERMINABLE_OUTCOME


def test_an_open_market_is_refused() -> None:
    result = _normalize("market_by_id")
    assert isinstance(result, ResolutionRefusal)
    assert result.reason is ResolutionRefusalReason.NOT_CLOSED


@pytest.mark.parametrize(
    ("prices", "reason"),
    [
        ('["1", "0", "0"]', ResolutionRefusalReason.NOT_A_BINARY_MARKET),
        ('["0.9999999", "0.0000001"]', ResolutionRefusalReason.PRICES_ARE_NOT_A_RESOLUTION),
        ('["1.0", "0.0"]', None),
        ("not json", ResolutionRefusalReason.MALFORMED_PAYLOAD),
        (None, ResolutionRefusalReason.MALFORMED_PAYLOAD),
    ],
)
def test_only_an_exact_one_zero_pair_determines_an_outcome(
    prices: str | None, reason: ResolutionRefusalReason | None
) -> None:
    """`["1.0","0.0"]` is accepted because it *is* exactly one and zero written
    differently -- the comparison is numeric, not textual, so a source that
    changes its formatting does not become a rejection storm."""
    market, digest = _payload("market_resolved")
    patched = dict(market)
    if prices is None:
        patched.pop("outcomePrices", None)
    else:
        patched["outcomePrices"] = prices
    result = normalize_gamma_resolution(patched, source_payload_sha256=digest, normalized_at=NOW)
    if reason is None:
        assert isinstance(result, ResolutionV1)
    else:
        assert isinstance(result, ResolutionRefusal)
        assert result.reason is reason


def test_a_refusal_is_returned_rather_than_raised() -> None:
    """These are the *common* case over a real sample -- an exception per market
    would turn the ordinary shape of this data into control flow."""
    for name in ("market_by_id", "market_closed_without_outcome"):
        assert isinstance(_normalize(name), ResolutionRefusal)


# --- status handling --------------------------------------------------------------


@pytest.mark.parametrize(
    ("trail", "expected"),
    [
        ('["proposed"]', ResolutionStatus.PROPOSED),
        ('["proposed", "resolved"]', ResolutionStatus.FINAL),
        ('["proposed", "disputed"]', ResolutionStatus.DISPUTED),
        ("[]", ResolutionStatus.UNKNOWN),
        ('["something_new"]', ResolutionStatus.UNKNOWN),
    ],
)
def test_an_unrecognized_status_degrades_to_unknown_rather_than_being_guessed(
    trail: str, expected: ResolutionStatus
) -> None:
    """Nothing establishes that the three observed values are the whole
    vocabulary, and the safe direction is to claim less: `unknown` says less
    than the truth, while an invented `final` says more than the source did on
    the record that decides whether a forecast gets scored."""
    market, digest = _payload("market_resolved")
    patched = dict(market) | {"umaResolutionStatuses": trail}
    result = normalize_gamma_resolution(patched, source_payload_sha256=digest, normalized_at=NOW)
    assert isinstance(result, ResolutionV1)
    assert result.resolution_status is expected


def test_a_determined_outcome_with_no_uma_trail_is_still_a_resolution() -> None:
    """8 of 500 recently-closed markets carry an exact 1/0 outcome and an empty
    trail. The outcome is determined and the status is not recorded, which is
    why they are separate fields rather than one."""
    market, digest = _payload("market_resolved")
    patched = dict(market) | {"umaResolutionStatuses": "[]"}
    result = normalize_gamma_resolution(patched, source_payload_sha256=digest, normalized_at=NOW)
    assert isinstance(result, ResolutionV1)
    assert result.resolution_status is ResolutionStatus.UNKNOWN
    assert result.winning_outcome in set(WinningOutcome)


def test_a_final_status_without_a_token_map_degrades_instead_of_raising() -> None:
    """The model refuses a `final` record with no winning token, and the
    normalizer must not therefore blow up on a payload missing `clobTokenIds` --
    the outcome is still determined, and only the claim about settlement
    weakens."""
    market, digest = _payload("market_resolved")
    patched = dict(market)
    patched.pop("clobTokenIds", None)
    result = normalize_gamma_resolution(patched, source_payload_sha256=digest, normalized_at=NOW)
    assert isinstance(result, ResolutionV1)
    assert result.winning_token_id is None
    assert result.resolution_status is ResolutionStatus.UNKNOWN


def test_a_resolution_round_trips() -> None:
    result = _normalize("market_resolved")
    assert isinstance(result, ResolutionV1)
    record = result.to_record()
    assert record["schema_version"] == "resolution.v1"
    assert ResolutionV1.from_record(record) == result
