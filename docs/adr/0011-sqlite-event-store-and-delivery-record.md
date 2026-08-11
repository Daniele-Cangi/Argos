# ADR-0011: The SQLite/WAL event store and the delivery record

- Status: Accepted
- Date: 2026-08-11
- Extends ADR-0004 (immutable source-linked event storage) and ADR-0010
  (observation identity). Supersedes neither.

## Context

Two decisions were deliberately deferred to this slice, and they turn out to be
one decision.

`docs/12_TECH_STACK.md` requires a comparison of a minimal SQLite/WAL store
against an append-log/Parquet path "using actual capture-volume measurements",
recorded in an ADR. `docs/DECISION_LOG.md` (2026-08-06) records the deferral
from M0 to M2 as deliberate: real capture volumes did not exist yet.

ADR-0010 defined `observation_id` and then stopped, explicitly: "The store must
still decide what a *delivery* record is... **The store slice must decide this
before it writes a row**." Identity excludes `capture_run_id`,
`ingest_sequence`, and `received_time`, because including any of them would make
a duplicate unable to collide and the M2 exit criterion unreachable. The
consequence is that a collapsed duplicate has nowhere to record its own arrival,
and `docs/02_ARCHITECTURE.md` requires duplicate inserts to be idempotent **and
observable**.

These are one decision because the delivery record is the reason the engine
choice is decidable at all: the properties that separate the candidates are
properties of writing an observation and its arrival together, not properties of
throughput.

## Decision

### 1. Volume was measured, and volume does not decide it

Measured from artifacts already in this repository — the 36.1 s live WebSocket
capture (`docs/research/fixtures/clob-ws-market-2026-08-10T184742Z.json`: 4
`book` events, 34 `price_change` frames, 68 `price_changes` entries,
independently recounted for this ADR) and a real `ObservationEnvelopeV1` built
from the recorded REST book fixture:

| Quantity | Measured |
|---|---|
| Accepted observations, subscribed token | 1.05 / second |
| Frame bytes | mean 877 B |
| Full envelope record, `to_record()` compact JSON | 4,362 B (payload 3,088) |
| **Envelope overhead independent of payload** | **1,274 B** |
| Embedded `SourceProvenanceV1` per record | 345 B |

The envelope's fixed overhead is roughly **6x its own payload** for a
`price_change` delta (~220 B), and ADR-0010's deliberate choice to embed
provenance per observation costs ~31 MB/day/token of duplicated provenance at
the measured rate. That choice stands — self-contained audit records survive
compaction — but the number is recorded here rather than left as a shrug.

**Extrapolated, and weakly**: ~91k observations/day/token, ~127 MB/day/token of
records; a 10-market set is single-digit GB/day.

**The weakness of this evidence is part of the decision.** n = 1 token, 36
seconds, one connection, no reconnect, and the token was selected as the
highest-24h-volume active market during a live tennis match — an upper-tail
market in its most active window. No quiet-market sample exists in the
repository at all. REST volume is not measured and is not yet measurable: no
polling cadence has been chosen, and unchanged re-polls collapse onto one
`observation_id` (`docs/research/m2-clob-rest-book.md`), so REST volume is
bounded by book *change* rate rather than poll rate.

`docs/12_TECH_STACK.md` asked for capture-volume measurements before choosing.
They now exist, and their honest verdict is that **both candidates clear the
requirement by roughly four orders of magnitude**. Measured on this machine, a
per-event transaction at `synchronous=FULL` with no batching sustains ~11.9k
events/s against a requirement of ~1-2/s. Volume is therefore not the
discriminating axis, and this ADR does not pretend it was. The correctness and
durability axes decided it.

### 2. The engine is SQLite in WAL mode

Two properties decide it, and both are about atomicity rather than speed.

**Idempotent insert keyed on `observation_id` must be enforced by the store, not
by the writer's memory.** An append-log has no unique key; it needs an in-process
index rebuilt by scanning the whole log at open. That index is exactly the
"hidden global state that changes output based on asset processing order" that
CLAUDE.md prohibits and that ADR-0009 already had to excise once from the retry
jitter. It also makes uniqueness a property of one process's memory: a crash
mid-run, or a second reader, silently breaks it.

**"Idempotent *and* observable" is a two-write operation that must be atomic.**
Detecting the duplicate and recording its arrival must both land or neither. In
SQLite that is one transaction. In an append-log it is two appends with a crash
window between them, and closing that window means writing a journal — that is,
re-implementing SQLite, worse.

