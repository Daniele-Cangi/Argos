# M4 asynchronous prospective cohort: design gate

Status: design in progress, **no live cohort frozen or launched**. The owner's
2026-09-26 decision authorizes beginning this phase; it does not turn T1–T8
into a predictive or calibration result. A live start still requires the
reproducible proof publication, append-only late-resolution boundary, clean
code revision, exact UTC windows, and a persisted protocol before first target
selection or capture.

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

1. Merge the corrected T1–T8 implementation and repository-contained proof
   only after clean-checkout verification and CI pass.
2. Implement and adversarially verify the append-only late-resolution record
   and an exclusive resumable lifecycle owner; preserve original raw bytes.
3. Freeze the exact UTC blocks, query, bounds, cutoff rule, stopping rule,
   missingness, metrics, input identities and code revision in a new protocol.
   Test candidate availability without admitting probe observations.
4. Start the first block only after the protocol and target/contract receipts
   are durable. Announce capture completion promptly; wait asynchronously for
   finality. Do not make the campaign's completion depend on a fixed 48-hour
   settlement window.
