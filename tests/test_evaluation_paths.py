"""The branches the real evaluation never reaches, and the CLOB refusals.

Written because the coverage gate caught `argos/evaluation/run.py` at 77.78%
against its 90% floor and `clob_resolution.py` at 70%. The real end-to-end
evaluation exercises exactly one happy path -- one market, one token, a
two-sided book at every state -- so everything that handles the *other* cases
was unexercised: the abstention path, the time-to-resolution buckets, and every
refusal the CLOB normalizer can produce.

That is worth stating rather than fixing quietly. A module whose only test is
its happy path is a module whose refusals have never run, and refusals are most
of what these two do.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_replay import CAPTURE_START, RUN_ID, TOKEN, _ListFrameSource, _recorded_frames

from argos.baselines import BaselineMethod
from argos.clock import ReplayClock
from argos.compiler import CompiledMarketContractV1
from argos.config import Settings
from argos.evaluation import evaluate_capture
from argos.evaluation.bundle import ExclusionReason
from argos.evaluation.run import _lead_bucket
from argos.ingestion.capture import run_capture
from argos.resolution import (
    ResolutionRefusal,
    ResolutionRefusalReason,
    ResolutionV1,
    WinningOutcome,
    normalize_clob_resolution,
)
from argos.store.event_store import open_sqlite_event_store

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
CONDITION = "0x94a39addec8bd24a2d03deeaa43bdee6a2b11eca403cd12495eb24132cc23173"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "clob" / "market_resolved.raw.json"


def _payload() -> tuple[dict[str, Any], str]:
    raw = FIXTURE.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


def _resolution(**overrides: Any) -> ResolutionV1:
    payload, digest = _payload()
    result = normalize_clob_resolution(
        payload, source_payload_sha256=digest, normalized_at=NOW, yes_token_id=TOKEN
    )
    assert isinstance(result, ResolutionV1)
    return result.model_copy(update=overrides) if overrides else result


def _contract() -> CompiledMarketContractV1:
    return CompiledMarketContractV1(
        contract_id="contract-" + "a" * 32,
        market_id=CONDITION,
        condition_id=CONDITION,
        source_market_hash="b" * 64,
        compiler_version="test-compiler/1",
        compiled_at=NOW,
        proposition="Recorded tennis market",
    )


async def _store() -> Any:
    store = open_sqlite_event_store(":memory:")
    await run_capture(
        frame_source=_ListFrameSource(_recorded_frames()),
        store=store,
        clock=ReplayClock(CAPTURE_START),
        capture_run_id=RUN_ID,
        subscribed_token_ids=[TOKEN],
    )
    return store


# --- evaluate_capture's refusals and branches -------------------------------------


async def test_a_resolution_with_no_winning_token_cannot_score_a_token() -> None:
    """Refused rather than scored against nothing. A resolution that says an
    outcome was determined but not *which token* won cannot answer "was this
    token's forecast right", and guessing would invert half the scores."""
    store = await _store()
    with pytest.raises(ValueError, match="winning token"):
        evaluate_capture(
            store=store,
            capture_run_id=RUN_ID,
            resolution=_resolution(winning_token_id=None, resolution_status="unknown"),
            token_id=TOKEN,
            settings=Settings(),
            clock=ReplayClock(NOW),
            evaluation_run_id="eval-x",
        )


async def test_a_nonfinal_resolution_is_refused_before_replay() -> None:
    store = await _store()
    with pytest.raises(ValueError, match="only a final resolution"):
        evaluate_capture(
            store=store,
            capture_run_id=RUN_ID,
            resolution=_resolution(resolution_status="proposed"),
            token_id=TOKEN,
            settings=Settings(),
            clock=ReplayClock(NOW),
            evaluation_run_id="eval-proposed",
        )


async def test_a_token_the_capture_never_covered_yields_no_forecasts() -> None:
    """Not an error: an evaluation over a token this capture does not hold is
    empty rather than wrong, and the report says so through its counts instead
    of raising."""
    store = await _store()
    other = "9" * 40
    result = evaluate_capture(
        store=store,
        capture_run_id=RUN_ID,
        resolution=_resolution(winning_token_id=other),
        token_id=other,
        settings=Settings(),
        clock=ReplayClock(NOW),
        evaluation_run_id="eval-empty",
    )
    assert result.report.forecast_count == 0
    assert result.report.scored_count == 0
    assert result.report.limitations, "an empty report still states its limitations"


async def test_the_subscribe_snapshot_supplies_last_trade_as_auxiliary_evidence() -> None:
    """The raw subscribe-time book carries 0.280 even though later snapshots do not."""
    store = await _store()
    result = evaluate_capture(
        store=store,
        capture_run_id=RUN_ID,
        resolution=_resolution(),
        token_id=TOKEN,
        settings=Settings(),
        clock=ReplayClock(NOW),
        evaluation_run_id="eval-lt",
        methods=(BaselineMethod.LAST_TRADE,),
    )
    assert result.report.scored_count == 0
    assert result.report.abstention_count == 0
    assert {forecast.raw_score for forecast in result.forecasts} == {Decimal("0.28")}
    assert "resolution_time_unknown" in result.report.headline_reasons