Supporting reasons: WAL recovers to the last committed transaction after an
interrupted capture, which makes "a run row with `ended_at IS NULL`" a queryable,
unambiguous incomplete-capture signal and satisfies the M2 exit criterion
directly; `ORDER BY` on an indexed key gives M3 a replay order independent of
file layout, where a JSONL reader depends on file order and must handle a torn
final line; and `sqlite3` is in the standard library, so no dependency-addition
process and no lock-file change.

**Parquet is rejected for M2 outright**, not deferred as a close second: it
requires a large new dependency, it is columnar and therefore wrong for
row-at-a-time append and sequential replay, and it enforces no unique key at
all. It may be reconsidered as an M4 *export* format if evaluation needs
columnar scans. It is not a candidate for the store.

**The observation table is a rowid table with a `UNIQUE` index on
`observation_id`, explicitly not `WITHOUT ROWID`.** The instinct is wrong here
and the cost was measured, then independently reproduced before this ADR was
written: a >1 KB record spills to overflow pages inside an index B-tree, costing
**4,681 B/row against 1,456 B/row**, a 3.2x storage penalty for no benefit.

### 3. The store stays opaque to payload types

Rows carry the canonical JSON of `to_record()` in a single `record` column, plus
a small set of columns duplicated out of it for keys, filtering, and migration
(`schema_version`, `payload_schema_version`, source, scope ids, event time, raw
hash). There is no column per contract field and no table per event type.

ADR-0010 paid for type erasure at the **write** boundary so that neither the
store nor the M3 replay reader has to become generic. A store that knows payload
types re-imports the cost that decision spent, and schema evolution then lives in
two places instead of in the pydantic contract where `schema_version` already is.

### 4. The raw archive survives; it is not absorbed

`docs/BACKLOG.md` asked whether the minimal raw archive survives the event store.
It does. Raw bytes stay in the content-addressed file archive — a multi-MiB REST
body has no business in a row — and the store references them through
`raw_payload_sha256` / `raw_payload_location`, which the envelope already
carries. This keeps rows at the measured ~1.4 KB.

Its missing `fsync` before rename (already filed in `docs/BACKLOG.md` as
something "the M2 event store cannot inherit") is closed in this slice, not
carried: an observation whose raw payload is unreadable after a crash cannot
satisfy core invariant 7.

### 5. The delivery record is one row per arrival

This is the shape ADR-0010 left open.

- **`observation`** — one immutable row per distinct `observation_id`. It carries
  `first_seen_capture_run_id`, `first_seen_ingest_sequence`, and
  `first_seen_received_time` as named columns. These are not redundant with the
  stored record: because identity excludes those three fields while the envelope
  still carries them, the stored `record` *necessarily* pins the first arrival's
  values. Naming that explicitly stops a reader believing those fields describe
  "the" arrival, and lets the store hand M3 back a byte-identical envelope.
- **`delivery`** — one row per arrival, keyed `(capture_run_id,
  ingest_sequence)`, carrying `observation_id`, `received_time`, and a
  `disposition` of `accepted_new` or `duplicate`. The composite key mechanically
  enforces `docs/04_DATA_CONTRACTS.md`'s "`ingest_sequence` is positive and
  unique per capture run", which nothing enforces today —
  `ObservationEnvelopeV1.ingest_sequence` has only `gt=0`.
- **`rejection`** — one row per rejected arrival, drawn from the *same*
  `ingest_sequence` counter, plus a nullable `duplicate_of_observation_id`.
- **`capture_run`** — the manifest row, with `ended_at NULL` meaning "not closed".

**One row per arrival, not a counter.** A counter discards `received_time` and
`ingest_sequence` for every arrival after the first — precisely what core
invariant 6 and ADR-0003 require to be preserved separately — and cannot answer
"was this duplicate a WebSocket resend 200 microseconds later, or a re-poll forty
minutes later", which section 6 makes an operational question rather than a
curiosity. The cost is ~200 B against a ~1.4 KB observation row.

**`ingest_sequence` is authoritative on the delivery record**, and on the
observation only as `first_seen_ingest_sequence`. It cannot be removed from
`ObservationEnvelopeV1` — `docs/04_DATA_CONTRACTS.md` specifies it there and M3
replays by it — so the envelope's copy is documented as *the sequence at which
this observation first arrived*. Anything vaguer makes one field mean two
different things depending on whether a duplicate happened, which is the kind of
ambiguity that surfaces during an M3 replay against a capture that cannot be
re-taken.

**Aggregate counters are derived, never incremented.**
`CaptureManifestV1.duplicate_count` is a `count(*)` over `delivery`, so a
summary cannot silently disagree with the rows it summarizes (core invariant 14).

`duplicate_of_observation_id` lives on the storage row, not on
`RejectedObservationV1`. The link is a storage relation; putting it on the
contract would drag a store concern into `argos.domain` and change how
`rejection_id` is derived.

