"""Market baseline forecasts, which are scores and say so.

Core invariant 8 and ADR-0006: no heuristic may masquerade as a probability, and
uncalibrated output is named ``raw_score`` while ``p_yes`` stays null until an
explicit calibration model and version exist. That applies here in a form worth
stating precisely, because the temptation to skip it is strongest at exactly
this record.

A market midpoint *is* a probability-like number, and it is not ARGOS's
probability. It is the market's price, which ARGOS has neither calibrated nor
evaluated. So a baseline records ``raw_score``, leaves ``p_yes`` null, and
carries ``calibration_status = uncalibrated``. When a calibration model exists
and has been fitted on chronologically earlier data
(``docs/05_RESEARCH_PROTOCOL.md``), the same record can carry ``p_yes`` and the
version that produced it — and until then, every report reading these records
says "score", not "probability".

**Abstention is a first-class result** (core invariant 10), and it is not an
error here. A one-sided book has no midpoint; a market that has never traded has
no last trade. Both produce a forecast record that carries no score and states
why, rather than no record at all — because "ARGOS declined to forecast" and
"ARGOS was never asked" must not look identical when the evaluation is counting
coverage.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar

import orjson
from pydantic import Field, field_validator, model_validator

from argos.baselines.quote import MarketQuoteV1
from argos.clock import ensure_utc
from argos.domain.orderbook import MAX_PRICE, MIN_PRICE, normalize_decimal
from argos.domain.versioning import VersionedModel

__all__ = [
    "AbstentionReason",
    "BaselineMethod",
    "CalibrationStatus",
    "MarketBaselineForecastV1",
    "MarketBaselineForecastV2",
    "build_baseline_forecast",
    "build_baseline_forecast_v2",
]

DISPLAYED_PRICE_LAST_TRADE_SPREAD = Decimal("0.10")


class BaselineMethod(StrEnum):
    """Which documented baseline produced a score.

    ``docs/05_RESEARCH_PROTOCOL.md`` names five. Four are implemented. The
    remaining category/base-rate baseline needs
      resolved data grouped by category, and the qualifier is the operative
      part. It is not implemented because that data does not exist here yet.
    """

    MIDPOINT = "midpoint"
    """The midpoint of a two-sided book. Abstains without both sides."""

    LAST_TRADE = "last_trade"
    """The last price that actually traded. Evidence about the past rather than
    a quote available now, which is why it is a separate baseline and not a
    fallback for a missing midpoint."""

    DISPLAYED_PRICE = "displayed_price"
    """Polymarket's documented display rule: midpoint for spreads at or below
    $0.10, otherwise last traded price. Kept distinct even when it happens to
    equal midpoint on a narrow-spread sample."""

    PERSISTENCE = "persistence"
    """The previous forecast for this token, carried forward unchanged.

    The baseline every time series must beat before any other result means
    anything. Abstains when there is no prior forecast, rather than seeding
    itself from the current quote — which would make it the midpoint baseline
    wearing another name.
    """


class CalibrationStatus(StrEnum):
    """Whether a score has been calibrated, and by what."""

    UNCALIBRATED = "uncalibrated"
    CALIBRATED = "calibrated"
    DEGRADED = "degraded"


class AbstentionReason(StrEnum):
    """Why a baseline declined to produce a score.

    Counted rather than dropped: an evaluation that reported only the forecasts
    it managed to make would overstate its own coverage, and coverage is one of
    the primary metrics ``docs/05_RESEARCH_PROTOCOL.md`` requires.
    """

    NO_TWO_SIDED_BOOK = "no_two_sided_book"
    NO_LAST_TRADE = "no_last_trade"
    NO_PRIOR_FORECAST = "no_prior_forecast"
    NO_DISPLAYED_PRICE = "no_displayed_price"


class MarketBaselineForecastV1(VersionedModel):
    """One baseline's score for one token at one point in a replay.

    Time-indexed three ways because core invariant 4 requires reconstructing
    what ARGOS knew at the forecast timestamp, and the three answer different
    questions: ``as_of_event_time`` is when the market moved,
    ``as_of_received_time`` is when ARGOS learned of it, and
    ``as_of_ingest_sequence`` is the exact position in the arrival order a
    replay can seek back to.
    """

    schema_version: ClassVar[str] = "market_baseline_forecast.v1"

    condition_id: str = Field(min_length=1)
    token_id: str = Field(min_length=1)
    method: BaselineMethod

    as_of_event_time: datetime | None = None
    as_of_received_time: datetime
    as_of_ingest_sequence: int = Field(gt=0)

    raw_score: Decimal | None = Field(default=None, ge=MIN_PRICE, le=MAX_PRICE)
    """The uncalibrated number, in [0, 1]. ``None`` when the baseline abstained.

    Named ``raw_score`` rather than ``probability`` or ``p_yes`` because ARGOS
    has not calibrated it — ADR-0006. The market's own price is not ARGOS's
    probability just because it happens to lie in [0, 1].
    """

    p_yes: Decimal | None = Field(default=None, ge=MIN_PRICE, le=MAX_PRICE)
    """Populated **only** when an explicit calibration model and version exist.
    Null for every record this milestone produces."""

    calibration_status: CalibrationStatus = CalibrationStatus.UNCALIBRATED
    calibration_version: str | None = Field(default=None, min_length=1)

    abstained: bool = False
    abstention_reason: AbstentionReason | None = None

    quote: MarketQuoteV1
    """The evidence the score was read from, kept whole. A score without the
    quote behind it cannot be audited, and core invariant 2 asks for bid, ask,
    depth and spread to be preserved rather than collapsed."""

    @field_validator("as_of_event_time", "as_of_received_time")
    @classmethod
    def _anchor(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @field_validator("raw_score", "p_yes")
    @classmethod
    def _canonicalize(cls, value: Decimal | None) -> Decimal | None:
        return None if value is None else normalize_decimal(value)

    @model_validator(mode="after")
    def _abstention_and_score_are_consistent(self) -> MarketBaselineForecastV1:
        if self.abstained:
            if self.raw_score is not None or self.p_yes is not None:
                raise ValueError("an abstaining forecast must carry no score")
            if self.abstention_reason is None:
                raise ValueError(
                    "an abstaining forecast must say why; an uncounted abstention is "
                    "indistinguishable from a market nobody asked about"
                )
        else:
            if self.abstention_reason is not None:
                raise ValueError("a forecast that produced a score did not abstain")
            if self.raw_score is None and self.p_yes is None:
                raise ValueError("a non-abstaining forecast must carry a score")
        return self

    @model_validator(mode="after")
    def _calibration_claims_are_backed(self) -> MarketBaselineForecastV1:
        """ADR-0006, enforced rather than documented.

        ``p_yes`` without a calibration version is precisely the "uncalibrated
        score wearing a probability's name" that ADR exists to prevent, and a
        `calibrated` status with nothing that produced it is the same claim made
        one field over.
        """
        if self.calibration_status is CalibrationStatus.UNCALIBRATED:
            if self.p_yes is not None:
                raise ValueError(
                    "p_yes requires a calibration model and version (ADR-0006); an "
                    "uncalibrated score belongs in raw_score"
                )
            if self.calibration_version is not None:
                raise ValueError("an uncalibrated forecast cannot name a calibration version")
        elif self.calibration_version is None:
            raise ValueError(
                f"calibration_status={self.calibration_status} requires a calibration_version"
            )
        return self

    @model_validator(mode="after")
    def _scope_matches_the_quote(self) -> MarketBaselineForecastV1:
        if (self.condition_id, self.token_id) != (self.quote.condition_id, self.quote.token_id):
            raise ValueError("the forecast and the quote it was read from must name the same token")
        return self


class MarketBaselineForecastV2(MarketBaselineForecastV1):
    """An identifiable forecast bound to one evaluation information state."""

    schema_version: ClassVar[str] = "market_baseline_forecast.v2"

    forecast_id: str = Field(min_length=1)
    evaluation_run_id: str = Field(min_length=1)
    source_capture_run_id: str = Field(min_length=1)
    source_observation_id: str = Field(min_length=1)
    information_state_hash: str = Field(min_length=64, max_length=64)
    market_id: str = Field(min_length=1)
    contract_id: str | None = Field(default=None, min_length=1)


def build_baseline_forecast(
    *,
    method: BaselineMethod,
    quote: MarketQuoteV1,
    as_of_received_time: datetime,
    as_of_ingest_sequence: int,
    previous_score: Decimal | None = None,
) -> MarketBaselineForecastV1:
    """Compute one baseline from a quote, abstaining rather than inventing.

    ``previous_score`` is used only by :attr:`BaselineMethod.PERSISTENCE`, and
    that method abstains without one instead of seeding itself from the current
    quote — a persistence baseline that starts at the midpoint is the midpoint
    baseline with a delay, and comparing the two would then be comparing a
    quantity with itself.
    """
    score: Decimal | None
    reason: AbstentionReason | None = None

    if method is BaselineMethod.MIDPOINT:
        score = quote.midpoint
        if score is None:
            reason = AbstentionReason.NO_TWO_SIDED_BOOK
    elif method is BaselineMethod.LAST_TRADE:
        score = quote.last_trade_price
        if score is None:
            reason = AbstentionReason.NO_LAST_TRADE
    elif method is BaselineMethod.DISPLAYED_PRICE:
        score = (
            quote.last_trade_price
            if quote.spread is not None and quote.spread > DISPLAYED_PRICE_LAST_TRADE_SPREAD
            else quote.midpoint
        )
        if score is None:
            reason = AbstentionReason.NO_DISPLAYED_PRICE
    else:
        score = previous_score
        if score is None:
            reason = AbstentionReason.NO_PRIOR_FORECAST

    return MarketBaselineForecastV1(
        condition_id=quote.condition_id,
        token_id=quote.token_id,
        method=method,
        as_of_event_time=quote.quote_time,
        as_of_received_time=as_of_received_time,
        as_of_ingest_sequence=as_of_ingest_sequence,
        raw_score=score,
        abstained=score is None,
        abstention_reason=reason,
        quote=quote,
    )


def build_baseline_forecast_v2(
    *,
    method: BaselineMethod,
    quote: MarketQuoteV1,
    as_of_received_time: datetime,
    as_of_ingest_sequence: int,
    previous_score: Decimal | None,
    evaluation_run_id: str,
    source_capture_run_id: str,
    source_observation_id: str,
    information_state_hash: str,
    market_id: str,
    contract_id: str | None,
) -> MarketBaselineForecastV2:
    """Build V1's score, then attach stable evidence identity."""
    legacy = build_baseline_forecast(
        method=method,
        quote=quote,
        as_of_received_time=as_of_received_time,
        as_of_ingest_sequence=as_of_ingest_sequence,
        previous_score=previous_score,
    )
    identity = {
        "version": "forecast_identity.v1",
        "capture_run_id": source_capture_run_id,
        "observation_id": source_observation_id,
        "information_state_hash": information_state_hash,
        "method": method.value,
        "condition_id": quote.condition_id,
        "token_id": quote.token_id,
        "contract_id": contract_id,
    }
    digest = hashlib.sha256(orjson.dumps(identity, option=orjson.OPT_SORT_KEYS)).hexdigest()
    return MarketBaselineForecastV2(
        **dict(legacy),
        forecast_id=f"forecast-{digest[:32]}",
        evaluation_run_id=evaluation_run_id,
        source_capture_run_id=source_capture_run_id,
        source_observation_id=source_observation_id,
        information_state_hash=information_state_hash,
        market_id=market_id,
        contract_id=contract_id,
    )
