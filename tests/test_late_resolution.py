"""Adversarial temporal guards for the append-only M4 outcome boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import orjson
import pytest
from pydantic import ValidationError

from argos.baselines import (
    BaselineMethod,
    MarketQuoteV1,
    build_baseline_forecast_v2,
)
from argos.config.manifest import WorkingTreeStatus
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.errors import ImmutabilityViolationError, StorageError
from argos.evaluation.bundle import record_sha256
from argos.evaluation.late_resolution import (
    FrozenForecastSnapshotV1,
    LateFinalOutcomeV1,
    build_frozen_forecast_snapshot_id,
    build_late_final_outcome_id,
)
from argos.evaluation.late_resolution_archive import verify_late_outcome_archives
from argos.evaluation.prospective import (
    AcrossTargetWeighting,
    CutoffBasis,
    EvidenceArtifactKind,
    EvidencePersistenceReceiptV1,
    LifecycleObservationV1,
    ProspectiveExperimentProtocolV2,
    ProspectiveTargetV1,
    StandaloneLastTradePolicy,
    WithinTargetAggregation,
    build_lifecycle_observation_id,
    build_persistence_receipt_id,
    build_target_id,
    load_persisted_record,
    persist_evidence_record,
)
from argos.resolution import ResolutionStatus, ResolutionV1, normalize_gamma_resolution
from argos.store.raw_archive import archive_relative_location, write_raw_payload

BASE = datetime(2026, 9, 26, tzinfo=UTC)
START = BASE + timedelta(hours=1)
END = START + timedelta(hours=1)
DEADLINE = END + timedelta(days=1)
FINAL_AT = DEADLINE + timedelta(hours=1)
CONDITION = "0x" + "a" * 64
EXPERIMENT = "synthetic-late-resolution-v1"


def _protocol() -> ProspectiveExperimentProtocolV2:
    return ProspectiveExperimentProtocolV2(
        experiment_id=EXPERIMENT,
        declared_at=BASE,
        target_population="synthetic public binary targets",
        inclusion_rules=("open binary market",),
        exclusion_rules=("missing contract",),
        market_selection_mechanism="synthetic deterministic fixture",
        observation_window_start=START,
        observation_window_end=END,
        stopping_rule="one bounded capture, then asynchronous finality",
        structural_minimum_independent_resolved_targets=2,
        minimum_intended_resolved_target_count=2,
        scientific_minimum_resolved_target_count=30,
        minimum_target_count_rationale="synthetic structural test, not calibration",
        within_target_aggregation=(WithinTargetAggregation.LAST_ADMISSIBLE_PRE_CUTOFF_PER_METHOD),
        across_target_weighting=AcrossTargetWeighting.EQUAL_RESOLVED_TARGET,
        metrics=("brier_score", "log_loss", "absolute_error"),
        calibration_bin_edges=(Decimal("0"), Decimal("0.5"), Decimal("1")),
        log_loss_epsilon=Decimal("0.000001"),
        uncertainty_reporting="none for structural fixture",
        missingness_treatment="pending remains in denominator",
        abstention_treatment="record abstentions without score",
        disputed_unresolved_treatment="no score until final",
        category_dispersion_requirement="two categories for calibration",
        outcome_dispersion_requirement="five YES and NO for calibration",
        minimum_category_count_for_calibration=2,
        minimum_yes_outcomes_for_calibration=5,
        minimum_no_outcomes_for_calibration=5,
        protocol_sufficiency_rule="thirty resolved and dispersion for calibration",
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


def _observation(
    *,
    ordinal: int,
    previous: str | None,
    finality: ResolutionStatus,
    retrieved_at: datetime,
    raw_sha256: str,
    byte_length: int,
    raw_location: str,
    resolution: ResolutionV1 | None = None,
) -> LifecycleObservationV1:
    fields = {
        "experiment_id": EXPERIMENT,
        "target_id": build_target_id(
            experiment_id=EXPERIMENT,
            market_id="market-1",
            condition_id=CONDITION,
            yes_token_id="11",
            no_token_id="22",
        ),
        "ordinal": ordinal,
        "previous_observation_id": previous,
        "source": "gamma",
        "endpoint": "https://gamma-api.polymarket.com/markets/market-1",
        "source_time": resolution.resolved_at if resolution else None,
        "retrieved_at": retrieved_at,
        "raw_payload_sha256": raw_sha256,
        "byte_length": byte_length,
        "raw_payload_location": raw_location,
        "finality": finality,
        "resolution_id": resolution.resolution_id if resolution else None,
        "resolution_record_sha256": record_sha256(resolution.to_record()) if resolution else None,
    }
    return LifecycleObservationV1(
        lifecycle_observation_id=build_lifecycle_observation_id(**fields), **fields
    )


def _archived_gamma(
    tmp_path: Path, payload: dict[str, object], at: datetime
) -> tuple[str, int, str]:
    return _archived_source(tmp_path, orjson.dumps(payload), at)


def _archived_source(
    tmp_path: Path, raw: bytes, at: datetime, *, source: str = "gamma"
) -> tuple[str, int, str]:
    provenance = SourceProvenanceV1(
        source=source,
        endpoint="https://gamma-api.polymarket.com/markets/market-1",
        http_status=200,
        retrieved_at=at,
        raw_sha256=sha256_hex(raw),
        byte_length=len(raw),
    )
    write_raw_payload(tmp_path / "source", raw=raw, provenance=provenance)
    return provenance.raw_sha256, provenance.byte_length, archive_relative_location(provenance)


def _receipt_with(
    receipt: EvidencePersistenceReceiptV1, **changes: object
) -> EvidencePersistenceReceiptV1:
    fields = {**dict(receipt), **changes}
    fields["receipt_id"] = build_persistence_receipt_id(
        experiment_id=fields["experiment_id"],
        artifact_kind=fields["artifact_kind"],
        artifact_id=fields["artifact_id"],
        artifact_schema_version=fields["artifact_schema_version"],
        artifact_sha256=fields["artifact_sha256"],
        artifact_byte_length=fields["artifact_byte_length"],
        persisted_at=fields["persisted_at"],
        storage_backend=fields["storage_backend"],
        storage_identity=fields["storage_identity"],
    )
    return EvidencePersistenceReceiptV1.model_validate(fields)


def _observation_with(
    tmp_path: Path, observation: LifecycleObservationV1, **changes: object
) -> tuple[LifecycleObservationV1, EvidencePersistenceReceiptV1]:
    fields = {**dict(observation), **changes}
    fields.pop("lifecycle_observation_id")
    updated = LifecycleObservationV1(
        lifecycle_observation_id=build_lifecycle_observation_id(**fields), **fields
    )
    receipt = persist_evidence_record(
        tmp_path / "evidence",
        record=updated,
        experiment_id=EXPERIMENT,
        artifact_kind=EvidenceArtifactKind.LIFECYCLE_OBSERVATION,
        artifact_id=updated.lifecycle_observation_id,
        persisted_at=updated.retrieved_at + timedelta(seconds=1),
    )
    return updated, receipt


def _late_outcome_with_final_source(
    tmp_path: Path, outcome: LateFinalOutcomeV1, raw: bytes, *, source: str = "gamma"
) -> LateFinalOutcomeV1:
    digest, byte_length, location = _archived_source(tmp_path, raw, FINAL_AT, source=source)
    resolution = outcome.resolution.model_copy(update={"source_payload_sha256": digest})
    altered, receipt = _observation_with(
        tmp_path,
        outcome.lifecycle_observations[-1],
        source=source,
        raw_payload_sha256=digest,
        byte_length=byte_length,
        raw_payload_location=location,
        resolution_record_sha256=record_sha256(resolution.to_record()),
    )
    receipts = (outcome.lifecycle_receipts[0], receipt)
    return outcome.model_copy(
        update={
            "late_outcome_id": build_late_final_outcome_id(
                protocol_receipt=outcome.protocol_receipt,
                snapshot_receipt=outcome.snapshot_receipt,
                lifecycle_receipts=receipts,
                resolution=resolution,
                selected_cutoff=outcome.selected_cutoff,
            ),
            "resolution": resolution,
            "lifecycle_observations": (outcome.lifecycle_observations[0], altered),
            "lifecycle_receipts": receipts,
        }
    )


def _late_record(tmp_path: Path) -> LateFinalOutcomeV1:
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
    target = ProspectiveTargetV1(
        target_id=build_target_id(
            experiment_id=EXPERIMENT,
            market_id="market-1",
            condition_id=CONDITION,
            yes_token_id="11",
            no_token_id="22",
        ),
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
        market_receipt_id="receipt-market",
        contract_id="contract-1",
        contract_record_sha256="d" * 64,
        contract_receipt_id="receipt-contract",
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
            evaluation_run_id="eval-1",
            source_capture_run_id="capture-1",
            source_observation_id="obs-1",
            information_state_hash="e" * 64,
            market_id="market-1",
            contract_id="contract-1",
        )
        for method in BaselineMethod
    )
    frozen_at = forecast_at + timedelta(seconds=1)
    snapshot_fields = {
        "target": target,
        "target_receipt": target_receipt,
        "capture_run_id": "capture-1",
        "capture_manifest_sha256": "f" * 64,
        "code_revision": protocol.code_revision,
        "config_fingerprint": protocol.config_fingerprint,
        "forecasts": forecasts,
        "frozen_at": frozen_at,
    }
    snapshot = FrozenForecastSnapshotV1(
        snapshot_id=build_frozen_forecast_snapshot_id(**snapshot_fields), **snapshot_fields
    )
    snapshot_receipt = persist_evidence_record(
        evidence,
        record=snapshot,
        experiment_id=EXPERIMENT,
        artifact_kind=EvidenceArtifactKind.FROZEN_FORECAST_SNAPSHOT,
        artifact_id=snapshot.snapshot_id,
        persisted_at=frozen_at + timedelta(seconds=1),
    )
    proposed_at = END + timedelta(minutes=5)
    proposed_raw = _archived_gamma(
        tmp_path,
        {
            "id": "market-1",
            "conditionId": CONDITION,
            "closed": False,
            "outcomePrices": ["0.5", "0.5"],
            "clobTokenIds": ["11", "22"],
            "umaResolutionStatuses": ["proposed"],
        },
        proposed_at,
    )
    final_raw = _archived_gamma(
        tmp_path,
        {
            "id": "market-1",
            "conditionId": CONDITION,
            "closed": True,
            "outcomePrices": ["1", "0"],
            "clobTokenIds": ["11", "22"],
            "umaResolutionStatuses": ["proposed", "resolved"],
            "updatedAt": (DEADLINE + timedelta(minutes=10)).isoformat(),
        },
        FINAL_AT,
    )
    normalized = normalize_gamma_resolution(
        orjson.loads((tmp_path / "source" / final_raw[2]).read_bytes()),
        source_payload_sha256=final_raw[0],
        normalized_at=FINAL_AT,
    )
    assert isinstance(normalized, ResolutionV1)
    resolution = normalized
    proposed = _observation(
        ordinal=1,
        previous=None,
        finality=ResolutionStatus.PROPOSED,
        retrieved_at=proposed_at,
        raw_sha256=proposed_raw[0],
        byte_length=proposed_raw[1],
        raw_location=proposed_raw[2],
    )
    final = _observation(
        ordinal=2,
        previous=proposed.lifecycle_observation_id,
        finality=ResolutionStatus.FINAL,
        retrieved_at=FINAL_AT,
        raw_sha256=final_raw[0],
        byte_length=final_raw[1],
        raw_location=final_raw[2],
        resolution=resolution,
    )
    observations = (proposed, final)
    receipts = tuple(
        persist_evidence_record(
            evidence,
            record=observation,
            experiment_id=EXPERIMENT,
            artifact_kind=EvidenceArtifactKind.LIFECYCLE_OBSERVATION,
            artifact_id=observation.lifecycle_observation_id,
            persisted_at=observation.retrieved_at + timedelta(seconds=1),
        )
        for observation in observations
    )
    fields = {
        "protocol": protocol,
        "protocol_receipt": protocol_receipt,
        "snapshot": snapshot,
        "snapshot_receipt": snapshot_receipt,
        "lifecycle_observations": observations,
        "lifecycle_receipts": receipts,
        "resolution": resolution,
        "selected_cutoff": FINAL_AT,
    }
    return LateFinalOutcomeV1(
        late_outcome_id=build_late_final_outcome_id(
            protocol_receipt=protocol_receipt,
            snapshot_receipt=snapshot_receipt,
            lifecycle_receipts=receipts,
            resolution=resolution,
            selected_cutoff=FINAL_AT,
        ),
        **fields,
    )


def test_late_finality_round_trips_without_changing_the_old_deadline(tmp_path: Path) -> None:
    outcome = _late_record(tmp_path)
    assert outcome.selected_cutoff > outcome.protocol.lifecycle_deadline
    assert LateFinalOutcomeV1.from_record(outcome.to_record()) == outcome
    receipt = persist_evidence_record(
        tmp_path / "evidence",
        record=outcome,
        experiment_id=EXPERIMENT,
        artifact_kind=EvidenceArtifactKind.LATE_FINAL_OUTCOME,
        artifact_id=outcome.late_outcome_id,
        persisted_at=FINAL_AT + timedelta(minutes=1),
    )
    assert load_persisted_record(tmp_path / "evidence", receipt, LateFinalOutcomeV1) == outcome
    verify_late_outcome_archives(
        outcome, evidence_dir=tmp_path / "evidence", source_dir=tmp_path / "source"
    )


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("bad_hash", "snapshot hashes must be hexadecimal"),
        ("wrong_receipt_kind", "target receipt names different evidence"),
        ("freeze_before_selection", "cannot predate target persistence"),
        ("missing_baseline", "one record per declared baseline"),
        ("mixed_information_state", "different information states"),
        ("wrong_capture", "different target or capture"),
        ("forecast_after_freeze", "forecast was not available"),
        ("wrong_identity", "snapshot identity disagrees"),
    ],
)
def test_frozen_snapshot_rejects_false_evidence(tmp_path: Path, case: str, message: str) -> None:
    snapshot = _late_record(tmp_path).snapshot
    forecast_at = snapshot.forecasts[0].as_of_received_time
    alterations: dict[str, object] = {
        "bad_hash": {"capture_manifest_sha256": "g" * 64},
        "wrong_receipt_kind": {
            "target_receipt": _receipt_with(
                snapshot.target_receipt, artifact_kind=EvidenceArtifactKind.MARKET_DEFINITION
            )
        },
        "freeze_before_selection": {
            "frozen_at": snapshot.target.selected_at - timedelta(seconds=1)
        },
        "missing_baseline": {"forecasts": snapshot.forecasts[:-1]},
        "mixed_information_state": {
            "forecasts": (
                *snapshot.forecasts[:-1],
                snapshot.forecasts[-1].model_copy(update={"as_of_ingest_sequence": 2}),
            )
        },
        "wrong_capture": {"capture_run_id": "another-capture"},
        "forecast_after_freeze": {"frozen_at": forecast_at - timedelta(seconds=1)},
        "wrong_identity": {"snapshot_id": "frozen-forecast-false"},
    }
    with pytest.raises(ValidationError, match=message):
        snapshot.model_copy(update=alterations[case])


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("wrong_protocol_receipt", "protocol receipt names different evidence"),
        ("wrong_snapshot_receipt", "snapshot receipt names different evidence"),
        ("protocol_persisted_late", "protocol was not durable"),
        ("snapshot_receipt_before_freeze", "receipt predates the forecast freeze"),
        ("no_lifecycle", "complete lifecycle receipt partition"),
        ("missing_lifecycle_receipt", "complete lifecycle receipt partition"),
        ("nonfinal_resolution", "only final resolution"),
        ("wrong_identity", "late outcome identity disagrees"),
    ],
)
def test_late_outcome_rejects_broken_binding(tmp_path: Path, case: str, message: str) -> None:
    outcome = _late_record(tmp_path)
    alterations: dict[str, dict[str, object]] = {
        "wrong_protocol_receipt": {
            "protocol_receipt": _receipt_with(
                outcome.protocol_receipt, artifact_kind=EvidenceArtifactKind.TARGET_DECLARATION
            )
        },
        "wrong_snapshot_receipt": {
            "snapshot_receipt": _receipt_with(
                outcome.snapshot_receipt, artifact_kind=EvidenceArtifactKind.TARGET_DECLARATION
            )
        },
        "protocol_persisted_late": {
            "protocol_receipt": _receipt_with(
                outcome.protocol_receipt,
                persisted_at=outcome.snapshot.target.selected_at + timedelta(seconds=1),
            )
        },
        "snapshot_receipt_before_freeze": {
            "snapshot_receipt": _receipt_with(
                outcome.snapshot_receipt,
                persisted_at=outcome.snapshot.frozen_at - timedelta(seconds=1),
            )
        },
        "no_lifecycle": {"lifecycle_observations": (), "lifecycle_receipts": ()},
        "missing_lifecycle_receipt": {"lifecycle_receipts": outcome.lifecycle_receipts[:-1]},
        "nonfinal_resolution": {
            "resolution": outcome.resolution.model_copy(
                update={"resolution_status": ResolutionStatus.PROPOSED}
            )
        },
        "wrong_identity": {"late_outcome_id": "late-final-false"},
    }
    with pytest.raises(ValidationError, match=message):
        outcome.model_copy(update=alterations[case])


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("ordinal_skip", "ordinal chain has a gap"),
        ("wrong_predecessor", "predecessor chain is broken"),
        ("retrieval_regression", "retrieval time regressed"),
        ("last_not_final", "no final lifecycle observation"),
    ],
)
def test_late_outcome_rejects_false_lifecycle_chain(
    tmp_path: Path, case: str, message: str
) -> None:
    outcome = _late_record(tmp_path)
    last = outcome.lifecycle_observations[-1]
    changes: dict[str, dict[str, object]] = {
        "ordinal_skip": {"ordinal": 3},
        "wrong_predecessor": {"previous_observation_id": "lifecycle-wrong"},
        "retrieval_regression": {"retrieved_at": END},
        "last_not_final": {
            "finality": ResolutionStatus.PROPOSED,
            "resolution_id": None,
            "resolution_record_sha256": None,
        },
    }
    altered, receipt = _observation_with(tmp_path, last, **changes[case])
    with pytest.raises(ValidationError, match=message):
        outcome.model_copy(
            update={
                "lifecycle_observations": (outcome.lifecycle_observations[0], altered),
                "lifecycle_receipts": (outcome.lifecycle_receipts[0], receipt),
            }
        )


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("prior_after_next_poll", "not durable before next poll"),
        ("final_receipt_before_poll", "receipt predates source retrieval"),
    ],
)
def test_late_outcome_rejects_false_receipt_order(tmp_path: Path, case: str, message: str) -> None:
    outcome = _late_record(tmp_path)
    receipts = (
        (
            _receipt_with(
                outcome.lifecycle_receipts[0], persisted_at=FINAL_AT + timedelta(seconds=1)
            ),
            outcome.lifecycle_receipts[1],
        )
        if case == "prior_after_next_poll"
        else (
            outcome.lifecycle_receipts[0],
            _receipt_with(
                outcome.lifecycle_receipts[1], persisted_at=FINAL_AT - timedelta(seconds=1)
            ),
        )
    )
    with pytest.raises(ValidationError, match=message):
        outcome.model_copy(update={"lifecycle_receipts": receipts})


def test_late_record_cannot_relabel_an_in_deadline_final_as_late(tmp_path: Path) -> None:
    outcome = _late_record(tmp_path)
    final, receipt = _observation_with(
        tmp_path, outcome.lifecycle_observations[-1], retrieved_at=DEADLINE
    )
    with pytest.raises(ValidationError, match="follow the old lifecycle deadline"):
        outcome.model_copy(
            update={
                "selected_cutoff": DEADLINE,
                "lifecycle_observations": (outcome.lifecycle_observations[0], final),
                "lifecycle_receipts": (outcome.lifecycle_receipts[0], receipt),
            }
        )


@pytest.mark.parametrize(
    ("normalized_at", "message"),
    [
        (FINAL_AT - timedelta(seconds=1), "predates source retrieval"),
        (FINAL_AT + timedelta(seconds=2), "postdates source receipt"),
    ],
)
def test_resolution_normalization_must_fit_retrieval_and_receipt(
    tmp_path: Path, normalized_at: datetime, message: str
) -> None:
    outcome = _late_record(tmp_path)
    resolution = outcome.resolution.model_copy(update={"normalized_at": normalized_at})
    final, receipt = _observation_with(
        tmp_path,
        outcome.lifecycle_observations[-1],
        resolution_record_sha256=record_sha256(resolution.to_record()),
    )
    with pytest.raises(ValidationError, match=message):
        outcome.model_copy(
            update={
                "resolution": resolution,
                "lifecycle_observations": (outcome.lifecycle_observations[0], final),
                "lifecycle_receipts": (outcome.lifecycle_receipts[0], receipt),
            }
        )


def test_backdated_first_observed_cutoff_is_refused_even_with_matching_id(
    tmp_path: Path,
) -> None:
    outcome = _late_record(tmp_path)
    false_cutoff = FINAL_AT - timedelta(minutes=1)
    false_id = build_late_final_outcome_id(
        protocol_receipt=outcome.protocol_receipt,
        snapshot_receipt=outcome.snapshot_receipt,
        lifecycle_receipts=outcome.lifecycle_receipts,
        resolution=outcome.resolution,
        selected_cutoff=false_cutoff,
    )
    with pytest.raises(ValidationError, match="cannot be backdated"):
        outcome.model_copy(update={"selected_cutoff": false_cutoff, "late_outcome_id": false_id})


def test_final_without_prior_chain_cannot_be_presented_as_first(tmp_path: Path) -> None:
    outcome = _late_record(tmp_path)
    with pytest.raises(ValidationError, match="ordinal chain has a gap"):
        outcome.model_copy(
            update={
                "lifecycle_observations": outcome.lifecycle_observations[1:],
                "lifecycle_receipts": outcome.lifecycle_receipts[1:],
            }
        )


def test_late_record_refuses_foreign_resolution_payload(tmp_path: Path) -> None:
    outcome = _late_record(tmp_path)
    altered = outcome.resolution.model_copy(update={"source_payload_sha256": "0" * 64})
    with pytest.raises(ValidationError, match="disagrees with source"):
        outcome.model_copy(update={"resolution": altered})


def test_forecast_receipt_created_after_finality_is_refused(tmp_path: Path) -> None:
    outcome = _late_record(tmp_path)
    original = outcome.snapshot_receipt
    too_late = FINAL_AT + timedelta(seconds=1)
    altered = original.model_copy(
        update={
            "persisted_at": too_late,
            "receipt_id": build_persistence_receipt_id(
                experiment_id=original.experiment_id,
                artifact_kind=original.artifact_kind,
                artifact_id=original.artifact_id,
                artifact_schema_version=original.artifact_schema_version,
                artifact_sha256=original.artifact_sha256,
                artifact_byte_length=original.artifact_byte_length,
                persisted_at=too_late,
                storage_backend=original.storage_backend,
                storage_identity=original.storage_identity,
            ),
        }
    )
    new_id = build_late_final_outcome_id(
        protocol_receipt=outcome.protocol_receipt,
        snapshot_receipt=altered,
        lifecycle_receipts=outcome.lifecycle_receipts,
        resolution=outcome.resolution,
        selected_cutoff=outcome.selected_cutoff,
    )
    with pytest.raises(ValidationError, match="not durable before finality"):
        outcome.model_copy(update={"snapshot_receipt": altered, "late_outcome_id": new_id})


def test_source_timestamp_before_forecast_freeze_is_refused(tmp_path: Path) -> None:
    outcome = _late_record(tmp_path)
    altered_resolution = outcome.resolution.model_copy(
        update={"resolved_at": outcome.snapshot.frozen_at - timedelta(seconds=1)}
    )
    altered_observation = outcome.lifecycle_observations[-1].model_copy(
        update={
            "source_time": altered_resolution.resolved_at,
            "lifecycle_observation_id": build_lifecycle_observation_id(
                experiment_id=EXPERIMENT,
                target_id=outcome.snapshot.target.target_id,
                ordinal=2,
                previous_observation_id=outcome.lifecycle_observations[0].lifecycle_observation_id,
                source="gamma",
                endpoint="https://gamma-api.polymarket.com/markets/market-1",
                source_time=altered_resolution.resolved_at,
                retrieved_at=FINAL_AT,
                raw_payload_sha256=altered_resolution.source_payload_sha256,
                byte_length=outcome.lifecycle_observations[-1].byte_length,
                raw_payload_location=outcome.lifecycle_observations[-1].raw_payload_location,
                finality=ResolutionStatus.FINAL,
                resolution_id=altered_resolution.resolution_id,
                resolution_record_sha256=record_sha256(altered_resolution.to_record()),
            ),
            "resolution_record_sha256": record_sha256(altered_resolution.to_record()),
        }
    )
    altered_receipt = persist_evidence_record(
        tmp_path / "evidence",
        record=altered_observation,
        experiment_id=EXPERIMENT,
        artifact_kind=EvidenceArtifactKind.LIFECYCLE_OBSERVATION,
        artifact_id=altered_observation.lifecycle_observation_id,
        persisted_at=FINAL_AT + timedelta(seconds=1),
    )
    altered_receipts = (outcome.lifecycle_receipts[0], altered_receipt)
    with pytest.raises(ValidationError, match="source terminal timestamp"):
        outcome.model_copy(
            update={
                "late_outcome_id": build_late_final_outcome_id(
                    protocol_receipt=outcome.protocol_receipt,
                    snapshot_receipt=outcome.snapshot_receipt,
                    lifecycle_receipts=altered_receipts,
                    resolution=altered_resolution,
                    selected_cutoff=outcome.selected_cutoff,
                ),
                "resolution": altered_resolution,
                "lifecycle_observations": (
                    outcome.lifecycle_observations[0],
                    altered_observation,
                ),
                "lifecycle_receipts": altered_receipts,
            }
        )


def test_missing_raw_source_blocks_archive_verification(tmp_path: Path) -> None:
    outcome = _late_record(tmp_path)
    final = outcome.lifecycle_observations[-1]
    (tmp_path / "source" / final.raw_payload_location).unlink()
    with pytest.raises(StorageError, match="no archived payload"):
        verify_late_outcome_archives(
            outcome, evidence_dir=tmp_path / "evidence", source_dir=tmp_path / "source"
        )


def test_corrupt_snapshot_bytes_block_archive_verification(tmp_path: Path) -> None:
    outcome = _late_record(tmp_path)
    (tmp_path / "evidence" / outcome.snapshot_receipt.storage_identity).write_bytes(b"tampered")
    with pytest.raises(ImmutabilityViolationError, match="no longer matches"):
        verify_late_outcome_archives(
            outcome, evidence_dir=tmp_path / "evidence", source_dir=tmp_path / "source"
        )


def test_changed_source_provenance_blocks_archive_verification(tmp_path: Path) -> None:
    outcome = _late_record(tmp_path)
    final = outcome.lifecycle_observations[-1]
    source_path = tmp_path / "source" / final.raw_payload_location
    sidecar = source_path.with_name(f"{final.raw_payload_sha256}.meta.json")
    changed = orjson.loads(sidecar.read_bytes())
    changed["source"] = "other"
    sidecar.write_bytes(orjson.dumps(changed))
    with pytest.raises(ValueError, match="source disagrees"):
        verify_late_outcome_archives(
            outcome, evidence_dir=tmp_path / "evidence", source_dir=tmp_path / "source"
        )


def test_non_gamma_source_cannot_be_verified_as_gamma(tmp_path: Path) -> None:
    outcome = _late_record(tmp_path)
    original = outcome.lifecycle_observations[-1]
    payload = orjson.loads((tmp_path / "source" / original.raw_payload_location).read_bytes())
    payload["fixture_marker"] = "distinct bytes"
    altered = _late_outcome_with_final_source(
        tmp_path, outcome, orjson.dumps(payload), source="other"
    )
    with pytest.raises(ValueError, match="only supports Gamma"):
        verify_late_outcome_archives(
            altered, evidence_dir=tmp_path / "evidence", source_dir=tmp_path / "source"
        )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"{", "not JSON"),
        (b"[]", "not a market object"),
        (b'{"id":"market-1","closed":false}', "does not yield the claimed resolution"),
    ],
)
def test_false_final_raw_payload_is_refused(tmp_path: Path, raw: bytes, message: str) -> None:
    outcome = _late_record(tmp_path)
    altered = _late_outcome_with_final_source(tmp_path, outcome, raw)
    with pytest.raises(ValueError, match=message):
        verify_late_outcome_archives(
            altered, evidence_dir=tmp_path / "evidence", source_dir=tmp_path / "source"
        )


def test_archived_earlier_final_cannot_be_hidden_as_proposed(tmp_path: Path) -> None:
    outcome = _late_record(tmp_path)
    earlier_at = outcome.lifecycle_observations[0].retrieved_at
    raw_sha, byte_length, location = _archived_gamma(
        tmp_path,
        {
            "id": "market-1",
            "conditionId": CONDITION,
            "closed": True,
            "outcomePrices": ["1", "0"],
            "clobTokenIds": ["11", "22"],
            "umaResolutionStatuses": ["resolved"],
            "updatedAt": (earlier_at - timedelta(minutes=1)).isoformat(),
        },
        earlier_at,
    )
    prior = outcome.lifecycle_observations[0]
    prior = prior.model_copy(
        update={
            "raw_payload_sha256": raw_sha,
            "byte_length": byte_length,
            "raw_payload_location": location,
            "lifecycle_observation_id": build_lifecycle_observation_id(
                experiment_id=EXPERIMENT,
                target_id=prior.target_id,
                ordinal=1,
                previous_observation_id=None,
                source=prior.source,
                endpoint=prior.endpoint,
                source_time=None,
                retrieved_at=earlier_at,
                raw_payload_sha256=raw_sha,
                byte_length=byte_length,
                raw_payload_location=location,
                finality=ResolutionStatus.PROPOSED,
                resolution_id=None,
                resolution_record_sha256=None,
            ),
        }
    )
    prior_receipt = persist_evidence_record(
        tmp_path / "evidence",
        record=prior,
        experiment_id=EXPERIMENT,
        artifact_kind=EvidenceArtifactKind.LIFECYCLE_OBSERVATION,
        artifact_id=prior.lifecycle_observation_id,
        persisted_at=earlier_at + timedelta(seconds=1),
    )
    old_final = outcome.lifecycle_observations[-1]
    final = old_final.model_copy(
        update={
            "previous_observation_id": prior.lifecycle_observation_id,
            "lifecycle_observation_id": build_lifecycle_observation_id(
                experiment_id=EXPERIMENT,
                target_id=old_final.target_id,
                ordinal=2,
                previous_observation_id=prior.lifecycle_observation_id,
                source=old_final.source,
                endpoint=old_final.endpoint,
                source_time=old_final.source_time,
                retrieved_at=old_final.retrieved_at,
                raw_payload_sha256=old_final.raw_payload_sha256,
                byte_length=old_final.byte_length,
                raw_payload_location=old_final.raw_payload_location,
                finality=ResolutionStatus.FINAL,
                resolution_id=old_final.resolution_id,
                resolution_record_sha256=old_final.resolution_record_sha256,
            ),
        }
    )
    final_receipt = persist_evidence_record(
        tmp_path / "evidence",
        record=final,
        experiment_id=EXPERIMENT,
        artifact_kind=EvidenceArtifactKind.LIFECYCLE_OBSERVATION,
        artifact_id=final.lifecycle_observation_id,
        persisted_at=FINAL_AT + timedelta(seconds=1),
    )
    receipts = (prior_receipt, final_receipt)
    forged = outcome.model_copy(
        update={
            "late_outcome_id": build_late_final_outcome_id(
                protocol_receipt=outcome.protocol_receipt,
                snapshot_receipt=outcome.snapshot_receipt,
                lifecycle_receipts=receipts,
                resolution=outcome.resolution,
                selected_cutoff=outcome.selected_cutoff,
            ),
            "lifecycle_observations": (prior, final),
            "lifecycle_receipts": receipts,
        }
    )
    with pytest.raises(ValueError, match="earlier source was already final"):
        verify_late_outcome_archives(
            forged, evidence_dir=tmp_path / "evidence", source_dir=tmp_path / "source"
        )
