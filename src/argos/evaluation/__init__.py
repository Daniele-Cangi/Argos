"""Scoring rules, calibration bins, and cohort reports (M4).

`scoring` implements Brier, log loss with a **declared** clipping epsilon, and
absolute error, and refuses to store a metric that disagrees with its own
inputs. `calibration` bins forecasts into a reliability curve where every bin
reports its count -- including the empty ones, because an absent bin and a zero
bin read identically in a table.

`score_forecast` takes a `WinningOutcome`, which has no `UNKNOWN` member: the
exit criterion "unresolved markets are not scored as negatives" is enforced by
the type rather than by a check somebody has to remember.
"""

from argos.evaluation.calibration import (
    DEFAULT_BIN_COUNT,
    CalibrationBin,
    CalibrationReport,
    CohortReport,
    calibration_report,
    cohort_report,
    spread_bucket,
)
from argos.evaluation.report import EvaluationReportV1
from argos.evaluation.scoring import (
    DEFAULT_LOG_LOSS_EPSILON,
    EVALUATOR_VERSION,
    ForecastEvaluationV1,
    brier_score,
    log_loss,
    score_forecast,
)

__all__ = [
    "DEFAULT_BIN_COUNT",
    "DEFAULT_LOG_LOSS_EPSILON",
    "EVALUATOR_VERSION",
    "CalibrationBin",
    "CalibrationReport",
    "CohortReport",
    "EvaluationReportV1",
    "ForecastEvaluationV1",
    "brier_score",
    "calibration_report",
    "cohort_report",
    "log_loss",
    "score_forecast",
    "spread_bucket",
]
