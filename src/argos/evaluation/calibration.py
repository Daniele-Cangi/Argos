"""Calibration bins and cohort breakdowns, with sample counts everywhere.

``.claude/rules/scientific-claims.md``: "Reports include sample count,
missingness, baseline, period, configuration, and limitations." Two of those are
structural here rather than editorial.

**Every bin reports its count, including empty ones.** An absent bin and a bin
with zero forecasts read identically in a table, and a reliability curve drawn
only over the bins that happened to be populated looks far better calibrated
than the data supports. So the bin edges are fixed, all ten are always present,
and an empty bin says so.

**Expected calibration error is reported with its bin count and its sample
count, or not at all.** ECE over 4 forecasts is a number with no content, and
one computed over different bin edges is not comparable with another. Both
travel with the value.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from argos.evaluation.scoring import ForecastEvaluationV1

__all__ = [
    "DEFAULT_BIN_COUNT",
    "CalibrationBin",
    "CalibrationReport",
    "CohortReport",
    "calibration_report",
    "cohort_report",
    "spread_bucket",
]

DEFAULT_BIN_COUNT = 10
"""Ten equal-width bins on [0, 1], declared rather than tuned.

Equal-width rather than equal-count, because the question a reliability curve
answers is "when this system says 0.7, how often is it right" — which is about
the *score* axis, and equal-count bins move the boundaries with the data so two
reports stop being comparable.
"""


@dataclass(frozen=True, slots=True)
class CalibrationBin:
    """One score band, always present even when empty."""

    lower: Decimal
    upper: Decimal
    count: int
    mean_score: Decimal | None
    observed_rate: Decimal | None
    """How often the outcome was YES among forecasts in this bin, or ``None``
    when the bin is empty. ``None`` rather than 0: "no evidence" and "never
    happened" are different, and a reliability curve that plotted the first as
    the second would show a perfectly wrong system as perfectly calibrated at
    the extremes."""

    def as_record(self) -> dict[str, object]:
        return {
            "lower": str(self.lower),
            "upper": str(self.upper),
            "count": self.count,
            "mean_score": None if self.mean_score is None else str(self.mean_score),
            "observed_rate": None if self.observed_rate is None else str(self.observed_rate),
        }


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    """A reliability curve plus the numbers that make it interpretable."""

    method: str
    bins: tuple[CalibrationBin, ...]
    sample_count: int
    expected_calibration_error: Decimal | None
    """``None`` when there is nothing to compute it from. An ECE of 0 over an
    empty sample is the most flattering number a calibration report can
    produce, and it means nothing."""

    mean_brier: Decimal | None
    mean_log_loss: Decimal | None
    log_loss_epsilon: Decimal | None
    clipped_count: int
    """How many forecasts hit the log-loss clip. A mean log loss whose clipping
    rate is unknown is a mean whose worst cases are unknown."""

    calibration_status: str
    """From the forecasts themselves. A report over uncalibrated scores says so
    in its own record, so a reader cannot mistake a scored baseline for a
    calibrated probability (ADR-0006)."""

    def as_record(self) -> dict[str, object]:
        return {
            "method": self.method,
            "calibration_status": self.calibration_status,
            "sample_count": self.sample_count,
            "bin_count": len(self.bins),
            "expected_calibration_error": (
                None
                if self.expected_calibration_error is None
                else str(self.expected_calibration_error)
            ),
            "mean_brier": None if self.mean_brier is None else str(self.mean_brier),
            "mean_log_loss": None if self.mean_log_loss is None else str(self.mean_log_loss),
            "log_loss_epsilon": (
                None if self.log_loss_epsilon is None else str(self.log_loss_epsilon)
            ),
            "clipped_count": self.clipped_count,
            "bins": [bin_.as_record() for bin_ in self.bins],
        }


def calibration_report(
    evaluations: Iterable[ForecastEvaluationV1],
    *,
    method: str,
    bin_count: int = DEFAULT_BIN_COUNT,
) -> CalibrationReport:
    """Bin scored forecasts and summarize, reporting empty bins as empty."""
    scored: list[ForecastEvaluationV1] = [e for e in evaluations if e.forecast_method == method]
    edges = [Decimal(index) / bin_count for index in range(bin_count + 1)]

    bins: list[CalibrationBin] = []
    weighted_gap = Decimal(0)
    for index in range(bin_count):
        lower, upper = edges[index], edges[index + 1]
        # The last bin is closed on the right so a score of exactly 1.0 lands
        # somewhere. Without it, a maximally confident correct forecast would
        # vanish from its own reliability curve.
        members = [
            e
            for e in scored
            if (lower <= e.score < upper) or (index == bin_count - 1 and e.score == upper)
        ]
        if members:
            mean_score = sum((e.score for e in members), Decimal(0)) / len(members)
            observed = Decimal(sum(e.outcome_yes for e in members)) / len(members)
            weighted_gap += len(members) * abs(mean_score - observed)
        else:
            mean_score = None
            observed = None
        bins.append(
            CalibrationBin(
                lower=lower,
                upper=upper,
                count=len(members),
                mean_score=mean_score,
                observed_rate=observed,
            )
        )

    count = len(scored)
    statuses = {e.calibration_status for e in scored}
    return CalibrationReport(
        method=method,
        bins=tuple(bins),
        sample_count=count,
        expected_calibration_error=(weighted_gap / count) if count else None,
        mean_brier=(sum((e.brier_score for e in scored), Decimal(0)) / count) if count else None,
        mean_log_loss=(sum((e.log_loss for e in scored), Decimal(0)) / count) if count else None,
        log_loss_epsilon=scored[0].log_loss_epsilon if scored else None,
        clipped_count=sum(1 for e in scored if e.log_loss_was_clipped),
        # A mixed set is reported as mixed rather than as whichever came first:
        # a report that silently labelled a mixture with one status would be
        # making exactly the claim ADR-0006 exists to prevent.
        calibration_status=(
            "none" if not statuses else (statuses.pop() if len(statuses) == 1 else "mixed")
        ),
    )


@dataclass(frozen=True, slots=True)
class CohortReport:
    """One cohort dimension, sliced, with a count on every slice."""

    dimension: str
    slices: tuple[tuple[str, CalibrationReport], ...]

    def as_record(self) -> dict[str, object]:
        return {
            "dimension": self.dimension,
            "slices": {name: report.as_record() for name, report in self.slices},
        }


def spread_bucket(spread: Decimal | None) -> str:
    """Bucket a spread into a named band.

    Fixed edges rather than quantiles of whatever sample is in front of them,
    for the reason the calibration bins are equal-width: quantile buckets move
    with the data and stop two reports being comparable. ``unknown`` is a
    bucket rather than an exclusion — a one-sided book has no spread, and
    dropping those markets from a cohort table would quietly narrow the sample
    the table claims to describe.
    """
    if spread is None:
        return "unknown"
    if spread <= Decimal("0.01"):
        return "0-1c"
    if spread <= Decimal("0.05"):
        return "1-5c"
    if spread <= Decimal("0.10"):
        return "5-10c"
    return "over-10c"


def cohort_report(
    evaluations: Sequence[ForecastEvaluationV1],
    *,
    method: str,
    dimension: str,
    key_of: dict[str, str],
) -> CohortReport:
    """Group scored forecasts by a caller-supplied slice key and report each.

    ``key_of`` maps ``evaluation_id`` to a slice name. Supplied by the caller
    rather than computed here because the cohort dimensions
    ``docs/07_MILESTONES.md`` names — category, spread bucket, time to
    resolution — live on three different records, and an evaluation module that
    reached for all three would need to know about markets, quotes and
    resolutions at once.
    """
    grouped: dict[str, list[ForecastEvaluationV1]] = {}
    for evaluation in evaluations:
        if evaluation.forecast_method != method:
            continue
        grouped.setdefault(key_of.get(evaluation.evaluation_id, "unknown"), []).append(evaluation)
    return CohortReport(
        dimension=dimension,
        slices=tuple(
            (name, calibration_report(members, method=method))
            for name, members in sorted(grouped.items())
        ),
    )
