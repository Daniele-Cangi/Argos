"""The evaluation must not depend on the ambient Decimal context (F1).

`decimal` arithmetic reads `decimal.getcontext()`, which is thread-local mutable
process-global state. `docs/DECISION_LOG.md` (2026-08-12) records this exact
class being closed once already, in `CANONICAL_DECIMAL_CONTEXT`, after one wire
price rendered as three different canonical texts under ambient precisions 5, 28
and 50.

It was still open in the evaluation path. Reproduced against this branch before
the fix: the same stored inputs, evaluator version, epsilon and bin count
produced **three different serialized artifacts** at ambient precisions 6, 28
and 50 -- a bin's `mean_score` came out `0.123457` at 6 and `0.123456789` at 28,
and the divergence propagated into every mean, the ECE, and every cohort slice.

These tests set the ambient context deliberately and hostilely. `conftest.py`
does not reset it between tests, so each one restores what it found -- a test
that leaked a precision of 6 into the rest of the suite would be the same defect
in a new place.
"""

from __future__ import annotations

import decimal
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from argos.errors import ContractViolationError
from argos.evaluation import (
    DEFAULT_LOG_LOSS_EPSILON,
    EVALUATION_PRECISION,
    MAX_LOG_LOSS_EPSILON,
    brier_score,
    calibration_report,
    cohort_report,
    log_loss,
    score_forecast,
)
from argos.resolution import WinningOutcome

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
CONDITION = "0x94a39addec8bd24a2d03deeaa43bdee6a2b11eca403cd12495eb24132cc23173"
TOKEN = "34691510069031117755834214800869745291092253295564665516660269316660628637961"

AMBIENT_PRECISIONS = (6, 28, 50)

# Deliberately awkward: a score with more digits than precision 6 can hold, both
# clipping bounds, and a repeating division (1/3 of the sample) so that any
# unpinned rounding shows up in the mean and the ECE rather than cancelling.
SAMPLE: tuple[tuple[str, WinningOutcome], ...] = (
    ("0.123456789012345678901234567890", WinningOutcome.NO),
    ("0.285", WinningOutcome.NO),
    ("0.7", WinningOutcome.YES),
    ("1", WinningOutcome.NO),
    ("0", WinningOutcome.YES),
    ("0.5", WinningOutcome.YES),
)


@pytest.fixture(autouse=True)
def _restore_ambient_context() -> Iterator[None]:
    """Put back whatever the process had. A test that leaked precision 6 into
    the rest of the suite would be this same defect in a new place."""
    saved = decimal.getcontext().copy()
    yield
    decimal.setcontext(saved)


