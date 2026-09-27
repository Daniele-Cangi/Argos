from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import orjson
import pytest

import argos.evaluation.late_monitor as late_monitor_module
from argos.baselines import (
    AbstentionReason,
    BaselineMethod,
    MarketBaselineForecastV2,
    MarketQuoteV1,
    build_baseline_forecast_v2,
)
from argos.clock import ensure_utc
from argos.config.manifest import WorkingTreeStatus
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.domain.versioning import SchemaVersionError
from argos.evaluation import (
    AcrossTargetWeighting,
    CutoffBasis,
    EvidenceArtifactKind,
    FrozenForecastSnapshotV1,
    LateFinalOutcomeV1,
    LateLifecycleMonitor,
    LateMonitoringScheduleV1,
    LateResolutionProgressV1,
    LateScoreDisposition,
    LateScoringResultV1,
    LifecycleMonitorGapEvidenceV1,
    LifecycleObservationV1,
    ProspectiveExperimentProtocolV2,
    ProspectiveTargetV1,
    StandaloneLastTradePolicy,
    WithinTargetAggregation,
    build_frozen_forecast_snapshot_id,
    build_late_final_outcome_id,
    build_late_monitoring_schedule_id,
    pending_late_resolution_score,
    score_late_final_outcome,
    verify_late_outcome_archives,
)
from argos.evaluation.late_monitor import LateResolutionStatus, _gap_id
from argos.evaluation.late_retrieval import load_lifecycle_poll_retrievals
from argos.evaluation.prospective import (
    build_lifecycle_observation_id,
    build_target_id,
    persist_evidence_record,
)
from argos.evaluation.scoring import score_forecast_v2
from argos.monitoring.resumable import ResumableMonitorCheckpointV1
from argos.resolution import ResolutionStatus, WinningOutcome
from argos.sources.gamma import GammaResponse
from argos.store.raw_archive import write_raw_payload

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


def _protocol(
    *, log_loss_epsilon: Decimal = Decimal("0.000001")
) -> ProspectiveExperimentProtocolV2:
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
        log_loss_epsilon=log_loss_epsilon,
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


def _response(
    payload: Any,
    retrieved_at: datetime,
    *,
    endpoint: str = "https://gamma-api.polymarket.com/markets/market-1",
    source: str = "gamma",
    reconstructed: bool = False,
    raw: bytes | None = None,
) -> GammaResponse:
    raw = raw if raw is not None else orjson.dumps(payload)
    provenance = SourceProvenanceV1(
        source=source,
        endpoint=endpoint,
        http_status=200,
        retrieved_at=retrieved_at,
        raw_sha256=sha256_hex(raw),
        byte_length=len(raw),
        reconstructed=reconstructed,
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
    tmp_path: Path,
    clock: _Clock,
    responses: list[GammaResponse | Exception],
    *,
    first_poll_at: datetime | None = None,
    log_loss_epsilon: Decimal = Decimal("0.000001"),
) -> LateLifecycleMonitor:
    evidence = tmp_path / "evidence"
    protocol = _protocol(log_loss_epsilon=log_loss_epsilon)
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
    first_poll_at = effective_at if first_poll_at is None else first_poll_at
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
            gamma_base_url="https://gamma-api.polymarket.com",
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
        gamma_base_url="https://gamma-api.polymarket.com",
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
        log_loss_epsilon=Decimal("0.00001"),
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
    assert pending_score.log_loss_epsilon is None
    pending_record = pending_score.to_record()
    assert "log_loss_epsilon" not in pending_record
    assert LateScoringResultV1.from_record(pending_record) == pending_score

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
    )
    assert score.disposition is LateScoreDisposition.SCORED
    assert len(score.evaluations) == 4
    assert score.planned_forecast_count == 4
    assert score.late_outcome_id == second.outcome.late_outcome_id
    assert score.log_loss_epsilon == monitor.protocol.log_loss_epsilon
    assert LateScoringResultV1.from_record(score.to_record()) == score

    with pytest.raises(ValueError, match="must match the frozen protocol"):
        score_late_final_outcome(
            second.outcome,
            created_at=next_at + timedelta(seconds=1),
            epsilon=Decimal("0.000001"),
        )

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
    restarted_clock = _Clock(second_at - timedelta(seconds=1))
    restarted = _monitor(
        tmp_path,
        restarted_clock,
        [_response(_pending_payload(), second_at)],
    )
    restarted.monitor.save(restarted._initial_checkpoint())
    resumed = await restarted.poll_once()
    assert resumed.observation is not None
    assert resumed.observation.ordinal == 2
    assert resumed.observation.previous_observation_id == first.observation.lifecycle_observation_id
    assert resumed.checkpoint.next_ordinal == 2
    assert cast(_Source, restarted.source).calls == 1
    assert len(restarted._load_chain()) == 2
    lifecycle_records = []
    for path in (tmp_path / "evidence" / "argos_evidence").glob("*.raw.json"):
        record = orjson.loads(path.read_bytes())
        if record.get("schema_version") == LifecycleObservationV1.schema_version:
            lifecycle_records.append(record)
    assert len(lifecycle_records) == 2


@pytest.mark.anyio
async def test_recovery_rejects_lifecycle_observation_claiming_non_gamma_source(
    tmp_path: Path,
) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    retrieved_at = schedule_at + timedelta(seconds=1)
    monitor = _monitor(
        tmp_path,
        _Clock(schedule_at),
        [_response(_pending_payload(), retrieved_at)],
    )
    polled = await monitor.poll_once()
    assert polled.observation is not None

    fields = polled.observation.model_dump(mode="python")
    fields["schema_version"] = LifecycleObservationV1.schema_version
    fields["source"] = "forged"
    fields["lifecycle_observation_id"] = build_lifecycle_observation_id(
        **{
            key: value
            for key, value in fields.items()
            if key not in {"schema_version", "lifecycle_observation_id"}
        }
    )
    forged = LifecycleObservationV1.from_record(fields)
    persist_evidence_record(
        monitor.evidence_archive,
        record=forged,
        experiment_id=EXPERIMENT,
        artifact_kind=EvidenceArtifactKind.LIFECYCLE_OBSERVATION,
        artifact_id=forged.lifecycle_observation_id,
        persisted_at=polled.receipt.persisted_at,
    )
    monitor._chain_cache = None

    with pytest.raises(ValueError, match="first-hand Gamma source bytes"):
        monitor._load_chain()


@pytest.mark.anyio
async def test_resume_rejects_checkpoint_head_that_disagrees_with_archive_prefix(
    tmp_path: Path,
) -> None:
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
    assert first.observation is not None
    checkpoint = first.checkpoint.model_copy(
        update={
            "last_receipt_id": "different-receipt",
            "last_record_sha256": "0" * 64,
        }
    )
    clock.set(first.next_poll_at or first_at)
    second = await monitor.poll_once()
    assert second.observation is not None and second.observation.ordinal == 2

    restarted = _monitor(tmp_path, _Clock(second_at + timedelta(seconds=1)), [])
    restarted.monitor.save(checkpoint)
    with pytest.raises(
        ValueError, match="checkpoint head disagrees with its durable lifecycle prefix"
    ):
        await restarted.poll_once()

    assert cast(_Source, restarted.source).calls == 0


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
    restarted = _monitor(tmp_path, _Clock(final_at + timedelta(seconds=60)), [])
    restarted.monitor.save(restarted._initial_checkpoint())
    resumed = await restarted.poll_once()

    assert not resumed.poll_performed
    assert resumed.checkpoint.next_ordinal == 1
    assert resumed.observation == first.observation
    assert resumed.outcome == first.outcome
    assert cast(_Source, restarted.source).calls == 0


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


@pytest.mark.anyio
async def test_poll_before_next_cadence_returns_last_pending_observation(tmp_path: Path) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    retrieved_at = schedule_at + timedelta(seconds=1)
    monitor = _monitor(
        tmp_path,
        _Clock(schedule_at),
        [_response(_pending_payload(), retrieved_at)],
    )
    first = await monitor.poll_once()
    assert first.next_poll_at is not None

    clock = cast(_Clock, monitor.clock)
    clock.set(first.next_poll_at - timedelta(seconds=1))
    deferred = await monitor.poll_once()

    assert not deferred.poll_performed
    assert deferred.observation == first.observation
    assert deferred.progress.status.value == "pending_resolution"
    assert deferred.next_poll_at == first.next_poll_at
    assert cast(_Source, monitor.source).calls == 1


