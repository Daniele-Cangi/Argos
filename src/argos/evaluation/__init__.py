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

from argos.evaluation.bundle import (
    EvaluationDecisionV1,
    EvaluationExclusionV1,
    EvaluationPolicyV1,
    EvaluationPolicyV2,
    EvaluationRunBundleV1,
    EvaluationRunBundleV2,
)
from argos.evaluation.calibration import (
    DEFAULT_BIN_COUNT,
    CalibrationBin,
    CalibrationReport,
    CohortReport,
    calibration_report,
    cohort_report,
    spread_bucket,
)
from argos.evaluation.numeric import (
    EVALUATION_DECIMAL_CONTEXT,
    EVALUATION_PRECISION,
    MAX_LOG_LOSS_EPSILON,
    evaluation_context,
    require_bin_count,
    require_epsilon,
)
from argos.evaluation.report import EvaluationReportV1, EvaluationReportV2, HeadlineStatus
from argos.evaluation.run_v2 import EvaluationResult, evaluate_capture
from argos.evaluation.scoring import (
    DEFAULT_LOG_LOSS_EPSILON,
    EVALUATOR_VERSION,
    ForecastEvaluationV1,
    ForecastEvaluationV2,
    brier_score,
    log_loss,
    score_forecast,
)

__all__ = [
    "DEFAULT_BIN_COUNT",
    "DEFAULT_LOG_LOSS_EPSILON",
    "EVALUATION_DECIMAL_CONTEXT",
    "EVALUATION_PRECISION",
    "EVALUATOR_VERSION",
    "MAX_LOG_LOSS_EPSILON",
    "CalibrationBin",
    "CalibrationReport",
    "CohortReport",
    "EvaluationDecisionV1",
    "EvaluationExclusionV1",
    "EvaluationPolicyV1",
    "EvaluationPolicyV2",
    "EvaluationReportV1",
    "EvaluationReportV2",
    "EvaluationResult",
    "EvaluationRunBundleV1",
    "EvaluationRunBundleV2",
    "ForecastEvaluationV1",
    "ForecastEvaluationV2",
    "HeadlineStatus",
    "brier_score",
    "calibration_report",
    "cohort_report",
    "evaluate_capture",
    "evaluation_context",
    "log_loss",
    "require_bin_count",
    "require_epsilon",
    "score_forecast",
    "spread_bucket",
]
