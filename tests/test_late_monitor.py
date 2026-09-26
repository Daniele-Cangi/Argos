from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import orjson
import pytest

from argos.baselines import BaselineMethod, MarketQuoteV1, build_baseline_forecast_v2
from argos.clock import ensure_utc
from argos.config.manifest import WorkingTreeStatus
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.evaluation import (
    AcrossTargetWeighting,
    CutoffBasis,
    EvidenceArtifactKind,
    FrozenForecastSnapshotV1,
    LateLifecycleMonitor,
    LateMonitoringScheduleV1,
    LateScoreDisposition,
    LateScoringResultV1,
    LifecycleObservationV1,
    ProspectiveExperimentProtocolV2,
    ProspectiveTargetV1,
    StandaloneLastTradePolicy,
    WithinTargetAggregation,
    build_frozen_forecast_snapshot_id,
    build_late_monitoring_schedule_id,
    pending_late_resolution_score,
    score_late_final_outcome,
    verify_late_outcome_archives,
)
from argos.evaluation.prospective import build_target_id, persist_evidence_record
from argos.sources.gamma import GammaResponse

BASE = datetime(2026, 9, 1, tzinfo=UTC)
START = BASE + timedelta(hours=1)
END = START + timedelta(hours=1)
DEADLINE = END + timedelta(days=1)
CONDITION = "0x" + "a" * 64
EXPERIMENT = "late-monitor-integration-test-v1"


class _Clock:
    def __init__(self, now: datetime) -> None:
        self._now = ensure_utc(now)

    def now(self) -> datetime:
        return self._now

    def set(self, now: datetime) -> None:
        self._now = ensure_utc(now)

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class _Source:
    def __init__(self, clock: _Clock, responses: list[GammaResponse | Exception]) -> None:
        self.clock = clock
        self.responses = responses
        self.calls = 0

    async def get_market(self, market_id: str) -> GammaResponse:
        assert market_id == "market-1"
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            self.clock.advance(timedelta(seconds=3))
            raise response
        self.clock.set(response.provenance.retrieved_at)
        return response


def _protocol() -> ProspectiveExperimentProtocolV2:
    return ProspectiveExperimentProtocolV2(
        experiment_id=EXPERIMENT,
        declared_at=BASE,
        target_population="synthetic public binary targets",
        inclusion_rules=("open binary market",),
        exclusion_rules=("missing contract",),
        market_selection_mechanism="deterministic test fixture",
        observation_window_start=START,
        observation_window_end=END,
        stopping_rule="bounded capture then asynchronous finality",
        structural_minimum_independent_resolved_targets=2,
        minimum_intended_resolved_target_count=2,
        scientific_minimum_resolved_target_count=30,
        minimum_target_count_rationale="integration fixture; not a calibration sample",
        within_target_aggregation=WithinTargetAggregation.LAST_ADMISSIBLE_PRE_CUTOFF_PER_METHOD,
        across_target_weighting=AcrossTargetWeighting.EQUAL_RESOLVED_TARGET,
        metrics=("brier_score", "log_loss", "absolute_error"),
        calibration_bin_edges=(Decimal("0"), Decimal("0.5"), Decimal("1")),
        log_loss_epsilon=Decimal("0.000001"),
        uncertainty_reporting="none for a structural fixture",
        missingness_treatment="pending remains in denominator",
        abstention_treatment="record abstentions without score",
        disputed_unresolved_treatment="no score until final",
        category_dispersion_requirement="two categories for calibration",
        outcome_dispersion_requirement="five YES and NO outcomes for calibration",
        minimum_category_count_for_calibration=2,
        minimum_yes_outcomes_for_calibration=5,
        minimum_no_outcomes_for_calibration=5,
        protocol_sufficiency_rule="thirty resolved and required dispersion",
        cutoff_basis=CutoffBasis.FIRST_OBSERVED_FINAL_SETTLEMENT,
        standalone_last_trade_policy=StandaloneLastTradePolicy.EXCLUDE_TARGET,
        target_replacement_policy="none",
        code_revision="a" * 40,
        working_tree=WorkingTreeStatus.CLEAN,
        config_fingerprint="b" * 64,
        lifecycle_deadline=DEADLINE,
        lifecycle_poll_interval_seconds=300,
        capture_max_seconds_per_target=120,
        capture_max_frames_per_target=500,
        capture_separate_database_per_target=True,
        capture_subscribe_both_tokens=True,
        capture_raw_archive=True,
    )