@pytest.mark.anyio
async def test_late_first_poll_records_initial_missed_cadence_gap(tmp_path: Path) -> None:
    first_poll_at = DEADLINE + timedelta(seconds=20)
    late_start = first_poll_at + timedelta(seconds=121)
    clock = _Clock(first_poll_at)
    monitor = _monitor(
        tmp_path,
        clock,
        [_response(_pending_payload(), late_start)],
        first_poll_at=first_poll_at,
    )
    clock.set(late_start)

    result = await monitor.poll_once()

    assert result.observation is not None
    assert result.observation.ordinal == 1
    assert len(result.checkpoint.gaps) == 1
    initial_gap = result.checkpoint.gaps[0]
    assert initial_gap.attempted_ordinal == 0
    assert initial_gap.started_at == first_poll_at
    assert initial_gap.ended_at == late_start
    assert "not backfilled" in initial_gap.reason
    archived_gaps = monitor._load_gap_evidence()
    assert len(archived_gaps) == 1
    assert archived_gaps[0][0].attempted_ordinal == 1
    assert archived_gaps[0][0].started_at == first_poll_at
    assert archived_gaps[0][0].ended_at == late_start


@pytest.mark.anyio
async def test_regular_polls_use_the_indexed_chain_without_rescanning_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    reads = 0
    read_links = monitor._read_receipt_links

    def count_link_archive_reads() -> dict[int, Any]:
        nonlocal reads
        reads += 1
        return read_links()

    monkeypatch.setattr(monitor, "_read_receipt_links", count_link_archive_reads)
    first = await monitor.poll_once()
    assert first.next_poll_at is not None
    clock.set(first.next_poll_at)
    await monitor.poll_once()

    assert reads == 1


@pytest.mark.anyio
async def test_alternating_owners_refresh_the_cached_lifecycle_chain(tmp_path: Path) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    first_at = schedule_at + timedelta(seconds=1)
    second_at = first_at + timedelta(seconds=61)
    third_at = second_at + timedelta(seconds=61)
    first_clock = _Clock(schedule_at)
    first_owner = _monitor(
        tmp_path,
        first_clock,
        [
            _response(_pending_payload(), first_at),
            _response(_pending_payload(), third_at),
        ],
    )

    first = await first_owner.poll_once()
    assert first.next_poll_at is not None
    assert len(first_owner._load_chain()) == 1

    second_owner = _monitor(
        tmp_path,
        _Clock(first.next_poll_at),
        [_response(_pending_payload(), second_at)],
    )
    second = await second_owner.poll_once()
    assert second.next_poll_at is not None
    assert second.checkpoint.next_ordinal == 2

    first_clock.set(second.next_poll_at)
    third = await first_owner.poll_once()

    assert third.poll_performed
    assert third.observation is not None and third.observation.ordinal == 3
    assert third.checkpoint.next_ordinal == 3
    assert len(first_owner._load_chain()) == 3
    assert cast(_Source, first_owner.source).calls == 2
    assert cast(_Source, second_owner.source).calls == 1


@pytest.mark.anyio
async def test_cached_owner_recovers_another_owners_uncheckpointed_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    first_at = schedule_at + timedelta(seconds=1)
    second_at = first_at + timedelta(seconds=61)
    first_clock = _Clock(schedule_at)
    first_owner = _monitor(
        tmp_path,
        first_clock,
        [_response(_pending_payload(), first_at)],
    )
    first = await first_owner.poll_once()
    assert first.next_poll_at is not None
    assert len(first_owner._load_chain()) == 1

    second_owner = _monitor(
        tmp_path,
        _Clock(first.next_poll_at),
        [_response(_pending_payload(), second_at)],
    )
    original_save = second_owner.monitor.save

    def fail_second_checkpoint(checkpoint: ResumableMonitorCheckpointV1) -> None:
        if checkpoint.next_ordinal == 2:
            raise OSError("simulated checkpoint rename failure")
        original_save(checkpoint)

    monkeypatch.setattr(second_owner.monitor, "save", fail_second_checkpoint)
    with pytest.raises(OSError, match="simulated checkpoint rename failure"):
        await second_owner.poll_once()
    assert first_owner.monitor.load().next_ordinal == 1

    first_clock.set(second_at + timedelta(seconds=1))
    recovered = await first_owner.poll_once()

    assert not recovered.poll_performed
    assert recovered.observation is not None and recovered.observation.ordinal == 2
    assert recovered.checkpoint.next_ordinal == 2
    assert cast(_Source, first_owner.source).calls == 1
    assert len(first_owner._load_chain()) == 2


@pytest.mark.anyio
async def test_cached_owner_recovers_another_owners_uncheckpointed_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    first_at = schedule_at + timedelta(seconds=1)
    first_clock = _Clock(schedule_at)
    first_owner = _monitor(
        tmp_path,
        first_clock,
        [_response(_pending_payload(), first_at)],
    )
    first = await first_owner.poll_once()
    assert first.next_poll_at is not None
    assert first_owner._load_gap_evidence() == []

    second_clock = _Clock(first.next_poll_at)
    second_owner = _monitor(tmp_path, second_clock, [TimeoutError("Gamma unavailable")])
    original_save = second_owner.monitor.save

    def fail_gap_checkpoint(checkpoint: ResumableMonitorCheckpointV1) -> None:
        if checkpoint.gaps:
            raise OSError("simulated gap checkpoint rename failure")
        original_save(checkpoint)

    monkeypatch.setattr(second_owner.monitor, "save", fail_gap_checkpoint)
    with pytest.raises(OSError, match="simulated gap checkpoint rename failure"):
        await second_owner.poll_once()
    assert first_owner.monitor.load().gaps == ()

    first_clock.set(second_clock.now() + timedelta(seconds=1))
    recovered = await first_owner.poll_once()

    assert not recovered.poll_performed
    assert recovered.checkpoint.next_ordinal == 1
    assert len(recovered.checkpoint.gaps) == 1
    assert "Gamma unavailable" in recovered.checkpoint.gaps[0].reason
    assert len(first_owner._load_gap_evidence()) == 1
    assert cast(_Source, first_owner.source).calls == 1


@pytest.mark.parametrize(
    ("receipt_name", "kind_name", "record_name", "artifact_name"),
    [
        (
            "protocol_receipt",
            "EXPERIMENT_PROTOCOL",
            "protocol",
            "experiment_id",
        ),
        (
            "target_receipt",
            "TARGET_DECLARATION",
            "target",
            "target_id",
        ),
        (
            "snapshot_receipt",
            "FROZEN_FORECAST_SNAPSHOT",
            "snapshot",
            "snapshot_id",
        ),
        (
            "schedule_receipt",
            "LATE_MONITORING_SCHEDULE",
            "schedule",
            "schedule_id",
        ),
    ],
)
def test_foreign_experiment_receipt_cannot_bind_selected_evidence(
    tmp_path: Path, receipt_name: str, kind_name: str, record_name: str, artifact_name: str
) -> None:
    clock = _Clock(DEADLINE + timedelta(seconds=20))
    monitor = _monitor(tmp_path, clock, [])
    original_receipt = getattr(monitor, receipt_name)
    record = getattr(monitor, record_name)
    foreign_receipt = persist_evidence_record(
        tmp_path / "evidence",
        record=record,
        experiment_id="another-experiment",
        artifact_kind=getattr(EvidenceArtifactKind, kind_name),
        artifact_id=getattr(record, artifact_name),
        persisted_at=original_receipt.persisted_at,
    )

    setattr(monitor, receipt_name, foreign_receipt)
    with pytest.raises(ValueError, match="receipts do not match"):
        monitor._validate_bindings()


