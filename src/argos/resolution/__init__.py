"""Lifecycle and winning-outcome normalization (M4).

`gamma_resolution` turns a public Gamma market payload into a `ResolutionV1`,
or refuses it with a counted reason. `clob_resolution` does the same from the
CLOB market record, where the source states the winner outright instead of
leaving it to be inferred from a price -- and where ARGOS's own captured market
is actually covered, which Gamma's index does not do. Both refuse far more than
they accept, and that is the point: measured on live data, `closed == true` does not imply a
determined outcome, and `outcomePrices` is usually a last price rather than a
settlement (`docs/research/m4-gamma-resolution.md`).
"""

from argos.resolution.clob_resolution import (
    CLOB_RESOLUTION_NORMALIZER_VERSION,
    normalize_clob_resolution,
)
from argos.resolution.gamma_resolution import (
    GAMMA_RESOLUTION_NORMALIZER_VERSION,
    ResolutionRefusal,
    ResolutionRefusalReason,
    ResolutionStatus,
    ResolutionV1,
    WinningOutcome,
    normalize_gamma_resolution,
)

__all__ = [
    "CLOB_RESOLUTION_NORMALIZER_VERSION",
    "GAMMA_RESOLUTION_NORMALIZER_VERSION",
    "ResolutionRefusal",
    "ResolutionRefusalReason",
    "ResolutionStatus",
    "ResolutionV1",
    "WinningOutcome",
    "normalize_clob_resolution",
    "normalize_gamma_resolution",
]
