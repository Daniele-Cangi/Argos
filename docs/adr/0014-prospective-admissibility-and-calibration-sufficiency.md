# ADR-0014: Prospective evidence and protocol-defined calibration sufficiency

- Status: Accepted
- Date: 2026-08-19
- Extends ADR-0013.
- Supersedes ADR-0013 section 4 only where it described two resolved targets as
  sufficient to establish calibration.

## Context

ADR-0013 correctly changed the headline unit from forecast points to
independently resolved targets. Its first implementation nevertheless named a
policy field `minimum_resolved_targets_for_calibration` and defaulted it to
two. Two targets are only the first non-degenerate independent-outcome sample;
they do not establish reliability, expected calibration error, or any other
scientific calibration claim.

The same review exposed two prospective-evidence gaps. A `resolved_at` value
does not say which source observation made the cutoff knowable, and a compiled
contract included in an evaluation bundle does not prove that the contract was
persisted before the forecast rather than reconstructed after settlement.

## Decision

### 1. Two targets are a structural floor, never a sufficiency threshold

`EvaluationPolicyV2` replaces the ambiguous field with
`structural_minimum_independent_resolved_targets`, still defaulting to two. The
one-target runner always reports
`single_target_runner_cannot_establish_calibration`; it never derives headline
status from the structural floor.

A multi-target calibration claim requires a protocol persisted before the
first included forecast. That protocol must declare at least:

- target population, selection rules, observation window and stopping rule;
- minimum resolved-target count and the scientific rationale for it;
- within-target aggregation and across-target weighting;
- metrics, calibration bins, clipping rules and uncertainty intervals;
- missingness, abstention, exclusion and disputed-market treatment; and
- the sufficiency rule, including any required category/outcome dispersion.

Passing the structural floor without passing that protocol's sufficiency rule
cannot produce `headline_status=established`.

### 2. Exact cutoff means one of two predeclared source observations

The prospective protocol must choose the cutoff basis before observation:

1. **Source terminal timestamp.** Use a terminal settlement timestamp explicitly
   stated by the resolution source and verifiable in the immutable source
   payload. The cutoff is that source time.
2. **First observed final settlement.** When no verifiable terminal timestamp
   exists, poll the lifecycle source and use the `retrieved_at` of the first
   immutably recorded payload that states a final settlement. The cutoff is
   receipt time, not a later source-update value.

The evidence keeps `source_time`, `retrieved_at` and the selected cutoff as
separate fields and binds source, endpoint, raw-payload digest, byte length,
observation identity, finality and resolution identity. Reconstructed
provenance is inadmissible. A later fetch or an ex-post estimate cannot be used
to move the cutoff earlier.

### 3. Contract availability must precede the forecast

The compiled contract record, its digest and a persistence receipt must exist
before the earliest included forecast's `as_of_received_time`. `compiled_at`
alone is not proof of persistence, and attaching a contract to an evaluation
bundle after resolution does not make a historical forecast contract-bound.
Contract supersession is evaluated by availability time.

The prospective branch must introduce versioned, digest-bound cutoff and
contract-availability evidence records before it can admit real forecasts.
Until then, current v2 trajectory scores are diagnostics and M4 remains
blocked.

### 4. Version the semantic correction

`evaluation_policy.v1` and `evaluation_run_bundle.v1` remain readable with
their declared meanings. The corrected runner writes `evaluation_policy.v2`
inside `evaluation_run_bundle.v2`; the evidence digest version also advances.
No existing record is rewritten under an old schema name.

## Consequences

- A sample of two markets can exercise structure but cannot establish
  calibration merely by reaching a constant.
- A source's generic `updatedAt`, a normalization time, or a later successful
  fetch is not automatically a resolution cutoff.
- A contract reconstructed from the same market payload after settlement is
  not admissible evidence for earlier forecasts.
- The next branch is restricted to the prospective M4 evidence experiment. It
  does not authorize M5, RESON, AI forecasting, or execution work.
- M4 remains **BLOCKED** until a real multi-target experiment traverses these
  requirements without reconstructed evidence.

## Rejected alternatives

- **Replace two with a larger generic constant.** A number without a declared
  population, weighting and stopping rule only hides the same ambiguity.
- **Use the source's last-update timestamp.** It may describe unrelated edits
  or be unavailable to ARGOS when settlement first became observable.
- **Trust `compiled_at`.** A timestamp inside a record does not prove when that
  record became durably available.
- **Document reconstruction as a limitation.** Ex-post reconstruction changes
  admissibility; it is not a caveat on otherwise valid prospective evidence.