@pytest.mark.parametrize(
    ("receipt_name", "kind_name", "record_name", "artifact_name"),
    [
        (
            "target_receipt",
            "TARGET_DECLARATION",
            "target",
            "target_id",
        ),
        (
            "snapshot_receipt",
            "FROZEN_FORECAST_SNAPSHOT",
            "snapshot",
            "snapshot_id",
        ),
        (
            "schedule_receipt",
            "LATE_MONITORING_SCHEDULE",
            "schedule",
            "schedule_id",
        ),
    ],
)
def test_receipt_must_match_archived_provenance_timestamp(
    tmp_path: Path, receipt_name: str, kind_name: str, record_name: str, artifact_name: str
) -> None:
    monitor = _monitor(tmp_path, _Clock(DEADLINE + timedelta(seconds=20)), [])
    original_receipt = getattr(monitor, receipt_name)
    record = getattr(monitor, record_name)
    alternate_receipt = persist_evidence_record(
        tmp_path / "alternate-evidence",
        record=record,
        experiment_id=EXPERIMENT,
        artifact_kind=getattr(EvidenceArtifactKind, kind_name),
        artifact_id=getattr(record, artifact_name),
        persisted_at=original_receipt.persisted_at + timedelta(seconds=1),
    )

    setattr(monitor, receipt_name, alternate_receipt)
    with pytest.raises(ValueError, match="receipt disagrees with archived provenance"):
        monitor._validate_bindings()


def test_same_target_cannot_reuse_lifecycle_archive_under_another_schedule(
    tmp_path: Path,
) -> None:
    clock = _Clock(DEADLINE + timedelta(seconds=20))
    monitor = _monitor(tmp_path, clock, [])
    original = monitor.schedule
    gamma_base_url = "https://gamma.example"
    schedule = LateMonitoringScheduleV1(
        schedule_id=build_late_monitoring_schedule_id(
            experiment_id=original.experiment_id,
            target_id=original.target_id,
            protocol_receipt_id=original.protocol_receipt_id,
            effective_at=original.effective_at,
            first_poll_at=original.first_poll_at,
            poll_interval_seconds=original.poll_interval_seconds,
            maximum_gap_multiple=original.maximum_gap_multiple,
            owner_code_revision=original.owner_code_revision,
            config_fingerprint=original.config_fingerprint,
            gamma_base_url=gamma_base_url,
        ),
        experiment_id=original.experiment_id,
        target_id=original.target_id,
        protocol_receipt_id=original.protocol_receipt_id,
        effective_at=original.effective_at,
        first_poll_at=original.first_poll_at,
        poll_interval_seconds=original.poll_interval_seconds,
        maximum_gap_multiple=original.maximum_gap_multiple,
        owner_code_revision=original.owner_code_revision,
        config_fingerprint=original.config_fingerprint,
        gamma_base_url=gamma_base_url,
        created_at=original.created_at,
    )
    schedule_receipt = persist_evidence_record(
        tmp_path / "evidence",
        record=schedule,
        experiment_id=EXPERIMENT,
        artifact_kind=EvidenceArtifactKind.LATE_MONITORING_SCHEDULE,
        artifact_id=schedule.schedule_id,
        persisted_at=original.created_at + timedelta(seconds=1),
    )

    monitor.schedule = schedule
    monitor.schedule_receipt = schedule_receipt
    with pytest.raises(ValueError, match="different frozen schedule"):
        monitor._validate_bindings()


@pytest.mark.anyio
async def test_gamma_endpoint_must_match_the_frozen_origin(tmp_path: Path) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    clock = _Clock(schedule_at)
    monitor = _monitor(
        tmp_path,
        clock,
        [
            _response(
                _pending_payload(),
                schedule_at + timedelta(seconds=1),
                endpoint="https://evil.example/markets/market-1",
            )
        ],
    )

    with pytest.raises(ValueError, match="frozen Gamma origin"):
        await monitor.poll_once()
    checkpoint = monitor.monitor.load()
    assert checkpoint.next_ordinal == 0
    assert len(checkpoint.gaps) == 1


@pytest.mark.anyio
async def test_resolved_status_without_normalizable_settlement_remains_unknown(
    tmp_path: Path,
) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    retrieved_at = schedule_at + timedelta(seconds=1)
    clock = _Clock(schedule_at)
    payload = {
        **_pending_payload(),
        "closed": True,
        "outcomePrices": ["0.4", "0.6"],
        "umaResolutionStatuses": ["resolved"],
    }
    monitor = _monitor(tmp_path, clock, [_response(payload, retrieved_at)])

    result = await monitor.poll_once()

    assert result.observation is not None
    assert result.observation.finality.value == "unknown"
    assert result.observation.resolution_id is None
    assert result.outcome is None
    assert result.progress.status.value == "pending_resolution"
    assert result.progress.selected_cutoff is None


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("gamma_base_url", "http://gamma-api.polymarket.com", "HTTPS origin"),
        ("gamma_base_url", "https://user:secret@gamma-api.polymarket.com", "HTTPS origin"),
        ("gamma_base_url", "https://gamma-api.polymarket.com/api", "HTTPS origin"),
        ("gamma_base_url", "https://gamma-api.polymarket.com/;token=secret", "HTTPS origin"),
        ("gamma_base_url", "https://gamma-api.polymarket.com:invalid", "invalid port"),
        ("config_fingerprint", "z" * 64, "hexadecimal"),
        ("owner_code_revision", "g" * 40, "hexadecimal"),
        ("created_at", (DEADLINE + timedelta(seconds=21)).isoformat(), "persisted before"),
        ("first_poll_at", (DEADLINE + timedelta(seconds=19)).isoformat(), "cannot precede"),
        ("schedule_id", "not-the-content-id", "identity disagrees"),
    ],
)
def test_schedule_rejects_invalid_or_changed_frozen_fields(
    tmp_path: Path, field: str, value: Any, message: str
) -> None:
    monitor = _monitor(tmp_path, _Clock(DEADLINE + timedelta(seconds=20)), [])
    record = monitor.schedule.to_record()
    record[field] = value

    with pytest.raises(ValueError, match=message):
        LateMonitoringScheduleV1.from_record(record)


def test_schedule_canonicalizes_an_ipv6_gamma_origin(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path, _Clock(DEADLINE + timedelta(seconds=20)), [])
    current = monitor.schedule
    origin = "https://[2001:db8::1]:443"
    schedule = LateMonitoringScheduleV1(
        schedule_id=build_late_monitoring_schedule_id(
            experiment_id=current.experiment_id,
            target_id=current.target_id,
            protocol_receipt_id=current.protocol_receipt_id,
            effective_at=current.effective_at,
            first_poll_at=current.first_poll_at,
            poll_interval_seconds=current.poll_interval_seconds,
            maximum_gap_multiple=current.maximum_gap_multiple,
            owner_code_revision=current.owner_code_revision,
            config_fingerprint=current.config_fingerprint,
            gamma_base_url=origin,
        ),
        experiment_id=current.experiment_id,
        target_id=current.target_id,
        protocol_receipt_id=current.protocol_receipt_id,
        effective_at=current.effective_at,
        first_poll_at=current.first_poll_at,
        poll_interval_seconds=current.poll_interval_seconds,
        maximum_gap_multiple=current.maximum_gap_multiple,
        owner_code_revision=current.owner_code_revision,
        config_fingerprint=current.config_fingerprint,
        gamma_base_url=origin,
        created_at=current.created_at,
    )
    assert schedule.gamma_base_url == "https://[2001:db8::1]"


@pytest.mark.anyio
async def test_durable_gap_round_trips_and_rejects_incoherent_records(tmp_path: Path) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    monitor = _monitor(
        tmp_path,
        _Clock(schedule_at),
        [TimeoutError("synthetic outage")],
    )
    with pytest.raises(TimeoutError):
        await monitor.poll_once()

    gap_path = next(
        path
        for path in (tmp_path / "evidence" / "argos_evidence").glob("*.raw.json")
        if orjson.loads(path.read_bytes()).get("schema_version")
        == LifecycleMonitorGapEvidenceV1.schema_version
    )
    gap_record = orjson.loads(gap_path.read_bytes())
    gap = LifecycleMonitorGapEvidenceV1.from_record(gap_record)
    assert gap.attempted_ordinal == 1

    wrong_id = dict(gap_record, gap_id="wrong-gap-id")
    with pytest.raises(ValueError, match="identity disagrees"):
        LifecycleMonitorGapEvidenceV1.from_record(wrong_id)

    reversed_interval = dict(
        gap_record,
        started_at=(gap.ended_at + timedelta(seconds=1)).isoformat(),
        ended_at=gap.ended_at.isoformat(),
    )
    with pytest.raises(ValueError, match="cannot precede"):
        LifecycleMonitorGapEvidenceV1.from_record(reversed_interval)


