# ADR-0017: Prospective operations and published claims are evidence-bound

- Status: Accepted
- Date: 2026-08-20
- Extends ADR-0016 and versions prospective protocol, target and aggregate boundaries forward.

## Context

The proof-backed V2 pilot aggregate authenticates its exclusions, but a final
hardening review found four narrower claim-boundary gaps:

1. lifecycle deadline, polling cadence and capture limits existed only in a
   specification-side `capture` object and prose `stopping_rule`, not in the
   persisted `ProspectiveExperimentProtocolV1` record;
2. a lifecycle request begun near the deadline could return after it and the
   poller could mint that late observation into cutoff evidence;
3. deadline elapsed, complete selected-target accounting and continuous
   lifecycle polling were being summarized as one kind of completeness; and
4. the full proof-bearing aggregate was content-addressed under ignored local
   `.data`, so a repository checkout contained a summary and receipt but not
   the bytes needed to verify the claim independently.

None changes the pilot's negative scientific result. Both targets remain
permanently excluded by exact capture evidence, with zero contributions,
`M4_BLOCKED` and `CALIBRATION_NOT_EVALUABLE`.

## Decision

### 1. Operational bounds are protocol evidence

`ProspectiveExperimentProtocolV2` adds typed, immutable fields for:

- lifecycle deadline and polling interval;
- maximum seconds and frames per target;
- separate capture database per target;
- subscription to both target tokens; and
- immutable raw archival.

The freeze helper copies the predeclared capture block into these fields before
persisting the protocol and refuses any disagreement. Historical V1 protocol
records remain V1 and are not rewritten.

### 2. A cutoff cannot cross the frozen deadline

The poller checks the frozen deadline before starting each target request. A
request that returns late may still be retained as append-only lifecycle
evidence, but it cannot mint a cutoff. Both `retrieved_at` and
`selected_cutoff` must be at or before the V2 protocol deadline.

`EvaluationRunBundleV4` carries the V2 protocol and the exact capture
`RunManifest`. Its digest binds the complete V3 claim plus that manifest. It
cross-validates run identity, clean revision, configuration fingerprint,
subscribed tokens, capture bounds and raw-archive policy.

`ProspectiveExperimentBundleV3` accepts V4 target bundles and proof-backed V2
exclusions under one V2 protocol. It rejects capture-bound disagreement and
prevents any capture run from being shared by two selected targets.

### 3. Closure, accounting and lifecycle continuity are distinct facts

The materialized result reports separately:

- `experiment_closed_by_frozen_deadline`;
- `target_accounting_complete`;
- `all_targets_final_by_frozen_deadline`; and
- `lifecycle_record_complete`.

Aggregate `observation_complete` continues to describe closed selected-target
accounting for the historical V2 artifact. It is not evidence of uninterrupted
lifecycle polling. Continuity is conservative: if an entire nominal polling
window is absent (a gap greater than twice the frozen cadence), the lifecycle
record is incomplete even after the experiment closes.

The V2 pilot therefore has
`experiment_closed_by_frozen_deadline = true` and
`lifecycle_record_complete = false`. Its last in-window observations were near
`05:35Z`, leaving more than four nominal five-minute intervals before the
`06:00Z` deadline; the later append-only poll at `07:09Z` cannot fill that gap.

### 4. Repository claims publish verifiable bytes

`ProspectiveClaimArtifactIndexV1` binds a repository-relative aggregate path to
its schema, scientific evidence digest, exact byte length, SHA-256 and original
persistence receipt. Verification resolves the path beneath the index
directory, rejects symlinks/traversal, hashes the exact bytes, parses the
versioned aggregate and rechecks the receipt and semantic identity.

The historical aggregate is published byte-for-byte at
`experiments/m4-pilot-20260819/proof/claim-bundle-v2.json`, with index
`claim-index-v1.json`. Its SHA-256 remains
`8c24e919d586bc81b523bddb129378bf23f79f6c478ae10be330a789bb6e1034`;
no historical evidence was regenerated.

## Consequences

- The clock-jump gap remains explicit incomplete lifecycle evidence; deadline
  closure does not upgrade it.
- A global re-digest cannot make a post-deadline cutoff or widened capture
  bound admissible.
- A clean checkout now contains enough bytes to verify the committed M4 claim
  and its original receipt without access to the producer's `.data` directory.
- No new experiment is run by this correction. Any future V3 experiment needs
  a newly frozen V2 protocol, fresh targets and fresh captures after this
  decision is merged.
- Gate A remains blocked. No M5, RESON, LLM, UI, wallet, authenticated trading
  or execution work is authorized.

## Rejected alternatives

- **Treat the textual stopping rule as structural evidence.** Prose cannot be
  cross-validated against a cutoff or capture manifest.
- **Drop a late poll.** Deletion would hide the deviation; retention without
  cutoff admission preserves both chronology and the deadline.
- **Mark lifecycle complete because the deadline elapsed.** This conflates a
  closed experiment with a continuous evidence chain.
- **Publish only hashes and summaries.** They identify missing bytes but do not
  let an independent checkout verify the claim.
