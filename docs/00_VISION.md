# Vision

## Product thesis

ARGOS is a **Probability Intelligence Engine for Polymarket**.

For each selected market and each point in time, ARGOS should answer:

1. What is the exact proposition that resolves this market?
2. What did the market imply at that moment?
3. What prices were actually executable at the available depth?
4. What independent evidence was available?
5. What probability did each forecasting engine assign to YES?
6. How uncertain and mutually dependent were those forecasts?
7. Did the combined system have enough information to act, or should it abstain?
8. How did the forecast perform against the market and eventual resolution?

The product is not a feed of dramatic signals. It is an auditable belief system that can prove what it knew, why it changed its belief, and whether its probabilities deserved trust.

## Core differentiation

ARGOS is designed around five properties:

- **Resolution semantics:** the rules are compiled into a reviewable contract before modeling.
- **Reproducibility:** live data can be replayed through the identical core.
- **Calibration:** a value called 0.70 must be evaluated as a probability, not a confidence-shaped score.
- **Reliability conditioning:** later phases learn which engines are useful by market category, regime, horizon, and data quality.
- **Selective prediction:** abstention and disagreement are measured, not hidden.

## North-star outputs

For a market at time `t`, a mature ARGOS snapshot should resemble:

```text
Market: Will proposition X resolve YES under rules R?
As of: 2027-01-14T13:08:22.418Z

Market midpoint:       0.54
Best executable YES:   0.56 ask
Best executable NO:    0.47 ask

ARGOS fair P(YES):      0.68
Calibrated interval:    [0.60, 0.75]
Forecast disagreement: high
Rule ambiguity:         low
Data freshness:         healthy
Decision:               ABSTAIN
Reasons:                correlated evidence; high engine dispersion
```

The system must be able to show the exact events, source payloads, engine versions, and configuration behind this snapshot.

## Initial success definition

The first owner-review state is not a predictive breakthrough. It is a trustworthy research instrument:

- correct market identity and token mapping;
- preserved rules and lifecycle;
- durable public market-data capture;
- deterministic replay;
- resolution ingestion;
- baseline evaluation against market-implied probability;
- strong contracts, tests, and handoff.

Only after this foundation passes review should the project add external evidence, semantic extraction, RESON, reliability learning, fusion, or user interfaces.

## Long-term research questions

- Does external evidence improve proper scoring rules relative to the market baseline?
- Does engine disagreement predict forecast error?
- Does abstention produce a useful risk–coverage curve?
- Can ARGOS identify information before the market incorporates it, under a pre-registered definition of lead?
- Which engine is reliable for which category, liquidity profile, time-to-resolution, and evidence regime?
- Can the system distinguish true evidence novelty from repeated reporting of the same source?
