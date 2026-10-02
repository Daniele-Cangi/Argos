# M4 asynchronous prospective cohort: revised implementation plan

Status: ADR-0020 accepted on 2026-09-27; **no live cohort frozen or launched**.
This plan replaces the unlaunched 4x4 design for future cohorts only. Historical
evidence and V1 behavior remain unchanged. See
`docs/adr/0020-budgeted-cohorts-and-separate-research-claims.md` for rationale.

## Deliver outcomes independently

1. **Operational M4:** bounded reviewed prospective capture, immutable shared
   forecast state, reproducible replay, accountable failures and tested scoring.
2. **Preliminary evaluation:** actual final outcomes and paired descriptive
   scores, with pending/abstained/excluded denominators and limitations.
3. **Scientific claims:** a later precision-justified study; the V2 descriptive
   cohort explicitly reports calibration `NOT_ESTABLISHED`.

Slow settlement does not erase capture evidence or block operational closure.
An operational failure still constrains its own verdict. Nothing here upgrades
V8, invents finality, establishes calibration/edge or authorizes M5/trading.

## Already implemented

- T1-T8 qualification and independently verified repository proof (merged PR #18).
- Frozen snapshot and late-outcome archive boundary, exclusive resumable
  post-deadline monitor, retrieval receipts and scorer (merged through PR #20).
  These are synthetic-tested components, not a unified live V2 cohort owner.
- Offline V1 protocol/selection/archive verifier and preflight (merged PR #21).
  V1 still rejects short blocks and anonymous entries; it is not the V2 selector.
- V2 declaration and read-only preflight: variable maximum slots, stratum caps,
  time/byte budgets, snapshot timing policies, three-hour-compatible monitoring
  and separate descriptive/scientific claims. Nested schemas and preflight
  compatibility are tested. **This is not runtime enforcement.**
- Offline V2 admission: exact-page human review attestation, source-linked public
  CLOB Yes-token bid/ask spread, per-entry partition (including anonymous
  quarantine), partial/empty slot accounting, quota and predecessor replay,
  and archive re-read of raw bytes and persistence receipts. Synthetic tests
  cover a retained 3/4 block, time-limited slots, an empty then valid block,
  event-group reuse, book failure, corrupt bytes and aliased identities. The
  review record is an attestation schema, not authentication of a person; no
  live review workflow or acquisition owner is wired yet.
- First V2 admission-to-freeze slice: versioned capture-close and shared
  forecast-snapshot records bind a ranked admitted target, V2 caps, both token
  subscriptions, archived ordered frames and all four baseline methods at one
  reviewed outcome-blind information state. A no-network archive-replay test
  includes a persistence-method abstention. This proves the synthetic close and
  freeze boundary only; it is not a live capture owner, finality monitor, or
  complete capture-to-score proof.
- Synthetic bounded-ingestion slice: `run_bounded_cohort_capture_v2` drives the
  existing `run_capture`/`EventStore` path with a required raw archive, enforces
  the first duration/frame/byte cap, and tests that the next out-of-window
  frame is not normalized or stored. For a time/byte boundary it returns only
  that excluded frame's size/hash in a non-persistent run summary; this is not
  complete evidence for the excluded payload. Tests use a fake source and no
  socket. The adapter alone does not produce durable close/snapshot evidence;
  the separate owner below adds a synthetic integration path, not disk or
  crash recovery qualification.
- Persistent run-accounting sub-slice: a versioned receipt-backed outcome
  records the adapter-reported stop reason, frame/byte counts, health counters
  and excluded-boundary size/hash, tied to the durable protocol and admitted
  target; replay checks those receipts and declared time/frame/byte bounds.
  The outcome alone is still an owner-reported assertion. The journal verifier
  below independently establishes its raw-frame/EventStore link.
- Capture-to-freeze synthetic owner: a fresh exclusive per-target database and
  raw archive retain all included frame arrivals (including identical resends
  and empty arrays). A versioned terminal journal binds the outcome receipt,
  frame ordinals/provenance/processing bounds and optional four-method freeze.
  Re-normalization of archived WS bytes checks the full normalized ledger,
  source-frame offsets and health; stored replay reconstructs information hashes,
  quotes and persistence history without needing a resolution. Processing-only
  rejection timestamps are checked against their frame interval, not reproduced
  as source times. First-arrival content-addressed sidecars do not replace each
  resend's journal provenance. Empty/unseeded captures remain admitted accounting
  with no snapshot; one-sided seeded states retain four explicit abstentions.
  The excluded boundary's size/hash and source-exhaustion reason remain owner
  assertions, not claims of independently archived boundary bytes.
  Acquisition closes before finalization. Snapshot time is the real finalization
  time, and a post-write clock check recorded as `finalized_at` must remain before
  the reviewed knowable-time margin (ADR-0020 section 4). This does not produce or
  reinterpret the earlier `CohortCaptureCloseV1` synthetic proof record, which
  requires a durable receipt before close. The new journal is terminal evidence,
  not a crash-resume checkpoint, and does not enforce total storage budgets or
  qualify a live source/campaign.

## Remaining vertical slice: capture, outcome and synthetic end-to-end proof

Implement the remaining path in this order, with versioned records and
archive-derived checks. Steps 1-3 below have an offline synthetic proof, not a
live runner or a full capture-to-score proof:

1. Semantic-review receipt bound to contract/version, human reviewer, outcome
   conditions/authority, canonical category, real-world event group and earliest
   outcome-knowable time. Compiler `UNREVIEWED` is not human approval.
2. Source-page partition with page hash/index/entry hash for anonymous quarantine;
   exact provenance/query verification, deterministic stratum/ranking replay,
   duplicate identity protection and verified predecessor accounting.
3. Partial block admission: retain 3 eligible targets when the cap is 4, record
   one empty slot, and allow a later block. An empty/failed predecessor must have
   evidence; a missing/corrupt predecessor must stop admission. No slot/quota
   transfer, post-admission replacement or recapture to select a better score.
4. The bounded acquisition-to-freeze owner and independent raw/store/baseline
   replay now have a synthetic proof. Complete durable failure/crash accounting,
   restart without recapture, total DB/WAL/log/artifact disk bounds and real
   adapter qualification before live use. A target can end before the *last*
   cohort block when its own timing is valid.
5. Unified early/late finality join and exclusive resumable owner, retaining raw
   hashes, ordinal/receipt chains, observed gaps and actual retrieval time.
   Do not force V2 through `LateFinalOutcomeV1`'s old post-deadline condition.
6. Separate operational accounting, outcome progress, per-method coverage and
   paired Brier/log-loss reports. Absolute error remains diagnostic; report
   raw-score calibration descriptively without a calibration pass flag.

Acceptance fixtures must cover: 3/4 targets retained; empty then valid block;
anonymous malformed entry alongside valid entries; aliased/correlated targets;
ambiguous/unreviewed contracts; known outcome before platform finality; one
abstaining method; immediate and delayed finality; indefinitely pending/disputed
target; exact time/frame/byte cap; low disk; crash/resume without duplicate
owner; corrupt predecessor; changed source bytes; superseding corrections.

Use small recorded/synthetic fixtures in CI; tests must not wait for external
settlement. The declared policy must be independently replayable from receipts,
not merely a producer-written `PASSED` or manually supplied counts.

## Bounded live pilot after the vertical proof

Use **2-4 target slots** across simple, reviewed event families and a small
number of explicit liquidity/spread/horizon strata. This is an integration
budget, not a scientific sample threshold. Freeze exact windows, query,
population, quotas, deterministic selection, clean revision and configuration.

- Windows and quotas are maximum acquisition opportunities, not a requirement
  to fill every slot. No requirement that all events outlive the final block.
- Separate target databases/raw archives; both tokens; first of 120 seconds,
  500 frames or declared bytes stops capture. Reserve finalization and evidence
  space; actual byte caps and free-space checks must be enforced by the runner.
- Evaluate the last shared information state before capture close, frozen before
  the reviewed earliest outcome time minus margin. Never cherry-pick a later
  forecast near known settlement.
- Keep planned slots, admitted targets, intact snapshots, finals, scored targets
  and per-method abstentions separate. Retain pending targets without scores.
- Use one lifecycle owner, normally one scheduled request per target every
  180 minutes. Declare tolerated scheduling gap and finite follow-up endpoint;
  record failures/gaps without fabricating observations or backdating finality.
- Report capture/accounting promptly. Operational review does not wait for all
  final outcomes. Continued follow-up requires an append-only declared schedule.

The owner reviews the concrete candidate and semantic receipts before launch.
Check a candidate from a clean checkout with:

```text
uv run python scripts/m4_async_cohort_preflight.py --spec <candidate.json>
```

For V2, `PREFLIGHT_ONLY_NOT_FROZEN`, `live_launch_supported: false` and
`disk_space_checked: false` explicitly identify the current boundary. The byte
requirement is a calculation, not a filesystem measurement. A passing declaration
cannot bypass the synthetic proof, runtime enforcement or durable freeze.

## Expand only to answer a named question

After pilot review, accumulate independent event groups under a new frozen
target/time/resource budget, preserving partial results and disclosed sampling
limitations. Do not keep rerunning a two-day or six-hour experiment by default.
Requalify affected runtime properties when code changes; reuse unchanged
technical qualification as qualification, never as new prospective evidence.

For a later scientific study, choose the estimand, required precision, sampling
units, missingness analysis and uncertainty/stopping method first. A universal
30-target threshold and five YES/five NO quotas are not proof of adequacy.
