"""Proper scoring rules, with the clipping declared rather than applied quietly.

``docs/05_RESEARCH_PROTOCOL.md`` requires Brier, "log loss with pre-declared
clipping epsilon", and absolute error. The clipping is the part that has to be
handled carefully, because it is the one place where a scoring rule can be made
to flatter a forecaster without anybody noticing.

Log loss is unbounded at 0 and 1. A market that displayed `1.00` for an outcome
that did not happen scores infinity, which is arithmetically correct and useless
in an average — one such market and the mean is infinite regardless of the other
ten thousand. Every implementation therefore clips, and the honest ones say by
how much and how often, because the clip is a **decision that changes the
number**: at ε = 1e-6 that market scores 13.8, at ε = 1e-3 it scores 6.9, and a
report that omits ε has not reported log loss at all.

So :class:`ForecastEvaluationV1` carries ``log_loss_epsilon`` on the record, and
:func:`score_forecast` counts whether *this* forecast was clipped. A calibration
report can then say "log loss 0.31, ε = 1e-6, 4 of 2,000 forecasts clipped"
instead of "log loss 0.31".

Brier and absolute error are unclipped: both are bounded on [0, 1] and need no
rescue.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from pydantic import Field, field_validator, model_validator

from argos.clock import ensure_utc
from argos.domain.orderbook import MAX_PRICE, MIN_PRICE
from argos.domain.versioning import VersionedModel
from argos.resolution.gamma_resolution import WinningOutcome

__all__ = [
    "DEFAULT_LOG_LOSS_EPSILON",
    "ForecastEvaluationV1",
    "brier_score",
    "log_loss",
    "score_forecast",
]

DEFAULT_LOG_LOSS_EPSILON = Decimal("0.000001")
"""1e-6, declared here and recorded on every evaluation that uses it.

Chosen rather than derived: it caps a single maximally-wrong forecast at
``-ln(1e-6) ≈ 13.8``, which is large enough to dominate an average the way a
confident error should and small enough not to. There is no principled optimum,
which is exactly why it must be stated on the record instead of buried in a
default nobody reads.
"""


def brier_score(score: Decimal, outcome: int) -> Decimal:
    """``(score - outcome)²``. Bounded on [0, 1]; no clipping needed."""
    difference = score - outcome
    return difference * difference


def log_loss(score: Decimal, outcome: int, *, epsilon: Decimal) -> tuple[Decimal, bool]:
    """``-ln(p)`` for the realized side, with ``score`` clipped into
    ``[epsilon, 1 - epsilon]``.

    Returns the loss **and whether this forecast was clipped**, so a report can
    say how much of its own number came from the clip rather than from the
    forecasts. A log loss whose clipping rate is unknown is a log loss whose
    worst cases are unknown.
    """
    low, high = epsilon, Decimal(1) - epsilon
    clipped = score < low or score > high
    bounded = min(max(score, low), high)
    realized = bounded if outcome == 1 else Decimal(1) - bounded
    return -_ln(realized), clipped


def _ln(value: Decimal) -> Decimal:
    """Natural log at `Decimal` precision.

    `Decimal.ln` rather than `math.log`, so the whole scoring path stays in the
    exact arithmetic the rest of this repository uses at boundaries. Converting
    to `float` here would reintroduce, in the metric itself, the rounding that
    `docs/04_DATA_CONTRACTS.md` keeps out of every price.
    """
    return value.ln()


class ForecastEvaluationV1(VersionedModel):
    """One forecast scored against one resolution."""

    schema_version: ClassVar[str] = "forecast_evaluation.v1"

    evaluation_id: str = Field(min_length=1)
    forecast_method: str = Field(min_length=1)
    condition_id: str = Field(min_length=1)
    token_id: str = Field(min_length=1)
    resolution_id: str = Field(min_length=1)

    score: Decimal = Field(ge=MIN_PRICE, le=MAX_PRICE)
    """The forecast's own number, before clipping. Kept so the clip is
    reversible and auditable rather than baked into the record."""

    outcome_yes: int = Field(ge=0, le=1)
    """0 or 1, from the resolution. Never inferred from anything else."""

    brier_score: Decimal
    log_loss: Decimal
    log_loss_epsilon: Decimal = Field(gt=0)
    log_loss_was_clipped: bool
    absolute_error: Decimal

    calibration_status: str = Field(min_length=1)
    """Carried onto the evaluation so a report cannot present a scored
    uncalibrated baseline as a scored probability (ADR-0006)."""

    evaluator_version: str = Field(min_length=1)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _anchor(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _scores_are_recomputable(self) -> ForecastEvaluationV1:
        """Refuse a record whose metrics disagree with its own inputs.

        The same "recomputable, never drifting" discipline
        ``PriceLevelChangeV1`` applies to ``kind`` versus ``size``. A stored
        evaluation is the artifact a claim is made from; one that can carry a
        Brier score unrelated to its own forecast and outcome is a claim nobody
        can check.
        """
        expected_brier = brier_score(self.score, self.outcome_yes)
        if self.brier_score != expected_brier:
            raise ValueError(
                f"brier_score {self.brier_score} disagrees with its own inputs ({expected_brier})"
            )
        expected_absolute = abs(self.score - self.outcome_yes)
        if self.absolute_error != expected_absolute:
            raise ValueError("absolute_error disagrees with its own inputs")
        expected_loss, expected_clip = log_loss(
            self.score, self.outcome_yes, epsilon=self.log_loss_epsilon
        )
        if self.log_loss != expected_loss or self.log_loss_was_clipped != expected_clip:
            raise ValueError("log_loss disagrees with its own inputs and declared epsilon")
        return self


EVALUATOR_VERSION = "argos-baseline-evaluator/1"


def score_forecast(
    *,
    forecast_method: str,
    condition_id: str,
    token_id: str,
    score: Decimal,
    resolution_id: str,
    winning_outcome: WinningOutcome,
    calibration_status: str,
    created_at: datetime,
    epsilon: Decimal = DEFAULT_LOG_LOSS_EPSILON,
) -> ForecastEvaluationV1:
    """Score one forecast against one determined outcome.

    Takes a :class:`WinningOutcome` rather than an ``int``, so there is no call
    site at which "unresolved" could be passed as 0. That enum has no
    ``UNKNOWN`` member for exactly this reason: the M4 exit criterion
    "unresolved markets are not scored as negatives" is enforced by the type,
    not by a check somebody has to remember.
    """
    outcome = 1 if winning_outcome is WinningOutcome.YES else 0
    loss, clipped = log_loss(score, outcome, epsilon=epsilon)
    return ForecastEvaluationV1(
        evaluation_id=f"evaluation-{forecast_method}-{token_id}-{resolution_id}",
        forecast_method=forecast_method,
        condition_id=condition_id,
        token_id=token_id,
        resolution_id=resolution_id,
        score=score,
        outcome_yes=outcome,
        brier_score=brier_score(score, outcome),
        log_loss=loss,
        log_loss_epsilon=epsilon,
        log_loss_was_clipped=clipped,
        absolute_error=abs(score - outcome),
        calibration_status=calibration_status,
        evaluator_version=EVALUATOR_VERSION,
        created_at=created_at,
    )