@pytest.mark.anyio
async def test_progress_and_score_records_reject_pending_finality_claims(tmp_path: Path) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    first_at = schedule_at + timedelta(seconds=1)
    next_at = first_at + timedelta(seconds=61)
    monitor = _monitor(
        tmp_path,
        _Clock(schedule_at),
        [
            _response(_pending_payload(), first_at),
            _response(_final_payload(DEADLINE), next_at),
        ],
    )
    result = await monitor.poll_once()
    assert result.progress.status is LateResolutionStatus.PENDING_RESOLUTION
    pending = pending_late_resolution_score(
        monitor.snapshot,
        latest_observation=result.observation,
        created_at=schedule_at + timedelta(seconds=2),
    )

    bad_progress = result.progress.to_record()
    bad_progress["status"] = LateResolutionStatus.FINAL.value
    with pytest.raises(ValueError, match="final progress requires"):
        LateResolutionProgressV1.from_record(bad_progress)

    pending_with_cutoff = result.progress.to_record()
    pending_with_cutoff["late_outcome_id"] = "unjustified-final-outcome"
    with pytest.raises(ValueError, match="pending target cannot carry"):
        LateResolutionProgressV1.from_record(pending_with_cutoff)

    pending_finality = result.progress.to_record()
    pending_finality["last_observed_finality"] = ResolutionStatus.FINAL.value
    with pytest.raises(ValueError, match="final observation cannot be reported as pending"):
        LateResolutionProgressV1.from_record(pending_finality)

    bad_score = pending.to_record()
    bad_score["latest_finality"] = ResolutionStatus.FINAL.value
    with pytest.raises(ValueError, match="pending target cannot be scored"):
        LateScoringResultV1.from_record(bad_score)

    assert result.next_poll_at is not None
    cast(_Clock, monitor.clock).set(result.next_poll_at)
    final = await monitor.poll_once()
    assert final.observation is not None
    assert final.outcome is not None
    final_progress_without_observation = final.progress.to_record()
    final_progress_without_observation["last_observation_id"] = None
    with pytest.raises(ValueError, match="bound observation"):
        LateResolutionProgressV1.from_record(final_progress_without_observation)
    final_progress_without_outcome = final.progress.to_record()
    final_progress_without_outcome["late_outcome_id"] = None
    with pytest.raises(ValueError, match="bound outcome"):
        LateResolutionProgressV1.from_record(final_progress_without_outcome)
    with pytest.raises(ValueError, match="final observations must be scored"):
        pending_late_resolution_score(
            monitor.snapshot,
            latest_observation=final.observation,
            created_at=schedule_at + timedelta(seconds=2),
        )


@pytest.mark.parametrize("epsilon", ["0", "-0.01", "NaN", "0.5", "0.9", "0.000001", "0.00001"])
def test_pending_score_record_rejects_declared_log_loss_epsilon(
    tmp_path: Path, epsilon: str
) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    monitor = _monitor(tmp_path, _Clock(schedule_at), [])
    score = pending_late_resolution_score(
        monitor.snapshot,
        latest_observation=None,
        created_at=schedule_at + timedelta(seconds=1),
    )
    record = score.to_record()
    record["log_loss_epsilon"] = epsilon

    with pytest.raises(ValueError):
        LateScoringResultV1.from_record(record)


@pytest.mark.anyio
async def test_ineligible_final_resolution_is_rejected_before_observation_persistence(
    tmp_path: Path,
) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    retrieved_at = schedule_at + timedelta(seconds=1)
    monitor = _monitor(
        tmp_path,
        _Clock(schedule_at),
        [_response(_final_payload(START), retrieved_at)],
    )

    with pytest.raises(ValueError, match="forecast was frozen after the source terminal timestamp"):
        await monitor.poll_once()

    assert monitor._load_chain() == []
    checkpoint = monitor.monitor.load()
    assert checkpoint.next_ordinal == 0
    assert len(checkpoint.gaps) == 1


@pytest.mark.anyio
async def test_late_final_score_cannot_predate_the_observed_cutoff(tmp_path: Path) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    final_at = schedule_at + timedelta(seconds=1)
    monitor = _monitor(
        tmp_path,
        _Clock(schedule_at),
        [_response(_final_payload(DEADLINE), final_at)],
    )
    result = await monitor.poll_once()
    assert result.outcome is not None

    with pytest.raises(ValueError, match="cannot predate"):
        score_late_final_outcome(
            result.outcome,
            created_at=result.outcome.selected_cutoff - timedelta(seconds=1),
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_outcome", "requires a bound final outcome"),
        ("not_final", "requires a bound final outcome"),
        ("incomplete_accounting", "denominator disagrees with the frozen snapshot"),
        ("scored_without_evaluations", "requires at least one evaluation"),
        ("unscorable_with_evaluations", "cannot carry evaluations"),
        ("duplicate_evaluations", "unique frozen forecasts"),
        ("overlapping_abstention", "both scored and abstained"),
        ("epsilon_mismatch", "disagrees with the frozen protocol"),
        ("forecast_not_in_snapshot", "accounting does not match the frozen forecasts"),
        ("score_disagrees_with_snapshot", "late evaluation disagrees with its frozen forecast"),
        ("wrong_resolution", "disagree with their bound late outcome"),
        ("wrong_winning_side", "disagree with their bound late outcome"),
        ("wrong_identity", "identity disagrees"),
    ],
)
async def test_late_score_record_rejects_inconsistent_final_claims(
    tmp_path: Path, mutation: str, message: str
) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    monitor = _monitor(
        tmp_path,
        _Clock(schedule_at),
        [_response(_final_payload(DEADLINE), schedule_at + timedelta(seconds=1))],
    )
    result = await monitor.poll_once()
    assert result.outcome is not None
    score = score_late_final_outcome(
        result.outcome,
        created_at=result.outcome.selected_cutoff + timedelta(seconds=1),
    )
    record = score.to_record()
    if mutation == "missing_outcome":
        record["late_outcome_id"] = None
    elif mutation == "not_final":
        record["latest_finality"] = ResolutionStatus.PROPOSED.value
    elif mutation == "incomplete_accounting":
        record["planned_forecast_count"] += 1
    elif mutation == "scored_without_evaluations":
        record["evaluations"] = []
        record["abstained_forecast_ids"] = ["a", "b", "c", "d"]
    elif mutation == "unscorable_with_evaluations":
        record["disposition"] = LateScoreDisposition.FINAL_UNSCORABLE.value
    elif mutation == "duplicate_evaluations":
        record["evaluations"][1] = record["evaluations"][0]
    elif mutation == "overlapping_abstention":
        record["evaluations"] = record["evaluations"][:3]
        record["abstained_forecast_ids"] = [record["evaluations"][0]["forecast_id"]]
    elif mutation == "epsilon_mismatch":
        record["log_loss_epsilon"] = "0.01"
    elif mutation == "forecast_not_in_snapshot":
        record["evaluations"][0]["forecast_id"] = "not-in-the-frozen-snapshot"
    elif mutation == "score_disagrees_with_snapshot":
        record["evaluations"][0]["evaluation_run_id"] = "not-the-frozen-run"
    elif mutation == "wrong_resolution":
        for evaluation in record["evaluations"]:
            evaluation["resolution_id"] = "different-final-resolution"
    elif mutation == "wrong_winning_side":
        record["evaluations"] = [
            score_forecast_v2(
                forecast_id=evaluation.forecast_id,
                evaluation_run_id=evaluation.evaluation_run_id,
                contract_id=evaluation.contract_id,
                forecast_method=evaluation.forecast_method,
                condition_id=evaluation.condition_id,
                token_id=evaluation.token_id,
                score=evaluation.score,
                resolution_id=evaluation.resolution_id,
                winning_outcome=(
                    WinningOutcome.NO if evaluation.outcome_yes else WinningOutcome.YES
                ),
                calibration_status=evaluation.calibration_status,
                created_at=evaluation.created_at,
                epsilon=evaluation.log_loss_epsilon,
            ).to_record()
            for evaluation in score.evaluations
        ]
    else:
        record["result_id"] = "wrong-result-id"

    with pytest.raises(ValueError, match=message):
        LateScoringResultV1.from_record(record)


