"""Market midpoint, executable quote, and naive baselines (M4).

`quote` preserves what the market showed, keeping executable prices and the
midpoint structurally apart (M4's first exit criterion). `forecast` turns a
quote into a baseline **score** — never a probability, because ARGOS has not
calibrated it (ADR-0006) — and abstains rather than inventing a number when the
evidence is not there.
"""

from argos.baselines.forecast import (
    AbstentionReason,
    BaselineMethod,
    CalibrationStatus,
    MarketBaselineForecastV1,
    build_baseline_forecast,
)
from argos.baselines.quote import MarketQuoteV1, quote_from_book_state

__all__ = [
    "AbstentionReason",
    "BaselineMethod",
    "CalibrationStatus",
    "MarketBaselineForecastV1",
    "MarketQuoteV1",
    "build_baseline_forecast",
    "quote_from_book_state",
]
