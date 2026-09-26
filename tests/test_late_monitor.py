from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import orjson
import pytest

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
from argos.evaluation.late_monitor import LateResolutionStatus
from argos.evaluation.prospective import build_target_id, persist_evidence_record
from argos.resolution import ResolutionStatus
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
    assert len(monitor._load_chain()) == 2
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
    with pytest.raises(ValueError, match="final observations must be scored"):
        pending_late_resolution_score(
            monitor.snapshot,
            latest_observation=final.observation,
            created_at=schedule_at + timedelta(seconds=2),
        )


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
        ("incomplete_accounting", "account for every planned baseline"),
        ("scored_without_evaluations", "requires at least one evaluation"),
        ("unscorable_with_evaluations", "cannot carry evaluations"),
        ("duplicate_evaluations", "unique frozen forecasts"),
        ("overlapping_abstention", "both scored and abstained"),
        ("epsilon_mismatch", "declared clipping epsilon"),
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
        record["abstained_forecast_ids"] = [record["evaluations"][0]["forecast_id"]]
        record["planned_forecast_count"] += 1
    elif mutation == "epsilon_mismatch":
        record["log_loss_epsilon"] = "0.01"
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
            monitor._persist_poll(monitor._initial_checkpoint(), (), response)
        else:
            await monitor.poll_once()


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
