# ADR-0016: Prospective exclusions require exact capture proof

- Status: Accepted
- Date: 2026-08-20
- Extends ADR-0015 and versions the prospective aggregate boundary forward.

## Context

`ProspectiveTargetExclusionV1` made an exclusion durable and digest-bound, but
its reason remained an assertion. A caller could construct a globally
self-consistent V1 aggregate, recompute every identity and digest, and still
claim that frozen capture code saw an unmodeled standalone last trade without
binding the claim to the rejection, source bytes or run manifest that would
prove it.

The bounded preflight had observed no standalone `last_trade_price`. The frozen
V2 pilot then observed genuine standalone payloads on both selected targets.
That contradiction was useful evidence: the repository now had exact bytes with
which to model the event, and the experiment had to honor its predeclared
target-exclusion rule rather than reinterpret the captures after the fact.

## Decision

### 1. Version forward; do not mutate historical claims

`ProspectiveTargetExclusionV1` and `ProspectiveExperimentBundleV1` retain their
historical schemas. The active proof-bearing boundary introduces:

- `CaptureRejectionEvidenceV1` (`capture_rejection_evidence.v1`);
- `ProspectiveTargetExclusionV2` (`prospective_target_exclusion.v2`); and
- `ProspectiveExperimentBundleV2` (`prospective_experiment_bundle.v2`).

### 2. Bind the exclusion to exact source and capture evidence

A capture-rejection proof embeds the clean `RunManifest`, exact
`RejectedObservationV1`, ingest sequence, exact UTF-8 source bytes and raw
archive location. Validation recomputes the rejection identity, byte length,
SHA-256 and content-addressed location; checks receipt/rejection chronology;
requires capture mode, raw archival, the rejection schema and an identified
clean code revision; and refuses a manifest that falsely claims the standalone
trade schema was already modeled.

The raw bytes are strictly parsed as `LastTradePriceV1`. Source, event type,
unknown-event rejection reason, condition id and token id must agree with the
rejection. Parsing documentation-shaped or semantically unrelated JSON is not
proof, even if all surrounding digests are recomputed.

### 3. Bind the proof to the selected target and frozen protocol

A V2 exclusion embeds the target and its receipt plus the capture proof and its
receipt. It accepts only the predeclared
`unmodeled_standalone_last_trade_price` reason, requires the captured condition
and token to be inside the target scope, requires the manifest subscription set
to equal both target tokens, and fixes `excluded_at` to the proven source
receipt time.

The V2 aggregate checks the capture revision and configuration fingerprint
against the frozen protocol and requires the rejection time to fall inside its
observation window. It prevents a target from appearing both as an included
bundle and an exclusion, then rederives contributions, report, verdicts and the
canonical aggregate digest.

### 4. The first qualifying rejection is permanent experiment evidence

The materializer selects the earliest qualifying standalone-trade rejection in
each completed capture ledger. Under this pilot's frozen rules, the affected
target is excluded with zero weight. No later parser, lifecycle outcome,
replacement target or reconstructed forecast can rescue it.

Modeling `LastTradePriceV1` after capture is a version-forward improvement for
future protocols. It does not change what revision `877060b` understood during
the V2 observation window and does not admit old frames into a new sample.

## Prospective pilot evidence

Protocol `m4-pilot-20260819-v2` was persisted before capture with a two-target
structural floor and a separate scientific minimum of 30 resolved targets, two
categories, five YES outcomes and five NO outcomes. Selection was one-shot;
capture was separate per target and bounded to 120 seconds or 500 frames; the
cutoff basis was first observed final settlement; replacement and rescue were
forbidden.

Both targets produced a qualifying rejection inside the frozen observation
window:

- market `3381603`, ingest sequence 7, rejection
  `rejection-a2a3fa257e8f6fb2d98968cf6cb0a9bb`, raw SHA-256
  `9a6d43ed15d780707b4e47694337cd7ef4e4c4f4deb9e0d52a562810f9666601`;
- market `3381709`, ingest sequence 23, rejection
  `rejection-beceda06336455741026ae833d54ecb8`, raw SHA-256
  `70877c8d3b91c31cc9ed310888c7e84e06bedec19ae1c31e1ef1c5ce7751794f`.

The proof-backed aggregate has two selected targets, two exclusions, zero
contributions, measurement verdict `M4_BLOCKED` and calibration verdict
`CALIBRATION_NOT_EVALUABLE`. The lifecycle closed at its frozen
`2026-08-20T06:00:00Z` deadline with both markets still `proposed` at the last
valid in-window poll (ordinal 70), no observed cutoff and
`observation_complete = true`. A host-clock jump caused one append-only poll
(ordinal 71) after the deadline. The record is retained and disclosed, but it is
inadmissible as cutoff evidence and changed no permanent exclusion,
contribution or verdict.

## Consequences

- A content digest authenticates bytes; it does not by itself prove the truth of
  relationships asserted inside them. Claim-bearing bundles must rederive those
  relationships from sibling evidence.
- The pilot is a completed negative measurement experiment, not missing data to
  be silently dropped and not a calibration result.
- Any repeat requires a new predeclared protocol and fresh captures using the
  now-observed schema. The old population, targets and evidence remain frozen.
- Gate A remains blocked. No M5, RESON, LLM, UI, wallet, authenticated trading
  or execution work is authorized.

## Rejected alternatives

- **Trust the V1 reason because its outer digest verifies.** This authenticates
  a claim without proving its source semantics.
- **Attach only a raw hash.** A hash without exact bytes, rejection, manifest,
  archive location and target scope cannot prove what was observed or where.
- **Re-run the captured bytes through the new parser and score them.** This
  rewrites the experiment after observation and violates the frozen revision.
- **Replace the excluded targets.** The protocol explicitly forbids replacement
  and selection after seeing missingness.
