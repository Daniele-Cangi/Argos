"""The pinned Decimal context every persistent evaluation number is computed in.

`decimal` arithmetic reads `decimal.getcontext()`, which is **thread-local
mutable process-global state**. `docs/DECISION_LOG.md` (2026-08-12) already
records this class being closed once, in
`argos.domain.orderbook.CANONICAL_DECIMAL_CONTEXT`, after one wire price
rendered as three different canonical texts — and therefore three different
`observation_id`s — under ambient precisions 5, 28 and 50. That was called out
then as "the hidden global state CLAUDE.md prohibits and a direct break of core
invariant 5".

It was still open here. Reproduced against this branch before this module
existed: the same stored inputs, evaluator version, epsilon and bin count
produced **three different serialized evaluation artifacts** under ambient
precisions 6, 28 and 50 — `mean_score` came out as `0.123457` at precision 6
and `0.123456789` at 28, and the divergence propagated into every mean, the
expected calibration error, and every cohort summary. A metric whose value
depends on what some unrelated code last did to the process context is not a
metric a report can cite.

**Why a second context rather than reusing the canonical one.** They are budgets
for different quantities. `CANONICAL_DECIMAL_CONTEXT` is sized for rendering a
*price* injectively, and refuses magnitudes outside a wire price's range;
evaluation arithmetic divides by sample counts and takes natural logarithms,
which needs a plain, generous, fixed precision and no magnitude policy at all.
Sharing one context would tie a change in either budget to the other, and the
first time somebody widened it for a price they would silently change every
recorded Brier score.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Final

from argos.errors import ContractViolationError

__all__ = [
    "EVALUATION_DECIMAL_CONTEXT",
    "EVALUATION_PRECISION",
    "MAX_LOG_LOSS_EPSILON",
    "evaluation_context",
    "require_bin_count",
    "require_epsilon",
]

EVALUATION_PRECISION: Final = 50
"""Significant digits for every evaluation computation.

Fixed rather than tuned. Generous enough that a mean over a large sample and a
natural logarithm near the clipping bound both keep far more precision than any
report renders, and small enough to be cheap. What matters is not the value but
that it is *pinned*: any fixed precision produces reproducible artifacts, and
none produces them if it is read from the ambient context.

Changing it changes every recorded number, which is why it is a named constant
rather than an inline argument.
"""

EVALUATION_DECIMAL_CONTEXT: Final = Context(
    prec=EVALUATION_PRECISION,
    # Set explicitly, never inherited. An unspecified `Context` field is filled
    # from `decimal.DefaultContext`, which is itself mutable process-global
    # state -- the same class of dependency this context exists to remove,
    # merely relocated from read time to import time. That exact mistake is
    # recorded against `CANONICAL_DECIMAL_CONTEXT` in `docs/DECISION_LOG.md`.
    Emin=-999_999,
    Emax=999_999,
    rounding=ROUND_HALF_EVEN,
    capitals=1,
    clamp=0,
)
"""The one context evaluation arithmetic runs in. See the module docstring."""


def evaluation_context() -> AbstractContextManager[Context]:
    """A context manager pinning the evaluation context for its body.

    Every operation whose result reaches a persistent evaluation artifact runs
    inside one of these: multiplication for Brier, subtraction for absolute
    error, `ln` for log loss, and every division that produces a mean, an
    observed rate or an expected calibration error.
    """
    return localcontext(EVALUATION_DECIMAL_CONTEXT)


MAX_LOG_LOSS_EPSILON: Final = Decimal("0.5")
"""The exclusive upper bound on a log-loss clipping epsilon.

At exactly `0.5` the clipping interval `[eps, 1 - eps]` collapses to the single
point `0.5`, so **every** forecast is clipped to a coin flip and the metric
reports `ln 2` regardless of what was forecast. Above `0.5` the interval
inverts: `min(max(score, 0.9), 0.1)` is `0.1` for every input, which is not a
clip but a substitution.

Reproduced against this branch before the bound existed: `epsilon=0.5` and
`epsilon=0.9` were both accepted, silently, and returned confident-looking
numbers. `epsilon <= 0` was already refused, but only incidentally, by the
record's own `gt=0` field -- after the arithmetic had already run.
"""


def require_epsilon(epsilon: Decimal) -> Decimal:
    """Refuse a clipping epsilon outside `(0, 0.5)`, at the public boundary.

    Checked before any arithmetic rather than after, so a degenerate epsilon
    cannot produce a number that a caller then discards an exception over. The
    refusal names the value, because "the epsilon was wrong" and "the epsilon
    was 0.9" lead to different fixes.
    """
    if not epsilon.is_finite():
        raise ContractViolationError(
            "log-loss epsilon must be a finite decimal", epsilon=str(epsilon)
        )
    if epsilon <= 0:
        raise ContractViolationError(
            "log-loss epsilon must be positive; a non-positive epsilon leaves "
            "log loss unbounded at 0 and 1, which is the failure clipping exists "
            "to prevent",
            epsilon=str(epsilon),
        )
    if epsilon >= MAX_LOG_LOSS_EPSILON:
        raise ContractViolationError(
            "log-loss epsilon must be below 0.5; at 0.5 the clipping interval "
            "collapses to a single point and every forecast scores ln 2, and "
            "above it the interval inverts and every forecast is replaced rather "
            "than clipped",
            epsilon=str(epsilon),
            limit=str(MAX_LOG_LOSS_EPSILON),
        )
    return epsilon


def require_bin_count(bin_count: int) -> int:
    """Refuse a non-positive calibration bin count, inside the taxonomy.

    Reproduced against this branch: `bin_count=0` raised a bare
    `decimal.InvalidOperation` from the edge computation -- outside the ARGOS
    error taxonomy, so a caller could neither count nor explain it, which is the
    "escapes the taxonomy" class this repository has closed five times. And
    `bin_count=-3` was accepted, producing a calibration report with **zero
    bins** whose expected calibration error was computed over nothing.
    """
    if bin_count <= 0:
        raise ContractViolationError(
            "calibration bin_count must be positive; a report with no bins has "
            "no reliability curve, and its expected calibration error would be "
            "computed over nothing",
            bin_count=bin_count,
        )
    return bin_count