@pytest.mark.anyio
async def test_final_unscorable_target_counts_each_abstention(tmp_path: Path) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    monitor = _monitor(
        tmp_path,
        _Clock(schedule_at),
        [_response(_final_payload(DEADLINE), schedule_at + timedelta(seconds=1))],
    )
    polled = await monitor.poll_once()
    assert polled.outcome is not None and polled.receipt is not None
    assert polled.observation is not None
    snapshot = monitor.snapshot
    abstained_forecasts = tuple(
        MarketBaselineForecastV2.from_record(
            {
                **forecast.to_record(),
                "raw_score": None,
                "p_yes": None,
                "abstained": True,
                "abstention_reason": AbstentionReason.NO_PRIOR_FORECAST.value,
            }
        )
        for forecast in snapshot.forecasts
    )
    replacement_snapshot = FrozenForecastSnapshotV1(
        snapshot_id=build_frozen_forecast_snapshot_id(
            target=snapshot.target,
            target_receipt=snapshot.target_receipt,
            capture_run_id=snapshot.capture_run_id,
            capture_manifest_sha256=snapshot.capture_manifest_sha256,
            code_revision=snapshot.code_revision,
            config_fingerprint=snapshot.config_fingerprint,
            forecasts=abstained_forecasts,
            frozen_at=snapshot.frozen_at,
        ),
        target=snapshot.target,
        target_receipt=snapshot.target_receipt,
        capture_run_id=snapshot.capture_run_id,
        capture_manifest_sha256=snapshot.capture_manifest_sha256,
        code_revision=snapshot.code_revision,
        config_fingerprint=snapshot.config_fingerprint,
        forecasts=abstained_forecasts,
        frozen_at=snapshot.frozen_at,
    )
    snapshot_receipt = persist_evidence_record(
        tmp_path / "evidence",
        record=replacement_snapshot,
        experiment_id=EXPERIMENT,
        artifact_kind=EvidenceArtifactKind.FROZEN_FORECAST_SNAPSHOT,
        artifact_id=replacement_snapshot.snapshot_id,
        persisted_at=replacement_snapshot.frozen_at + timedelta(seconds=1),
    )
    outcome = LateFinalOutcomeV1(
        late_outcome_id=build_late_final_outcome_id(
            protocol_receipt=monitor.protocol_receipt,
            snapshot_receipt=snapshot_receipt,
            lifecycle_receipts=(polled.receipt,),
            resolution=polled.outcome.resolution,
            selected_cutoff=polled.outcome.selected_cutoff,
        ),
        protocol=monitor.protocol,
        protocol_receipt=monitor.protocol_receipt,
        snapshot=replacement_snapshot,
        snapshot_receipt=snapshot_receipt,
        lifecycle_observations=(polled.observation,),
        lifecycle_receipts=(polled.receipt,),
        resolution=polled.outcome.resolution,
        selected_cutoff=polled.outcome.selected_cutoff,
    )

    score = score_late_final_outcome(
        outcome,
        created_at=polled.outcome.selected_cutoff + timedelta(seconds=1),
    )
    assert score.disposition is LateScoreDisposition.FINAL_UNSCORABLE
    assert score.evaluations == ()
    assert len(score.abstained_forecast_ids) == 4
    invalid_epsilon = score.to_record()
    invalid_epsilon["log_loss_epsilon"] = "NaN"
    with pytest.raises(ValueError):
        LateScoringResultV1.from_record(invalid_epsilon)
    mismatched_epsilon = score.to_record()
    mismatched_epsilon["log_loss_epsilon"] = "0.01"
    with pytest.raises(ValueError, match="disagrees with the frozen protocol"):
        LateScoringResultV1.from_record(mismatched_epsilon)


@pytest.mark.anyio
async def test_late_resume_records_a_gap_instead_of_backfilling(tmp_path: Path) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    first_at = schedule_at + timedelta(seconds=1)
    clock = _Clock(schedule_at)
    monitor = _monitor(
        tmp_path,
        clock,
        [
            _response(_pending_payload(), first_at),
            _response(
                _pending_payload(),
                first_at + timedelta(seconds=200),
            ),
        ],
    )

    first = await monitor.poll_once()
    assert first.next_poll_at is not None
    clock.set(first.next_poll_at + timedelta(seconds=121))
    resumed = await monitor.poll_once()

    assert resumed.observation is not None
    assert resumed.observation.ordinal == 2
    assert resumed.checkpoint.next_ordinal == 2
    assert len(resumed.checkpoint.gaps) == 1
    assert resumed.checkpoint.gaps[0].attempted_ordinal == 1
    assert "not backfilled" in resumed.checkpoint.gaps[0].reason
    evidence_gaps = [
        orjson.loads(path.read_bytes())
        for path in (tmp_path / "evidence" / "argos_evidence").glob("*.raw.json")
        if orjson.loads(path.read_bytes()).get("schema_version")
        == LifecycleMonitorGapEvidenceV1.schema_version
    ]
    assert len(evidence_gaps) == 1
    assert evidence_gaps[0]["attempted_ordinal"] == 2


@pytest.mark.anyio
async def test_gap_archive_recovers_idempotently_after_checkpoint_save_failure(
    tmp_path: Path,
) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    first_at = schedule_at + timedelta(seconds=1)
    second_at = first_at + timedelta(seconds=200)
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
    assert first.next_poll_at is not None
    original_save = monitor.monitor.save
    failed = False

    def fail_once_when_gap_is_checkpointed(checkpoint: Any) -> None:
        nonlocal failed
        if checkpoint.gaps and not failed:
            failed = True
            raise OSError("simulated checkpoint rename failure")
        original_save(checkpoint)

    monitor.monitor.save = fail_once_when_gap_is_checkpointed  # type: ignore[method-assign]
    clock.set(first.next_poll_at + timedelta(seconds=121))
    with pytest.raises(OSError, match="simulated checkpoint rename failure"):
        await monitor.poll_once()

    gap_paths = [
        path
        for path in (tmp_path / "evidence" / "argos_evidence").glob("*.raw.json")
        if orjson.loads(path.read_bytes()).get("schema_version")
        == LifecycleMonitorGapEvidenceV1.schema_version
    ]
    assert len(gap_paths) == 1

    restarted = _monitor(tmp_path, _Clock(clock.now()), [])
    recovered = await restarted.poll_once()
    assert not recovered.poll_performed
    assert len(recovered.checkpoint.gaps) == 1
    gap_paths_after_recovery = [
        path
        for path in (tmp_path / "evidence" / "argos_evidence").glob("*.raw.json")
        if orjson.loads(path.read_bytes()).get("schema_version")
        == LifecycleMonitorGapEvidenceV1.schema_version
    ]
    assert len(gap_paths_after_recovery) == 1


