"""Adversarial contracts for proof-bearing terminal prospective evidence."""

from __future__ import annotations

from datetime import timedelta

from argos.evaluation.prospective_terminal import (
    LifecycleContinuityStatus,
    ResolutionAdmissibilityStatus,
    TargetAccountingStatus,
)


def test_terminal_claims_are_three_distinct_types() -> None:
    """The old single observation_complete flag cannot express terminal V3."""

    assert TargetAccountingStatus.COMPLETE.value == "TARGET_ACCOUNTING_COMPLETE"
    assert LifecycleContinuityStatus.INCOMPLETE.value == "LIFECYCLE_CONTINUITY_INCOMPLETE"
    assert (
        ResolutionAdmissibilityStatus.NO_ADMISSIBLE_CUTOFF.value == "NO_ADMISSIBLE_CUTOFF_OBSERVED"
    )
    assert timedelta(seconds=300) * 2 < timedelta(hours=3, minutes=37)
