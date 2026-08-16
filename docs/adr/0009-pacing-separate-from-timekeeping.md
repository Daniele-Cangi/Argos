# ADR-0009: Pacing is separate from timekeeping

- Status: Accepted
- Date: 2026-08-07
- Refines: ADR-0003 (does not supersede it)

## Context

`Clock` exposed both `now()` and `sleep()`. `LiveClock.sleep` awaited
`anyio.sleep`; `ReplayClock.sleep` advanced virtual time and never awaited.
`GammaClient` slept its retry backoff on the injected clock, and only a
docstring forbade handing an adapter the replay scheduler's clock.

`docs/02_ARCHITECTURE.md` already lists **pacing policy** as an axis distinct
from **clock implementation**. The code had merged the two. This ADR restores
the separation the architecture document already specified; it does not
introduce a new concept.

Four defects follow from the merge. Each was reproduced before this decision.

1. **A backoff sleep has no meaning in replay.** A retry is an interaction with
   a live network peer. Its outcome is already in the capture: successful
   responses are archived payloads, failed attempts are health counters and
   rejection-ledger rows, and the elapsed real time is baked into `received_time`
   deltas. A replay run issues no request, so there is nothing to retry.
   Re-executing retries in replay would invent events that were never captured
   or double-count ones that were. `sleep` is therefore not a capability that
   *differs* between live and replay — it is one that exists **only in live
   mode**. Placing it on the protocol whose entire purpose is to differ between
   the two was a category error.

2. **Order-dependent hidden global state, today, in committed M1 code.**
   `wait_exponential_jitter.__call__` draws from module-level `random.uniform`
   with no injection hook (verified in tenacity 9.x `wait.py`). In a capture
   loop over N markets, the backoff one market receives depends on how many
   sibling markets retried before it. This violates the CLAUDE.md prohibition on
   "hidden global state that changes output based on asset processing order" and
   ADR-0003's stable-tie-breaking clause, independently of replay.

3. **`ReplayClock.sleep` is not an async checkpoint.** It contains no `await`,
   so control never returns to the event loop and a cancellation aimed at that
   task cannot be delivered. Two implementations of one protocol with different
   concurrency semantics strains core invariant 5. This bites in M2, not M3: the
   WebSocket reconnect loop must be cancellable *during* backoff.

4. **The injected clock was already bypassed.** `GammaClient._get` bounds the
   whole request with `anyio.move_on_after`, which reads the event-loop
   monotonic clock. Reproduced: five virtual hours of `ReplayClock.sleep` inside
   a 0.05 s deadline scope, `cancelled_caught = False`. Consequently the M1
   security fix "one overall deadline bounds the request, retries included" is
   tested only on the zero-retry path — `tests/test_gamma_client.py` pins
   `http_max_attempts=1` and its docstring concedes a virtual clock cannot
   demonstrate the property. The needed port is not "sleep"; it is **real
   elapsed time**, of which waiting and deadlines are two operations.

## Decision

Split the two concerns into two ports.

- `Clock` keeps `now()` only. It is a pure reader with no mutator, and
  `@runtime_checkable` is dropped: it advertised a structural check that cannot
  distinguish `LiveClock` from `ReplayClock`, which is worse than no check.
- `ReplayClock` keeps `now`, `advance_to`, and `advance_by`. It loses `sleep`.
- A new `Pacer` port owns all real elapsed time: `wait(seconds)` and
  `move_on_after(seconds)`. `RealPacer` delegates to `anyio`.
- Adapters receive a `Pacer` and their own `random.Random(seed)`. The seed is
  configuration and is recorded in the run manifest. `random.seed()` is not
  used — it would swap one hidden global for another.
- Because tenacity offers no RNG injection point, jitter is computed by a small
  `wait_base` subclass drawing from the adapter-owned generator.

Retry pacing is live-only and lives in `argos.sources`. Scheduler pacing for
M3's accelerated and stepwise modes is a different concern, will live in
`argos.replay`, and must never influence the output hash.

## Rejected alternatives

- **One protocol, seeded jitter, plus an enforced prohibition.** The rule it
  must enforce is "do not pass *this instance* to *that* constructor" — a
  value-flow property no AST scan or type annotation can express, since
  `ReplayClock` structurally satisfies `Clock` by design. Any grep-based guard
  is defeated by a factory function. It also leaves defects 3 and 4 untouched.
  Recorded here as investigated and rejected as unenforceable, so the question
  does not reopen in M3.
- **Make `ReplayClock.sleep` raise.** Converts a design error into a runtime
  landmine discovered at capture time, and every free-backoff test must move to
  a fake regardless. Strictly worse at nearly the same cost.

## Consequences

- "An adapter moves replay time" becomes impossible rather than forbidden:
  `Clock` has no mutating method. The M3 deliverable "`ReplayClock` controlled
  only by the replay scheduler" becomes a type-level fact.
- The deadline-during-backoff path becomes testable for the first time, via a
  recording pacer whose deadline budget is consumed by recorded waits.
- Adapters gain one constructor parameter. M2's CLOB REST and WebSocket
  adapters take the same shape. A shared bounded-retry helper is justified once
  the second client exists, not speculatively before.
- tenacity's `RetryCallState` still reads `time.monotonic()` internally. This is
  accepted: adapters are outside the "no wall clock inside deterministic domain
  logic" scope, which governs domain logic.
- Boundary tests guard the regressions types cannot: `Clock` exposes only `now`;
  `ReplayClock` has no `sleep`/`wait`; no domain/projection/baseline/evaluation
  module imports a pacing name; no module under `sources`/`ingestion` calls
  `anyio.sleep` or `anyio.move_on_after` directly. Without the last one, the
  next adapter re-creates the bypass in defect 4 and nothing notices.
