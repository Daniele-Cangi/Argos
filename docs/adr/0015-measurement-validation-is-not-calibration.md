# ADR-0015: Prospective measurement validation is not calibration

- Status: Accepted
- Date: 2026-08-19
- Extends ADR-0014 without weakening its calibration standard.

## Context

ADR-0014 requires a prospective, independently resolved, multi-target
experiment before M4 can be trusted. Its closing sentence described that
experiment as the remaining M4 blocker, but did not name the two different
claims the experiment can answer:

1. whether ARGOS can predeclare, preserve, replay and score admissible evidence
   without hindsight; and
2. whether enough independent outcomes exist to support a scientific
   calibration claim.

The first can be tested with the smallest non-degenerate multi-target pilot.
The second normally needs a much larger predeclared sample, outcome dispersion,
category coverage and uncertainty analysis. Coupling them would either keep a
working measurement layer permanently open or encourage lowering the
calibration threshold after seeing results.

## Decision

### 1. Publish two independent verdicts

Every prospective experiment aggregate reports exactly one measurement verdict:

- `M4_MEASUREMENT_LAYER_PASSED`;
- `M4_PARTIALLY_VALIDATED`; or
- `M4_BLOCKED`.

It separately reports exactly one calibration verdict:

- `CALIBRATION_ESTABLISHED`;
- `CALIBRATION_NOT_ESTABLISHED`; or
- `CALIBRATION_NOT_EVALUABLE`.

No code path derives one verdict from the other. Passing the structural floor
can validate the measurement path while calibration remains not established.

### 2. Version the prospective boundary

Historical V2 records keep their declared meaning. `EvaluationRunBundleV3`
adds a frozen protocol and persistence receipt, exact market/contract/target
records and receipts, an ordered receipt-bound lifecycle chain, cutoff evidence
for the first observed final settlement (or the predeclared source-terminal
alternative), the original resolution record, and all replay children.

`EvaluationReportV3.resolution_cutoff` is explicitly the cutoff proven by the
cutoff-evidence record. It is not a reinterpretation of
`ResolutionV1.resolved_at`. The V3 validator derives chronology, identities,
counts, partitions and child digests from sibling evidence before it accepts the
outer digest.

### 3. One target contributes one unit per method

Within a target, forecast points are a dependent trajectory. The supported V1
aggregation selects the last admissible pre-cutoff evaluation per method.
Across targets, each independently resolved target has weight one. Duplicate
target identities cannot supply two independent units. Target counts and
forecast-point counts remain separate.

The experiment protocol records three distinct quantities:

- structural minimum independent resolved targets;
- intended target count for this experiment; and
- scientific minimum resolved targets for calibration.

Category and YES/NO outcome minima are structured fields, not prose parsed
after the result. Current calibration bins must be equal-width because that is
the only binning implemented by the evaluator; unsupported declarations are
refused.

### 4. The standalone last-trade uncertainty fails visibly

The bounded public preflight did not observe a standalone
`last_trade_price` event. Its schema therefore remains unmodeled. The pilot
predeclares target-level exclusion if one appears. It may not be silently
ignored, fabricated from documentation, or used to terminate unrelated target
evidence unless a later protocol explicitly chooses that stronger policy.

### 5. Measurement passage does not authorize M5

An M4 measurement pass ends this experiment and returns to the owner gate. It
does not authorize RESON, LLM forecasting, UI, wallet, orders, execution or any
authenticated trading channel.

## Consequences

- A two-target pilot can close the measurement-layer blocker only if both
  targets traverse the frozen protocol, pre-forecast receipts, public capture,
  finality evidence, reconstruction and aggregation successfully.
- That same pilot must normally publish `CALIBRATION_NOT_ESTABLISHED`.
- Excluded targets remain persistent records with reasons and do not disappear
  from the intended population.
- A report cannot promote either verdict by changing its bytes and recomputing
  the outer digest; the aggregate bundle rederives both verdicts.

## Rejected alternatives

- **Call a structural pilot a calibration study.** This changes the scientific
  threshold after choosing a convenient sample.
- **Average every forecast point.** It makes a busier target carry more weight
  without adding an independent outcome.
- **Keep one ambiguous “M4 passed” flag.** It hides whether the code path or the
  statistical claim was validated.
- **Start M5 after a measurement pass.** Owner Gate A remains a separate
  authorization boundary.