### 6. The residual identity collision is made countable, not solved

`docs/research/m2-clob-websocket.md` established that `(timestamp, hash)` names a
*post-state*, not a wire message: six `price_changes` entries shared one hash in
one message, and two frames 196 microseconds apart carried an identical pair.
ADR-0010's residual — genuinely different messages sharing every stable field —
is therefore reachable in practice on this source.

Re-measured for this decision: the capture holds 58 distinct `(timestamp, hash)`
pairs but **68 distinct `(timestamp, full-entry-content)` keys and zero exact
content duplicates**, so the shipped identity separates every recorded entry.
The residual is reachable in principle and **has not been observed**.

The store does not try to solve it in identity. Any discriminator the store could
invent — arrival index, receipt microsecond — re-imports `received_time` or
`ingest_sequence` into identity and breaks duplicate collapse, the failure
ADR-0010 rejected twice.

Instead the delivery row carries **`source_frame_sha256` and
`source_frame_offset`**, which make three cases distinguishable from stored data
alone, without re-taking a capture:

- same frame hash, different offsets — one frame carried the same entry twice:
  the irreducible case, now counted;
- different frame hashes, same `observation_id` — a genuine resend, or the
  two-frame case; the delivery rows' `received_time` delta separates them;
- same frame hash, same offset — a reprocessing bug, not a source event.

This is the difference between "we may be collapsing real events" and "we can
count how often we did", which is what core invariant 14 asks for.

### 7. `rejection_id` is a grouping key, not a unique key

`_rejection_identity` derives from reason, source, event type, the scope ids, and
`raw_payload_sha256`. Verified directly against
`src/argos/domain/observation.py`: there is **no within-payload discriminator**.
Since one frame can carry several entries for the same token, two *different*
malformed entries in one frame, refused for the same reason against the same
archived frame hash, collapse onto one `rejection_id` — one ledger identity where
two rejections occurred. That is a silent loss inside the mechanism built to
prevent silent loss, and it sits directly under the M2 exit criterion "invalid
messages enter a rejection ledger with reason and raw hash".

Decided explicitly rather than left to be discovered during a capture: the
`rejection` table keys on `(capture_run_id, ingest_sequence)`, so **both rows are
preserved**, and `rejection_id` is documented as a non-unique grouping key. No
domain contract changes, and the design stays symmetric with `delivery`. The
alternative — adding a frame-offset component to the identity — is rejected for
now because it would make the ledger identity depend on framing, which is a
transport property the rejection may exist precisely because ARGOS could not
parse.

### 8. Identity is verified, not trusted, on write and on read

`recompute_observation_id` is called on both paths. `docs/BACKLOG.md` already
nominates this slice ("nothing obliges a reader to call them. The store slice is
the right place to make verification mandatory on read"). It is cheap and it
converts the primary key from trusted to checked.

## Consequences

- Append-only is enforced mechanically, not by convention: no `UPDATE`,
  `DELETE`, or `ALTER` literal may appear in `argos.store`, checked the same way
  `tests/test_boundaries.py` already checks for direct pacing calls.
- **No wall clock inside persistence.** No `CURRENT_TIMESTAMP` default in any
  DDL; every timestamp arrives from the injected `Clock` via the caller. A SQL
  string default would be invisible to the existing boundary tests, which scan
  Python AST rather than SQL, and would break replay reproducibility.
- The connection is injected like every other adapter. A module-level or
  import-cached connection would reproduce the ADR-0009 hidden-global-state
  defect in a new place.
- SQL stays inside `argos.store`. The boundary test is extended to forbid
  `sqlite3` in `projections`, `replay`, `baselines`, `evaluation`, and `cli` —
  the packages M3 and M4 will be tempted to query directly, at which point the
  `EventStore` port becomes decorative.
- `synchronous=FULL` is chosen over `NORMAL`. The measured cost is irrelevant at
  this volume, and `docs/02_ARCHITECTURE.md` requires the system to fail loudly
  when it cannot prove the integrity of a capture.
- `supersedes_observation_id` is still **not** added. It remains an open backlog
  item; adding an unused nullable field now is the compatibility wrapper the
  engineering rules warn against, and because the record is JSON, adding it later
  is cheap.
- Retention and compaction (ADR-0004's own consequence), migrations, a second
  backend, a query DSL, and the `payload_schema_version` -> model registry are
  all explicitly out of this slice and remain separate backlog items.
- The volume evidence behind section 1 is thin by its own admission. If a capture
  over a quiet market or a multi-market set contradicts the extrapolation by an
  order of magnitude, that is a reason to revisit retention and compaction — it
  is not a reason to revisit the engine, because volume did not choose it.
