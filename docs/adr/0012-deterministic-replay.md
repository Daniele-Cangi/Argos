# ADR-0012: What a deterministic replay is, and what it refuses to do

- Status: Accepted
- Date: 2026-08-17
- Extends ADR-0003 (deterministic arrival-order replay), ADR-0009 (pacing is
  separate from timekeeping) and ADR-0011 (the event store). Supersedes nothing.

## Context

ADR-0003 decided *that* replay reproduces original arrival order and *that*
event time is used under an explicit watermark policy. It did not decide what
happens to a duplicate arrival, what a watermark actually does to an event that
arrives behind it, what the "output state hash" covers, what the clock is
advanced to, or whether a replay may span more than one capture run. Each of
those has a defensible answer that produces a *different* hash from the other
defensible answers, so leaving them implicit would mean the M3 exit criterion
"identical input + code + config produces identical output hash" was satisfied
by whatever the first implementation happened to do.

The M3 readiness audit also established two facts this decision rests on. The
recorded live capture driven through the real capture path produces
`ingest_sequence` **contiguous 1..42 across the delivery and rejection ledgers
combined**, with exactly one record per value, enforced inside the insert's own
transaction. And `iter_deliveries` returns one row per *arrival*, duplicates
included.

## Decision

### 1. A replay is scoped to exactly one capture run

`ingest_sequence` is unique and contiguous within a run and means nothing
between runs. A replay spanning two runs would need a total order somebody
invents — by `started_at`, which two concurrent captures can share or
interleave; by run id, which is alphabetical rather than temporal — and
ADR-0003's "reproducibility requires stable tie-breaking" is precisely a warning
against inventing one.

So `replay_capture` takes one `capture_run_id` and refuses anything else. A
multi-run replay is a later slice that must arrive with evidence about what
those runs actually are, not a generalization taken on faith now.

### 2. Arrivals are replayed by merging both ledgers on `ingest_sequence`

Deliveries and rejections draw from one counter, and the store already refuses a
reused value across both tables. Merging them by sequence therefore reconstructs
the original arrival order exactly, and it needs no new query on the `EventStore`
port — which keeps ADR-0011's "SQL stays inside `argos.store`" intact rather
than widening the port for one consumer.

A rejection is replayed as an *arrival* — counted, its reason tallied — and
never applied to state. It carries no payload by construction; a rejection
ledger whose records could reach a projection would defeat its own purpose.

### 3. A duplicate delivery is counted, never re-applied

This is the decision most likely to be got wrong by accident, because
"re-applying is harmless" is nearly true and not true.

A duplicate delivery names an `observation_id` already applied. Re-applying a
`price_change` group is **not** idempotent in general: a second `REMOVE` of a
level names a price the projection no longer holds, and `OrderBookProjection`
correctly records `REMOVE_OF_ABSENT_LEVEL` — an anomaly that exists to signal a
missed delta or a stale seed. Replaying duplicates would manufacture that signal
out of the deduplication mechanism itself, and the resulting anomaly count would
depend on how many times the *source* happened to resend, which is the opposite
of deterministic.

The arrival is still real and is still counted. What is refused is letting it
move state twice.

### 4. The watermark marks late events; it never reorders, buffers, or drops them

ADR-0003 is explicit: replay original arrival order, and "reordering late data
into the past is forbidden unless running a separately labeled corrected-history
experiment". A watermark that held events back to release them in event-time
order would be exactly that reordering, done silently.

So the watermark here is **observational**. It tracks the maximum `event_time`
of every dispatched observation, minus a configured `allowed_lateness`. An
observation whose own `event_time` is strictly earlier than the current
watermark is **late**: it is applied anyway, in arrival order, and counted as
late. Nothing else changes.

`allowed_lateness` defaults to **zero**, and that default is evidence-driven
rather than cautious. Neither live capture nor the recorded capture contains a
single out-of-order arrival — measured: zero `event_time` regressions across all
42 arrivals — so a zero tolerance flags nothing that has ever been observed,
while flagging any genuine regression the first time it happens. A non-zero
tolerance would be a guess about a phenomenon this project has never seen. It is
configurable because the guess becomes informed the moment one is seen, and it
is recorded in the replay manifest because it changes the counts.

Equal event times are **not** late. The WebSocket research observed one logical
book transition spanning several frames with an identical `(timestamp, hash)`,
so treating equality as disorder would flag normal traffic — the same reasoning
`OrderBookProjection._note_event_time_regression` already applies.

An observation with no usable event time (`MISSING` or `UNPARSEABLE`) cannot be
judged late or on time. It is counted as **undatable** rather than silently
treated as either, because "the source sent no timestamp" and "the source sent
one and it was late" are different facts about the source.

### 5. The replay clock is advanced to `received_time`, and never backwards

