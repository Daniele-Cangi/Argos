# ADR-0019: Decouple bounded technical qualification from asynchronous resolution

- Status: Accepted
- Date: 2026-09-20
- Supersedes ADR-0015 only where an end-to-end measurement verdict required every
  technical test target to resolve inside one continuously monitored lifecycle
  window.
- Extends ADR-0017 and ADR-0018 without weakening immutable evidence, cutoff or
  calibration requirements.

## Context

The V3, V5 and V8 prospective experiments demonstrated that a long lifecycle
window couples two different questions:

1. can ARGOS capture, persist, replay and account for market evidence correctly;
2. has an external market operator published a final settlement yet?

V8 completed two clean bounded captures and persisted 554 contiguous,
receipt-bound lifecycle observations per target over almost 47 hours. Both
targets remained `proposed`. The external monitor then stopped at
`2026-09-19T21:00:23Z`, after its last successful polls near `20:55Z` and before
the frozen `22:00Z` deadline. The missing tail correctly prevents V8 from
claiming continuous observation or an admissible cutoff through the deadline.
It does not erase the independently verifiable capture and lifecycle evidence
that preceded the failure.

Repeating multi-day all-or-nothing runs is a poor way to test deterministic
capture and recovery. It spends most wall-clock time waiting on an external
settlement and makes one late operational fault dominate otherwise distinct
technical results.

## Decision

### 1. Publish technical qualification and outcome evaluation separately

A future campaign has two independent tracks:

- **bounded technical qualification** tests capture, persistence, replay,
  accounting and recovery under predeclared short scenarios;
- **asynchronous outcome evaluation** appends a final outcome only when an
  admissible source observation becomes available.

A technical result never becomes a predictive result merely because it passed.
An unresolved target never becomes a technical failure merely because the
source has not settled it.

### 2. Use a bounded scenario matrix instead of one 48-hour soak

The minimum technical campaign contains independently reported scenarios:

| Scenario | Nominal bound | Required claim |
|---|---:|---|
| Functional capture | 10 minutes | separate target storage, raw archive, clean accounting and deterministic replay |
| Stability | 2 hours | bounded resource growth, intact receipts and no unexplained event loss |
| Endurance | 6 hours | sustained cadence and restartable checkpoints |
| Network interruption | 60 minutes | bounded failure, explicit gap and idempotent resume |
| Process interruption | 60 minutes | exclusive ownership, crash detection and resume without duplicate evidence |
| Storage interruption | 60 minutes | no partial claim publication and explicit durable failure |
| Terminal-state simulation | 60 minutes | `unknown`, `proposed`, `disputed`, `final` and administrative close paths |

Durations are upper bounds, not evidence that elapsed time itself validates a
property. A scenario may stop earlier when its predeclared bound or falsification
condition is reached. A longer soak is optional and must answer a named risk.

### 3. Make every scenario independently accountable

Each scenario records its configuration, revision, start/end, input identities,
fault injection (if any), expected outcome, observed outcome, artifacts and
verdict. The campaign reports passed, failed, incomplete and not-run scenarios
separately. No aggregate boolean may hide a failed scenario or upgrade an
incomplete one.

Failures do not invalidate artifacts that were already durably and correctly
recorded. They do constrain the claims those artifacts support.

### 4. Resolution is append-only and may arrive later

Forecast or baseline evidence remains fixed at its original availability time.
Outcome labels may be attached later through a new versioned record carrying
the immutable source payload, source time, retrieval time, finality and receipt.
Late outcome retrieval cannot introduce a forecast, move its timestamp or
manufacture a historical first-observed cutoff.

Targets without admissible finality remain `PENDING_RESOLUTION`. They are
reported in the intended-target denominator and contribute no score until a
valid outcome exists. Administrative review dates report pending work; they do
not destructively exclude a target solely because settlement is slow.

### 5. Resume is allowed only with explicit continuity evidence

A future monitor may resume after failure when it:

- obtains exclusive ownership before issuing a source request;
- validates the frozen configuration and append-only chain;
- continues from the last persisted ordinal without rewriting evidence;
- records downtime as an explicit gap; and
- never backfills a missing first-observed-final timestamp.

Recovery can preserve capture and later outcome evaluation. It cannot turn an
unobserved interval into continuous lifecycle evidence.

### 6. Calibration requirements remain unchanged

This decision does not lower ADR-0014's scientific threshold. Calibration still
requires at least 30 independently resolved targets, the predeclared category
and outcome dispersion, calibrated forecasts and a frozen uncertainty method.
Short technical scenarios validate machinery, not predictive quality.

## Consequences

- V8 remains an incomplete frozen experiment: its clean evidence is retained,
  its missing tail is disclosed and it is not reinterpreted as an M4 pass.
- The next technical campaign can finish in hours and expose failures by
  scenario instead of waiting days for settlement.
- Real targets can accumulate asynchronously; slow settlement reduces the
  currently scorable sample rather than erasing technical evidence.
- A resumable monitor and versioned late-resolution record are required before
  the next real predictive campaign.
- M4 remains blocked until the new boundaries are implemented and exercised.

## Rejected alternatives

- **Retroactively shorten V8's deadline.** This chooses the rule after seeing
  the failure and would manufacture continuity.
- **Treat the last proposed observation as final.** Proposed is not settled and
  cannot supply a winning outcome.
- **Ignore monitoring gaps below an arbitrary tolerance.** A tolerance can
  classify operational quality but cannot prove what the source said while it
  was unobserved.
- **Keep repeating 48-hour two-target runs.** This confounds source settlement
  latency with technical qualification and has an unacceptable all-or-nothing
  failure surface.
