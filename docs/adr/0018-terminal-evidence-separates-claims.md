# ADR-0018: Terminal evidence separates accounting, continuity and admissibility

- Status: Accepted
- Date: 2026-08-21
- Extends ADR-0017 and versions the prospective terminal aggregate forward.

## Context

The fresh V3 experiment `m4-prospective-20260820-v3` exercised the modeled
standalone last-trade path successfully. Both preregistered target captures
completed without rejected, unknown or undecodable input. Neither selected
target produced an observed admissible final-settlement cutoff before the
frozen lifecycle deadline.

The monitor persisted 98 receipt-bound lifecycle observations per target. Its
last observations were `proposed` near `02:23Z`, then host suspension left
an approximately 3h37 unobserved tail before the `06:00Z` deadline. There
were no post-deadline observations.

This failure mode is not a capture rejection, so ADR-0016's
`CaptureRejectionEvidenceV1` and `ProspectiveTargetExclusionV2` cannot
represent it honestly. A text-only exclusion would be unauthenticated.
Likewise, one `observation_complete` flag cannot express all of these true
facts at once:

1. all selected targets are accounted for;
2. lifecycle continuity is incomplete;
3. zero admissible cutoffs were observed.

Silence during the final interval cannot establish whether either market
settled. ARGOS needs a terminal proof that authenticates what was observed
without inferring what was not.

## Decision

### 1. Bind clean capture completion separately

`CaptureRunSummaryV1` binds the selected target and experiment to the exact
clean `RunManifest`, capture start/end, completion status, frame and accepted
counts, duplicate/rejection/decode/unknown counts, schema/rejection counts,
SQLite ledger SHA-256 and byte length, and delivery/rejection collection
digests.

The summary must agree with protocol V2 revision, configuration, observation
window, per-target limits, raw-archive policy and exact two-token subscription.
A terminal no-cutoff claim cannot hide a rejected or unknown capture event.

### 2. Make every lifecycle poll portable proof

`LifecyclePollEvidenceV1` embeds one `LifecycleObservationV1`, its
`EvidencePersistenceReceiptV1` and the exact raw UTF-8 source bytes. It
recomputes:

- observation and receipt identities;
- source payload SHA-256 and byte length;
- source, endpoint and interpreted finality; and
- the poll evidence identity.

The target terminal chain requires contiguous ordinals and predecessor links in
retrieval order. Removing, reordering or retargeting a poll invalidates the
claim.

### 3. Define a terminal target without inventing resolution

`ProspectiveTargetTerminalEvidenceV1` binds:

- experiment identity and protocol V2 digest/receipt;
- the original selected target and target receipt;
- its clean capture summary;
- the complete ordered portable poll chain;
- the last admissible in-window observation and observed finality;
- mechanically derived final and maximum cadence gaps;
- lifecycle continuity;
- zero observed admissible cutoffs;
- closure time; and
- a fixed `TARGET_NOT_PROVEN_FINAL_BY_DEADLINE` disposition.

It structurally forbids target replacement, rescue and
`claims_observed_through_deadline = true`. It rejects a final lifecycle
observation because that evidence belongs in the existing cutoff/resolution
path, not in a no-cutoff terminal claim. Closure after the deadline proves only
that the collection window ended.

### 4. Publish three independent aggregate claims

`ProspectiveTerminalReportV1` reports:

- `TargetAccountingStatus`;
- `LifecycleContinuityStatus`; and
- `ResolutionAdmissibilityStatus`.

`ProspectiveExperimentBundleV4` embeds protocol/receipt, every terminal target
and terminal receipt, and the derived report. It rechecks each child against
protocol V2, requires unique ordered frozen targets and capture runs, derives
the three report states, and hashes the complete portable claim.

For the real V3 evidence these states derive as:

- `TARGET_ACCOUNTING_COMPLETE`;
- `LIFECYCLE_CONTINUITY_INCOMPLETE`; and
- `NO_ADMISSIBLE_CUTOFF_OBSERVED`.

The existing verdict policy consequently remains `M4_BLOCKED` and
`CALIBRATION_NOT_EVALUABLE`. These are not evidence that capture,
normalization or replay failed; they mean the experiment did not obtain its
required 2/2 admissible cutoffs and therefore did not validate M4 end to end.

### 5. Publish exact repository-verifiable bytes

`ProspectiveClaimArtifactIndexV1` recognizes the V4 aggregate without changing
its own schema meaning. It binds the repository-relative bundle path, V4
schema, evidence digest, exact byte length, SHA-256 and original aggregate
receipt. The verifier hashes the actual committed bytes, parses V4 and reruns
all semantic validation.

The committed real bundle is
`experiments/m4-prospective-20260820-v3/proof/claim-bundle-v4.json`.
Its evidence digest is
`42443a664441b3fa77c10a920eaec587a4d50d13dd9573e5eceb755d8d13c92a`,
its SHA-256 is
`400f7b9619643900e41a0c38db6f62a13aba6112cef247209ab343268538c86d`,
and its byte length is 1,230,831.

## Consequences

- A clean checkout can independently verify target identity, capture
  completion, every lifecycle observation/receipt/raw payload, cutoff absence,
  final gap, continuity and terminal disposition.
- A global outer re-digest cannot authenticate changed finality, deadline,
  cadence gap, continuity, target identity, poll order or terminal
  disposition. Nested identities, receipts and semantic validators still fail.
- The last observed state is reported as `proposed`; no claim extends it
  through the unobserved tail and no cutoff is reconstructed.
- Historical V1-V3 aggregate meanings remain unchanged.
- Gate A remains blocked. M5, RESON, LLM forecasting, UI, wallet,
  authentication, trading and execution remain unauthorized.

## Rejected alternatives

- **Reuse the last-trade exclusion proof.** No capture rejection occurred, so
  that would manufacture evidence.
- **Use a text-only unresolved exclusion.** It would not authenticate the
  lifecycle chain or receipt-backed cutoff absence.
- **Treat deadline passage as continuous observation.** Host suspension proves
  the opposite: the collection window ended with a material unobserved tail.
- **Infer that proposed persisted until the deadline.** The source record stops
  near `02:23Z`; ARGOS has no evidence for the remaining interval.
- **Mark the targets resolved without cutoff evidence.** Resolution
  admissibility requires a matching observed final lifecycle record and cutoff
  proof.