def _artifact() -> str:
    evaluations = [
        score_forecast(
            forecast_method="midpoint",
            condition_id=CONDITION,
            token_id=TOKEN,
            score=Decimal(score),
            resolution_id="resolution-x",
            winning_outcome=outcome,
            calibration_status="uncalibrated",
            created_at=NOW,
        ).model_copy(update={"evaluation_id": f"evaluation-{index}"})
        for index, (score, outcome) in enumerate(SAMPLE)
    ]
    payload = {
        "records": [e.to_record() for e in evaluations],
        "calibration": calibration_report(evaluations, method="midpoint").as_record(),
        "cohort": cohort_report(
            evaluations,
            method="midpoint",
            dimension="spread_bucket",
            key_of={
                e.evaluation_id: ("wide" if index % 2 else "narrow")
                for index, e in enumerate(evaluations)
            },
        ).as_record(),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def test_the_whole_evaluation_artifact_is_byte_identical_across_ambient_precisions() -> None:
    """The property F1 asks for, stated exactly: the same stored inputs,
    evaluator version, epsilon and bin count produce byte-identical persistent
    evaluation records regardless of ambient Decimal context."""
    produced = {}
    for precision in AMBIENT_PRECISIONS:
        decimal.getcontext().prec = precision
        produced[precision] = _artifact()
    assert len(set(produced.values())) == 1, (
        "the artifact still depends on the ambient context; "
        f"lengths {[len(v) for v in produced.values()]}"
    )


@pytest.mark.parametrize("precision", AMBIENT_PRECISIONS)
def test_a_single_scored_forecast_is_identical_under_any_ambient_precision(
    precision: int,
) -> None:
    decimal.getcontext().prec = 28
    reference = score_forecast(
        forecast_method="midpoint",
        condition_id=CONDITION,
        token_id=TOKEN,
        score=Decimal("0.123456789012345678901234567890"),
        resolution_id="r",
        winning_outcome=WinningOutcome.YES,
        calibration_status="uncalibrated",
        created_at=NOW,
    ).to_record()
    decimal.getcontext().prec = precision
    again = score_forecast(
        forecast_method="midpoint",
        condition_id=CONDITION,
        token_id=TOKEN,
        score=Decimal("0.123456789012345678901234567890"),
        resolution_id="r",
        winning_outcome=WinningOutcome.YES,
        calibration_status="uncalibrated",
        created_at=NOW,
    ).to_record()
    assert again == reference


@pytest.mark.parametrize("precision", AMBIENT_PRECISIONS)
def test_the_scoring_rules_themselves_are_pinned(precision: int) -> None:
    """Brier and log loss individually, not only through a report -- a report
    that agreed while its inputs differed would be hiding a cancellation."""
    decimal.getcontext().prec = 28
    expected_brier = brier_score(Decimal("0.123456789012345678901234567890"), 1)
    expected_loss, expected_clip = log_loss(
        Decimal("0.123456789012345678901234567890"), 1, epsilon=DEFAULT_LOG_LOSS_EPSILON
    )
    decimal.getcontext().prec = precision
    assert brier_score(Decimal("0.123456789012345678901234567890"), 1) == expected_brier
    assert log_loss(
        Decimal("0.123456789012345678901234567890"), 1, epsilon=DEFAULT_LOG_LOSS_EPSILON
    ) == (expected_loss, expected_clip)


def test_a_hostile_ambient_context_cannot_change_a_stored_record() -> None:
    """Not just precision: rounding mode and exponent limits are ambient too,
    and an unpinned computation reads all of them."""
    decimal.getcontext().prec = 28
    reference = _artifact()
    hostile = decimal.Context(prec=7, rounding=decimal.ROUND_CEILING, Emin=-20, Emax=20)
    decimal.setcontext(hostile)
    assert _artifact() == reference


def test_the_evaluation_precision_is_a_named_constant() -> None:
    """Changing it changes every recorded number, so it is pinned by name rather
    than passed at a call site where two callers could disagree."""
    assert EVALUATION_PRECISION == 50


# --- public boundary validation ---------------------------------------------------


@pytest.mark.parametrize("epsilon", ["0", "-1", "-0.000001"])
def test_a_non_positive_epsilon_is_refused(epsilon: str) -> None:
    """A non-positive epsilon leaves log loss unbounded at 0 and 1, which is the
    failure clipping exists to prevent."""
    with pytest.raises(ContractViolationError):
        log_loss(Decimal("0.5"), 1, epsilon=Decimal(epsilon))


@pytest.mark.parametrize("epsilon", ["0.5", "0.9", "1", "2"])
def test_an_epsilon_of_half_or_more_is_refused(epsilon: str) -> None:
    """At exactly 0.5 the clipping interval collapses to a point and every
    forecast scores ln 2 whatever was forecast; above it the interval inverts and
    every score is replaced rather than clipped. Both were silently accepted
    before this bound existed."""
    with pytest.raises(ContractViolationError):
        log_loss(Decimal("0.9"), 0, epsilon=Decimal(epsilon))


def test_a_degenerate_epsilon_is_refused_at_the_scoring_boundary_too() -> None:
    """Refused before the arithmetic runs, not only by the record it would have
    landed on -- otherwise a caller catching the validation error still paid for
    a computation whose result was meaningless."""
    with pytest.raises(ContractViolationError):
        score_forecast(
            forecast_method="midpoint",
            condition_id=CONDITION,
            token_id=TOKEN,
            score=Decimal("0.5"),
            resolution_id="r",
            winning_outcome=WinningOutcome.YES,
            calibration_status="uncalibrated",
            created_at=NOW,
            epsilon=MAX_LOG_LOSS_EPSILON,
        )


@pytest.mark.parametrize("bin_count", [0, -1, -10])
def test_a_non_positive_bin_count_is_refused_inside_the_taxonomy(bin_count: int) -> None:
    """`bin_count=0` raised a bare `decimal.InvalidOperation` before this --
    outside the ARGOS taxonomy, so a caller could neither count nor explain it.
    `bin_count=-3` was accepted and produced a report with zero bins whose ECE
    was computed over nothing."""
    with pytest.raises(ContractViolationError):
        calibration_report([], method="midpoint", bin_count=bin_count)


def test_a_cohort_report_uses_the_same_bins_as_its_headline() -> None:
    """A cohort table whose slices were binned differently from the headline
    report, or from each other, would not be comparable with either."""
    evaluations = [
        score_forecast(
            forecast_method="midpoint",
            condition_id=CONDITION,
            token_id=TOKEN,
            score=Decimal("0.5"),
            resolution_id="r",
            winning_outcome=WinningOutcome.YES,
            calibration_status="uncalibrated",
            created_at=NOW,
        ).model_copy(update={"evaluation_id": f"evaluation-{index}"})
        for index in range(2)
    ]
    report = cohort_report(
        evaluations,
        method="midpoint",
        dimension="spread_bucket",
        key_of={e.evaluation_id: "narrow" for e in evaluations},
        bin_count=4,
    )
    assert [len(sliced.bins) for _, sliced in report.slices] == [4]
