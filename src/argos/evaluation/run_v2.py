"""Auditable baseline evaluation over one stored replay trajectory."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from itertools import chain

import orjson

from argos.baselines import (
    BaselineMethod,
    MarketBaselineForecastV2,
    build_baseline_forecast_v2,
    quote_from_book_state,
)
from argos.clock import Clock, ReplayClock
from argos.compiler.contract import CompiledMarketContractV1
from argos.config.manifest import WorkingTreeStatus
from argos.config.settings import Settings
from argos.evaluation.bundle import (
    INFORMATION_STATE_HASH_VERSION,
    TRAJECTORY_HASH_VERSION,
    DecisionReason,
    EvaluationDecisionV1,
    EvaluationExclusionV1,
    EvaluationPolicyV2,
    EvaluationRunBundleV2,
    ExclusionReason,
    bundle_evidence_digest_v2,
    record_sha256,
)
from argos.evaluation.calibration import DEFAULT_BIN_COUNT, calibration_report
from argos.evaluation.numeric import require_bin_count, require_epsilon
from argos.evaluation.report import EvaluationReportV2, HeadlineStatus
from argos.evaluation.scoring import (
    DEFAULT_LOG_LOSS_EPSILON,
    EVALUATOR_VERSION,
    ForecastEvaluationV2,
    score_forecast_v2,
)
from argos.projections.dispatch import DispatchOutcomeKind, ObservationDispatcher, Watermark
from argos.replay.pacing import VirtualPacer
from argos.replay.reader import ArrivalKind, ReplayArrival, read_capture_arrivals
from argos.replay.session import ReplayMode, ReplaySession
from argos.resolution.gamma_resolution import ResolutionStatus, ResolutionV1, WinningOutcome
from argos.store.event_store import CompletionStatus, EventStore

__all__ = ["EvaluationResult", "evaluate_capture"]


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """The persisted bundle and convenient views of its child records."""

    bundle: EvaluationRunBundleV2

    @property
    def report(self) -> EvaluationReportV2:
        return self.bundle.report

    @property
    def forecasts(self) -> tuple[MarketBaselineForecastV2, ...]:
        return self.bundle.forecasts

    @property
    def evaluations(self) -> tuple[ForecastEvaluationV2, ...]:
        return self.bundle.evaluations

    @property
    def decisions(self) -> tuple[EvaluationDecisionV1, ...]:
        return self.bundle.decisions

    @property
    def exclusions(self) -> tuple[EvaluationExclusionV1, ...]:
        return self.bundle.exclusions


def evaluate_capture(
    *,
    store: EventStore,
    capture_run_id: str,
    resolution: ResolutionV1,
    token_id: str,
    settings: Settings,
    clock: Clock,
    evaluation_run_id: str,
    contract: CompiledMarketContractV1 | None = None,
    methods: tuple[BaselineMethod, ...] = (
        BaselineMethod.MIDPOINT,
        BaselineMethod.DISPLAYED_PRICE,
        BaselineMethod.LAST_TRADE,
        BaselineMethod.PERSISTENCE,
    ),
    epsilon: Decimal = DEFAULT_LOG_LOSS_EPSILON,
    bin_count: int = DEFAULT_BIN_COUNT,
    code_revision: str | None = None,
    working_tree: WorkingTreeStatus = WorkingTreeStatus.UNKNOWN,
    extra_limitations: tuple[str, ...] = (),
    policy: EvaluationPolicyV2 | None = None,
) -> EvaluationResult:
    """Evaluate target information-state changes, never transport arrivals.

    A non-final resolution is refused. Missing contract or temporal evidence is
    represented as exclusions in a bundle with zero scored points, so the CLI
    still produces an auditable explanation without manufacturing a result.
    """
    require_epsilon(epsilon)
    require_bin_count(bin_count)
    selected_policy = policy or EvaluationPolicyV2()
    if resolution.winning_token_id is None:
        raise ValueError("a final resolution must name the winning token")
    if resolution.resolution_status is not ResolutionStatus.FINAL:
        raise ValueError(
            "only a final resolution may enter evaluation; proposed, disputed, "
            "and unknown statuses are not negative outcomes"
        )
    if contract is not None and contract.condition_id != resolution.condition_id:
        raise ValueError("the contract and resolution name different conditions")

    capture = store.get_capture_run(capture_run_id)
    if capture is None:
        raise ValueError(f"no such capture_run: {capture_run_id}")

    created_at = clock.now()
    dispatcher = ObservationDispatcher(watermark=Watermark())
    arrivals = read_capture_arrivals(store, capture_run_id)
    first = next(arrivals, None)
    session = ReplaySession(
        arrivals=arrivals if first is None else chain([first], arrivals),
        dispatcher=dispatcher,
        clock=ReplayClock(capture.started_at if first is None else first.received_time),
        mode=ReplayMode.ACCELERATED,
        pacer=VirtualPacer(),
    )

    forecasts: list[MarketBaselineForecastV2] = []
    decisions: list[EvaluationDecisionV1] = []
    trajectory_material: list[dict[str, object]] = []
    previous_information_hash: str | None = None
    previous_midpoint: Decimal | None = None

    while True:
        step = session.step()
        if step is None:
            break
        arrival = step.arrival
        trajectory_material.append(_arrival_material(arrival))
        envelope = arrival.envelope

        if arrival.kind is ArrivalKind.REJECTED:
            assert arrival.rejection is not None
            decisions.append(
                _decision(
                    arrival,
                    reason=DecisionReason.REJECTED_ARRIVAL,
                    rejection_id=arrival.rejection.rejection_id,
                )
            )
            continue
        assert envelope is not None and step.outcome is not None
        dispatch_outcome = step.outcome

        terminal_reason = _terminal_decision_reason(dispatch_outcome.kind)
        if terminal_reason is not None:
            decisions.append(
                _decision(
                    arrival,
                    reason=terminal_reason,
                    observation_id=envelope.observation_id,
                    dispatch_outcome=dispatch_outcome.kind.value,
                )
            )
            continue
        if (envelope.condition_id, envelope.token_id) != (resolution.condition_id, token_id):
            decisions.append(
                _decision(
                    arrival,
                    reason=DecisionReason.IRRELEVANT_SCOPE,
                    observation_id=envelope.observation_id,
                    dispatch_outcome=dispatch_outcome.kind.value,
                )
            )
            continue

        projection = dispatcher.projections.get((resolution.condition_id, token_id))
        if projection is None or not projection.is_seeded:
            decisions.append(
                _decision(
                    arrival,
                    reason=DecisionReason.TARGET_NOT_SEEDED,
                    observation_id=envelope.observation_id,
                    dispatch_outcome=dispatch_outcome.kind.value,
                )
            )
            continue

        information_hash = _information_state_hash(dispatcher, resolution.condition_id, token_id)
        if information_hash == previous_information_hash:
            decisions.append(
                _decision(
                    arrival,
                    reason=DecisionReason.TARGET_STATE_UNCHANGED,
                    observation_id=envelope.observation_id,
                    dispatch_outcome=dispatch_outcome.kind.value,
                    information_state_hash=information_hash,
                )
            )
            continue
        previous_information_hash = information_hash
        decisions.append(
            _decision(
                arrival,
                reason=DecisionReason.INCLUDED_INFORMATION_CHANGE,
                observation_id=envelope.observation_id,
                dispatch_outcome=dispatch_outcome.kind.value,
                information_state_hash=information_hash,
                included=True,
            )
        )

        last_trade = dispatcher.last_trades.get((resolution.condition_id, token_id))
        quote = quote_from_book_state(
            projection.state(),
            quote_time=envelope.event_time,
            last_trade_price=last_trade.price if last_trade else None,
        )
        for method in methods:
            forecasts.append(
                build_baseline_forecast_v2(
                    method=method,
                    quote=quote,
                    as_of_received_time=arrival.received_time,
                    as_of_ingest_sequence=arrival.ingest_sequence,
                    previous_score=previous_midpoint,
                    evaluation_run_id=evaluation_run_id,
                    source_capture_run_id=capture_run_id,
                    source_observation_id=envelope.observation_id,
                    information_state_hash=information_hash,
                    market_id=resolution.market_id,
                    contract_id=contract.contract_id if contract else None,
                )
            )
        if quote.midpoint is not None:
            previous_midpoint = quote.midpoint

    counts = session.counts
    blockers = _headline_blockers(
        capture_completion=capture.completion_status,
        resolution=resolution,
        contract=contract,
        sequence_gaps=counts.sequence_gaps,
        received_time_regressions=counts.received_time_regressions,
        unhandled_payloads=dispatcher.counts.unhandled_payloads,
    )
    evaluations: list[ForecastEvaluationV2] = []
    exclusions: list[EvaluationExclusionV1] = []
    winning_outcome = _outcome_for(resolution, token_id)

    for forecast in forecasts:
        exclusion = _forecast_exclusion(forecast, resolution, blockers)
        if exclusion is not None:
            exclusions.append(exclusion)
            continue
        assert forecast.raw_score is not None and contract is not None
        evaluations.append(
            score_forecast_v2(
                forecast_id=forecast.forecast_id,
                evaluation_run_id=evaluation_run_id,
                contract_id=contract.contract_id,
                forecast_method=forecast.method.value,
                condition_id=forecast.condition_id,
                token_id=forecast.token_id,
                score=forecast.raw_score,
                resolution_id=resolution.resolution_id,
                winning_outcome=winning_outcome,
                calibration_status=forecast.calibration_status.value,
                created_at=created_at,
                epsilon=epsilon,
            )
        )

    eligible_targets = 1 if evaluations and not blockers else 0
    headline_reasons = list(blockers)
    headline_reasons.append("single_target_runner_cannot_establish_calibration")
    headline_status = (
        HeadlineStatus.ESTABLISHED if not headline_reasons else HeadlineStatus.NOT_ESTABLISHED
    )
    trajectory_hash = record_sha256(
        {"version": TRAJECTORY_HASH_VERSION, "arrivals": trajectory_material}
    )
    child_digests = {
        "forecasts": record_sha256([item.to_record() for item in forecasts]),
        "evaluations": record_sha256([item.to_record() for item in evaluations]),
        "decisions": record_sha256([item.to_record() for item in decisions]),
        "exclusions": record_sha256([item.to_record() for item in exclusions]),
    }
    trajectory_diagnostics = {
        method.value: calibration_report(
            evaluations, method=method.value, bin_count=bin_count
        ).as_record()
        for method in methods
    }
    report = EvaluationReportV2(
        evaluation_run_id=evaluation_run_id,
        created_at=created_at,
        code_revision=code_revision,
        working_tree=working_tree,
        config_fingerprint=settings.fingerprint(),
        settings_snapshot=settings.snapshot(),
        evaluator_version=EVALUATOR_VERSION,
        log_loss_epsilon=epsilon,
        calibration_bin_count=bin_count,
        source_capture_run_ids=(capture_run_id,),
        source_state_hash=dispatcher.state_hash(),
        source_trajectory_hash=trajectory_hash,
        source_trajectory_hash_version=TRAJECTORY_HASH_VERSION,
        source_completion_status=(
            capture.completion_status.value if capture.completion_status else None
        ),
        replay_counts={"arrivals": counts.as_record(), "dispatch": dispatcher.counts.as_record()},
        resolution_id=resolution.resolution_id,
        resolution_record_sha256=record_sha256(resolution.to_record()),
        resolution_status=resolution.resolution_status.value,
        resolution_normalizer_version=resolution.normalizer_version,
        resolution_cutoff=resolution.resolved_at,
        contract_id=contract.contract_id if contract else None,
        contract_record_sha256=record_sha256(contract.to_record()) if contract else None,
        evaluation_policy_version=selected_policy.schema_version,
        headline_status=headline_status,
        headline_reasons=tuple(dict.fromkeys(headline_reasons)),
        forecast_count=len(forecasts),
        abstention_count=sum(1 for item in forecasts if item.abstained),
        scored_count=len(evaluations),
        unresolved_count=0,
        abstention_reasons=_abstention_reasons(forecasts),
        calibration={},
        cohorts={},
        limitations=_limitations(evaluations, extra_limitations),
        arrival_count=counts.arrivals,
        target_information_state_count=sum(item.target_state_included for item in decisions),
        forecast_point_count=len(forecasts),
        scored_forecast_point_count=len(evaluations),
        resolved_target_count=1,
        headline_eligible_target_count=eligible_targets,
        trajectory_diagnostics=trajectory_diagnostics,
        child_record_digests=child_digests,
    )
    evidence_digest = bundle_evidence_digest_v2(
        evaluation_run_id=evaluation_run_id,
        policy=selected_policy,
        resolution=resolution,
        contract=contract,
        report=report,
        forecasts=forecasts,
        evaluations=evaluations,
        decisions=decisions,
        exclusions=exclusions,
    )
    bundle = EvaluationRunBundleV2(
        evaluation_run_id=evaluation_run_id,
        policy=selected_policy,
        resolution=resolution,
        contract=contract,
        report=report,
        forecasts=tuple(forecasts),
        evaluations=tuple(evaluations),
        decisions=tuple(decisions),
        exclusions=tuple(exclusions),
        evidence_digest=evidence_digest,
    )
    return EvaluationResult(bundle=bundle)


def _terminal_decision_reason(kind: DispatchOutcomeKind) -> DecisionReason | None:
    return {
        DispatchOutcomeKind.SKIPPED_DUPLICATE: DecisionReason.DUPLICATE_DELIVERY,
        DispatchOutcomeKind.UNHANDLED_PAYLOAD: DecisionReason.UNHANDLED_PAYLOAD,
        DispatchOutcomeKind.UNSCOPED: DecisionReason.UNSCOPED,
    }.get(kind)


def _arrival_material(arrival: ReplayArrival) -> dict[str, object]:
    return {
        "ingest_sequence": arrival.ingest_sequence,
        "kind": arrival.kind.value,
        "received_time": arrival.received_time.isoformat(),
        "envelope": arrival.envelope.to_record() if arrival.envelope else None,
        "rejection": arrival.rejection.to_record() if arrival.rejection else None,
    }


def _decision(
    arrival: ReplayArrival,
    *,
    reason: DecisionReason,
    observation_id: str | None = None,
    rejection_id: str | None = None,
    dispatch_outcome: str | None = None,
    information_state_hash: str | None = None,
    included: bool = False,
) -> EvaluationDecisionV1:
    return EvaluationDecisionV1(
        ingest_sequence=arrival.ingest_sequence,
        arrival_kind=arrival.kind.value,
        observation_id=observation_id,
        rejection_id=rejection_id,
        dispatch_outcome=dispatch_outcome,
        target_state_included=included,
        reason=reason,
        information_state_hash=information_state_hash,
    )


def _information_state_hash(
    dispatcher: ObservationDispatcher, condition_id: str, token_id: str
) -> str:
    projection = dispatcher.projections[(condition_id, token_id)]
    last_trade = dispatcher.last_trades.get((condition_id, token_id))
    material = {
        "version": INFORMATION_STATE_HASH_VERSION,
        "condition_id": condition_id,
        "token_id": token_id,
        "book_digest": projection.digest(),
        "last_trade": (
            {
                "price": str(last_trade.price),
                "observation_id": last_trade.source_observation_id,
                "event_time": last_trade.event_time.isoformat() if last_trade.event_time else None,
                "received_time": last_trade.received_time.isoformat(),
            }
            if last_trade
            else None
        ),
    }
    return hashlib.sha256(orjson.dumps(material, option=orjson.OPT_SORT_KEYS)).hexdigest()


def _headline_blockers(
    *,
    capture_completion: CompletionStatus | None,
    resolution: ResolutionV1,
    contract: CompiledMarketContractV1 | None,
    sequence_gaps: int,
    received_time_regressions: int,
    unhandled_payloads: int,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if capture_completion is not CompletionStatus.COMPLETED:
        reasons.append("capture_incomplete")
    if resolution.resolved_at is None:
        reasons.append("resolution_time_unknown")
    if contract is None:
        reasons.append("contract_identity_missing")
    if sequence_gaps:
        reasons.append("replay_sequence_gaps")
    if received_time_regressions:
        reasons.append("replay_received_time_regressions")
    if unhandled_payloads:
        reasons.append("replay_unhandled_payloads")
    return tuple(reasons)


def _forecast_exclusion(
    forecast: MarketBaselineForecastV2,
    resolution: ResolutionV1,
    blockers: tuple[str, ...],
) -> EvaluationExclusionV1 | None:
    if forecast.abstained:
        reason = forecast.abstention_reason.value if forecast.abstention_reason else "unknown"
        return EvaluationExclusionV1(
            forecast_id=forecast.forecast_id,
            reason=ExclusionReason.ABSTAINED,
            detail=f"baseline abstained: {reason}",
        )
    blocker_map = (
        ("capture_incomplete", ExclusionReason.CAPTURE_INCOMPLETE, "capture is incomplete"),
        (
            "contract_identity_missing",
            ExclusionReason.CONTRACT_IDENTITY_MISSING,
            "no persisted contract identifies the target rules",
        ),
        (
            "resolution_time_unknown",
            ExclusionReason.RESOLUTION_TIME_UNKNOWN,
            "resolution carries no exact settlement cutoff",
        ),
    )
    for key, reason, detail in blocker_map:
        if key in blockers:
            return EvaluationExclusionV1(
                forecast_id=forecast.forecast_id, reason=reason, detail=detail
            )
    if any(item.startswith("replay_") for item in blockers):
        return EvaluationExclusionV1(
            forecast_id=forecast.forecast_id,
            reason=ExclusionReason.REPLAY_INTEGRITY,
            detail="replay integrity counters are non-zero",
        )
    assert resolution.resolved_at is not None
    if forecast.as_of_received_time >= resolution.resolved_at:
        return EvaluationExclusionV1(
            forecast_id=forecast.forecast_id,
            reason=ExclusionReason.POST_RESOLUTION,
            detail="forecast was not made strictly before the resolution cutoff",
        )
    return None


def _outcome_for(resolution: ResolutionV1, token_id: str) -> WinningOutcome:
    return WinningOutcome.YES if resolution.winning_token_id == token_id else WinningOutcome.NO


def _abstention_reasons(forecasts: list[MarketBaselineForecastV2]) -> dict[str, int]:
    reasons: dict[str, int] = {}
    for forecast in forecasts:
        if forecast.abstained:
            key = forecast.abstention_reason.value if forecast.abstention_reason else "unknown"
            reasons[key] = reasons.get(key, 0) + 1
    return reasons


def _limitations(
    evaluations: list[ForecastEvaluationV2], extra: tuple[str, ...]
) -> tuple[str, ...]:
    return (
        "Scores are uncalibrated market baselines, not ARGOS probabilities (ADR-0006).",
        "Forecast points within one capture are autocorrelated trajectory diagnostics, "
        "not independent resolved targets.",
        "This runner evaluates one resolved target and cannot establish calibration. "
        "Two independent targets are only a structural floor; scientific sufficiency "
        "requires a predeclared multi-target protocol.",
        "No comparison against a non-market forecaster is made "
        f"({len(evaluations)} scored points).",
        *extra,
    )
