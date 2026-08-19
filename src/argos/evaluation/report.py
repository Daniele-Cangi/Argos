"""The evaluation report: metrics, the configuration behind them, and limits.

``.claude/rules/scientific-claims.md`` requires that reports include sample
count, missingness, baseline, period, configuration, and limitations. All six
are fields here rather than prose somebody remembers to write, because a
limitations section that is optional is a limitations section that disappears
exactly when the results look good.

``limitations`` in particular is **required to be non-empty**. There has never
been an evaluation of this system without limitations worth stating, and a
report that offered none would be making its strongest claim silently.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from pydantic import Field, field_serializer, field_validator

from argos.clock import ensure_utc
from argos.domain.versioning import VersionedModel, freeze, thaw

__all__ = ["EvaluationReportV1"]


class EvaluationReportV1(VersionedModel):
    """One evaluation run, reproducible from what it records about itself."""

    schema_version: ClassVar[str] = "evaluation_report.v1"

    evaluation_run_id: str = Field(min_length=1)
    created_at: datetime

    code_revision: str | None = None
    config_fingerprint: str = Field(min_length=1)
    evaluator_version: str = Field(min_length=1)
    log_loss_epsilon: Decimal = Field(gt=0)
    calibration_bin_count: int = Field(gt=0)
    """The four numbers that make a metric reproducible rather than merely
    repeatable. Two evaluations with different epsilons or different bin counts
    are not comparable, and a report that omitted them would invite exactly that
    comparison."""

    source_capture_run_ids: tuple[str, ...] = ()
    source_state_hash: str | None = None
    """Which replay produced the forecasts. `docs/OWNER_REVIEW_GATE.md` asks for
    a stable golden replay hash; carrying it here is what ties a set of scores to
    the exact reconstruction they were read from."""

    forecast_count: int = Field(ge=0)
    abstention_count: int = Field(ge=0)
    scored_count: int = Field(ge=0)
    unresolved_count: int = Field(ge=0)
    """Missingness, split into its causes. A single "n" would hide the
    difference between "the baseline declined" and "the market has not
    resolved", and only the first is a property of ARGOS."""

    resolution_refusals: Mapping[str, int] = Field(default_factory=dict)
    abstention_reasons: Mapping[str, int] = Field(default_factory=dict)

    calibration: Mapping[str, Any] = Field(default_factory=dict)

    cohorts: Mapping[str, Any] = Field(default_factory=dict)
    """Cohort breakdowns keyed by dimension name.

    A mapping rather than a list, so one dimension cannot appear twice with
    two different slicings and leave a reader to guess which is current.
    """

    limitations: tuple[str, ...]
    """What this report does not establish. Required and non-empty."""

    @field_validator("created_at")
    @classmethod
    def _anchor(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("resolution_refusals", "abstention_reasons", "calibration", "cohorts")
    @classmethod
    def _freeze_mapping(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        frozen: Mapping[str, Any] = freeze(value)
        return frozen

    @field_serializer("resolution_refusals", "abstention_reasons", "calibration", "cohorts")
    def _thaw_mapping(self, value: Mapping[str, Any]) -> dict[str, Any]:
        thawed: dict[str, Any] = thaw(value)
        return thawed

    @field_validator("limitations")
    @classmethod
    def _limitations_are_stated(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or not all(item.strip() for item in value):
            raise ValueError(
                "an evaluation report must state its limitations; there has never "
                "been an evaluation of this system without any, and an empty list "
                "would make the report's strongest claim silently"
            )
        return value

    def describe(self) -> str:
        """A one-line summary that never omits the sample size."""
        return (
            f"{self.evaluation_run_id}: {self.scored_count} scored of "
            f"{self.forecast_count} forecasts "
            f"({self.abstention_count} abstained, {self.unresolved_count} unresolved), "
            f"eps={self.log_loss_epsilon}, bins={self.calibration_bin_count}"
        )