async def test_a_known_cutoff_and_contract_allow_point_scoring_but_not_calibration() -> None:
    store = await _store()
    result = evaluate_capture(
        store=store,
        capture_run_id=RUN_ID,
        resolution=_resolution(resolved_at=datetime(2026, 8, 11, tzinfo=UTC)),
        token_id=TOKEN,
        settings=Settings(),
        clock=ReplayClock(NOW),
        evaluation_run_id="eval-lead",
        methods=(BaselineMethod.MIDPOINT,),
        contract=_contract(),
    )
    assert result.report.scored_count > 0
    assert result.report.calibration == {}
    assert result.report.trajectory_diagnostics["midpoint"]["sample_count"] > 0
    assert result.report.headline_status == "not_established"
    assert "insufficient_resolved_targets_for_calibration" in result.report.headline_reasons


async def test_post_resolution_points_are_excluded_not_bucketed_into_scores() -> None:
    store = await _store()
    result = evaluate_capture(
        store=store,
        capture_run_id=RUN_ID,
        resolution=_resolution(resolved_at=datetime(2026, 8, 1, tzinfo=UTC)),
        token_id=TOKEN,
        settings=Settings(),
        clock=ReplayClock(NOW),
        evaluation_run_id="eval-after",
        methods=(BaselineMethod.MIDPOINT,),
        contract=_contract(),
    )
    assert result.report.scored_count == 0
    assert {item.reason for item in result.exclusions} == {ExclusionReason.POST_RESOLUTION}


@pytest.mark.parametrize(
    ("lead", "bucket"),
    [
        (None, "unknown"),
        (timedelta(minutes=-5), "after_resolution"),
        (timedelta(minutes=30), "under-1h"),
        (timedelta(hours=5), "1h-1d"),
        (timedelta(days=3), "1d-7d"),
        (timedelta(days=30), "over-7d"),
    ],
)
def test_every_lead_bucket_has_a_name(lead: timedelta | None, bucket: str) -> None:
    """The label remains available for diagnostics; scoring excludes it."""
    made_at = NOW
    resolved_at = None if lead is None else NOW + lead
    assert _lead_bucket(made_at, resolved_at) == bucket
    assert _lead_bucket(None, resolved_at) == "unknown"


# --- the CLOB normalizer's refusals -----------------------------------------------


def _refuse(**patch: Any) -> ResolutionRefusal:
    payload, digest = _payload()
    result = normalize_clob_resolution(
        {**payload, **patch}, source_payload_sha256=digest, normalized_at=NOW, yes_token_id=TOKEN
    )
    assert isinstance(result, ResolutionRefusal), result
    return result


def test_a_record_with_no_condition_id_is_refused() -> None:
    assert _refuse(condition_id=None).reason is ResolutionRefusalReason.MALFORMED_PAYLOAD


def test_an_open_market_is_refused() -> None:
    assert _refuse(closed=False).reason is ResolutionRefusalReason.NOT_CLOSED


@pytest.mark.parametrize("tokens", [[], [{"token_id": "1"}], "not a list", None])
def test_a_market_without_exactly_two_tokens_is_refused(tokens: Any) -> None:
    assert _refuse(tokens=tokens).reason is ResolutionRefusalReason.NOT_A_BINARY_MARKET


def test_a_closed_market_with_no_winner_flag_is_refused() -> None:
    """Closed and undetermined is a real state, and it must not be resolved by
    falling back to whichever token has the higher price."""
    payload, _ = _payload()
    tokens = [dict(token) | {"winner": False, "price": 0} for token in payload["tokens"]]
    assert _refuse(tokens=tokens).reason is ResolutionRefusalReason.NO_DETERMINABLE_OUTCOME


def test_two_winners_are_refused_rather_than_the_first_one_taken() -> None:
    payload, _ = _payload()
    tokens = [dict(token) | {"winner": True, "price": 1} for token in payload["tokens"]]
    assert _refuse(tokens=tokens).reason is ResolutionRefusalReason.MALFORMED_PAYLOAD


def test_a_winner_without_a_token_id_is_refused() -> None:
    payload, _ = _payload()
    tokens = [
        dict(token) | ({"token_id": ""} if token.get("winner") else {})
        for token in payload["tokens"]
    ]
    assert _refuse(tokens=tokens).reason is ResolutionRefusalReason.MALFORMED_PAYLOAD


@pytest.mark.parametrize("winner_price", [Decimal("0.5"), 0])
def test_a_price_that_contradicts_the_winner_flag_is_refused(winner_price: Any) -> None:
    """A settlement two fields disagree about is not a settlement, and preferring
    one silently would record a claim the source did not unambiguously make."""
    payload, _ = _payload()
    tokens = [
        dict(token) | ({"price": winner_price} if token.get("winner") else {})
        for token in payload["tokens"]
    ]
    refusal = _refuse(tokens=tokens)
    assert refusal.reason is ResolutionRefusalReason.MALFORMED_PAYLOAD
    assert "contradicts itself" in refusal.detail


def test_a_loser_priced_above_zero_is_also_a_contradiction() -> None:
    payload, _ = _payload()
    tokens = [
        dict(token) | ({} if token.get("winner") else {"price": Decimal("0.4")})
        for token in payload["tokens"]
    ]
    assert "contradicts itself" in _refuse(tokens=tokens).detail


def test_without_a_yes_token_the_first_listed_token_is_the_yes_side() -> None:
    """Stated in the docstring and asserted here, because it is exactly the kind
    of convention that corrupts an evaluation when it is assumed rather than
    read: this source labels its outcomes with player names, not Yes/No."""
    payload, digest = _payload()
    result = normalize_clob_resolution(payload, source_payload_sha256=digest, normalized_at=NOW)
    assert isinstance(result, ResolutionV1)
    first_token = payload["tokens"][0]["token_id"]
    expected = WinningOutcome.YES if result.winning_token_id == first_token else WinningOutcome.NO
    assert result.winning_outcome is expected
