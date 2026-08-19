# ADR-0013: Evaluation claims require admissible, digest-bound evidence

- Status: Accepted
- Date: 2026-08-19
- Extends ADR-0003, ADR-0004, ADR-0006, ADR-0011 and ADR-0012.
- Supersedes the arrival-weighted evaluation semantics in the M4 v1 runner.

## Context

The independent owner review reproduced five defects in the M4 result and
found four more. The evaluator emitted forecasts once per transport arrival,
so duplicate deliveries and irrelevant sibling traffic changed metric weights
without changing the target market's information. It accepted non-final
resolutions, included forecasts made after settlement, and persisted only a
summary that could not reconstruct or authenticate its children. It also
called forecast records samples even though 75 scores referred to one resolved
target.

The recorded initial book already contained a last trade, but dispatch dropped
it. The displayed-price baseline had also been collapsed onto midpoint even
though the source's documented rule uses last trade for a spread greater than
0.10. Finally, the same resolution payload could be assigned one identity
while changing the caller-supplied YES-token mapping.

Green arithmetic and branch coverage therefore proved implementation
consistency, not measurement validity.

## Decision

### 1. One evaluation produces one self-contained evidence bundle

`EvaluationRunBundleV1` contains its versioned policy, resolution, optional
compiled contract, report, ordered forecasts, evaluations, per-arrival
decisions and exclusions. A canonical digest binds the complete nested
records. Deserialization validates every child schema version, link and digest;
a summary without those children is not an ARGOS evaluation claim.

The report separately records the ordered input-trajectory hash, final
projection-state hash, settings snapshot and fingerprint, revision and working
tree status, capture completion, replay/dispatch counts, resolution and
contract record hashes, and child-collection hashes.

### 2. Forecast emission follows target information-state transitions

The v2 evaluator drives the existing `ReplaySession` and shared dispatcher.
It persists a decision for every arrival. It emits a forecast only when the
selected target's information-state hash changes after dispatch.

Duplicate deliveries, rejections, unhandled payloads, unscoped records,
irrelevant sibling traffic, unseeded target state and unchanged target state
are counted decisions, not implicit control flow. Duplicate and sibling
arrivals may change the trajectory hash, because the evidence trajectory
changed, but must not change the target forecast series.

### 3. Admissibility is strict and visible

Only a `final` resolution can enter evaluation. Forecasts at or after the
recorded resolution cutoff are excluded. If the exact cutoff is absent,
temporal admissibility is unproved and no forecast is scored.

A persisted compiled contract is required for scoring, and its condition must
match the resolution. An incomplete capture, sequence gap, received-time
regression or unhandled payload blocks headline scoring. Every affected
forecast receives a versioned exclusion reason.

### 4. Headline units are resolved targets

Reports distinguish arrivals, target information states, forecast points,
scored forecast points, resolved targets and headline-eligible targets.
Successive trajectory points are diagnostics, not independent outcomes.

The one-target evaluator leaves headline calibration empty. Its per-trajectory
calibration is explicitly diagnostic. Two independently resolved targets are
only a structural floor, not scientific sufficiency; ADR-0014 supersedes the
original wording of this paragraph and requires a predeclared multi-target
protocol to define weighting and sufficiency before any calibration claim.

### 5. Market prices keep distinct meanings

Midpoint, last trade and displayed price are separate baseline methods.
Displayed price uses last trade when the bid/ask spread is strictly greater
than 0.10 and midpoint otherwise. Missing evidence causes a counted abstention.

Last trade is auxiliary market evidence with its own observation provenance.
It does not enter `book_state_digest.v1` or `state_hash.v1`; changing those
hashes would redefine the M3 projection property. Initial REST and WebSocket
book snapshots preserve their supplied last trade. A standalone trade event
will be modeled only from a pinned raw fixture that establishes its schema.

### 6. Identity changes require declared version changes

Forecast identity is derived from source capture/observation, information
state, method, target and contract, not from the evaluation-run identifier.
Evaluation identity binds forecast, resolution, contract, evaluator version and
epsilon. Resolution identity binds the normalized outcome and caller-supplied
YES-token mapping.

The evaluator and both resolution normalizers move to version `/2`. The v1
arrival-weighted runner remains only as an explicitly named unsafe compatibility
module and is not exported from `argos.evaluation`.

The displayed-price and last-trade distinction is grounded in the source's
current public contracts: Polymarket's
[real-time data guide](https://docs.polymarket.com/market-data/realtime-data)
documents book snapshots and last-trade updates; its official
[WebSocket reference](https://github.com/Polymarket/agent-skills/blob/main/websocket.md)
and
[market-data reference](https://github.com/Polymarket/agent-skills/blob/main/market-data.md)
record the conditional display rule and event fields. The repository fixture,
not documentation alone, remains the prerequisite for a new persistent event
schema.

## Consequences

- The committed historical capture cannot produce a headline result: its CLOB
  resolution has no exact cutoff and no persisted compiled contract accompanies
  the evaluation. The correct result is a digest-bound bundle with explicit
  exclusions and zero scored points.
- Adding more markets is not the next step until capture records finality,
  exact cutoff and contract identity.
- A duplicate/sibling adversarial test, finality/cutoff tests and bundle-tamper
  tests become scientific acceptance criteria.
- M4 returns to blocked until the corrected multi-target evidence experiment
  exists. M5-M8 remain out of scope.

## Rejected alternatives

- **Keep one forecast per arrival and disclose autocorrelation.** Disclosure
  does not stop transport fan-out from changing the estimator.
- **Use the final projection hash as trajectory identity.** It intentionally
  excludes arrivals and cannot bind the evidence path.
- **Score without a cutoff and add a limitation.** That cannot prove the
  forecast preceded the outcome.
- **Treat displayed price as midpoint everywhere.** A narrow-spread sample
  cannot establish the wide-spread branch.
- **Put last trade into the book-state hash.** It would conflate auxiliary trade
  evidence with the order-book projection and silently change ADR-0012.
