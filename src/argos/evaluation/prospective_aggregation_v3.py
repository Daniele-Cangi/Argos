"""Version-forward aggregate for evidence-bound operational protocols."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar, cast

from pydantic import model_validator

from argos.clock import ensure_utc
from argos.domain.versioning import ensure_supported_version
from argos.evaluation import prospective_aggregation as aggregation_v1
from argos.evaluation.prospective import (
    EvidencePersistenceReceiptV1,
    ProspectiveExperimentProtocolV2,
)
from argos.evaluation.prospective_aggregation import (
    ProspectiveExperimentReportV1,
    ProspectiveTargetContributionV1,
)
from argos.evaluation.prospective_aggregation_v2 import (
    ProspectiveExperimentBundleV2,
    ProspectiveTargetExclusionV2,
    prospective_experiment_digest_v2,
)
from argos.evaluation.prospective_bundle_v4 import EvaluationRunBundleV4

__all__ = ["ProspectiveExperimentBundleV3", "aggregate_prospective_experiment_v3"]


class ProspectiveExperimentBundleV3(ProspectiveExperimentBundleV2):
    """Aggregate whose protocol and resolved target cutoffs are deadline-bound."""

    schema_version: ClassVar[str] = "prospective_experiment_bundle.v3"

    protocol: ProspectiveExperimentProtocolV2
    target_bundles: tuple[EvaluationRunBundleV4, ...]

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> ProspectiveExperimentBundleV3:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["protocol"] = ProspectiveExperimentProtocolV2.from_record(dict(payload["protocol"]))
        payload["protocol_receipt"] = EvidencePersistenceReceiptV1.from_record(
            dict(payload["protocol_receipt"])
        )
        payload["target_bundles"] = tuple(
            EvaluationRunBundleV4.from_record(dict(item)) for item in payload["target_bundles"]
        )
        payload["target_exclusions"] = tuple(
            ProspectiveTargetExclusionV2.from_record(dict(item))
            for item in payload["target_exclusions"]
        )
        payload["contributions"] = tuple(
            ProspectiveTargetContributionV1.from_record(dict(item))
            for item in payload["contributions"]
        )
        payload["report"] = ProspectiveExperimentReportV1.from_record(dict(payload["report"]))
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def _capture_bounds_match_protocol(self) -> ProspectiveExperimentBundleV3:
        run_ids: set[str] = set()
        for bundle in self.target_bundles:
            capture_run_id = bundle.capture_run_manifest.capture_run_id
            if capture_run_id is None:
                raise ValueError("prospective capture manifest omits its capture run id")
            if capture_run_id in run_ids:
                raise ValueError("prospective targets cannot share one capture run")
            run_ids.add(capture_run_id)
        for exclusion in self.target_exclusions:
            manifest = exclusion.capture_evidence.capture_run_manifest
            parameters = manifest.run_parameters
            capture_run_id = manifest.capture_run_id
            if capture_run_id is None:
                raise ValueError("prospective capture manifest omits its capture run id")
            if capture_run_id in run_ids:
                raise ValueError("prospective targets cannot share one capture run")
            run_ids.add(capture_run_id)
            try:
                max_seconds = Decimal(str(parameters.get("max_seconds")))
            except InvalidOperation as error:
                raise ValueError("capture duration is not a numeric frozen bound") from error
            if max_seconds != Decimal(self.protocol.capture_max_seconds_per_target):
                raise ValueError("capture duration disagrees with the frozen protocol")
            if parameters.get("max_frames") != self.protocol.capture_max_frames_per_target:
                raise ValueError("capture frame bound disagrees with the frozen protocol")
            if parameters.get("raw_archive") is not self.protocol.capture_raw_archive:
                raise ValueError("capture raw-archive policy disagrees with the frozen protocol")
        return self


def aggregate_prospective_experiment_v3(
    *,
    protocol: ProspectiveExperimentProtocolV2,
    protocol_receipt: EvidencePersistenceReceiptV1,
    target_bundles: Sequence[EvaluationRunBundleV4],
    target_exclusions: Sequence[ProspectiveTargetExclusionV2],
    created_at: datetime,
    observation_complete: bool,
) -> ProspectiveExperimentBundleV3:
    """Build the operationally bound aggregate without changing V2 history."""

    bundles = tuple(sorted(target_bundles, key=lambda item: item.target.selection_rank))
    exclusions = tuple(sorted(target_exclusions, key=lambda item: item.target.selection_rank))
    contributions = aggregation_v1._target_contributions(bundles)
    report_exclusions = cast(
        Sequence[aggregation_v1.ProspectiveTargetExclusionV1],
        exclusions,
    )
    report = aggregation_v1._experiment_report(
        protocol=protocol,
        bundles=bundles,
        exclusions=report_exclusions,
        contributions=contributions,
        created_at=ensure_utc(created_at),
        observation_complete=observation_complete,
    )
    digest = prospective_experiment_digest_v2(
        protocol=protocol,
        protocol_receipt=protocol_receipt,
        target_bundles=bundles,
        target_exclusions=exclusions,
        contributions=contributions,
        report=report,
    )
    return ProspectiveExperimentBundleV3(
        protocol=protocol,
        protocol_receipt=protocol_receipt,
        target_bundles=bundles,
        target_exclusions=exclusions,
        contributions=contributions,
        report=report,
        evidence_digest=digest,
    )