The question a replay answers is "what did ARGOS know, and when did it know
it". That is `received_time` — when the observation reached ARGOS — not
`event_time`, which is when the source says the thing happened. Advancing to
`event_time` would hand a replayed forecast information at the moment the market
moved rather than at the moment ARGOS learned of it, which is the leakage
ADR-0003 exists to prevent.

`ReplayClock.advance_to` raises on a backwards move, and a stored
`received_time` **can** regress: it comes from `LiveClock.now()`, and a system
clock can step backwards under NTP. Killing a replay of an already-recorded
capture on that basis is not a policy, it is a crash. The scheduler therefore
advances only when the target is later, leaves the clock where it is otherwise,
and counts a `received_time_regression`. Core invariant 14: counted and
explained, never silently discarded and never fatal.

### 6. The output state hash covers projected state and nothing else

`state_hash.v1` is a length-prefixed digest over every projection's own
`digest()`, in `(condition_id, asset_id)` order.

It deliberately excludes: run ids, timestamps, wall-clock duration, the replay
mode, `ingest_sequence` values, anomaly history, and counts. Two replays of one
capture in three different modes must produce one hash, and a hash containing
any of those would instead prove they were three different runs — which they
were, and which is not the property under test.

The counts are not thereby lost: `ReplayManifestV1.output_record_counts` carries
them beside the hash, and the determinism criterion compares both. A hash that
absorbed the counts would make one number answer two questions, and a difference
in it would no longer say *which* had changed.

The digest is versioned inside the hashed material, like
`BOOK_STATE_DIGEST_VERSION` already is, so changing the encoding changes every
pinned golden value visibly rather than letting two ARGOS revisions quietly
agree.

### 7. Scheduler pacing is a mode, and cannot reach the hash

ADR-0009 already requires this: "Scheduler pacing for M3's accelerated and
stepwise modes ... must never influence the output hash." Three modes exist —
`accelerated` (no waiting), `original_arrival` (the real inter-arrival gaps),
and `stepwise` (the caller drives one arrival at a time) — and all three run the
identical `step()`. Pacing is a `ReplayPacer`, separate from `Clock` for the
reason ADR-0009 separated them, and the default `VirtualPacer` records the waits
it would have made instead of making them, so every test is both fast and honest
about what it skipped.

`docs/04_DATA_CONTRACTS.md` lists `replay_mode` as `original_arrival |
accelerated_arrival`. `stepwise` is added because `docs/07_MILESTONES.md` names
it a deliverable in the same breath as accelerated, and the contract document is
amended rather than the deliverable quietly dropped.

### 8. Live and replay share one dispatcher, and the sharing is exercised

Core invariant 5 has been true so far only because there was one path: the
capture loop wrote to the store and stopped, and every projection this
repository has driven was driven by a test. `ObservationDispatcher` lives in
`argos.projections`, takes `ObservationEnvelopeV1`, and is called by *both*
`run_capture` (optionally, live) and the replay scheduler.

It is a shared object rather than two implementations of one protocol on
purpose. A protocol would let the two drift and be satisfied by a test that
never compared them; one object cannot drift from itself. The property is then
testable directly, and is tested directly: the same recorded frames driven live
through `run_capture` and re-read through `replay_capture` produce the same
state hash.

## Rejected alternatives

- **Replaying duplicates for fidelity.** "Replay exactly what arrived" is the
  right instinct and the wrong conclusion: the anomaly counts would become a
  function of source resend behaviour, and the M3 criterion is about *ARGOS*
  being deterministic given fixed input, not about re-enacting the network.
- **A buffering watermark that emits in event-time order.** Forbidden by
  ADR-0003, and it would hand historical models information earlier than ARGOS
  actually had it — the one failure that ADR exists to prevent.
- **Hashing the counts into the state hash.** Cheaper to compare, and it makes
  one difference indistinguishable from another. Rejected for the same reason
  ADR-0011 derives aggregate counters rather than storing them: a summary must
  not be able to disagree with what it summarizes, and it must also not be able
  to hide *which* thing disagreed.
- **Advancing the clock to `event_time`.** Reads as "replay the market", and is
  leakage. Recorded here because it is the more intuitive of the two and would
  not look wrong in review.

## Consequences

- A replay is reproducible from `(database, capture_run_id, code revision,
  config fingerprint, allowed_lateness)`. Mode is deliberately absent from that
  list, and that absence is a tested property rather than a claim.
- `ObservationDispatcher` is the first thing in this repository that both a live
  path and a replay path call, so core invariant 5 stops being an aspiration
  about handlers and becomes a fact about one object.
- A payload kind with no wired handler is a counted, explicit `unhandled`
  outcome — not a skip. When `last_trade_price` gains a model at M4, the count
  is what will show it arriving.
- `allowed_lateness` and the digest version both enter the replay manifest.
  Changing either changes recorded output, which is the point.
