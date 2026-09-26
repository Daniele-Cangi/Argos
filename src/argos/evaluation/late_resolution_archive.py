"""Read-only archive verification for an append-only late outcome.

The pure late-resolution contract proves temporal and identity consistency;
this boundary checks that its referenced receipt and Gamma source bytes are
actually present. It does not issue polls, repair evidence, or score forecasts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import orjson

from argos.evaluation.late_resolution import FrozenForecastSnapshotV1, LateFinalOutcomeV1
from argos.evaluation.prospective import (
    LifecycleObservationV1,
    ProspectiveExperimentProtocolV2,
    ProspectiveTargetV1,
    load_persisted_record,
)
from argos.resolution import ResolutionStatus, ResolutionV1, normalize_gamma_resolution
from argos.store.raw_archive import archive_relative_location, read_raw_payload

__all__ = ["verify_late_outcome_archives"]


def verify_late_outcome_archives(
    outcome: LateFinalOutcomeV1, *, evidence_dir: Path, source_dir: Path
) -> None:
    """Reject absent/corrupt receipts and raw bytes, including an earlier hidden final."""
    if (
        load_persisted_record(
            evidence_dir, outcome.protocol_receipt, ProspectiveExperimentProtocolV2
        )
        != outcome.protocol
    ):
        raise ValueError("archived late protocol differs from the claimed protocol")
    if (
        load_persisted_record(evidence_dir, outcome.snapshot.target_receipt, ProspectiveTargetV1)
        != outcome.snapshot.target
    ):
        raise ValueError("archived late target differs from the claimed target")
    if (
        load_persisted_record(evidence_dir, outcome.snapshot_receipt, FrozenForecastSnapshotV1)
        != outcome.snapshot
    ):
        raise ValueError("archived frozen forecast differs from the claimed snapshot")

    for observation, receipt in zip(
        outcome.lifecycle_observations, outcome.lifecycle_receipts, strict=True
    ):
        if load_persisted_record(evidence_dir, receipt, LifecycleObservationV1) != observation:
            raise ValueError("archived lifecycle observation differs from the claimed chain")
        raw, provenance = read_raw_payload(source_dir, observation.raw_payload_sha256)
        if (
            provenance.reconstructed
            or provenance.source != observation.source
            or provenance.endpoint != observation.endpoint
            or provenance.retrieved_at > observation.retrieved_at
            or provenance.byte_length != observation.byte_length
            or archive_relative_location(provenance) != observation.raw_payload_location
        ):
            raise ValueError("archived lifecycle source disagrees with the claimed observation")
        if observation.source != "gamma":
            raise ValueError("late outcome verifier only supports Gamma lifecycle sources")
        try:
            payload: Any = orjson.loads(raw)
        except orjson.JSONDecodeError as error:
            raise ValueError("archived lifecycle source is not JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("archived lifecycle source is not a market object")
        normalized = normalize_gamma_resolution(
            payload,
            source_payload_sha256=observation.raw_payload_sha256,
            normalized_at=(
                outcome.resolution.normalized_at
                if observation.finality is ResolutionStatus.FINAL
                else observation.retrieved_at
            ),
        )
        if observation.finality is ResolutionStatus.FINAL:
            if not isinstance(normalized, ResolutionV1) or normalized != outcome.resolution:
                raise ValueError("archived final source does not yield the claimed resolution")
        elif (
            isinstance(normalized, ResolutionV1)
            and normalized.resolution_status is ResolutionStatus.FINAL
        ):
            raise ValueError("archived earlier source was already final")
