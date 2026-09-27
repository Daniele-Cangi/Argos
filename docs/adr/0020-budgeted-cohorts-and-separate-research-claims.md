# ADR-0020: Budgeted cohorts and separate research claims

- Status: Accepted
- Date: 2026-09-27
- Owner decision: revise M4 after the methodological audit, then implement.
- Supersedes ADR-0019 section 6 for **future** cohorts. Extends ADR-0014's
  protocol-defined sufficiency; replaces future-cohort all-or-nothing and
  last-before-settlement choices inherited from ADR-0015.
- Does not reinterpret any frozen protocol, historical verdict or stored schema.

## Context

The unlaunched V1 operator requires exactly four blocks of four targets. It
discards eligible targets from a short block and requires every predecessor to
be admitted. This recreates the all-or-nothing failure mode ADR-0019 addressed.
Requiring every target to end after the last block also excludes useful early
events. Different provider event IDs do not prove independent real-world events.

ADR-0014 rejected a universal larger sample-size constant. ADR-0019 nevertheless
attributed a 30-resolved minimum to it. Thirty observations, two categories and
five outcomes of each class do not establish precision or calibration. Requiring
calibrated forecasts before assessing calibration is circular; ADR-0006's naming
of `p_yes` is a separate matter. Likewise, a forecast before delayed platform
settlement but after the event is knowable is not necessarily prospective.

## Decision

### 1. Three results, not one success flag

| Result | Evidence required | Does not establish |
|---|---|---|
| Operational M4 | bounded reviewed prospective capture, durable shared snapshot, replay, complete accounting, tested outcome/scoring path | predictive usefulness or calibration |
| Preliminary outcome evaluation | admissible final labels joined to original snapshots; per-method coverage and paired descriptive scores | performance on pending/excluded targets or other populations |
| Scientific claim | separately frozen estimand, population, precision-based sample plan, dependence-aware uncertainty and stopping rule | trading edge or execution authorization |

Operational closure can occur with pending real outcomes, but still needs
verified prospective evidence and owner review. A complete ledger alone, or
synthetic tests alone, is not an M4 pass. Capture/integrity/accounting failures
remain `PARTIAL`/`BLOCKED` as warranted. Publish the three results separately.

### 2. Partial blocks and predeclared budgets

Freeze variable UTC blocks and maximum admission slots. Stop acquisition at the
target cap, final block deadline or resource cap, whichever applies first.
Short/empty blocks retain valid targets, record missing slots/reasons and permit
later blocks. Do not transfer unused block slots or stratum quotas. Every
predecessor, including empty/failed ones, needs verified hash-linked accounting;
missing/corrupt evidence cannot be silently treated as an empty block.

Freeze deterministic candidate ordering before forecasts. Candidates may be
excluded and the next considered only **before** their target admission receipt.
No replacement after admission due to failure, abstention, score or slow outcome;
no recapture to choose a favorable snapshot. Resuming monitoring is not admission.

Report planned slots, admitted targets, intact snapshots, final targets,
admissibly scored targets, and per-method predictions/abstentions/exclusions.
Pending targets stay in the admitted denominator; unfilled slots stay in the
planned denominator, not as fictional markets.

### 3. Variation and dependence

Declare a few nonoverlapping category/liquidity/spread/horizon strata and quotas.
Process strata by ID; within each use decreasing observed liquidity then
canonical numeric market ID. Archive exact query, source page and selection
features. This is a purposive sample of declared bands, not all markets.

Persist provider IDs plus a reviewed real-world event-group identity. Admit at
most one target per group across blocks, rejecting market/condition/token aliases
too. Review documents related events instead of assuming unequal strings prove
independence. Disclose residual cross-group dependence. The pilot uses simple
human-reviewable market families; future inference must justify its sampling
units, weights and uncertainty method.

### 4. Freeze before the answer is knowable

Before admission, persist a human semantic-review receipt bound to the exact
contract, outcome conditions, resolution authority, category, event group and
conservative earliest outcome-knowable time. Nonempty text or compiler
`UNREVIEWED` is not approval. Exclude ambiguity/already knowable outcomes with
reasons; no agent invents human approval.

Use the last shared information state before **capture close**, not the last
before settlement. Freeze all four baseline decisions, including abstentions,
at that state. The snapshot receipt must precede the reviewed outcome-knowable
time minus a declared margin. Reserve capture/finalization time before starting;
an actual missed boundary excludes predictive use without erasing evidence.
Gamma end time alone does not prove outcome blindness. Report actual horizons.

