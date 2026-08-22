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
from argos.evaluation.claim_artifact import (
    ProspectiveClaimArtifactIndexV1,
    verify_published_claim_artifact,
)
from argos.evaluation.numeric import (
    EVALUATION_DECIMAL_CONTEXT,
    EVALUATION_PRECISION,
    MAX_LOG_LOSS_EPSILON,
    evaluation_context,
    require_bin_count,
    require_epsilon,
)
from argos.evaluation.prospective import (
    AcrossTargetWeighting,
    CutoffBasis,
    EvidenceArtifactKind,
    EvidencePersistenceReceiptV1,
    LifecycleObservationV1,
    ProspectiveExperimentProtocolV1,
    ProspectiveExperimentProtocolV2,
    ProspectiveTargetV1,
    ResolutionCutoffEvidenceV1,
    StandaloneLastTradePolicy,
    WithinTargetAggregation,
)
from argos.evaluation.prospective_aggregation import (
    CalibrationVerdict,
    MeasurementLayerVerdict,
    ProspectiveExperimentBundleV1,
    ProspectiveExperimentReportV1,
    ProspectiveTargetContributionV1,
    ProspectiveTargetExclusionReason,
    ProspectiveTargetExclusionV1,
    aggregate_prospective_experiment,
    build_target_exclusion,
)
from argos.evaluation.prospective_aggregation_v2 import (
    CaptureRejectionEvidenceV1,
    ProspectiveExperimentBundleV2,
    ProspectiveTargetExclusionV2,
    aggregate_prospective_experiment_v2,
    build_capture_rejection_evidence,
    build_target_exclusion_v2,
    prospective_experiment_digest_v2,
)
from argos.evaluation.prospective_aggregation_v3 import (
    ProspectiveExperimentBundleV3,
    aggregate_prospective_experiment_v3,
)
from argos.evaluation.prospective_bundle import EvaluationRunBundleV3
from argos.evaluation.prospective_bundle_v4 import EvaluationRunBundleV4
from argos.evaluation.prospective_terminal import (
    CaptureRunSummaryV1,
    LifecycleContinuityStatus,
    LifecyclePollEvidenceV1,
    ProspectiveExperimentBundleV4,
    ProspectiveTargetTerminalEvidenceV1,
    ProspectiveTerminalReportV1,
    ResolutionAdmissibilityStatus,
    TargetAccountingStatus,
    TargetTerminalDisposition,
    aggregate_prospective_terminal_experiment,
    build_capture_run_summary,
    build_lifecycle_poll_evidence,
    build_target_terminal_evidence,
    prospective_terminal_digest_v1,
)
from argos.evaluation.report import (
    EvaluationReportV1,
    EvaluationReportV2,
    EvaluationReportV3,
    HeadlineStatus,
)
from argos.evaluation.run_v2 import EvaluationResult, evaluate_capture
from argos.evaluation.run_v3 import ProspectiveEvaluationResult, evaluate_prospective_capture
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
    "AcrossTargetWeighting",
    "CalibrationBin",
    "CalibrationReport",
    "CalibrationVerdict",
    "CaptureRejectionEvidenceV1",
    "CaptureRunSummaryV1",
    "CohortReport",
    "CutoffBasis",
    "EvaluationDecisionV1",
    "EvaluationExclusionV1",
    "EvaluationPolicyV1",
    "EvaluationPolicyV2",
    "EvaluationReportV1",
    "EvaluationReportV2",
    "EvaluationReportV3",
    "EvaluationResult",
    "EvaluationRunBundleV1",
    "EvaluationRunBundleV2",
    "EvaluationRunBundleV3",
    "EvaluationRunBundleV4",
    "EvidenceArtifactKind",
    "EvidencePersistenceReceiptV1",
    "ForecastEvaluationV1",
    "ForecastEvaluationV2",
    "HeadlineStatus",
    "LifecycleContinuityStatus",
    "LifecycleObservationV1",
    "LifecyclePollEvidenceV1",
    "MeasurementLayerVerdict",
    "ProspectiveClaimArtifactIndexV1",
    "ProspectiveEvaluationResult",
    "ProspectiveExperimentBundleV1",
    "ProspectiveExperimentBundleV2",
    "ProspectiveExperimentBundleV3",
    "ProspectiveExperimentBundleV4",
    "ProspectiveExperimentProtocolV1",
    "ProspectiveExperimentProtocolV2",
    "ProspectiveExperimentReportV1",
    "ProspectiveTargetContributionV1",
    "ProspectiveTargetExclusionReason",
    "ProspectiveTargetExclusionV1",
    "ProspectiveTargetExclusionV2",
    "ProspectiveTargetTerminalEvidenceV1",
    "ProspectiveTargetV1",
    "ProspectiveTerminalReportV1",
    "ResolutionAdmissibilityStatus",
    "ResolutionCutoffEvidenceV1",
    "StandaloneLastTradePolicy",
    "TargetAccountingStatus",
    "TargetTerminalDisposition",
    "WithinTargetAggregation",
    "aggregate_prospective_experiment",
    "aggregate_prospective_experiment_v2",
    "aggregate_prospective_experiment_v3",
    "aggregate_prospective_terminal_experiment",
    "brier_score",
    "build_capture_rejection_evidence",
    "build_capture_run_summary",
    "build_lifecycle_poll_evidence",
    "build_target_exclusion",
    "build_target_exclusion_v2",
    "build_target_terminal_evidence",
    "calibration_report",
    "cohort_report",
    "evaluate_capture",
    "evaluate_prospective_capture",
    "evaluation_context",
    "log_loss",
    "prospective_experiment_digest_v2",
    "prospective_terminal_digest_v1",
    "require_bin_count",
    "require_epsilon",
    "score_forecast",
    "spread_bucket",
    "verify_published_claim_artifact",
]
