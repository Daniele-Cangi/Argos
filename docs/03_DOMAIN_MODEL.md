# Domain model

## Polymarket concepts

### Event

A Polymarket event may group one or more related markets. It is not the same as an individual binary market.

### Market

A market is the resolution proposition identified by market metadata and `condition_id`. It has outcomes and one or more outcome token IDs.

### Outcome token

A tradable CLOB asset representing an outcome such as YES or NO. Token ID and outcome mapping must be verified and stored together.

### Market contract

ARGOS's compiled, versioned interpretation of the original market question and rules. It never replaces source text and may require human review.

## ARGOS objects

### `MarketDefinition`

Normalized source metadata and lifecycle state. It points to immutable raw payloads.

### `CompiledMarketContract`

A reviewable representation containing proposition, YES condition, NO condition, deadline, resolution source, edge cases, ambiguity flags, compiler version, and review status.

### `ObservationEnvelope`

Canonical transport for every source observation. It separates source metadata and times from the typed payload.

### `MarketState`

A deterministic projection at an ingest sequence. Includes lifecycle, latest books, quotes, trades, and source health. It is derived and rebuildable.

### `MarketQuote`

A point-in-time quote with midpoint, best bid/ask for outcome tokens, spread, depth assumptions, and source timestamps. Midpoint is not executable edge.

### `EngineForecast`

One engine's versioned estimate for the probability of the market resolving YES, with uncertainty, evidence dependencies, data cutoff, and calibration status.

### `FusionForecast`

A combined estimate with engine contributions, dependency treatment, dispersion, and calibration version.

### `EdgeAssessment`

Compares a fair probability with specific executable prices and size assumptions. It is not a trade instruction.

### `Decision`

One of `MONITOR`, `ABSTAIN`, `EDGE_YES`, or `EDGE_NO` in later phases, with reasons. Through M4 only baseline and evaluation records are required.

### `Resolution`

The normalized resolved outcome, resolution timestamp, winning token/outcome, source payload, and any dispute/clarification metadata available.

### `ForecastEvaluation`

Links one forecast to a resolution and computes proper scores and cohort metadata.

## Separation rules

- A quote never contains an ARGOS forecast.
- A forecast never contains an order instruction.
- An edge assessment always names the exact quote side and size assumption.
- A decision always references a forecast and market contract version.
- An evaluation never mutates the original forecast.
- A market compiler output is not considered reviewed merely because parsing succeeded.
