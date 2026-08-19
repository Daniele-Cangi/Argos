"""Legacy M4 evaluator retained only for v1 compatibility tests.

The composition root for M4, and the answer to "baseline evaluation is
reproducible from stored records". Both inputs are *records*: an event store
this repository wrote, and a source payload recorded to a file with its own
hash. Nothing is fetched while evaluating, deliberately — an evaluation that
reached the network could produce a different answer tomorrow from the same
arguments, which is the opposite of the property being claimed.

Forecasts are read off the **replayed** book, one per arrival, so the whole
chain a score depends on is the chain M3 made deterministic: the same event
store, the same arrival order, the same projections, the same state hash.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from argos.baselines import (
    BaselineMethod,
    MarketBaselineForecastV1,
    build_baseline_forecast,
    quote_from_book_state,
)
from argos.clock import Clock
from argos.config.settings import Settings
from argos.evaluation.calibration import (
    DEFAULT_BIN_COUNT,
    calibration_report,
    cohort_report,
    spread_bucket,
)
from argos.evaluation.numeric import require_bin_count, require_epsilon
from argos.evaluation.report import EvaluationReportV1
from argos.evaluation.scoring import (
    DEFAULT_LOG_LOSS_EPSILON,
    EVALUATOR_VERSION,
    ForecastEvaluationV1,
    score_forecast,
)
from argos.projections.dispatch import ObservationDispatcher
from argos.replay.reader import read_capture_arrivals
from argos.resolution.gamma_resolution import ResolutionV1, WinningOutcome
from argos.store.event_store import EventStore

__all__ = ["EvaluationResult", "evaluate_capture_v1_unsafe"]


@dataclass(frozen=True, slots=True)
class EvaluationResult:  # pragma: no cover - superseded v1 compatibility
    """The report, plus the records behind it for a caller that wants them."""

    report: EvaluationReportV1
    forecasts: tuple[MarketBaselineForecastV1, ...]
    evaluations: tuple[ForecastEvaluationV1, ...]


def _refuse_v1() -> None:  # pragma: no cover - superseded v1 compatibility
    raise RuntimeError("the arrival-weighted v1 evaluator is superseded by ADR-0013")


def evaluate_capture_v1_unsafe(  # pragma: no cover - superseded v1 compatibility
    *,
    store: EventStore,
    capture_run_id: str,
    resolution: ResolutionV1,
    token_id: str,
    settings: Settings,
    clock: Clock,
    evaluation_run_id: str,
    methods: tuple[BaselineMethod, ...] = (BaselineMethod.MIDPOINT, BaselineMethod.PERSISTENCE),
    epsilon: Decimal = DEFAULT_LOG_LOSS_EPSILON,
    bin_count: int = DEFAULT_BIN_COUNT,
    code_revision: str | None = None,
    extra_limitations: tuple[str, ...] = (),
) -> EvaluationResult:
    """Replay one capture with superseded arrival-weighted v1 semantics.

    This function is deliberately not exported from :mod:`argos.evaluation`.
    New callers must use the evidence-bundled v2 evaluator exported there.

    ``token_id`` names the side being forecast. The resolution says which token
    won; a forecast is a score for *this* token winning, so the outcome is YES
    exactly when ``resolution.winning_token_id`` is this token — computed here
    rather than taken on trust, because getting it backwards would invert every
    score in the report and nothing else would look wrong.
    """
    _refuse_v1()

    require_epsilon(epsilon)
    require_bin_count(bin_count)
    if resolution.winning_token_id is None:
        raise ValueError(
            "the resolution names no winning token, so no forecast about a "
            "particular token can be scored against it"
        )
    outcome = _outcome_for(resolution, token_id)

    dispatcher = ObservationDispatcher()
    forecasts: list[MarketBaselineForecastV1] = []
    evaluations: list[ForecastEvaluationV1] = []
    abstention_reasons: dict[str, int] = {}
    created_at = clock.now()

    # The previous *observed* midpoint, carried across arrivals for the
    # persistence baseline. One value rather than one per method, and updated
    # whether or not any method produced a score: keeping it inside the
    # non-abstaining branch made persistence abstain on every arrival forever,
    # since it can only ever score once something has been carried forward.
    # Found by reading the output -- 38 of 38 abstained with
    # `no_prior_forecast` -- rather than by a test, which is recorded because a
    # baseline that silently never fires is exactly the shape of defect a green
    # suite hides.
    previous_midpoint: Decimal | None = None

    for arrival in read_capture_arrivals(store, capture_run_id):
        envelope = arrival.envelope
        if envelope is None:
            continue
        dispatcher.dispatch(envelope)
        projection = dispatcher.projections.get((resolution.condition_id, token_id))
        if projection is None or not projection.is_seeded:
            continue

        quote = quote_from_book_state(projection.state(), quote_time=envelope.event_time)
        for method in methods:
            forecast = build_baseline_forecast(
                method=method,
                quote=quote,
                as_of_received_time=arrival.received_time,
                as_of_ingest_sequence=arrival.ingest_sequence,
                previous_score=previous_midpoint,
            )
            forecasts.append(forecast)
            if forecast.abstained or forecast.raw_score is None:
                reason = forecast.abstention_reason
                key = reason.value if reason else "unknown"
                abstention_reasons[key] = abstention_reasons.get(key, 0) + 1
                continue
            evaluations.append(
                score_forecast(
                    forecast_method=method.value,
                    condition_id=forecast.condition_id,
                    token_id=forecast.token_id,
                    score=forecast.raw_score,
                    resolution_id=resolution.resolution_id,
                    winning_outcome=outcome,
                    calibration_status=forecast.calibration_status.value,
                    created_at=created_at,
                    epsilon=epsilon,
                ).model_copy(
                    # One evaluation per (method, arrival): the default id is per
                    # (method, token, resolution), which would collapse every
                    # forecast in a run onto one identity.
                    update={"evaluation_id": f"evaluation-{method.value}-{arrival.ingest_sequence}"}
                )
            )

        # After every method has seen the arrival, not before: persistence must
        # carry the *previous* midpoint, and updating earlier would make it a
        # copy of the midpoint baseline rather than a lag of it.
        if quote.midpoint is not None:
            previous_midpoint = quote.midpoint

    spread_by_evaluation = _spread_keys(forecasts, evaluations)
    return EvaluationResult(
        report=EvaluationReportV1(
            evaluation_run_id=evaluation_run_id,
            created_at=created_at,
            code_revision=code_revision,
            config_fingerprint=settings.fingerprint(),
            evaluator_version=EVALUATOR_VERSION,
            log_loss_epsilon=epsilon,
            calibration_bin_count=bin_count,
            source_capture_run_ids=(capture_run_id,),
            source_state_hash=dispatcher.state_hash(),
            forecast_count=len(forecasts),
            abstention_count=len(forecasts) - len(evaluations),
            scored_count=len(evaluations),
            unresolved_count=0,
            abstention_reasons=abstention_reasons,
            calibration={
                method.value: calibration_report(
                    evaluations, method=method.value, bin_count=bin_count
                ).as_record()
                for method in methods
            },
            cohorts={
                "spread_bucket": {
                    method.value: cohort_report(
                        evaluations,
                        method=method.value,
                        dimension="spread_bucket",
                        key_of=spread_by_evaluation,
                        bin_count=bin_count,
                    ).as_record()
                    for method in methods
                },
                "time_to_resolution": {
                    method.value: cohort_report(
                        evaluations,
                        method=method.value,
                        dimension="time_to_resolution",
                        key_of=_time_to_resolution_keys(forecasts, evaluations, resolution),
                        bin_count=bin_count,
                    ).as_record()
                    for method in methods
                },
            },
            limitations=_limitations(evaluations, extra_limitations),
        ),
        forecasts=tuple(forecasts),
        evaluations=tuple(evaluations),
    )


def _outcome_for(  # pragma: no cover - superseded v1 compatibility
    resolution: ResolutionV1, token_id: str
) -> WinningOutcome:
    return WinningOutcome.YES if resolution.winning_token_id == token_id else WinningOutcome.NO


def _spread_keys(  # pragma: no cover - superseded v1 compatibility
    forecasts: list[MarketBaselineForecastV1], evaluations: list[ForecastEvaluationV1]
) -> dict[str, str]:
    by_sequence = {(f.method.value, f.as_of_ingest_sequence): f.quote.spread for f in forecasts}
    keys: dict[str, str] = {}
    for evaluation in evaluations:
        sequence = int(evaluation.evaluation_id.rsplit("-", 1)[1])
        keys[evaluation.evaluation_id] = spread_bucket(
            by_sequence.get((evaluation.forecast_method, sequence))
        )
    return keys


def _time_to_resolution_keys(  # pragma: no cover - superseded v1 compatibility
    forecasts: list[MarketBaselineForecastV1],
    evaluations: list[ForecastEvaluationV1],
    resolution: ResolutionV1,
) -> dict[str, str]:
    """Bucket each forecast by how long before settlement it was made.

    ``unknown`` when the resolution carries no ``resolved_at`` — which is the
    normal case on the CLOB path, whose record states the winner but not when.
    Reported as a bucket rather than dropped, so a cohort table that can say
    nothing about timing says so instead of quietly shrinking.
    """
    resolved_at = resolution.resolved_at
    by_sequence = {
        (f.method.value, f.as_of_ingest_sequence): f.as_of_received_time for f in forecasts
    }
    keys: dict[str, str] = {}
    for evaluation in evaluations:
        sequence = int(evaluation.evaluation_id.rsplit("-", 1)[1])
        made_at = by_sequence.get((evaluation.forecast_method, sequence))
        keys[evaluation.evaluation_id] = _lead_bucket(made_at, resolved_at)
    return keys


def _lead_bucket(  # pragma: no cover - superseded v1 compatibility
    made_at: datetime | None, resolved_at: datetime | None
) -> str:
    if made_at is None or resolved_at is None:
        return "unknown"
    lead = resolved_at - made_at
    if lead < timedelta(0):
        return "after_resolution"
    if lead <= timedelta(hours=1):
        return "under-1h"
    if lead <= timedelta(days=1):
        return "1h-1d"
    if lead <= timedelta(days=7):
        return "1d-7d"
    return "over-7d"


def _limitations(  # pragma: no cover - superseded v1 compatibility
    evaluations: list[ForecastEvaluationV1], extra: tuple[str, ...]
) -> tuple[str, ...]:
    """Always non-empty, and specific to what this run actually was.

    Generated rather than left to a caller, because a limitations list a caller
    supplies is a limitations list a caller can forget. The three below hold for
    every evaluation this milestone can produce, and the first two would still
    hold for a much larger one.
    """
    stated = [
        "Scores are uncalibrated market baselines, not ARGOS probabilities "
        "(ADR-0006): raw_score is populated and p_yes is null.",
        "Forecasts within one capture are successive states of the same order "
        "book and are heavily autocorrelated, so the effective sample size is "
        "far below the forecast count.",
        f"No comparison against any non-market forecaster is made or implied "
        f"({len(evaluations)} scored forecasts).",
    ]
    if len({e.condition_id for e in evaluations}) <= 1:
        stated.append(
            "All forecasts come from a single market. One market is not a sample, "
            "and no calibration or edge claim can rest on it."
        )

    # A method whose every score is identical has an effective sample size of
    # one, whatever its count says. Detected rather than left to a reader,
    # because it is invisible in the metrics: the recorded capture's top of book
    # never moved (best bid 0.28, best ask 0.29 across all 38 states), so
    # midpoint and persistence produce byte-identical Brier, log loss and ECE --
    # two baselines agreeing perfectly, which reads as corroboration and is an
    # artifact.
    for method in sorted({e.forecast_method for e in evaluations}):
        scores = {e.score for e in evaluations if e.forecast_method == method}
        if len(scores) == 1:
            stated.append(
                f"Every '{method}' forecast carries the same score "
                f"({next(iter(scores))}), so its metrics are one number repeated "
                "and its effective sample size is 1. Any other method with the "
                "same constant score will agree with it perfectly, and that "
                "agreement is an artifact rather than corroboration."
            )
    return tuple([*stated, *extra])
