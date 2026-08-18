"""Scoring, calibration, and the end-to-end evaluation on real resolved data.

The last test in this file is the one the owner gate asks for: **a completed
baseline evaluation, without edge claims**. It runs the whole chain on data
ARGOS actually holds — the 40-second WebSocket capture recorded on 2026-08-10,
replayed through the M3 pipeline into an order book, read as a midpoint
baseline, and scored against the market's real settlement, which the CLOB
records explicitly and which was fetched after the match finished.

The market is *National Bank Open: Diana Shnaider vs Iga Swiatek*. The captured
token is Shnaider's. She lost. So a baseline that read the market's midpoint as
a score for "Shnaider wins" is being scored against an outcome of 0, and the
question the numbers answer is whether the market's own price was any good —
which is what a baseline is for.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from argos.baselines import BaselineMethod, build_baseline_forecast, quote_from_book_state
from argos.evaluation import (
    DEFAULT_LOG_LOSS_EPSILON,
    ForecastEvaluationV1,
    brier_score,
    calibration_report,
    cohort_report,
    log_loss,
    score_forecast,
    spread_bucket,
)
from argos.evaluation.report import EvaluationReportV1
from argos.resolution import ResolutionV1, WinningOutcome, normalize_clob_resolution

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
CONDITION = "0x94a39addec8bd24a2d03deeaa43bdee6a2b11eca403cd12495eb24132cc23173"
SHNAIDER = "34691510069031117755834214800869745291092253295564665516660269316660628637961"


def _score(value: str, outcome: WinningOutcome, method: str = "midpoint") -> ForecastEvaluationV1:
    return score_forecast(
        forecast_method=method,
        condition_id=CONDITION,
        token_id=SHNAIDER,
        score=Decimal(value),
        resolution_id="resolution-x",
        winning_outcome=outcome,
        calibration_status="uncalibrated",
        created_at=NOW,
    )


# --- scoring rules ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "outcome", "expected"),
    [("1", 1, "0"), ("0", 1, "1"), ("0.5", 1, "0.25"), ("0.25", 0, "0.0625")],
)
def test_brier_is_the_squared_error(score: str, outcome: int, expected: str) -> None:
    assert brier_score(Decimal(score), outcome) == Decimal(expected)


def test_log_loss_reports_whether_it_clipped() -> None:
    """The clip is a decision that changes the number, so the number is not
    reportable without it. A market that displayed 1.00 for something that did
    not happen scores 13.8 at eps=1e-6 and 6.9 at 1e-3."""
    loss, clipped = log_loss(Decimal(1), 0, epsilon=DEFAULT_LOG_LOSS_EPSILON)
    assert clipped is True
    assert Decimal("13.8") < loss < Decimal("13.9")

    loose, _ = log_loss(Decimal(1), 0, epsilon=Decimal("0.001"))
    assert Decimal("6.9") < loose < Decimal("6.95")

    ordinary, was_clipped = log_loss(Decimal("0.5"), 1, epsilon=DEFAULT_LOG_LOSS_EPSILON)
    assert was_clipped is False
    assert Decimal("0.69") < ordinary < Decimal("0.694")


def test_an_evaluation_whose_metrics_disagree_with_its_inputs_is_refused() -> None:
    """A stored evaluation is the artifact a claim is made from. One that can
    carry a Brier score unrelated to its own forecast is a claim nobody can
    check."""
    honest = _score("0.25", WinningOutcome.NO)
    with pytest.raises(ValidationError):
        honest.model_copy(update={"brier_score": Decimal("0.01")})


def test_scoring_takes_an_outcome_that_cannot_express_unresolved() -> None:
    """The M4 exit criterion "unresolved markets are not scored as negatives",
    enforced by the type rather than by a check somebody has to remember:
    `WinningOutcome` has exactly two members, and an undetermined market
    produces a `ResolutionRefusal` instead of a resolution at all."""
    assert set(WinningOutcome) == {WinningOutcome.YES, WinningOutcome.NO}


# --- calibration ------------------------------------------------------------------


def test_every_bin_is_reported_including_the_empty_ones() -> None:
    """An absent bin and a zero bin read identically in a table, and a
    reliability curve drawn only over populated bins looks far better calibrated
    than the data supports."""
    report = calibration_report([_score("0.55", WinningOutcome.YES)], method="midpoint")
    assert len(report.bins) == 10
    assert report.sample_count == 1
    populated = [b for b in report.bins if b.count]
    assert len(populated) == 1
    assert populated[0].lower == Decimal("0.5")
    empty = next(b for b in report.bins if b.count == 0)
    assert empty.observed_rate is None, "an empty bin has no observed rate, and 0 is not one"


def test_a_score_of_exactly_one_lands_in_the_last_bin() -> None:
    """Without a closed right edge a maximally confident correct forecast would
    vanish from its own reliability curve."""
    report = calibration_report([_score("1", WinningOutcome.YES)], method="midpoint")
    assert report.bins[-1].count == 1
    assert report.sample_count == 1


def test_an_empty_sample_reports_no_metrics_rather_than_flattering_ones() -> None:
    """An ECE of 0 over an empty sample is the most flattering number a
    calibration report can produce, and it means nothing."""
    report = calibration_report([], method="midpoint")
    assert report.sample_count == 0
    assert report.expected_calibration_error is None
    assert report.mean_brier is None
    assert all(b.count == 0 for b in report.bins)


def test_a_perfectly_calibrated_sample_has_no_calibration_error() -> None:
    evaluations = [
        *[_score("0.5", WinningOutcome.YES) for _ in range(5)],
        *[_score("0.5", WinningOutcome.NO) for _ in range(5)],
    ]
    report = calibration_report(evaluations, method="midpoint")
    assert report.expected_calibration_error == 0
    assert report.sample_count == 10


def test_a_mixed_calibration_status_is_reported_as_mixed() -> None:
    """Labelling a mixture with one status would be exactly the claim ADR-0006
    exists to prevent."""
    a = _score("0.5", WinningOutcome.YES)
    b = a.model_copy(update={"calibration_status": "calibrated"})
    assert calibration_report([a, b], method="midpoint").calibration_status == "mixed"


def test_the_clipping_rate_travels_with_the_mean() -> None:
    report = calibration_report(
        [_score("1", WinningOutcome.NO), _score("0.5", WinningOutcome.YES)], method="midpoint"
    )
    assert report.clipped_count == 1
    assert report.log_loss_epsilon == DEFAULT_LOG_LOSS_EPSILON


@pytest.mark.parametrize(
    ("spread", "bucket"),
    [
        (None, "unknown"),
        ("0.005", "0-1c"),
        ("0.03", "1-5c"),
        ("0.08", "5-10c"),
        ("0.5", "over-10c"),
    ],
)
def test_spread_buckets_have_fixed_edges_and_keep_the_unknowns(
    spread: str | None, bucket: str
) -> None:
    """`unknown` is a bucket rather than an exclusion: a one-sided book has no
    spread, and dropping those markets would quietly narrow the sample the
    table claims to describe."""
    assert spread_bucket(None if spread is None else Decimal(spread)) == bucket


def test_a_cohort_report_slices_and_counts_each_slice() -> None:
    wide = _score("0.5", WinningOutcome.YES)
    narrow = _score("0.9", WinningOutcome.YES, method="midpoint").model_copy(
        update={"evaluation_id": "evaluation-narrow"}
    )
    report = cohort_report(
        [wide, narrow],
        method="midpoint",
        dimension="spread_bucket",
        key_of={wide.evaluation_id: "over-10c", "evaluation-narrow": "0-1c"},
    )
    assert [name for name, _ in report.slices] == ["0-1c", "over-10c"]
    assert all(sliced.sample_count == 1 for _, sliced in report.slices)


# --- the report -------------------------------------------------------------------


def _report(**overrides: Any) -> EvaluationReportV1:
    fields: dict[str, Any] = {
        "evaluation_run_id": "eval-1",
        "created_at": NOW,
        "config_fingerprint": "f" * 64,
        "evaluator_version": "argos-baseline-evaluator/1",
        "log_loss_epsilon": DEFAULT_LOG_LOSS_EPSILON,
        "calibration_bin_count": 10,
        "forecast_count": 1,
        "abstention_count": 0,
        "scored_count": 1,
        "unresolved_count": 0,
        "limitations": ("one market is not a sample",),
    }
    fields.update(overrides)
    return EvaluationReportV1(**fields)


def test_a_report_without_limitations_is_refused() -> None:
    """A limitations section that is optional disappears exactly when the
    results look good."""
    with pytest.raises(ValidationError):
        _report(limitations=())
    with pytest.raises(ValidationError):
        _report(limitations=("   ",))


def test_a_report_round_trips_and_never_omits_its_sample_size() -> None:
    report = _report()
    record = report.to_record()
    assert record["schema_version"] == "evaluation_report.v1"
    assert EvaluationReportV1.from_record(record) == report
    assert "1 scored of 1 forecasts" in report.describe()


# --- the real evaluation ----------------------------------------------------------


def test_the_captured_market_really_resolved_and_argos_can_read_it() -> None:
    """The CLOB states the winner rather than leaving it to be inferred.

    Gamma returns nothing at all for this condition id, so a resolution pipeline
    built only on Gamma would have had zero coverage of ARGOS's own capture.
    """
    raw = (Path(__file__).parent / "fixtures" / "clob" / "market_resolved.raw.json").read_bytes()
    payload: dict[str, Any] = json.loads(raw.decode("utf-8"))
    resolution = normalize_clob_resolution(
        payload,
        source_payload_sha256=hashlib.sha256(raw).hexdigest(),
        normalized_at=NOW,
        yes_token_id=SHNAIDER,
    )
    assert isinstance(resolution, ResolutionV1)
    assert resolution.condition_id == CONDITION
    # Shnaider's token is the one ARGOS captured, and she lost.
    assert resolution.winning_outcome is WinningOutcome.NO
    assert resolution.winning_token_id != SHNAIDER


async def test_a_complete_baseline_evaluation_on_real_captured_and_resolved_data() -> None:
    """The owner gate's "at least one completed baseline evaluation exists".

    Whole chain, no constructed inputs: the recorded live capture is replayed
    through the M3 pipeline, the reconstructed book yields a midpoint baseline
    at each state, and every forecast is scored against the settlement the CLOB
    published after the match.

    **No edge claim is made and none could be.** This is one market, one
    40-second window, one token, and the forecasts are heavily autocorrelated
    because they are successive states of the same order book. The numbers
    below are evidence that the machinery runs end to end on real data, and are
    reported with exactly that limitation attached.
    """
    from test_replay import RUN_ID, _captured, _replay

    from argos.replay import read_capture_arrivals

    store = await _captured()
    replayed = _replay(store)
    assert replayed.counts.arrivals == 38

    raw = (Path(__file__).parent / "fixtures" / "clob" / "market_resolved.raw.json").read_bytes()
    resolution = normalize_clob_resolution(
        json.loads(raw.decode("utf-8")),
        source_payload_sha256=hashlib.sha256(raw).hexdigest(),
        normalized_at=NOW,
        yes_token_id=SHNAIDER,
    )
    assert isinstance(resolution, ResolutionV1)

    # Walk the same arrivals again, taking a midpoint baseline at every state
    # the projection passes through. Forecast times come from the capture, never
    # from a clock.
    from argos.projections.dispatch import ObservationDispatcher

    dispatcher = ObservationDispatcher()
    evaluations: list[ForecastEvaluationV1] = []
    abstentions = 0
    for arrival in read_capture_arrivals(store, RUN_ID):
        assert arrival.envelope is not None
        dispatcher.dispatch(arrival.envelope)
        projection = dispatcher.projections.get((CONDITION, SHNAIDER))
        if projection is None or not projection.is_seeded:
            continue
        quote = quote_from_book_state(projection.state(), quote_time=arrival.envelope.event_time)
        forecast = build_baseline_forecast(
            method=BaselineMethod.MIDPOINT,
            quote=quote,
            as_of_received_time=arrival.received_time,
            as_of_ingest_sequence=arrival.ingest_sequence,
        )
        if forecast.abstained or forecast.raw_score is None:
            abstentions += 1
            continue
        evaluations.append(
            score_forecast(
                forecast_method=forecast.method.value,
                condition_id=forecast.condition_id,
                token_id=forecast.token_id,
                score=forecast.raw_score,
                resolution_id=resolution.resolution_id,
                winning_outcome=resolution.winning_outcome,
                calibration_status=forecast.calibration_status.value,
                created_at=NOW,
            ).model_copy(update={"evaluation_id": f"evaluation-{arrival.ingest_sequence}"})
        )

    assert evaluations, "the real capture must yield at least one scored baseline"
    report = calibration_report(evaluations, method="midpoint")
    assert report.sample_count == len(evaluations)
    assert report.calibration_status == "uncalibrated"
    assert report.mean_brier is not None
    assert Decimal(0) <= report.mean_brier <= Decimal(1)

    # Every forecast scored against a real 0 outcome, so a market that priced
    # Shnaider low was right. The assertion is deliberately weak -- that the
    # metric is in range and the sample is what it is -- because one market is
    # not a result, and asserting a *good* Brier score here would be the edge
    # claim `.claude/rules/scientific-claims.md` forbids.
    assert all(e.outcome_yes == 0 for e in evaluations)

    final = EvaluationReportV1(
        evaluation_run_id="eval-real-1",
        created_at=NOW,
        config_fingerprint="0" * 64,
        evaluator_version=evaluations[0].evaluator_version,
        log_loss_epsilon=DEFAULT_LOG_LOSS_EPSILON,
        calibration_bin_count=10,
        source_capture_run_ids=(RUN_ID,),
        source_state_hash=replayed.state_hash,
        forecast_count=len(evaluations) + abstentions,
        abstention_count=abstentions,
        scored_count=len(evaluations),
        unresolved_count=0,
        calibration=report.as_record(),
        limitations=(
            "One market, one 40-second window, one token. This is not a sample.",
            "Forecasts are successive states of one order book and are heavily "
            "autocorrelated; the effective sample size is far below the count.",
            "The scores are uncalibrated market midpoints, not ARGOS probabilities.",
            "No comparison against any other forecaster is made or implied.",
        ),
        cohorts={
            "spread_bucket": cohort_report(
                evaluations,
                method="midpoint",
                dimension="spread_bucket",
                key_of={e.evaluation_id: "unknown" for e in evaluations},
            ).as_record()
        },
    )
    assert final.scored_count == len(evaluations)
    assert final.source_state_hash == replayed.state_hash
    assert EvaluationReportV1.from_record(final.to_record()) == final