Later evidence of a previously public answer produces a superseding exclusion,
not a shifted forecast. Early and late finality use one versioned join: original
snapshot, exact raw outcome, contract, source time, actual retrieval and receipts.
Never backdate across gaps. Proposed/disputed/unknown are not final outcomes.

### 5. Scores and uncertainty match the claim

Brier is primary; clipped log loss secondary with frozen epsilon in `(0, 0.5)`.
Absolute error is diagnostic, not a proper probability-scoring objective.
Retain `raw_score` naming until ADR-0006 is met. Calibration diagnostics can
assess raw scores without renaming them `p_yes` or establishing calibration.

Descriptive scores weight independent event groups equally. Method comparisons
use paired admissible decisions from the same target snapshot; report paired
count and each method's coverage/abstention. Do not impute pending labels or
generalize the faster-resolving subset to the full cohort. Report strata/horizons
where counts permit, including empty groups.

V2 has no universal 30/2/5/5 scientific gate and always declares calibration
`NOT_ESTABLISHED`. A later scientific protocol needs estimand, desired precision,
confidence procedure/assumptions, independent units, sample-size rationale and
maximum budget. Choose fixed-sample or valid sequential inference beforehand;
repeated ordinary confidence intervals until favorable are not a stopping rule.
Budget exhaustion can honestly remain inconclusive. Sparse bins/missing outcome
classes are limitations, not permission to harvest until balanced.

### 6. Resources and monitoring

Keep separate raw archive/database per target and subscribe both tokens. Stop
capture at the first time, frame or byte cap. Reserve finalization time, campaign
evidence bytes and free-disk headroom. Runtime limits cover raw payloads, DB/WAL,
logs, temporary/final artifacts and space for an honest failure record. A
declaration is not a disk measurement. Do not duplicate historical evidence.

Three-hour finality polling is a reasonable pilot default after freezing the
forecast: 36 times fewer scheduled requests than five minutes over equal time,
with about three hours of scheduled detection latency plus failures/source
delay. Record late/missed polls and actual retrieval times under one exclusive
owner. Acquisition end, operational review and finite follow-up end are distinct.
Pending stays pending at follow-up end. Further follow-up requires an append-only
schedule, not a hidden extension or new forecast.

An anonymous malformed entry can be addressed by exact page hash, zero-based
entry index and canonical entry hash. A future V2 selector may quarantine it
while retaining valid entries only after archive replay proves a full partition.
Ambiguous duplicate identities still fail closed. Do not relax V1 in place.

## Implementation and compatibility

`AsynchronousCohortProtocolV2` is a new independent declaration: variable slot
caps, disjoint strata, resource/time feasibility and fixed snapshot/claim policies.
The schema-dispatched read-only preflight explicitly reports V2
`live_launch_supported: false`. It does not prove human review, disk space,
source eligibility, runtime enforcement or a durable protocol freeze.

V1 remains 4x4 with original validators/tests. Historical V2/V3/V5/V8 experiments,
T1-T8 proof, old aggregation and `LateFinalOutcomeV1` remain unchanged. Do not
route V2 declarations into V1 selection or post-deadline lifecycle code. New
review, selection and unified outcome records/adapters remain launch blockers.

Follow `docs/research/m4-asynchronous-cohort-design.md`: reuse qualified technical
machinery, rerun affected regression/fault tests, prove a small synthetic vertical
path, then a 2-4-slot live integration pilot. A methodology change alone does not
justify another full six-hour soak.

## Rejected alternatives

- Retroactively shorten historical deadlines: manufactures a pass.
- Wait for all real settlements to close operations: confounds latency with
  capture/replay correctness again.
- Treat 30 targets as calibration proof: no precision justification.
- Drop pending/failed targets or redraw for enough YES/NO: outcome-driven selection.
- Launch from a declaration validator alone: no evidence of runtime enforcement.

## Methodological references

- Gneiting and Raftery (2007), *Strictly Proper Scoring Rules, Prediction, and
  Estimation*, [JASA](https://doi.org/10.1198/016214506000001437): proper
  probability scores are distinct from absolute-error diagnostics.
- Dimitriadis, Gneiting and Jordan (2021), *Stable reliability diagrams*,
  [PNAS](https://doi.org/10.1073/pnas.2016191118): assessment/uncertainty should
  not be reduced to arbitrary calibration bin counts.
- Dimitriadis et al., *Honest calibration assessment for binary outcome
  predictions*, [Biometrika](https://doi.org/10.1093/biomet/asac068): uncertainty
  needs explicit assumptions, not a universal resolved-market count. No specific
  sample size is inferred from these references.
