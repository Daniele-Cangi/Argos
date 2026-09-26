"""Versioned contracts for bounded technical qualification campaigns.

These records deliberately keep technical reliability separate from market
resolution.  A campaign cannot become green by omitting a scenario: T1--T8
must each have exactly one explicit result, including ``NOT_RUN``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from pydantic import Field, field_validator, model_validator

from argos.clock import ensure_utc
from argos.domain.provenance import SHA256_LENGTH
from argos.domain.versioning import VersionedModel

__all__ = [
    "TechnicalCampaignV1",
    "TechnicalScenario",
    "TechnicalScenarioResultV1",
    "TechnicalScenarioSpecV1",
    "TechnicalScenarioStatus",
]


class TechnicalScenario(StrEnum):
    FUNCTIONAL = "T1_FUNCTIONAL"
    STABILITY = "T2_STABILITY"
    ENDURANCE = "T3_ENDURANCE"
    NETWORK_INTERRUPTION = "T4_NETWORK_INTERRUPTION"
    PROCESS_INTERRUPTION = "T5_PROCESS_INTERRUPTION"
    STORAGE_INTERRUPTION = "T6_STORAGE_INTERRUPTION"
    TERMINAL_SIMULATION = "T7_TERMINAL_SIMULATION"
    CROSS_VOLUME = "T8_CROSS_VOLUME"


class TechnicalScenarioStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    INCOMPLETE = "INCOMPLETE"
    NOT_RUN = "NOT_RUN"


class TechnicalScenarioSpecV1(VersionedModel):
    """Frozen bounds and expected outcome for one technical scenario."""

    schema_version: ClassVar[str] = "technical_scenario_spec.v1"

    scenario: TechnicalScenario
    maximum_duration_seconds: int = Field(gt=0)
    maximum_frame_count: int = Field(gt=0)
    expected_outcome: str = Field(min_length=1)
    fault_injection: str | None = None

    @field_validator("expected_outcome", "fault_injection")
    @classmethod
    def _nonblank_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("scenario text must not be blank")
        return value


class TechnicalScenarioResultV1(VersionedModel):
    """Auditable outcome for one declared scenario."""

    schema_version: ClassVar[str] = "technical_scenario_result.v1"

    campaign_id: str = Field(min_length=1)
    scenario: TechnicalScenario
    status: TechnicalScenarioStatus
    started_at: datetime | None = None
    last_checkpoint_at: datetime | None = None
    ended_at: datetime | None = None
    observed_frame_count: int = Field(ge=0)
    artifact_identities: tuple[str, ...] = ()
    observed_outcome: str | None = None
    reason: str | None = None
    limitations: tuple[str, ...] = ()
    follow_up_action: str | None = None

    @field_validator("started_at", "last_checkpoint_at", "ended_at")
    @classmethod
    def _utc_times(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    @field_validator("artifact_identities")
    @classmethod
    def _artifact_identities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("artifact identities must not be blank")
        if len(set(value)) != len(value):
            raise ValueError("artifact identities must be unique")
        return value

    @field_validator("observed_outcome", "reason")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("result text must not be blank")
        return value

    @field_validator("limitations")
    @classmethod
    def _limitations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("limitations must not be blank")
        return value

    @field_validator("follow_up_action")
    @classmethod
    def _follow_up(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("follow-up action must not be blank")
        return value

    @model_validator(mode="after")
    def _coherent_result(self) -> TechnicalScenarioResultV1:
        times = (self.started_at, self.last_checkpoint_at, self.ended_at)
        if self.status is TechnicalScenarioStatus.NOT_RUN:
            if any(value is not None for value in times):
                raise ValueError("NOT_RUN cannot carry execution timestamps")
            if self.observed_frame_count or self.artifact_identities or self.observed_outcome:
                raise ValueError("NOT_RUN cannot carry execution evidence")
            if self.reason is None:
                raise ValueError("NOT_RUN requires a reason")
            if self.follow_up_action is None:
                raise ValueError("NOT_RUN requires a follow-up action")
            return self

        if self.started_at is None or self.last_checkpoint_at is None:
            raise ValueError("an executed scenario requires start and checkpoint timestamps")
        if self.last_checkpoint_at < self.started_at:
            raise ValueError("last checkpoint cannot precede scenario start")
        if self.ended_at is not None and self.ended_at < self.last_checkpoint_at:
            raise ValueError("scenario end cannot precede its last checkpoint")
        if self.status in (TechnicalScenarioStatus.PASSED, TechnicalScenarioStatus.FAILED):
            if self.ended_at is None:
                raise ValueError("a terminal scenario result requires an end timestamp")
        elif self.ended_at is not None:
            raise ValueError("INCOMPLETE must not claim a terminal end timestamp")
        if self.status is TechnicalScenarioStatus.PASSED:
            if not self.artifact_identities or self.observed_outcome is None:
                raise ValueError("PASSED requires artifacts and an observed outcome")
            if self.reason is not None:
                raise ValueError("PASSED cannot carry a failure reason")
            if self.follow_up_action is not None:
                raise ValueError("PASSED cannot carry a follow-up action")
        elif self.reason is None:
            raise ValueError("FAILED and INCOMPLETE require a reason")
        elif self.follow_up_action is None:
            raise ValueError("FAILED and INCOMPLETE require a follow-up action")
        return self


class TechnicalCampaignV1(VersionedModel):
    """Complete T1--T8 accounting, bound to code and configuration."""

    schema_version: ClassVar[str] = "technical_campaign.v1"

    campaign_id: str = Field(min_length=1)
    created_at: datetime
    code_revision: str = Field(min_length=7, max_length=64)
    configuration_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    input_identities: tuple[str, ...]
    specifications: tuple[TechnicalScenarioSpecV1, ...]
    results: tuple[TechnicalScenarioResultV1, ...]
    passed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    incomplete_count: int = Field(ge=0)
    not_run_count: int = Field(ge=0)

    @field_validator("created_at")
    @classmethod
    def _created_at_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("code_revision", "configuration_sha256")
    @classmethod
    def _hex_identity(cls, value: str) -> str:
        lowered = value.lower()
        if not all(character in "0123456789abcdef" for character in lowered):
            raise ValueError("revision and configuration identities must be hexadecimal")
        return lowered

    @field_validator("input_identities")
    @classmethod
    def _inputs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item.strip() for item in value):
            raise ValueError("campaign requires nonblank input identities")
        if len(set(value)) != len(value):
            raise ValueError("input identities must be unique")
        return value

    @model_validator(mode="after")
    def _complete_accounting(self) -> TechnicalCampaignV1:
        expected = set(TechnicalScenario)
        spec_scenarios = [item.scenario for item in self.specifications]
        result_scenarios = [item.scenario for item in self.results]
        if len(spec_scenarios) != len(set(spec_scenarios)):
            raise ValueError("campaign contains duplicate scenario specifications")
        if len(result_scenarios) != len(set(result_scenarios)):
            raise ValueError("campaign contains duplicate scenario results")
        if set(spec_scenarios) != expected:
            raise ValueError("campaign specifications must contain exactly T1--T8")
        if set(result_scenarios) != expected:
            raise ValueError("campaign results must contain exactly T1--T8")
        if any(item.campaign_id != self.campaign_id for item in self.results):
            raise ValueError("scenario result belongs to a different campaign")

        specs = {item.scenario: item for item in self.specifications}
        for result in self.results:
            spec = specs[result.scenario]
            if (
                result.status is TechnicalScenarioStatus.PASSED
                and result.observed_frame_count > spec.maximum_frame_count
            ):
                raise ValueError("scenario result exceeds its declared frame bound")
            if (
                result.status is TechnicalScenarioStatus.PASSED
                and result.started_at is not None
                and result.ended_at is not None
                and (result.ended_at - result.started_at).total_seconds()
                > spec.maximum_duration_seconds
            ):
                raise ValueError("scenario result exceeds its declared duration bound")

        actual = {
            status: sum(item.status is status for item in self.results)
            for status in TechnicalScenarioStatus
        }
        declared = {
            TechnicalScenarioStatus.PASSED: self.passed_count,
            TechnicalScenarioStatus.FAILED: self.failed_count,
            TechnicalScenarioStatus.INCOMPLETE: self.incomplete_count,
            TechnicalScenarioStatus.NOT_RUN: self.not_run_count,
        }
        if declared != actual:
            raise ValueError("declared status counts disagree with scenario results")
        return self

    @property
    def technically_qualified(self) -> bool:
        """True only when every declared scenario passed."""

        return self.passed_count == len(TechnicalScenario)
