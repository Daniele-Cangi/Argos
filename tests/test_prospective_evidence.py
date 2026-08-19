from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from argos.config.manifest import WorkingTreeStatus
from argos.evaluation.prospective import (
    AcrossTargetWeighting,
    CutoffBasis,
    EvidenceArtifactKind,
    EvidencePersistenceReceiptV1,
    LifecycleObservationV1,
    ProspectiveExperimentProtocolV1,
    ProspectiveTargetV1,
    ResolutionCutoffEvidenceV1,
    StandaloneLastTradePolicy,
    WithinTargetAggregation,
    build_cutoff_evidence_id,
    build_lifecycle_observation_id,
    build_persistence_receipt_id,
    build_target_id,
    load_persisted_record,
    persist_evidence_record,
    verify_receipt_for_record,
)
from argos.resolution.gamma_resolution import ResolutionStatus

DECLARED_AT = datetime(2026, 8, 19, 20, 0, tzinfo=UTC)
WINDOW_START = DECLARED_AT + timedelta(hours=1)
WINDOW_END = WINDOW_START + timedelta(days=2)


def _protocol(**updates: object) -> ProspectiveExperimentProtocolV1:
    fields: dict[str, object] = {
        "experiment_id": "m4-pilot-2026-08-19",
        "declared_at": DECLARED_AT,
        "target_population": "public liquid binary Polymarket markets ending in the window",
        "inclusion_rules": (
            "active, open, non-archived, standard Yes/No market",
            "public CLOB order book available for both tokens",
        ),
        "exclusion_rules": (
            "contract or persistence receipt unavailable before capture",
            "unmodeled standalone last_trade_price observed for this target",
        ),
        "market_selection_mechanism": (
            "sort eligible markets by descending liquidity, then market id; take first two"
        ),
        "observation_window_start": WINDOW_START,
        "observation_window_end": WINDOW_END,
        "stopping_rule": "stop after two selected targets resolve or the window ends",
        "structural_minimum_independent_resolved_targets": 2,
        "minimum_intended_resolved_target_count": 2,
        "scientific_minimum_resolved_target_count": 30,
        "minimum_target_count_rationale": (
            "thirty is predeclared for a future calibration study; this pilot is structural"
        ),
        "within_target_aggregation": (
            WithinTargetAggregation.LAST_ADMISSIBLE_PRE_CUTOFF_PER_METHOD
        ),
        "across_target_weighting": AcrossTargetWeighting.EQUAL_RESOLVED_TARGET,
        "metrics": ("brier_score", "log_loss", "absolute_error"),
        "calibration_bin_edges": (Decimal("0"), Decimal("0.5"), Decimal("1")),
        "log_loss_epsilon": Decimal("0.000001"),
        "uncertainty_reporting": "target bootstrap intervals only when scientifically sufficient",
        "missingness_treatment": "preserve missing targets with reason and zero weight",
        "abstention_treatment": "preserve each abstention and exclude it from scoring",
        "disputed_unresolved_treatment": "exclude until first observed final settlement",
        "category_dispersion_requirement": "at least two categories for calibration",
        "outcome_dispersion_requirement": "at least five YES and five NO outcomes",
        "minimum_category_count_for_calibration": 2,
        "minimum_yes_outcomes_for_calibration": 5,
        "minimum_no_outcomes_for_calibration": 5,
        "protocol_sufficiency_rule": (
            "calibration requires all dispersion rules and at least thirty resolved targets"
        ),
        "cutoff_basis": CutoffBasis.FIRST_OBSERVED_FINAL_SETTLEMENT,
        "standalone_last_trade_policy": StandaloneLastTradePolicy.EXCLUDE_TARGET,
        "target_replacement_policy": "no replacement after capture begins",
        "code_revision": "a" * 40,
        "working_tree": WorkingTreeStatus.CLEAN,
        "config_fingerprint": "b" * 64,
    }
    fields.update(updates)
    return ProspectiveExperimentProtocolV1.model_validate(fields)