@pytest.mark.anyio
async def test_append_failure_gap_uses_the_durable_observation_as_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    retrieved_at = schedule_at + timedelta(seconds=1)
    monitor = _monitor(
        tmp_path,
        _Clock(schedule_at),
        [_response(_pending_payload(), retrieved_at)],
    )
    original_persist = persist_evidence_record
    failed = False

    def fail_receipt_link_once(directory: Path, **kwargs: Any) -> Any:
        nonlocal failed
        record = kwargs.get("record")
        if (
            not failed
            and getattr(record, "schema_version", None) == "lifecycle_receipt_chain_link.v1"
        ):
            failed = True
            raise OSError("simulated receipt-link append failure")
        return original_persist(directory, **kwargs)

    monkeypatch.setattr(
        "argos.evaluation.late_monitor.persist_evidence_record", fail_receipt_link_once
    )
    with pytest.raises(OSError, match="simulated receipt-link append failure"):
        await monitor.poll_once()

    records = [
        orjson.loads(path.read_bytes())
        for path in (tmp_path / "evidence" / "argos_evidence").glob("*.raw.json")
    ]
    observations = [
        record
        for record in records
        if record.get("schema_version") == LifecycleObservationV1.schema_version
    ]
    gaps = [
        record
        for record in records
        if record.get("schema_version") == LifecycleMonitorGapEvidenceV1.schema_version
    ]
    assert len(observations) == len(gaps) == 1
    assert gaps[0]["predecessor_observation_id"] == observations[0]["lifecycle_observation_id"]
    receipt_links = [
        record
        for record in records
        if record.get("schema_version") == "lifecycle_receipt_chain_link.v1"
    ]
    assert len(receipt_links) == 1
    assert (
        gaps[0]["predecessor_receipt_id"] == receipt_links[0]["observation_receipt"]["receipt_id"]
    )


@pytest.mark.anyio
async def test_poll_rejects_existing_reconstructed_source_sidecar(
    tmp_path: Path,
) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    retrieved_at = schedule_at + timedelta(seconds=1)
    response = _response(_pending_payload(), retrieved_at)
    monitor = _monitor(tmp_path, _Clock(schedule_at), [response])
    reconstructed = response.provenance.model_copy(update={"reconstructed": True})
    write_raw_payload(monitor.source_archive, raw=response.raw, provenance=reconstructed)

    with pytest.raises(ValueError, match="does not preserve first-hand source provenance"):
        await monitor.poll_once()

    checkpoint = monitor.monitor.load()
    assert checkpoint.next_ordinal == 0
    assert len(checkpoint.gaps) == 1
    records = [
        orjson.loads(path.read_bytes())
        for path in (tmp_path / "evidence" / "argos_evidence").glob("*.raw.json")
    ]
    assert all(
        record.get("schema_version") != LifecycleObservationV1.schema_version for record in records
    )


@pytest.mark.anyio
async def test_repeated_identical_gamma_bytes_keep_the_first_archived_timestamp(
    tmp_path: Path,
) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    first_retrieval = schedule_at + timedelta(seconds=1)
    later_retrieval = first_retrieval + timedelta(seconds=300)
    raw = orjson.dumps(_pending_payload())
    response = _response(_pending_payload(), later_retrieval)
    monitor = _monitor(tmp_path, _Clock(schedule_at), [response])
    write_raw_payload(
        monitor.source_archive,
        raw=raw,
        provenance=_response(_pending_payload(), first_retrieval).provenance,
    )

    result = await monitor.poll_once()

    assert result.observation is not None
    assert result.observation.retrieved_at == later_retrieval
    retrievals = [
        (path, orjson.loads(path.read_bytes()))
        for path in (tmp_path / "evidence" / "argos_evidence").glob("*.raw.json")
        if orjson.loads(path.read_bytes()).get("schema_version") == "lifecycle_poll_retrieval.v1"
    ]
    assert len(retrievals) == 1
    assert datetime.fromisoformat(retrievals[0][1]["provenance"]["retrieved_at"]) == later_retrieval

    # The old content-addressed sidecar is not proof of this later request.
    retrievals[0][0].unlink()
    retrievals[0][0].with_name(
        f"{retrievals[0][0].name.removesuffix('.raw.json')}.meta.json"
    ).unlink()
    restarted = _monitor(tmp_path, _Clock(later_retrieval), [])
    with pytest.raises(ValueError, match="retrieval evidence"):
        restarted._load_chain()


@pytest.mark.anyio
async def test_duplicate_payload_polls_have_distinct_retrieval_proofs_and_archive_verification(
    tmp_path: Path,
) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    first_at = schedule_at + timedelta(seconds=1)
    second_at = first_at + timedelta(seconds=61)
    final_at = second_at + timedelta(seconds=61)
    clock = _Clock(schedule_at)
    monitor = _monitor(
        tmp_path,
        clock,
        [
            _response(_pending_payload(), first_at),
            _response(_pending_payload(), second_at),
            _response(_final_payload(DEADLINE + timedelta(seconds=30)), final_at),
        ],
    )

    first = await monitor.poll_once()
    assert first.observation is not None and first.next_poll_at is not None
    clock.set(first.next_poll_at)
    second = await monitor.poll_once()
    assert second.observation is not None and second.next_poll_at is not None
    assert first.observation.raw_payload_sha256 == second.observation.raw_payload_sha256
    clock.set(second.next_poll_at)
    final = await monitor.poll_once()
    assert final.outcome is not None

    retrievals = load_lifecycle_poll_retrievals(
        monitor.evidence_archive, experiment_id=EXPERIMENT, target_id=monitor.target.target_id
    )
    assert len(retrievals) == 3
    assert (
        retrievals[first.observation.lifecycle_observation_id][0].provenance.retrieved_at
        == first_at
    )
    assert (
        retrievals[second.observation.lifecycle_observation_id][0].provenance.retrieved_at
        == second_at
    )
    verify_late_outcome_archives(
        final.outcome, evidence_dir=monitor.evidence_archive, source_dir=monitor.source_archive
    )

    # Removing the second request proof must not let the first sidecar attest
    # both poll times, including in a standalone archive verification.
    retrieval = retrievals[second.observation.lifecycle_observation_id][1]
    raw_path = monitor.evidence_archive / retrieval.storage_identity
    raw_path.unlink()
    raw_path.with_name(f"{retrieval.artifact_sha256}.meta.json").unlink()
    with pytest.raises(ValueError, match="retrieval evidence"):
        verify_late_outcome_archives(
            final.outcome, evidence_dir=monitor.evidence_archive, source_dir=monitor.source_archive
        )


@pytest.mark.anyio
async def test_archive_verifier_requires_unique_final_poll_retrieval(tmp_path: Path) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    final_at = schedule_at + timedelta(seconds=1)
    monitor = _monitor(
        tmp_path,
        _Clock(schedule_at),
        [_response(_final_payload(DEADLINE), final_at)],
    )
    result = await monitor.poll_once()
    assert result.outcome is not None and result.observation is not None
    verify_late_outcome_archives(
        result.outcome, evidence_dir=monitor.evidence_archive, source_dir=monitor.source_archive
    )

    retrievals = load_lifecycle_poll_retrievals(
        monitor.evidence_archive, experiment_id=EXPERIMENT, target_id=monitor.target.target_id
    )
    assert len(retrievals) == 1
    retrieval_receipt = retrievals[result.observation.lifecycle_observation_id][1]
    retrieval_path = monitor.evidence_archive / retrieval_receipt.storage_identity
    retrieval_path.unlink()
    retrieval_path.with_name(f"{retrieval_receipt.artifact_sha256}.meta.json").unlink()

    with pytest.raises(ValueError, match="missing per-poll retrieval evidence"):
        verify_late_outcome_archives(
            result.outcome, evidence_dir=monitor.evidence_archive, source_dir=monitor.source_archive
        )