def _response(payload: dict[str, Any], retrieved_at: datetime) -> GammaResponse:
    raw = orjson.dumps(payload)
    provenance = SourceProvenanceV1(
        source="gamma",
        endpoint="https://gamma-api.polymarket.com/markets/market-1",
        http_status=200,
        retrieved_at=retrieved_at,
        raw_sha256=sha256_hex(raw),
        byte_length=len(raw),
    )
    return GammaResponse(provenance=provenance, raw=raw, payload=payload)


def _pending_payload() -> dict[str, Any]:
    return {
        "id": "market-1",
        "conditionId": CONDITION,
        "closed": False,
        "outcomePrices": ["0.5", "0.5"],
        "clobTokenIds": ["11", "22"],
        "umaResolutionStatuses": ["proposed"],
    }


def _final_payload(resolved_at: datetime) -> dict[str, Any]:
    return {
        "id": "market-1",
        "conditionId": CONDITION,
        "closed": True,
        "outcomePrices": ["1", "0"],
        "clobTokenIds": ["11", "22"],
        "umaResolutionStatuses": ["proposed", "resolved"],
        "updatedAt": resolved_at.isoformat(),
    }


def _monitor(
    tmp_path: Path, clock: _Clock, responses: list[GammaResponse | Exception]
) -> LateLifecycleMonitor:
    evidence = tmp_path / "evidence"
    protocol = _protocol()
    protocol_receipt = persist_evidence_record(
        evidence,
        record=protocol,
        experiment_id=EXPERIMENT,
        artifact_kind=EvidenceArtifactKind.EXPERIMENT_PROTOCOL,
        artifact_id=EXPERIMENT,
        persisted_at=BASE,
    )
    target_id = build_target_id(
        experiment_id=EXPERIMENT,
        market_id="market-1",
        condition_id=CONDITION,
        yes_token_id="11",
        no_token_id="22",
    )
    target = ProspectiveTargetV1(
        target_id=target_id,
        experiment_id=EXPERIMENT,
        selection_rank=1,
        selected_at=START - timedelta(minutes=10),
        market_id="market-1",
        condition_id=CONDITION,
        yes_token_id="11",
        no_token_id="22",
        category="synthetic",
        cutoff_basis=CutoffBasis.FIRST_OBSERVED_FINAL_SETTLEMENT,
        market_record_sha256="c" * 64,
        market_receipt_id="market-receipt",
        contract_id="contract-1",
        contract_record_sha256="d" * 64,
        contract_receipt_id="contract-receipt",
    )
    target_receipt = persist_evidence_record(
        evidence,
        record=target,
        experiment_id=EXPERIMENT,
        artifact_kind=EvidenceArtifactKind.TARGET_DECLARATION,
        artifact_id=target.target_id,
        persisted_at=target.selected_at + timedelta(seconds=1),
    )
    forecast_at = START + timedelta(minutes=5)
    quote = MarketQuoteV1(
        condition_id=CONDITION,
        token_id="11",
        best_bid=Decimal("0.4"),
        best_ask=Decimal("0.6"),
        best_bid_size=Decimal("10"),
        best_ask_size=Decimal("10"),
        midpoint=Decimal("0.5"),
        spread=Decimal("0.2"),
        last_trade_price=Decimal("0.49"),
        bid_levels=1,
        ask_levels=1,
        quote_time=forecast_at,
    )
    forecasts = tuple(
        build_baseline_forecast_v2(
            method=method,
            quote=quote,
            as_of_received_time=forecast_at,
            as_of_ingest_sequence=1,
            previous_score=Decimal("0.5"),
            evaluation_run_id="evaluation-run-1",
            source_capture_run_id="capture-run-1",
            source_observation_id="capture-observation-1",
            information_state_hash="e" * 64,
            market_id="market-1",
            contract_id="contract-1",
        )
        for method in BaselineMethod
    )
    frozen_at = forecast_at + timedelta(seconds=1)
    capture_run_id = "capture-run-1"
    capture_manifest_sha256 = "f" * 64
    code_revision = protocol.code_revision
    config_fingerprint = protocol.config_fingerprint
    snapshot = FrozenForecastSnapshotV1(
        snapshot_id=build_frozen_forecast_snapshot_id(
            target=target,
            target_receipt=target_receipt,
            capture_run_id=capture_run_id,
            capture_manifest_sha256=capture_manifest_sha256,
            code_revision=code_revision,
            config_fingerprint=config_fingerprint,
            forecasts=forecasts,
            frozen_at=frozen_at,
        ),
        target=target,
        target_receipt=target_receipt,
        capture_run_id=capture_run_id,
        capture_manifest_sha256=capture_manifest_sha256,
        code_revision=code_revision,
        config_fingerprint=config_fingerprint,
        forecasts=forecasts,
        frozen_at=frozen_at,
    )
    snapshot_receipt = persist_evidence_record(
        evidence,
        record=snapshot,
        experiment_id=EXPERIMENT,
        artifact_kind=EvidenceArtifactKind.FROZEN_FORECAST_SNAPSHOT,
        artifact_id=snapshot.snapshot_id,
        persisted_at=frozen_at + timedelta(seconds=1),
    )
    created_at = DEADLINE + timedelta(seconds=10)
    effective_at = created_at + timedelta(seconds=10)
    first_poll_at = effective_at
    schedule = LateMonitoringScheduleV1(
        schedule_id=build_late_monitoring_schedule_id(
            experiment_id=EXPERIMENT,
            target_id=target.target_id,
            protocol_receipt_id=protocol_receipt.receipt_id,
            effective_at=effective_at,
            first_poll_at=first_poll_at,
            poll_interval_seconds=60,
            maximum_gap_multiple=2,
            owner_code_revision="1" * 40,
            config_fingerprint="2" * 64,
        ),
        experiment_id=EXPERIMENT,
        target_id=target.target_id,
        protocol_receipt_id=protocol_receipt.receipt_id,
        effective_at=effective_at,
        first_poll_at=first_poll_at,
        poll_interval_seconds=60,
        maximum_gap_multiple=2,
        owner_code_revision="1" * 40,
        config_fingerprint="2" * 64,
        created_at=created_at,
    )
    schedule_receipt = persist_evidence_record(
        evidence,
        record=schedule,
        experiment_id=EXPERIMENT,
        artifact_kind=EvidenceArtifactKind.LATE_MONITORING_SCHEDULE,
        artifact_id=schedule.schedule_id,
        persisted_at=created_at + timedelta(seconds=1),
    )
    return LateLifecycleMonitor(
        protocol=protocol,
        protocol_receipt=protocol_receipt,
        target=target,
        target_receipt=target_receipt,
        snapshot=snapshot,
        snapshot_receipt=snapshot_receipt,
        schedule=schedule,
        schedule_receipt=schedule_receipt,
        source=_Source(clock, responses),
        clock=clock,
        source_archive=tmp_path / "source",
        evidence_archive=evidence,
        checkpoint_path=tmp_path / "monitor" / "checkpoint.json",
        lock_path=tmp_path / "monitor" / "owner.lock",
    )


