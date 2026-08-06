# Research protocol

## Objective

Evaluate whether ARGOS produces useful, calibrated probability estimates and whether its additional components improve on clearly defined market baselines.

## Prevent leakage

- A forecast may access only observations with `received_time` at or before its cutoff in original-arrival replay.
- Event-time corrections arriving later remain unavailable to the historical forecast unless running a separately labeled corrected-history analysis.
- Market metadata changes and rule clarifications are versioned by availability time.
- Resolution outcomes never enter features or calibration fitting for earlier forecasts.
- Train, calibration, and evaluation windows are chronological.

## Forecast target

The primary target is the final binary resolution under the stored market contract version.

Every forecast records:

- market and contract version;
- as-of sequence and times;
- probability or explicitly uncalibrated score;
- evidence cutoff;
- engine/config versions;
- uncertainty when available.

## Baselines

At minimum evaluate:

1. market displayed-price proxy according to a documented method;
2. midpoint when both sides exist;
3. last traded price when required by the display rule or used as a separate baseline;
4. simple persistence baseline;
5. category/base-rate baseline when enough resolved data exists.

Do not compare a fair probability only with midpoint when discussing actionable edge. Edge analyses must use exact executable bid/ask and a depth assumption.

## Primary metrics

- Brier score;
- log loss with pre-declared clipping epsilon;
- calibration/reliability curve;
- expected calibration error with declared bins;
- absolute error;
- sample count and coverage.

Later selective-prediction metrics:

- risk–coverage curve;
- error conditional on abstention threshold;
- engine disagreement versus error;
- calibration by category, liquidity, spread, time-to-resolution, and market age.

## Information lead

Do not claim information lead until a protocol is pre-registered. A future definition must specify:

- what constitutes an ARGOS probability change;
- what constitutes a corresponding market move;
- minimum move magnitude;
- quote source and executable-price handling;
- clock and latency correction;
- maximum matching window;
- treatment of shared external sources;
- statistical significance and multiple testing.

## Engine independence

Each engine declares:

- raw input sources;
- derived features consumed;
- other engine outputs consumed;
- evidence dependency groups.

Consensus between dependent engines is not counted as independent confirmation.

## Resolution and semantics

- Evaluate against the market's final resolution record, not an intuitive interpretation of the title.
- Keep forecasts associated with the contract version available at the time.
- Analyze disputed or materially clarified markets separately.
- Exclude or flag markets where token/outcome mapping or rules cannot be validated.

## Reporting rules

Every report includes:

- period and market selection procedure;
- resolved sample count;
- unresolved/censored sample count;
- missing-data rate;
- baseline definitions;
- confidence intervals or bootstrap intervals where appropriate;
- negative results;
- known leakage risks;
- all configuration and code revisions.

No cherry-picked screenshots or isolated successful forecasts qualify as evidence.