@pytest.mark.anyio
async def test_orphaned_retrieval_after_crash_does_not_replace_the_next_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    first_at = schedule_at + timedelta(seconds=1)
    clock = _Clock(schedule_at)
    monitor = _monitor(tmp_path, clock, [_response(_pending_payload(), first_at)])
    persist = late_monitor_module.persist_evidence_record

    def fail_observation(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("artifact_kind") is EvidenceArtifactKind.LIFECYCLE_OBSERVATION:
            raise RuntimeError("simulated observation write failure")
        return persist(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(late_monitor_module, "persist_evidence_record", fail_observation)
        with pytest.raises(RuntimeError, match="simulated observation write failure"):
            await monitor.poll_once()

    checkpoint = monitor.monitor.load()
    assert checkpoint.next_ordinal == 0
    assert len(checkpoint.gaps) == 1
    second_at = checkpoint.updated_at + timedelta(seconds=61)
    clock.set(second_at - timedelta(seconds=1))
    restarted = _monitor(tmp_path, clock, [_response(_pending_payload(), second_at)])
    resumed = await restarted.poll_once()
    assert resumed.observation is not None and resumed.observation.ordinal == 1
    retrievals = load_lifecycle_poll_retrievals(
        restarted.evidence_archive, experiment_id=EXPERIMENT, target_id=restarted.target.target_id
    )
    assert len(retrievals) == 2
    assert resumed.observation.lifecycle_observation_id in retrievals


@pytest.mark.anyio
async def test_late_poll_rejects_response_timestamp_before_attempt_start(tmp_path: Path) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    attempt_at = schedule_at + timedelta(seconds=10)
    response_at = attempt_at - timedelta(seconds=1)
    monitor = _monitor(
        tmp_path,
        _Clock(attempt_at),
        [_response(_pending_payload(), response_at)],
    )

    with pytest.raises(ValueError, match="before the poll attempt"):
        await monitor.poll_once()
    gap = monitor.monitor.load().gaps[0]
    assert gap.started_at == gap.ended_at == attempt_at
    assert not any(
        orjson.loads(path.read_bytes()).get("schema_version")
        == LifecycleObservationV1.schema_version
        for path in (tmp_path / "evidence" / "argos_evidence").glob("*.raw.json")
    )


@pytest.mark.anyio
async def test_malformed_archive_record_fails_closed_during_reconstruction(
    tmp_path: Path,
) -> None:
    clock = _Clock(DEADLINE + timedelta(seconds=20))
    monitor = _monitor(tmp_path, clock, [])
    malformed = b"{not-json"
    write_raw_payload(
        monitor.evidence_archive,
        raw=malformed,
        provenance=SourceProvenanceV1(
            source="argos_evidence",
            endpoint=f"argos-evidence://{LifecycleObservationV1.schema_version}/malformed",
            retrieved_at=clock.now(),
            raw_sha256=sha256_hex(malformed),
            byte_length=len(malformed),
        ),
    )

    with pytest.raises(ValueError, match="evidence archive contains malformed JSON"):
        _monitor(tmp_path, clock, [])


def test_lifecycle_evidence_decoder_rejects_nonobjects_and_unknown_schemas() -> None:
    with pytest.raises(ValueError, match="not a JSON object"):
        LateLifecycleMonitor._decode_evidence_record(b"[]")
    with pytest.raises(ValueError, match="no schema version"):
        LateLifecycleMonitor._decode_evidence_record(b"{}")
    with pytest.raises(SchemaVersionError, match="no registered model declares"):
        LateLifecycleMonitor._decode_evidence_record(b'{"schema_version":"future.v99"}')


def test_archived_gap_reconciliation_is_idempotent_and_persists_its_anchor(
    tmp_path: Path,
) -> None:
    now = DEADLINE + timedelta(seconds=20)
    monitor = _monitor(tmp_path, _Clock(now), [])
    checkpoint = monitor._initial_checkpoint()
    started_at = checkpoint.updated_at + timedelta(seconds=1)
    ended_at = started_at + timedelta(seconds=2)
    monitor._persist_gap(
        checkpoint=checkpoint,
        started_at=started_at,
        ended_at=ended_at,
        reason="simulated owner interruption",
    )

    chain = monitor._load_chain()
    reconciled = monitor._reconcile_archived_gaps(checkpoint, checkpoint, chain)
    repeated = monitor._reconcile_archived_gaps(checkpoint, reconciled, chain)
    assert len(reconciled.gaps) == 1
    assert repeated == reconciled
    assert repeated.updated_at == ended_at


@pytest.mark.anyio
async def test_archived_gap_predecessor_must_match_verified_lifecycle_chain(
    tmp_path: Path,
) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    retrieved_at = schedule_at + timedelta(seconds=1)
    monitor = _monitor(
        tmp_path,
        _Clock(schedule_at),
        [_response(_pending_payload(), retrieved_at)],
    )
    first = await monitor.poll_once()
    chain = [(first.observation, first.receipt)]
    attempted_ordinal = 2
    started_at = retrieved_at + timedelta(seconds=1)
    ended_at = started_at + timedelta(seconds=2)
    reason = "forged predecessor"
    fields = {
        "schedule_id": monitor.schedule.schedule_id,
        "experiment_id": monitor.protocol.experiment_id,
        "target_id": monitor.target.target_id,
        "attempted_ordinal": attempted_ordinal,
        "started_at": started_at,
        "ended_at": ended_at,
        "predecessor_observation_id": "lifecycle-observation-from-no-ordinal",
        "predecessor_receipt_id": "receipt-from-no-ordinal",
        "reason": reason,
    }
    gap_id = _gap_id(
        schedule_id=fields["schedule_id"],
        attempted_ordinal=attempted_ordinal,
        started_at=started_at,
        ended_at=ended_at,
        predecessor_observation_id=fields["predecessor_observation_id"],
        predecessor_receipt_id=fields["predecessor_receipt_id"],
        reason=reason,
    )
    gap = LifecycleMonitorGapEvidenceV1(gap_id=gap_id, **fields)
    persist_evidence_record(
        monitor.evidence_archive,
        record=gap,
        experiment_id=monitor.protocol.experiment_id,
        artifact_kind=EvidenceArtifactKind.LIFECYCLE_MONITOR_GAP,
        artifact_id=gap.gap_id,
        persisted_at=ended_at,
    )
    monitor._gap_cache = None

    with pytest.raises(
        ValueError, match="predecessor disagrees with the verified observation chain"
    ):
        monitor._reconcile_archived_gaps(first.checkpoint, first.checkpoint, chain)


@pytest.mark.anyio
async def test_archived_gap_ahead_of_durable_chain_is_rejected(tmp_path: Path) -> None:
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
    assert first.next_poll_at is not None
    clock.set(first.next_poll_at)
    second = await monitor.poll_once()
    assert second.checkpoint.next_ordinal == 2
    monitor._persist_gap(
        checkpoint=second.checkpoint,
        started_at=clock.now(),
        ended_at=clock.now() + timedelta(seconds=1),
        reason="impossible third attempt in an empty checkpoint",
    )

    checkpoint = monitor._initial_checkpoint()
    with pytest.raises(ValueError, match="future poll ordinal"):
        monitor._reconcile_archived_gaps(checkpoint, checkpoint, monitor._load_chain())


def test_archived_gap_that_predates_checkpoint_is_rejected(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path, _Clock(DEADLINE + timedelta(seconds=20)), [])
    initial = monitor._initial_checkpoint()
    started_at = initial.updated_at + timedelta(seconds=1)
    ended_at = started_at + timedelta(seconds=2)
    monitor._persist_gap(
        checkpoint=initial,
        started_at=started_at,
        ended_at=ended_at,
        reason="simulated owner interruption",
    )
    advanced_checkpoint = ResumableMonitorCheckpointV1(
        campaign_id=initial.campaign_id,
        configuration_sha256=initial.configuration_sha256,
        next_ordinal=0,
        updated_at=started_at + timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="regresses behind the checkpoint"):
        monitor._reconcile_archived_gaps(
            advanced_checkpoint, advanced_checkpoint, monitor._load_chain()
        )


def test_gap_persistence_is_idempotent_and_invalidates_failed_archive_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = DEADLINE + timedelta(seconds=20)
    monitor = _monitor(tmp_path, _Clock(now), [])
    checkpoint = monitor._initial_checkpoint()
    started_at = checkpoint.updated_at + timedelta(seconds=1)
    ended_at = started_at + timedelta(seconds=2)
    first = monitor._persist_gap(
        checkpoint=checkpoint,
        started_at=started_at,
        ended_at=ended_at,
        reason="same immutable gap",
    )
    second = monitor._persist_gap(
        checkpoint=checkpoint,
        started_at=started_at,
        ended_at=ended_at,
        reason="same immutable gap",
    )
    assert first == second
    assert len(monitor._load_gap_evidence()) == 1

    failing_monitor = _monitor(tmp_path / "failed", _Clock(now), [])

    def fail_gap_write(directory: Path, **kwargs: Any) -> Any:
        if isinstance(kwargs.get("record"), LifecycleMonitorGapEvidenceV1):
            raise OSError("simulated gap archive failure")
        return persist_evidence_record(directory, **kwargs)

    monkeypatch.setattr("argos.evaluation.late_monitor.persist_evidence_record", fail_gap_write)
    failed_checkpoint = failing_monitor._initial_checkpoint()
    with pytest.raises(OSError, match="simulated gap archive failure"):
        failing_monitor._persist_gap(
            checkpoint=failed_checkpoint,
            started_at=failed_checkpoint.updated_at + timedelta(seconds=1),
            ended_at=failed_checkpoint.updated_at + timedelta(seconds=2),
            reason="failed immutable gap",
        )
    assert failing_monitor._gap_cache is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("id", "another-market", "does not identify the frozen target"),
        ("conditionId", "another-condition", "does not identify the frozen target"),
        ("clobTokenIds", ["22", "11"], "token mapping disagrees"),
        ("clobTokenIds", "not-json", "token mapping is malformed"),
    ],
)
async def test_gamma_payload_must_match_the_frozen_market_tokens(
    tmp_path: Path, field: str, value: Any, message: str
) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    retrieved_at = schedule_at + timedelta(seconds=1)
    payload = {**_pending_payload(), field: value}
    monitor = _monitor(
        tmp_path,
        _Clock(schedule_at),
        [_response(payload, retrieved_at)],
    )

    with pytest.raises(ValueError, match=message):
        await monitor.poll_once()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        ("http://gamma-api.polymarket.com/markets/market-1", "credential-free HTTPS"),
        (
            "https://user:secret@gamma-api.polymarket.com/markets/market-1",
            "credential-free HTTPS",
        ),
        ("https://gamma-api.polymarket.com/markets/other", "frozen Gamma origin"),
        (
            "https://gamma-api.polymarket.com:invalid/markets/market-1",
            "invalid port",
        ),
        (
            "https://gamma-api.polymarket.com/markets/market-1?token=secret",
            "credential-free HTTPS",
        ),
        (
            "https://gamma-api.polymarket.com/markets/market-1;token=secret",
            "credential-free HTTPS",
        ),
    ],
)
async def test_gamma_endpoint_rejects_untrusted_authority_and_url_parts(
    tmp_path: Path, endpoint: str, message: str
) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    monitor = _monitor(
        tmp_path,
        _Clock(schedule_at),
        [
            _response(
                _pending_payload(),
                schedule_at + timedelta(seconds=1),
                endpoint=endpoint,
            )
        ],
    )

    with pytest.raises(ValueError, match=message):
        await monitor.poll_once()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("source", "reconstructed", "retrieved_offset", "message"),
    [
        ("other", False, 1, "first-hand Gamma source bytes"),
        ("gamma", True, 1, "first-hand Gamma source bytes"),
        ("gamma", False, -10, "regresses behind durable monitor state"),
        ("gamma", False, -5, "before its frozen cadence"),
    ],
)
async def test_gamma_response_requires_first_hand_monotone_cadence_evidence(
    tmp_path: Path,
    source: str,
    reconstructed: bool,
    retrieved_offset: int,
    message: str,
) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    monitor = _monitor(
        tmp_path,
        _Clock(schedule_at),
        [
            _response(
                _pending_payload(),
                schedule_at + timedelta(seconds=retrieved_offset),
                source=source,
                reconstructed=reconstructed,
            )
        ],
    )

    with pytest.raises(ValueError, match=message):
        response = cast(_Source, monitor.source).responses[0]
        if source == "gamma" and not reconstructed and retrieved_offset < 0:
            assert isinstance(response, GammaResponse)
            monitor._persist_poll(
                monitor._initial_checkpoint(), (), response, attempted_at=schedule_at
            )
        else:
            await monitor.poll_once()