@pytest.mark.anyio
async def test_late_monitor_keeps_pending_unscored_then_scores_actual_final_cutoff(
    tmp_path: Path,
) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    first_at = schedule_at + timedelta(seconds=1)
    next_at = first_at + timedelta(seconds=61)
    resolved_at = DEADLINE + timedelta(seconds=30)
    clock = _Clock(schedule_at)
    monitor = _monitor(
        tmp_path,
        clock,
        [
            _response(_pending_payload(), first_at),
            _response(_final_payload(resolved_at), next_at),
        ],
    )

    first = await monitor.poll_once()
    assert first.poll_performed
    assert first.progress.status.value == "pending_resolution"
    assert first.next_poll_at is not None
    pending_score = pending_late_resolution_score(
        monitor.snapshot,
        latest_observation=first.observation,
        created_at=first_at + timedelta(seconds=1),
    )
    assert pending_score.disposition is LateScoreDisposition.PENDING_RESOLUTION
    assert pending_score.evaluations == ()
    assert LateScoringResultV1.from_record(pending_score.to_record()) == pending_score

    clock.set(first.next_poll_at)
    second = await monitor.poll_once()
    assert second.poll_performed
    assert second.outcome is not None
    assert second.observation is not None
    assert second.outcome.selected_cutoff == second.observation.retrieved_at == next_at
    assert second.outcome.selected_cutoff > DEADLINE
    assert second.outcome.selected_cutoff > resolved_at
    assert second.progress.status.value == "final"
    assert second.checkpoint.next_ordinal == 2
    verify_late_outcome_archives(
        second.outcome,
        evidence_dir=tmp_path / "evidence",
        source_dir=tmp_path / "source",
    )

    score = score_late_final_outcome(
        second.outcome,
        created_at=next_at + timedelta(seconds=1),
        epsilon=Decimal("0.000001"),
    )
    assert score.disposition is LateScoreDisposition.SCORED
    assert len(score.evaluations) == 4
    assert score.planned_forecast_count == 4
    assert score.late_outcome_id == second.outcome.late_outcome_id
    assert LateScoringResultV1.from_record(score.to_record()) == score

    terminal = await monitor.poll_once()
    assert not terminal.poll_performed
    assert terminal.outcome == second.outcome
    assert cast(_Source, monitor.source).calls == 2