def test_protocol_keeps_structural_and_scientific_minima_separate() -> None:
    protocol = _protocol()
    assert protocol.structural_minimum_independent_resolved_targets == 2
    assert protocol.minimum_intended_resolved_target_count == 2
    assert protocol.scientific_minimum_resolved_target_count == 30


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"declared_at": WINDOW_START + timedelta(seconds=1)}, "declared before"),
        ({"observation_window_end": WINDOW_START}, "positive duration"),
        (
            {
                "minimum_intended_resolved_target_count": 2,
                "structural_minimum_independent_resolved_targets": 3,
            },
            "structural",
        ),
        (
            {
                "minimum_intended_resolved_target_count": 3,
                "scientific_minimum_resolved_target_count": 2,
            },
            "scientific sufficiency",
        ),
        ({"metrics": ("brier_score",)}, "required scoring metrics"),
        ({"calibration_bin_edges": (Decimal("0"), Decimal("1"))}, "calibration bins"),
        (
            {
                "calibration_bin_edges": (
                    Decimal("0"),
                    Decimal("0.4"),
                    Decimal("1"),
                )
            },
            "equal-width",
        ),
        ({"working_tree": WorkingTreeStatus.DIRTY}, "clean revision"),
    ],
)
def test_protocol_refuses_claim_strengthening_shortcuts(
    updates: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _protocol(**updates)


def test_persistence_receipt_round_trips_canonical_bytes_and_keeps_first_time(
    tmp_path: Path,
) -> None:
    protocol = _protocol()
    first = persist_evidence_record(
        tmp_path,
        record=protocol,
        experiment_id=protocol.experiment_id,
        artifact_kind=EvidenceArtifactKind.EXPERIMENT_PROTOCOL,
        artifact_id=protocol.experiment_id,
        persisted_at=DECLARED_AT,
    )
    second = persist_evidence_record(
        tmp_path,
        record=protocol,
        experiment_id=protocol.experiment_id,
        artifact_kind=EvidenceArtifactKind.EXPERIMENT_PROTOCOL,
        artifact_id=protocol.experiment_id,
        persisted_at=DECLARED_AT + timedelta(hours=6),
    )

    assert second == first
    assert first.persisted_at == DECLARED_AT
    assert not first.storage_identity.startswith(("/", "\\"))
    assert load_persisted_record(tmp_path, first, ProspectiveExperimentProtocolV1) == protocol


def test_hash_valid_false_receipt_still_disagrees_with_the_artifact(tmp_path: Path) -> None:
    protocol = _protocol()
    receipt = persist_evidence_record(
        tmp_path,
        record=protocol,
        experiment_id=protocol.experiment_id,
        artifact_kind=EvidenceArtifactKind.EXPERIMENT_PROTOCOL,
        artifact_id=protocol.experiment_id,
        persisted_at=DECLARED_AT,
    )
    false_digest = "0" * 64
    false_id = build_persistence_receipt_id(
        experiment_id=receipt.experiment_id,
        artifact_kind=receipt.artifact_kind,
        artifact_id=receipt.artifact_id,
        artifact_schema_version=receipt.artifact_schema_version,
        artifact_sha256=false_digest,
        artifact_byte_length=receipt.artifact_byte_length,
        persisted_at=receipt.persisted_at,
        storage_backend=receipt.storage_backend,
        storage_identity=receipt.storage_identity,
    )
    contradictory = receipt.model_copy(
        update={"artifact_sha256": false_digest, "receipt_id": false_id}
    )

    with pytest.raises(ValueError, match="canonical artifact"):
        verify_receipt_for_record(contradictory, protocol)


def test_receipt_refuses_machine_specific_or_escaping_storage_identity() -> None:
    fields = {
        "receipt_id": "temporary",
        "experiment_id": "experiment",
        "artifact_kind": EvidenceArtifactKind.EXPERIMENT_PROTOCOL,
        "artifact_id": "protocol",
        "artifact_schema_version": ProspectiveExperimentProtocolV1.schema_version,
        "artifact_sha256": "a" * 64,
        "artifact_byte_length": 10,
        "persisted_at": DECLARED_AT,
        "storage_identity": "../outside.json",
    }
    with pytest.raises(ValidationError, match="relative"):
        EvidencePersistenceReceiptV1.model_validate(fields)


def _target(**updates: object) -> ProspectiveTargetV1:
    experiment_id = "m4-pilot-2026-08-19"
    market_id = "3647905"
    condition_id = "0xcondition"
    yes_token_id = "11"
    no_token_id = "22"
    fields: dict[str, object] = {
        "target_id": build_target_id(
            experiment_id=experiment_id,
            market_id=market_id,
            condition_id=condition_id,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
        ),
        "experiment_id": experiment_id,
        "selection_rank": 1,
        "selected_at": WINDOW_START - timedelta(minutes=30),
        "market_id": market_id,
        "condition_id": condition_id,
        "yes_token_id": yes_token_id,
        "no_token_id": no_token_id,
        "category": "sports",
        "cutoff_basis": CutoffBasis.FIRST_OBSERVED_FINAL_SETTLEMENT,
        "market_record_sha256": "c" * 64,
        "market_receipt_id": "receipt-market",
        "contract_id": "contract-1",
        "contract_record_sha256": "d" * 64,
        "contract_receipt_id": "receipt-contract",
    }
    fields.update(updates)
    return ProspectiveTargetV1.model_validate(fields)


def test_target_identity_binds_the_yes_no_mapping() -> None:
    target = _target()
    with pytest.raises(ValidationError, match="target_id disagrees"):
        _target(no_token_id="33", target_id=target.target_id)


def _lifecycle(
    *,
    ordinal: int,
    previous: str | None,
    finality: ResolutionStatus,
    retrieved_at: datetime,
    resolution_id: str | None = None,
    resolution_digest: str | None = None,
) -> LifecycleObservationV1:
    raw_digest = f"{ordinal:x}" * 64
    byte_length = 100 + ordinal
    raw_location = f"gamma/{ordinal}.raw.json"
    fields: dict[str, object] = {
        "experiment_id": "m4-pilot-2026-08-19",
        "target_id": _target().target_id,
        "ordinal": ordinal,
        "previous_observation_id": previous,
        "source": "gamma",
        "endpoint": "https://gamma-api.polymarket.com/markets/3647905",
        "source_time": None,
        "retrieved_at": retrieved_at,
        "raw_payload_sha256": raw_digest,
        "byte_length": byte_length,
        "raw_payload_location": raw_location,
        "finality": finality,
        "resolution_id": resolution_id,
        "resolution_record_sha256": resolution_digest,
    }
    fields["lifecycle_observation_id"] = build_lifecycle_observation_id(
        experiment_id=str(fields["experiment_id"]),
        target_id=str(fields["target_id"]),
        ordinal=ordinal,
        previous_observation_id=previous,
        source=str(fields["source"]),
        endpoint=str(fields["endpoint"]),
        source_time=None,
        retrieved_at=retrieved_at,
        raw_payload_sha256=raw_digest,
        byte_length=byte_length,
        raw_payload_location=raw_location,
        finality=finality,
        resolution_id=resolution_id,
        resolution_record_sha256=resolution_digest,
    )
    return LifecycleObservationV1.model_validate(fields)


def test_lifecycle_chain_requires_a_bound_resolution_for_finality() -> None:
    with pytest.raises(ValidationError, match="must bind its resolution"):
        _lifecycle(
            ordinal=1,
            previous=None,
            finality=ResolutionStatus.FINAL,
            retrieved_at=WINDOW_END,
        )


def test_first_observed_final_cutoff_cannot_be_backdated() -> None:
    first = _lifecycle(
        ordinal=1,
        previous=None,
        finality=ResolutionStatus.PROPOSED,
        retrieved_at=WINDOW_END,
    )
    final_time = WINDOW_END + timedelta(minutes=5)
    final = _lifecycle(
        ordinal=2,
        previous=first.lifecycle_observation_id,
        finality=ResolutionStatus.FINAL,
        retrieved_at=final_time,
        resolution_id="resolution-1",
        resolution_digest="e" * 64,
    )
    selected_cutoff = final.retrieved_at - timedelta(minutes=1)
    fields: dict[str, object] = {
        "experiment_id": final.experiment_id,
        "target_id": final.target_id,
        "cutoff_basis": CutoffBasis.FIRST_OBSERVED_FINAL_SETTLEMENT,
        "lifecycle_observation_id": final.lifecycle_observation_id,
        "source": final.source,
        "endpoint": final.endpoint,
        "source_time": final.source_time,
        "retrieved_at": final.retrieved_at,
        "selected_cutoff": selected_cutoff,
        "raw_payload_sha256": final.raw_payload_sha256,
        "byte_length": final.byte_length,
        "raw_payload_location": final.raw_payload_location,
        "finality": final.finality,
        "resolution_id": final.resolution_id,
        "resolution_record_sha256": final.resolution_record_sha256,
    }
    fields["cutoff_evidence_id"] = build_cutoff_evidence_id(
        experiment_id=str(fields["experiment_id"]),
        target_id=str(fields["target_id"]),
        cutoff_basis=CutoffBasis.FIRST_OBSERVED_FINAL_SETTLEMENT,
        lifecycle_observation_id=str(fields["lifecycle_observation_id"]),
        source=str(fields["source"]),
        endpoint=str(fields["endpoint"]),
        source_time=None,
        retrieved_at=final.retrieved_at,
        selected_cutoff=selected_cutoff,
        raw_payload_sha256=str(fields["raw_payload_sha256"]),
        byte_length=final.byte_length,
        raw_payload_location=str(fields["raw_payload_location"]),
        resolution_id=str(fields["resolution_id"]),
        resolution_record_sha256=str(fields["resolution_record_sha256"]),
    )

    with pytest.raises(ValidationError, match="must equal retrieval time"):
        ResolutionCutoffEvidenceV1.model_validate(fields)


def test_source_terminal_cutoff_requires_a_source_time() -> None:
    fields = {
        "cutoff_evidence_id": "temporary",
        "experiment_id": "experiment",
        "target_id": "target",
        "cutoff_basis": CutoffBasis.SOURCE_TERMINAL_TIMESTAMP,
        "lifecycle_observation_id": "observation",
        "source": "gamma",
        "endpoint": "https://example.invalid/public",
        "source_time": None,
        "retrieved_at": WINDOW_END,
        "selected_cutoff": WINDOW_END,
        "raw_payload_sha256": "f" * 64,
        "byte_length": 1,
        "raw_payload_location": "gamma/f.raw.json",
        "finality": ResolutionStatus.FINAL,
        "resolution_id": "resolution",
        "resolution_record_sha256": "a" * 64,
    }
    with pytest.raises(ValidationError, match="source terminal time"):
        ResolutionCutoffEvidenceV1.model_validate(fields)