def test_gamma_retrieval_cannot_precede_the_frozen_first_poll_time(tmp_path: Path) -> None:
    effective_at = DEADLINE + timedelta(seconds=20)
    first_poll_at = effective_at + timedelta(seconds=30)
    monitor = _monitor(
        tmp_path,
        _Clock(first_poll_at),
        [],
        first_poll_at=first_poll_at,
    )
    response = _response(_pending_payload(), first_poll_at - timedelta(seconds=1))

    with pytest.raises(ValueError, match="before its frozen cadence"):
        monitor._persist_poll(
            monitor._initial_checkpoint(), (), response, attempted_at=first_poll_at
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("raw", "message"),
    [(b"{", "not JSON"), (b"[]", "not a market object")],
)
async def test_gamma_raw_bytes_must_decode_to_a_market_object(
    tmp_path: Path, raw: bytes, message: str
) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    monitor = _monitor(
        tmp_path,
        _Clock(schedule_at),
        [_response({}, schedule_at + timedelta(seconds=1), raw=raw)],
    )

    with pytest.raises(ValueError, match=message):
        await monitor.poll_once()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("updated_at", "message"),
    [
        (7, "must be an ISO timestamp string"),
        ("not-a-timestamp", "not a valid ISO timestamp"),
        ("2026-09-02T00:00:00", "must include an explicit UTC offset"),
    ],
)
async def test_gamma_source_timestamp_must_be_explicit_and_valid(
    tmp_path: Path, updated_at: Any, message: str
) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    payload = {**_pending_payload(), "updatedAt": updated_at}
    monitor = _monitor(
        tmp_path,
        _Clock(schedule_at),
        [_response(payload, schedule_at + timedelta(seconds=1))],
    )

    with pytest.raises(ValueError, match=message):
        await monitor.poll_once()


@pytest.mark.anyio
async def test_gamma_token_mapping_json_and_disputed_status_are_supported(tmp_path: Path) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    payload = {
        **_pending_payload(),
        "clobTokenIds": '["11", "22"]',
        "umaResolutionStatuses": ["disputed"],
    }
    monitor = _monitor(
        tmp_path,
        _Clock(schedule_at),
        [_response(payload, schedule_at + timedelta(seconds=1))],
    )

    result = await monitor.poll_once()

    assert result.observation is not None
    assert result.observation.finality is ResolutionStatus.DISPUTED
    assert result.outcome is None


@pytest.mark.anyio
@pytest.mark.parametrize("statuses", ["not-json", [], "unknown"])
async def test_unrecognized_uma_statuses_remain_unknown(tmp_path: Path, statuses: Any) -> None:
    schedule_at = DEADLINE + timedelta(seconds=20)
    payload = {**_pending_payload(), "umaResolutionStatuses": statuses}
    monitor = _monitor(
        tmp_path,
        _Clock(schedule_at),
        [_response(payload, schedule_at + timedelta(seconds=1))],
    )

    result = await monitor.poll_once()

    assert result.observation is not None
    assert result.observation.finality is ResolutionStatus.UNKNOWN


@pytest.mark.anyio
async def test_receipt_link_rejects_wrong_owner_predecessor_time_and_identity(
    tmp_path: Path,
) -> None:
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
    assert first.next_poll_at is not None
    clock.set(first.next_poll_at)
    await monitor.poll_once()
    links = monitor._read_receipt_links()

    wrong_owner = links[1].to_record()
    wrong_owner["target_id"] = "another-target"
    with pytest.raises(ValueError, match="different lifecycle evidence"):
        type(links[1]).from_record(wrong_owner)

    first_predecessor = links[1].to_record()
    first_predecessor["previous_receipt_id"] = "unexpected-predecessor"
    with pytest.raises(ValueError, match="first lifecycle receipt"):
        type(links[1]).from_record(first_predecessor)

    second_link = links[2].to_record()
    second_link["previous_receipt_id"] = None
    with pytest.raises(ValueError, match="later lifecycle receipts require"):
        type(links[2]).from_record(second_link)

    early_link = links[1].to_record()
    early_link["linked_at"] = (
        links[1].observation_receipt.persisted_at - timedelta(seconds=1)
    ).isoformat()
    with pytest.raises(ValueError, match="predates the lifecycle receipt"):
        type(links[1]).from_record(early_link)

    wrong_identity = links[1].to_record()
    wrong_identity["link_id"] = "wrong-link-id"
    with pytest.raises(ValueError, match="identity disagrees"):
        type(links[1]).from_record(wrong_identity)