@pytest.mark.anyio
async def test_failed_poll_is_a_durable_gap_and_does_not_consume_ordinal(tmp_path: Path) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    clock = _Clock(schedule_at)
    monitor = _monitor(
        tmp_path,
        clock,
        [
            TimeoutError("synthetic Gamma outage"),
            _response(_pending_payload(), schedule_at + timedelta(seconds=64)),
        ],
    )

    with pytest.raises(TimeoutError, match="synthetic Gamma outage") as error:
        await monitor.poll_once()
    assert not getattr(error.value, "__notes__", ())
    checkpoint = monitor.monitor.load()
    assert checkpoint.next_ordinal == 0
    assert len(checkpoint.gaps) == 1
    gap_records = []
    for path in (tmp_path / "evidence" / "argos_evidence").glob("*.raw.json"):
        record = orjson.loads(path.read_bytes())
        if record.get("schema_version") == "lifecycle_monitor_gap.v1":
            gap_records.append(record)
    assert len(gap_records) == 1
    assert gap_records[0]["attempted_ordinal"] == 1
    assert "TimeoutError" in gap_records[0]["reason"]

    clock.set(checkpoint.updated_at + timedelta(seconds=60))
    recovered = await monitor.poll_once()
    assert recovered.poll_performed
    assert recovered.observation is not None
    assert recovered.observation.ordinal == 1
    assert recovered.checkpoint.next_ordinal == 1
    assert len(monitor.monitor.load().gaps) == 1


@pytest.mark.anyio
async def test_resume_reconciles_archived_observation_before_polling_again(tmp_path: Path) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    first_at = schedule_at + timedelta(seconds=1)
    second_at = first_at + timedelta(seconds=61)
    clock = _Clock(schedule_at)
    monitor = _monitor(
        tmp_path,
        clock,
        [
            _response(_pending_payload(), first_at),
            _response(_pending_payload(), second_at),
        ],
    )

    first = await monitor.poll_once()
    assert first.observation is not None and first.observation.ordinal == 1
    # Model a crash after the evidence archive rename but before checkpoint rename.
    monitor.monitor.save(monitor._initial_checkpoint())
    clock.set(second_at - timedelta(seconds=1))
    resumed = await monitor.poll_once()
    assert resumed.observation is not None
    assert resumed.observation.ordinal == 2
    assert resumed.observation.previous_observation_id == first.observation.lifecycle_observation_id
    assert resumed.checkpoint.next_ordinal == 2
    assert cast(_Source, monitor.source).calls == 2
    lifecycle_records = []
    for path in (tmp_path / "evidence" / "argos_evidence").glob("*.raw.json"):
        record = orjson.loads(path.read_bytes())
        if record.get("schema_version") == LifecycleObservationV1.schema_version:
            lifecycle_records.append(record)
    assert len(lifecycle_records) == 2


@pytest.mark.anyio
async def test_resume_reconciles_archived_finality_before_stopping(tmp_path: Path) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    final_at = schedule_at + timedelta(seconds=1)
    clock = _Clock(schedule_at)
    monitor = _monitor(
        tmp_path,
        clock,
        [_response(_final_payload(DEADLINE - timedelta(hours=1)), final_at)],
    )

    first = await monitor.poll_once()
    assert first.outcome is not None
    # Simulate a crash after durable source/evidence writes but before checkpoint rename.
    monitor.monitor.save(monitor._initial_checkpoint())
    clock.set(final_at + timedelta(seconds=60))
    resumed = await monitor.poll_once()

    assert not resumed.poll_performed
    assert resumed.checkpoint.next_ordinal == 1
    assert resumed.observation == first.observation
    assert resumed.outcome == first.outcome
    assert cast(_Source, monitor.source).calls == 1


@pytest.mark.anyio
async def test_poll_before_frozen_first_poll_time_is_deferred(tmp_path: Path) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    clock = _Clock(schedule_at - timedelta(seconds=1))
    monitor = _monitor(tmp_path, clock, [])
    result = await monitor.poll_once()
    assert not result.poll_performed
    assert result.observation is None
    assert result.progress.status.value == "pending_resolution"
    assert result.next_poll_at == schedule_at
    assert cast(_Source, monitor.source).calls == 0
