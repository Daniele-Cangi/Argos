# M4 asynchronous prospective cohort: design gate

Status: design and implementation in progress, **no live cohort frozen or
launched**. The owner's 2026-09-26 decision authorizes beginning this phase;
it does not turn T1–T8 into a predictive or calibration result. Technical proof
publication and CI are complete in merged PR #18. A live start still requires
an integrated late-resolution owner and scorer, clean code revision, exact UTC
windows, and a persisted protocol before first target selection or capture.

## Separate the clocks

The unit of observation is an independently selected event target. A short
capture freezes its contract, raw source, both tokens, replay and forecast.
Settlement is a later fact. A missed two-day finality deadline must not erase
a valid forecast or invent a historical cutoff. The capture-complete report
is produced independently of the resolution-progress and scored-target
reports. At any review date, unresolved targets remain `PENDING_RESOLUTION`
and in the original intended denominator.

## First operational cohort

- Target 16 distinct Gamma event identities, in four UTC time blocks of four
  targets. The final block times and selection query must be frozen in the
  protocol before the first block. Use no predecessor experiment evidence.
- Select public active binary Yes/No markets with a valid compiled contract,
  both CLOB tokens, adequate liquidity and a declared end time; persist the
  raw discovery response, deterministic ordering and every selected target.
  Reject a block before any forecast if it cannot meet its declared count.
- Cap each separate capture at 120 seconds or 500 frames, with raw archive and
  separate SQLite database. Record the actual capture start/end and any
  abstention or exclusion. Never replace a target after selection.
- Keep one exclusive, resumable lifecycle owner. It persists ordinal,
  predecessor digest, exact raw payload, receipt, source time and retrieval
  time for each observation. Failed requests create no observation; gaps are
  explicit. Proposed, disputed and unknown states are not final.
- At the operational review (proposed: day 14), publish captured, intact,
  final, pending, disputed and excluded counts separately. Stop high-frequency
  polling if warranted, but retain pending targets for lower-frequency
  follow-up under a separately frozen cadence. Never backfill the first final
  observation or alter an earlier claim.
- A newly observed admissible final outcome appends a versioned late-resolution
  receipt to the frozen target and forecast; scoring uses its actual first
  observed final cutoff. Corrections supersede rather than overwrite. An
  outcome found after a monitoring gap is not assigned an earlier timestamp.

The 16-target cohort tests operational comparability and missingness, not
calibration. Any calibration claim still requires at least 30 independently
resolved targets, at least two categories, at least five YES and five NO
outcomes, calibrated forecasts, and a predeclared uncertainty method. Brier,
log loss and absolute error use equal target weight and the last admissible
pre-cutoff forecast per method. Report numerator and all denominators. Do not
claim edge or superiority from a partial cohort.

## Implementation and launch gate

1. Complete: merge the corrected T1–T8 implementation and repository-contained
   proof after clean-checkout verification and Windows/Ubuntu CI pass (PR #18).
2. Complete as an offline/synthetic boundary: the append-only late-resolution
   record and exclusive resumable lifecycle owner preserve
   original raw bytes. `FrozenForecastSnapshotV1` binds the four baseline
   forecasts, target receipt, capture identity, manifest hash, revision and
   configuration. `LateFinalOutcomeV1` binds the original protocol, durable
   snapshot, contiguous lifecycle/receipt chain and actual first-observed
   final retrieval time. Archive verification reloads every referenced record
   and source payload, re-normalizes Gamma finality and refuses an earlier raw
   final hidden as nonfinal. The late owner and scorer merged in PR #20 remain a
   synthetic-tested implementation, not an integrated live cohort operator.
   Each poll writes a separate append-only `LifecyclePollRetrievalV1` so
   identical Gamma bytes cannot reuse the first retrieval timestamp as proof
   of a later poll; a response timestamp before its request is rejected.
3. In progress: `AsynchronousCohortProtocolV1` and
   `scripts/m4_async_cohort_preflight.py` validate a candidate four-block,
   16-target declaration and deterministic public-Gamma selection *offline*.
   The preflight writes no evidence, queries no source and does not freeze or
   launch a campaign. Exact future UTC blocks, query, bounds, cutoff rule,
   stopping rule, missingness, metrics, input identities and clean revision
   still require an owner-approved candidate, then durable protocol/receipt
   persistence before any target selection. Candidate availability probes
   must not admit observations.
   Offline block selection now reserves enough remaining time for all four
   bounded captures, accepts only a discovery retrieval within that block,
   checks the versioned first-hand Gamma retrieval provenance against the
   frozen endpoint/query and source bytes, then derives normalization and
   eligibility flags from that page. It rejects duplicate numeric market IDs
   (including integer/text aliases
   before normalization can quarantine an entry),
   and returns a versioned partition of selected and excluded candidates with
   reasons and source hash. A page with an entry lacking an accountable market
   ID fails closed rather than recording an anonymous exclusion. Liquidity
   ranking is independent of the ambient decimal precision, and event-ID
   deduplication ignores surrounding whitespace. Later blocks derive earlier
   event identities from admitted, hash-linked prior block records, including
   their capture-time reservation. Each predecessor is replayed from its
   archived discovery bytes before admission of the next block; callers cannot
   supply an independent ID set. Prior market, event, condition, and token
   identities cannot be selected again, even if a later page changes other
   fields. Numeric event-ID aliases count as the same event. The protocol keeps
   the earliest eligible target end
   after the final observation block; admitted records recheck distinct
   canonical market IDs, explicit event identities, distinct condition and
   CLOB token identities, and source linkage on reload. Candidates without
   verifiable resolution material or observed liquidity are excluded before a
   no-replacement block is admitted. Explicit discovery IDs must be unique
   across both selected and excluded entries; ambiguous duplicate IDs fail
   closed before normalization.
   Reloading a selection checks its structure only; consumers must call
   `verify_block_selection` with the archived page and predecessor chain before
   trusting any admitted record, including the final block.
   A rejected short block admits no target or forecast.
   A candidate record can be checked with
   `uv run python scripts/m4_async_cohort_preflight.py --spec <candidate.json>`
   from a clean checkout. Its `PREFLIGHT_ONLY_NOT_FROZEN` output is not a
   persistence receipt or permission to start the first block.
4. Start the first block only after the protocol and target/contract receipts
   are durable. Announce capture completion promptly; wait asynchronously for
   finality. Do not make the campaign's completion depend on a fixed 48-hour
   settlement window.
