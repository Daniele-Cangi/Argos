"""ADR-0020 declaration only; not an admission, forecast or live-run capability.

V1 remains readable under its original rules. V2 deliberately does not inherit
its fixed 4x4 denominator or the older continuous-lifecycle deadline. Source
selection, semantic-review receipts and a unified finality owner must implement
this contract before a V2 live launch is possible.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from itertools import combinations, pairwise
from typing import Any, ClassVar, Literal

from pydantic import Field, field_validator, model_validator

from argos.clock import ensure_utc
from argos.domain.versioning import VersionedModel, ensure_supported_version
from argos.evaluation.async_cohort import CohortBlockV1, GammaSelectionV1


class CohortStratumV1(VersionedModel):
    """Disjoint, lower-inclusive/upper-exclusive bands, measured at selection.

    Category and earliest outcome-knowable time require a human-reviewed contract;
    the Gamma end timestamp alone does not establish the latter. Quotas are caps,
    not promises or observed-outcome stopping conditions.
    """

    schema_version: ClassVar[str] = "m4_cohort_stratum.v1"

    stratum_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    category: str = Field(min_length=1)
    minimum_liquidity: Decimal = Field(ge=0)
    maximum_liquidity: Decimal = Field(gt=0)
    minimum_spread: Decimal = Field(ge=0, le=1)
    maximum_spread: Decimal = Field(gt=0, le=1)
    minimum_horizon_seconds: int = Field(gt=0, strict=True)
    maximum_horizon_seconds: int = Field(gt=0, strict=True)
    maximum_targets: int = Field(gt=0, strict=True)

    @field_validator("category")
    @classmethod
    def _canonical_category(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("category must be nonblank with no surrounding whitespace")
        return value

    @model_validator(mode="after")
    def _increasing_bands(self) -> CohortStratumV1:
        if (
            self.minimum_liquidity >= self.maximum_liquidity
            or self.minimum_spread >= self.maximum_spread
            or self.minimum_horizon_seconds >= self.maximum_horizon_seconds
        ):
            raise ValueError("stratum bands must be strictly increasing")
        return self

    def overlaps(self, other: CohortStratumV1) -> bool:
        """Adjacent half-open bands do not overlap; category labels are exact."""
        return (
            self.category == other.category
            and self.minimum_liquidity < other.maximum_liquidity
            and other.minimum_liquidity < self.maximum_liquidity
            and self.minimum_spread < other.maximum_spread
            and other.minimum_spread < self.maximum_spread
            and self.minimum_horizon_seconds < other.maximum_horizon_seconds
            and other.minimum_horizon_seconds < self.maximum_horizon_seconds
        )


class AsynchronousCohortProtocolV2(VersionedModel):
    """Budgeted descriptive cohort; cannot authorize a calibration/edge claim.

    Block ``intended_targets`` are maximum admission slots in V2. Unused slots
    remain accountable, but never invalidate valid targets or prevent later blocks.
    Policies are declarations, not proof that runtime enforcement has occurred.
    """

    schema_version: ClassVar[str] = "m4_asynchronous_cohort_protocol.v2"

    experiment_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    declared_at: datetime
    code_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    working_tree_state: Literal["CLEAN"] = "CLEAN"
    target_population: str = Field(min_length=1)
    blocks: tuple[CohortBlockV1, ...] = Field(min_length=1)
    selection: GammaSelectionV1
    strata: tuple[CohortStratumV1, ...] = Field(min_length=1)
    target_budget: int = Field(gt=0, strict=True)
    capture_max_seconds_per_target: int = Field(gt=0, le=120, strict=True)
    capture_max_frames_per_target: int = Field(gt=0, le=500, strict=True)
    capture_max_bytes_per_target: int = Field(gt=0, strict=True)
    finalization_reserve_seconds: int = Field(gt=0, strict=True)
    evidence_reserve_bytes: int = Field(gt=0, strict=True)
    campaign_max_bytes: int = Field(gt=0, strict=True)
    free_disk_margin_bytes: int = Field(gt=0, strict=True)
    outcome_blind_margin_seconds: int = Field(gt=0, strict=True)
    operational_review_at: datetime
    follow_up_until: datetime
    poll_interval_seconds: int = Field(gt=0, strict=True)
    maximum_poll_gap_seconds: int = Field(gt=0, strict=True)
    log_loss_epsilon: Decimal = Field(gt=0, lt=Decimal("0.5"))

    admission_policy: Literal["retain_partial_blocks_no_slot_transfer"] = (
        "retain_partial_blocks_no_slot_transfer"
    )
    stopping_rule: Literal["target_budget_or_last_block_end_or_resource_limit"] = (
        "target_budget_or_last_block_end_or_resource_limit"
    )
    selection_order: Literal["stratum_id_then_liquidity_desc_then_numeric_market_id"] = (
        "stratum_id_then_liquidity_desc_then_numeric_market_id"
    )
    replacement_policy: Literal["pre_admission_only_never_after_target_receipt"] = (
        "pre_admission_only_never_after_target_receipt"
    )
    semantic_review_policy: Literal["human_review_receipt_before_admission"] = (
        "human_review_receipt_before_admission"
    )
    independence_policy: Literal["one_target_per_reviewed_real_world_event_group"] = (
        "one_target_per_reviewed_real_world_event_group"
    )
    snapshot_policy: Literal["last_shared_state_before_capture_close"] = (
        "last_shared_state_before_capture_close"
    )
    finality_policy: Literal["first_observed_final_no_backdating"] = (
        "first_observed_final_no_backdating"
    )
    missingness_policy: Literal["retain_all_slots_targets_and_method_abstentions"] = (
        "retain_all_slots_targets_and_method_abstentions"
    )
    primary_metric: Literal["brier_score"] = "brier_score"
    secondary_metric: Literal["log_loss"] = "log_loss"
    diagnostic_metric: Literal["absolute_error"] = "absolute_error"
    comparison_policy: Literal["paired_same_snapshot_descriptive_only"] = (
        "paired_same_snapshot_descriptive_only"
    )
    calibration_claim: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"

    @field_validator("declared_at", "operational_review_at", "follow_up_until")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("target_population")
    @classmethod
    def _nonblank_population(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("target population must be nonblank")
        return value

    @model_validator(mode="after")
    def _coherent_budget(self) -> AsynchronousCohortProtocolV2:
        if any(block.ordinal != index for index, block in enumerate(self.blocks, start=1)):
            raise ValueError("blocks must have contiguous ordinals starting at one")
        if any(left.end > right.start for left, right in pairwise(self.blocks)):
            raise ValueError("blocks must be chronological and nonoverlapping")
        if self.declared_at >= self.blocks[0].start:
            raise ValueError("declaration must precede the first block")
        if sum(block.intended_targets for block in self.blocks) != self.target_budget:
            raise ValueError("block slot caps must sum to target budget")
        if sum(stratum.maximum_targets for stratum in self.strata) != self.target_budget:
            raise ValueError("stratum caps must sum to target budget")
        if len({stratum.stratum_id for stratum in self.strata}) != len(self.strata):
            raise ValueError("stratum identities must be unique")
        if any(left.overlaps(right) for left, right in combinations(self.strata, 2)):
            raise ValueError("stratum bands must not overlap")
        reserved_seconds = self.capture_max_seconds_per_target + self.finalization_reserve_seconds
        if any(
            (block.end - block.start).total_seconds() < reserved_seconds for block in self.blocks
        ):
            raise ValueError("each block must fit at least one capture and finalization reserve")
        if any(
            stratum.minimum_horizon_seconds < reserved_seconds + self.outcome_blind_margin_seconds
            for stratum in self.strata
        ):
            raise ValueError("horizon must reserve capture, finalization and outcome-blind margin")
        reserved_window = timedelta(seconds=reserved_seconds)
        if any(
            not any(
                self.selection.target_end_max
                >= block.start + timedelta(seconds=stratum.minimum_horizon_seconds)
                and self.selection.target_end_min
                < block.end - reserved_window + timedelta(seconds=stratum.maximum_horizon_seconds)
                for stratum in self.strata
            )
            for block in self.blocks
        ):
            raise ValueError("Gamma target-end window cannot intersect a block's stratum horizon")
        if any(
            stratum.minimum_liquidity < self.selection.minimum_liquidity for stratum in self.strata
        ):
            raise ValueError("stratum cannot extend below discovery liquidity bound")
        required_bytes = self.target_budget * self.capture_max_bytes_per_target
        if self.campaign_max_bytes < required_bytes + self.evidence_reserve_bytes:
            raise ValueError("campaign byte cap cannot fund target caps and evidence reserve")
        if self.operational_review_at < self.blocks[-1].end:
            raise ValueError("operational review cannot precede the final block end")
        if self.follow_up_until < self.operational_review_at:
            raise ValueError("follow-up must include the operational review")
        if self.maximum_poll_gap_seconds < self.poll_interval_seconds:
            raise ValueError("maximum gap cannot be shorter than the scheduled interval")
        return self

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        record["blocks"] = [block.to_record() for block in self.blocks]
        record["selection"] = self.selection.to_record()
        record["strata"] = [stratum.to_record() for stratum in self.strata]
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> AsynchronousCohortProtocolV2:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["blocks"] = tuple(
            CohortBlockV1.from_record(dict(item)) for item in payload["blocks"]
        )
        payload["selection"] = GammaSelectionV1.from_record(dict(payload["selection"]))
        payload["strata"] = tuple(
            CohortStratumV1.from_record(dict(item)) for item in payload["strata"]
        )
        return cls.model_validate(payload)
