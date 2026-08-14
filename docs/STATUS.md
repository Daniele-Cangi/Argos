# ARGOS status

Last updated: 2026-08-12

## Current state

- Current milestone: **M2 — CLOB capture — in progress**. M0 and M1 closed. The
  pacing-versus-timekeeping ADR that gated M2 (ADR-0009) is resolved and merged.
  Seven M2 vertical slices have landed: the canonical `ObservationEnvelopeV1` /
  `RejectedObservationV1` contracts and their identity derivation (ADR-0010);
  the first typed payload, `OrderBookSnapshotV1`; a security review of both
  contracts (verdict **PASS_WITH_FINDINGS**, no blocker, findings closed in
  `1fb057c`); a public CLOB WebSocket market-channel research note built from
  live capture, not documentation alone; the idempotent, append-only SQLite
  event store and delivery record specified by ADR-0011; the public CLOB
  REST order-book adapter and its normalization step
  (`src/argos/sources/clob.py`, `src/argos/ingestion/clob_book.py`) — the
  first slice that is a genuinely complete vertical: real recorded bytes go
  in one end and a deduplicated, identity-stable observation lands in
  `SQLiteEventStore` at the other; and now the `price_change.v1` typed delta
  payload (`src/argos/domain/pricechange.py`), the second and last payload
  model M2 needs, built against the recorded live WebSocket capture.
  **No WebSocket adapter, no capture manifest and no capture CLI exist yet** —
  the transport side of ingestion, and the loop that would run either adapter
  against live traffic continuously, are still unbuilt.
- Autonomous target: **complete M0-M4**
- Owner gate: **required after M4**
- Execution capability: **prohibited and absent**
- External evidence / LLM forecasting: **not started**

## Current objective

Build the WebSocket market-channel **adapter** — the transport half of
ingestion — using the same `Pacer`/`Clock` separation (ADR-0009) and
`ObservationEnvelopeV1`/`RejectedObservationV1` contracts (ADR-0010) the REST
adapter slice proved end-to-end against `SQLiteEventStore`. Both payload
models M2 needs now exist, so this next slice is transport and ingestion only,
not schema design.

`docs/BACKLOG.md` carries four constraints this slice must close, each with the
measurement that produced it rather than a reminder: a byte cap checked
*before* parsing (measured: 900,000 `price_changes` entries cost 146.87 s CPU
and 731.7 MiB, and no `Pacer` can bound synchronous CPU work); validating
`entry_hash` inside the ingestion `try` rather than leaving the envelope to
refuse it with no ledger entry; `ingest_sequence` allocation and cross-token
fan-out, still deliberately undecided; and the deliberate refusal of a
`(frame, token)` group carrying more than one distinct hash, a shape never
observed live.

## M2 slice: the capture loop, and three decisions deferred four times

`src/argos/ingestion/capture.py` (`run_capture`, `FrameSource`,
`CaptureHealth`) plus `tests/test_capture_loop.py`. This is where the three
decisions deliberately deferred since the REST adapter slice were finally
made. No CLI.

Quality gate: PASS — ruff, ruff format, mypy strict on 40 source files,
**1,199 tests** (up from 1,182).

**Decision 1 — `ingest_sequence` is allocated per *record produced*, not per
candidate.** A "peek, then commit" allocator: a sequence is consumed only in
the branch that actually writes an observation or a rejection. A `None` from
the normalizer ("this frame was not about this token") is counted and burns
nothing. The alternative — reserve unconditionally, skip writing — was
rejected because it would carve permanent gaps into the ledger for every
unsubscribed-sibling frame, which the research shows is routine traffic on
this source, not an edge case.

**Decision 2 — fan-out iterates the *configured* token set, sorted, never the
frame's own membership.** This is the load-bearing one for M3. A frame carries
entries for the unsubscribed binary sibling, so iterating "tokens present in
this frame" would make sequence numbers a function of what the server happened
to bundle, and replay determinism would inherit that dependence on connection
topology. Verified directly: the same frames produce identical sequences with
and without the sibling subscribed.

**Decision 3 — there is no new manifest concept.** `RunManifest`
(`run_manifest.v2`) plus the store's append-only `capture_run` rows already are
the manifest. `run_capture` writes no `SourceProvenanceV1` of its own; every
one it touches is already embedded inside a stored envelope or rejection. This
honours the pre-M2 constraint that an M2 capture manifest must **not** embed
one provenance record per ingested event — by construction, not by added logic.

**Verified independently of the slice's own tests**, driving the real recorded
capture through a hand-written source:

- 34 real frames, both tokens subscribed → 68 accepted observations; one token
  → 34. Fan-out is exactly the configured set.
- Sequences span **both** ledgers with **no gaps and no reuse**: exactly
  1..38 across deliveries and rejections combined.
- Two identical runs produce identical sequences and identical health counters.
- Re-opening the same `capture_run_id` is refused with `StorageError`, so a
  restarted loop cannot silently restart the sequence and collide — the
  "reconnect does not reset ingest sequence" property is structural, not a
  check that could be forgotten.
- A redelivered burst produces duplicates counted and zero extra observation
  rows.
- The four `book` events in the capture are counted as
  `UNKNOWN_EVENT_TYPE` rejections rather than dropped: no payload model is
  wired for them here, and invariant 14 wants that visible rather than
  convenient.

**A backlog item I wrote was wrong, and the agent was asked to challenge it
rather than satisfy it.** `docs/BACKLOG.md` said this slice must close the
transport's "oversized frame is counted but produces no rejection-ledger row"
gap. It cannot be closed as written, and the reasoning is now recorded rather
than the item quietly dropped: `WebsocketsConnector` passes the same
`MAX_FRAME_BYTES` to `websockets.connect` that `_classify` later checks, and
the library enforces `max_size` during frame reassembly — it raises out of
`recv()` before an oversized payload is ever assembled into a Python string. No
bytes, and therefore no `raw_payload_sha256`, ever reach ARGOS.
`RejectedObservationV1.raw_payload_sha256` is required, so writing a row would
mean **inventing a hash for content ARGOS never received** — fabricated
evidence, against core invariant 7. The counter is the honest maximum. A
consequence worth naming: with the shipped connector, `_classify`'s own size
check is unreachable, and it is defence-in-depth only for an injected connector
with a larger or unenforced `max_size`.

**A known limitation the implementing agent raised unprompted, now measured
rather than assumed.** `run_capture` calls the store's synchronous SQLite
methods directly from async code, with no thread offload, so each write blocks
the event loop — including the transport's independent heartbeat task.
Measured on a real ext4 database: median **0.23 ms** per
`append_observation`, maximum **0.49 ms**, which is 0.005% of the 10-second
heartbeat interval. Real as a class, not material at this scale; filed with the
number so a future slice that batches, or runs on a slower device, has the
baseline rather than an opinion.

## M2 slice: public market WebSocket transport, and two adversarial findings

Two pieces landed together: the transport adapter
(`src/argos/sources/clob_ws.py`, `tests/test_clob_ws.py`) and an independent
adversarial review of the WebSocket ingestion step
(`tests/test_clob_price_change_ingestion_adversarial.py`), whose findings are
fixed here in `src/argos/domain/text.py` and
`src/argos/domain/observation.py`.

Quality gate: PASS — ruff, ruff format, mypy strict on 39 source files,
**1,182 tests** (up from 1,101).

### The transport

`ClobMarketWsClient` connects to the public, unauthenticated market channel,
subscribes with `{"assets_ids": [...], "type": "market"}`, sends the plain-text
`PING` heartbeat every 10 s and consumes the `PONG` reply, and yields raw
frames. All of it follows the REST sibling's shape: injected `Clock` and
`Pacer` (ADR-0009), an adapter-owned seeded `random.Random`, a frozen health
record, no wall clock. Verified independently rather than taken on trust:
backoff is reproducible across two clients sharing a seed, and its maximum
across 200 seeds × 12 attempts is exactly `MAX_BACKOFF_SECONDS`, so jitter
cannot outrun the ceiling at any legal configuration. The connection is behind
a `MarketWebSocket`/`WebSocketConnector` protocol pair, so every test drives a
fake and no test opens a socket.

**The transport decodes nothing.** It yields `MarketFrame(text, received_time,
provenance)` and does not parse JSON, split arrays, or filter by token — core
invariant 7 keeps raw data immutable and normalization a separate versioned
step, which already exists in `argos.ingestion`.

**Backpressure is real, not advisory.** The frame buffer is bounded at 64; when
it fills, the receive loop blocks, so `recv()` is not called again and
backpressure propagates to the TCP receive buffer. Frames are never dropped to
keep up. The heartbeat runs independently, so a slow consumer does not make the
client look dead to the source.

**Two gaps stated rather than hidden.** An oversized frame (above an explicit
1 MiB `max_size`, set rather than inherited from the library default) is
counted on the health record but produces **no rejection-ledger row**, because
no ingestion layer has seen those bytes and this module has no
`capture_run_id`/`ingest_sequence` to write one under — the "counted" half of
`.claude/rules/data-integrity.md` without the "reasoned" half, left for the
capture-loop slice. And a reconnect **may lose messages**: this channel has no
sequence number (confirmed absent by observation), so a gap across a reconnect
is not detectable from the channel alone. Nothing in the module claims
gap-freedom.

This slice does **not** close "reconnect does not reset ingest sequence or
silently lose manifest state": sequence allocation and the manifest are the
capture-loop slice, and neither exists.

### Adversarial findings, both fixed

**HIGH — a lone UTF-16 surrogate escaped the taxonomy with no ledger entry, on
both adapters.** `is_display_control` never inspected Unicode category `Cs`, so
`is_clean_identifier` called a lone surrogate *clean*. It passed the ingestion
check and `ObservationEnvelopeV1._validate_identifier`, then reached
`_observation_identity` → `_digest`, whose `"|".join(parts).encode()` defaults
to strict UTF-8 and raises `UnicodeEncodeError` on an unpaired surrogate —
outside every `try` in the ingestion path. Python's `json` decodes the wire
escape `"\ud800"` into exactly that string without checking pairing, so an
ordinary-looking body reaches ARGOS carrying one, with an entirely honest
`byte_length`. Reproduced on the WebSocket adapter **and on the
already-committed REST adapter**; both now return a rejection. This is the
fourth distinct instance of the "escapes the ARGOS error taxonomy, therefore no
ledger entry" class in M2.

**MEDIUM — the M1 audit-forgery attack, on a durably stored identifier.** The
rejection ledger neutralizes identifiers rather than refusing them (refusing
would mean the rejection itself could not be written), but it did so with the
*prose* rules, and prose deliberately exempts newline and tab. So a newline in
the source's `market` field survived into a stored `condition_id`, carrying
`"line1\nline2: review status: human_reviewed"` — the original M1 attack,
waiting for the first renderer that prints a ledger row without block-quoting.
A new `neutralize_identifier_and_bound` applies the identifier rule as a
*replacement* rather than a refusal.

**One reported finding was rejected on the merits.** A hash spelled in
Arabic-Indic digits was filed as a fourth hostile shape; the code is right and
the expectation was wrong. Those are category `Nd` — ordinary text that cannot
move a cursor, reorder a line, or hide itself — so refusing them would mean
enforcing a *format* on `hash`, which ARGOS deliberately does not do on either
adapter: the research observed 40 hex characters every time but never
established it as a source guarantee, and enforcing it would turn a legitimate
future format change into a total rejection storm. Contrast `token_id`, where a
format **is** enforced, because the research did establish one there. The rule
is "enforce what the evidence supports", not "enforce what looks tidy". The
test now records the decision — its original docstring claimed all four shapes
were "correctly caught" while its own assertion disproved it, the
claim-outruns-assertion pattern again.

## M2 slice: pre-WebSocket decimal and hash hardening

Two defects reported by the owner against `a9b9802`, both **reproduced before
being acted on**, both closed here in a small slice deliberately scoped to
exclude any capture-loop, reconnect, projection or manifest work. Changed:
`src/argos/domain/orderbook.py`, `src/argos/domain/pricechange.py`, and the
three test modules that pin them.

Quality gate: PASS — ruff, ruff format, mypy strict, **1,101 tests** (up from
1,068).

**1. A non-zero decimal could canonicalize to zero — and this one was mine.**
`normalize_decimal` ran `Decimal.normalize()` inside `CANONICAL_DECIMAL_CONTEXT`,
whose `Emin` was finite and inherited from `decimal.DefaultContext`. A
sufficiently small non-zero value therefore **underflowed to zero**: measured,
`parse_wire_decimal("1E-1000064")` returned `Decimal(0)` and rendered as the
canonical text `"0"` — numerically and textually identical to a genuine zero, so
a tiny non-zero price and a real zero produced the **same identity-bearing
payload**.

The provenance of this defect is worth recording precisely, because it is a
lesson about the fix and not only about the bug. The previous slice *had* a
symmetric exponent bound, and I removed the negative half deliberately, arguing
that only a positive exponent reaches the branch that expands a value into long
plain text while a very negative exponent keeps a short, equally deterministic
scientific rendering. That argument was about **text length**. The defect is
**arithmetic**. The reasoning was locally correct and answered the wrong
question, and it shipped inside the very commit whose purpose was to stop
decimal canonicalization from silently changing values.

Closed three ways, deliberately overlapping:

- `MIN_DECIMAL_EXPONENT = -1000` restores the lower bound — far below anything
  this source can produce (real tick size `0.001`, adjusted exponent `-3`) and
  comfortably preserving the already-committed `1E-50` case;
- `CANONICAL_DECIMAL_CONTEXT` now sets `Emin`/`Emax` **explicitly** instead of
  inheriting them from `decimal.DefaultContext`, which is itself mutable
  process-global state — the same class of dependency the pinned context exists
  to remove, merely relocated from read time to import time;
- a **fail-closed postcondition** refuses any result that is not numerically
  equal to its input. This is the part that matters most: the two bounds are a
  fast, legible refusal, but the postcondition guarantees the property they are
  only *believed* to imply, without depending on anyone having reasoned
  correctly about `Emin`, `prec`, or which branch expands which exponent —
  which is exactly how the underflow survived review the first time.

Zeros return before the exponent bounds are applied, since a zero carries no
magnitude: `0E-100000` is still exactly zero and must not be refused for its
exponent, while every negative-zero spelling still collapses onto positive zero.

**2. A malformed `hash` escaped as a bare `TypeError`.**
`parse_price_change_group` built its hash set — and `sorted()` it while
composing the error message for a *different* refusal — before type-checking
any hash. Reproduced: `{"hash": []}` raises `TypeError: unhashable type: 'list'`,
and a frame mixing `"a"` with `3` raises `TypeError: '<' not supported between
instances of 'str' and 'int'`. A `TypeError` is outside the ARGOS taxonomy, so
it flew past `clob_price_change`'s `except ValueError` and the frame left **no
rejection-ledger entry at all** — the silent drop core invariant 14 forbids, and
the same shape as the `decimal.InvalidOperation` escape closed one module over
two slices ago. Every selected entry's hash is now validated as a non-empty
string before the set is built, and the error message uses a bounded `repr` so
that composing the rejection detail cannot itself become the exhaustion vector.

Pinned at the boundary that actually owes the row: list, dict, null, numeric,
boolean, empty-string and mixed-type hashes each produce a
`RejectedObservationV1` that is really persisted to and read back from the
ledger with its reason and raw hash, not merely a `ValueError` in the domain.

**Acceptance criteria, all verified directly**: `1E-1000064` refused and never
zero; `1E-50` still exact; negative-zero spellings still collapse to positive
zero; no accepted finite non-zero value changes numerically; tiny non-zero and
zero cannot share one canonical identity; every malformed hash type raises
`ValueError`, never `TypeError`; and the WebSocket ingestion path records them
as rejections.

## M2 slice: WebSocket `price_change` ingestion and normalization

The ingestion-layer counterpart to `argos.domain.pricechange`, the same
relationship `clob_book.py` has to `argos.domain.orderbook`. New:
`src/argos/ingestion/clob_price_change.py` (`normalize_clob_price_change`,
`CLOB_WS_PRICE_CHANGE_EVENT_TYPE`), `src/argos/ingestion/wire.py`
(`parse_event_time`, moved out of `clob_book.py` so both adapters share one
millisecond-timestamp parser instead of duplicating it),
`argos.domain.pricechange.NoEntriesForToken`,
`tests/test_clob_price_change_ingestion.py`. Changed:
`src/argos/ingestion/clob_book.py`, `src/argos/ingestion/__init__.py`,
`src/argos/domain/__init__.py`.

Quality gate: PASS — ruff, ruff format, mypy strict on 38 source files,
**1,068 tests** (up from 1,044).

**Still no transport.** This module takes one already-decoded frame. Opening
the socket, subscribing, heartbeat, and reconnect are a separate slice, and it
is the one that introduces a new dependency — kept out of this slice
deliberately so the normalization contract could be settled first.

**Both backlog constraints this slice owed are closed**, each verified
independently of the slice's own tests:

- The byte cap is checked against `provenance.byte_length` before a single key
  is read out of the frame, returning a rejection rather than raising. A
  14,149,053-byte frame carrying 60,000 entries is refused in **0.0002 s**,
  against a parse path measured at 16.15 s for 100,000 entries.
- `entry_hash` is validated inside the module's own `try` — length against
  `MAX_IDENTIFIER_LENGTH` plus `is_clean_identifier` — reproducing the check
  `clob_book._extract_source_hash` already applies rather than the gap. This
  was the third appearance of that same shape.

**A third outcome, and why it is not a rejection.** `normalize_clob_price_change`
returns `ObservationEnvelopeV1 | RejectedObservationV1 | None`. `None` means
the frame carried no entry for the requested token — normal traffic about the
unsubscribed binary sibling, not a defect. Writing a ledger row for each would
flood the rejection ledger with rows describing healthy traffic and destroy its
signal value. It is a **counted non-event**: the capture loop must count it,
and the docstring says so unmissably, because this function has no
health-counter to write to.

**A boundary contract tightened during integration review.** The ingestion
layer originally recognised that case by comparing the exception's *message
text* against a literal copied from the domain module's source. The failure
direction was safe — a text change would produce a noisy false rejection rather
than a silent drop — but the coupling rots invisibly, so it is now a named
`NoEntriesForToken(ValueError)`, where a rename breaks the import instead of
quietly changing behaviour. It subclasses `ValueError` so every other consumer
keeps working with one `except ValueError`.

**End-to-end evidence, verified directly rather than taken from the slice's own
test names.** Real recorded frames through `normalize_clob_price_change` →
`SQLiteEventStore`:

- all 34 real `price_change` frames normalize into accepted envelopes;
- both real zero-size entries reach an envelope payload as `REMOVE`
  (`0.17` on the bid side for the YES token, `0.83` on the ask side for its
  sibling) — the exit criterion now has evidence *through the store*, not only
  at the payload;
- redelivering an identical frame yields **one observation row and a
  `duplicate` delivery** (`accepted_new=3, duplicate=1` across the run
  measured);
- the three real frames sharing one `(timestamp, hash)` for one token, each
  carrying a different level change, mint **three distinct observation rows** —
  the identity hazard proven at the store, not only in the payload's canonical
  JSON.

**No independent security review ran on this slice.** Recorded plainly rather
than implied: the module is closely modelled on an already-reviewed sibling and
was verified directly, but that is not the same as a review, and four
consecutive subagent runs on the preceding slice terminated without delivering
a report. Adversarial testing was commissioned separately.

## M2 slice: the `price_change.v1` typed delta payload

The second and last typed payload M2 needs (`src/argos/domain/pricechange.py`,
`PriceChangeV1`, `PriceLevelChangeV1`, `PriceLevelChangeKind`,
`PriceChangeGroup`, `parse_price_change_group`), built and checked against the
recorded live WebSocket capture — 34 real `price_change` frames for two tokens,
not constructed examples alone. New: `tests/test_price_change.py`,
`tests/test_price_change_adversarial.py`,
`tests/fixtures/clob/ws_market_price_change.{raw,meta}.json`. Changed:
`src/argos/domain/orderbook.py`, `src/argos/domain/__init__.py`,
`tests/test_orderbook_snapshot.py`, `tests/test_clob_adapter_adversarial.py`.

Quality gate: PASS — ruff, ruff format, mypy strict on 36 source files,
**1,044 tests** (up from 951 at the REST adapter slice).

**The exit criterion, closed structurally rather than by convention.** "Zero-size
level update is represented as removal" is now a validated invariant, not a
parsing habit: `kind is REMOVE` **if and only if** `size == 0`, enforced by a
model validator on *every* construction path including replay and
`from_record`. A record claiming `SET` at size 0, or `REMOVE` at nonzero size,
is refused rather than silently re-derived — the same "recomputable, never
drifting" discipline `_validate_anomalies_are_recomputable` already applies to
book anomalies. The two real zero-size entries in the capture (`0.17 BUY` and
`0.83 SELL`, on the two sibling tokens) are pinned as *observed*, not merely
constructible.

Deliberately **unlike** the REST snapshot path: there, a zero-size level is a
counted `OrderBookAnomaly` (`ZERO_SIZE_LEVEL_DROPPED`), because a snapshot
describing a resting order of size zero is malformed. On the delta stream, size
`"0"` is the source's only vocabulary for removal, confirmed on live traffic
rather than inferred from documentation. The two conventions differ on purpose
and the difference is the point.

**An identity hazard measured from the real bytes, stronger than the research
note recorded.** The same `(timestamp, hash)` pair for the **same token**
arrives across up to **three separate frames**, each carrying a *different*
price level change — token `34691…637961`, timestamp `1786387666174`, hash
`5ce704de…`: `0.49/636`, then `0.65/142.85`, then `0.48/17`. Since ADR-0010
derives identity from the canonical payload, a payload carrying only the hash
would have collapsed three genuine deltas onto one `observation_id` and
**silently lost two**. Carrying the level changes in the payload is what keeps
that closed, and a test built on the real frames pins it. Separately measured:
none of the 58 distinct `(timestamp, hash)` pairs in the capture spans more
than one `asset_id`, which is *why* grouping by token yields a well-defined
per-token post-state hash.

**A HIGH defect found by independent adversarial testing — and it was not
confined to this slice.** `normalize_decimal` raised a bare
`decimal.InvalidOperation` — an `ArithmeticError`, **not** a `ValueError` —
from `quantize`, for any value needing more integer digits than the ambient
precision, reachable with a plain 29-digit integer string. Being outside the
ARGOS taxonomy, it flew past `argos.ingestion.clob_book`'s `except ValueError`:
a **214-byte** body carrying `"tick_size": "1E+29"` escaped `normalize_clob_book`
entirely and produced **no rejection ledger entry** — the silent drop core
invariant 14 exists to prevent, on the *already-committed* REST adapter.

Investigating it surfaced the deeper half. `normalize()`, `quantize()` and `%`
all read `decimal.getcontext()`, which is thread-local **mutable global
state**: the wire price `0.123456789012345678901234567890123` rendered as
**three different canonical texts** — and therefore three different
`observation_id`s — under ambient precisions 5 / 28 / 50. That is the hidden
global state CLAUDE.md prohibits outright, and a direct break of core invariant
5, since a replay under a different ambient context would not reproduce the
live identity. Both are closed by pinning `CANONICAL_DECIMAL_CONTEXT` and
refusing an explicit magnitude budget *before* any context-sensitive operation
runs. Re-measured after the fix: identical rendering across 18 ambient contexts
spanning precision, rounding mode and `Emin`/`Emax`, and across a worker thread
with a hostile default context.

Widening the context instead of refusing was considered and rejected as a fresh
resource-exhaustion vector rather than a fix: `"1E+1000000"` is 11 wire bytes
that would render as a one-megabyte canonical integer.

| Severity | Finding | Fix |
|---|---|---|
| HIGH | `decimal.InvalidOperation` escaping the error taxonomy, reachable on the committed REST adapter with a 214-byte body, leaving no ledger entry | `CANONICAL_DECIMAL_CONTEXT` plus explicit budgets in `normalize_decimal`; re-measured, no input to `parse_wire_decimal`, `parse_order_book_snapshot` or `parse_price_change_group` now escapes as a non-`ValueError` |
| HIGH | Canonical form — and therefore `observation_id` — depended on `decimal.getcontext()`, thread-local mutable global state | Same fix; verified independent across 18 ambient contexts and a second thread |
| MEDIUM | A 1,000,001-digit `int` cost **63.12 s** of CPU, because the magnitude budget ran *downstream* of the superlinear `Decimal(int)` conversion it was meant to bound. Not reachable through this project's own JSON decoding today, but only because CPython's `int_max_str_digits` default blocks it — a third-party default this module does not own | `_refuse_oversized_int` checks `bit_length()` first; refusal now costs 0.21 s |
| MEDIUM | An unrecognized **top-level** frame key was silently ignored while entry-level keys were refused — including a key spelled `sequence`, which this channel is confirmed to lack and whose arrival would have been discarded without trace | `EXPECTED_EVENT_KEYS` refuses it |
| MEDIUM | An entry that is not an object, or carries no `asset_id`, was skipped silently — and since parsing runs once per token, it would have been dropped for *every* token, never counted | Refused; found during integration review, not by an agent |

**A committed test was asserting something it never checked.** The old
`test_very_high_precision_beyond_any_real_tick_size_does_not_crash_and_is_preserved`
asserted only `str(...).startswith("0.4444")` — which a *truncated* value
satisfies just as well as a preserved one. Measured: the 61-digit wire value was
silently rounded to 28 digits and the stored value compared **unequal** to the
source. ARGOS durably stored a price the source never sent, called it
preserved, and passed its own adversarial test. Rewritten to assert refusal,
plus a non-degenerate companion asserting exact preservation *within* budget.
This is the "claim outrunning its assertion" pattern STATUS already names as
recurring — third occurrence.

**A committed test also caught a defect in the fix itself.** The exponent budget
was first written symmetrically; only a *positive* exponent reaches the branch
that expands a value into plain text, so the negative half silently narrowed an
already-shipped contract, and
`test_very_high_precision_beyond_any_real_tick_size_does_not_crash_and_is_preserved`
failed on a `1E-50` tick size. The bound is now one-sided, with the reasoning
recorded in the code.

**What held up under attack, recorded as negative results.** No collision was
found inside the `changes` tuple's canonical JSON — real JSON structure, rather
than a custom separator, closes the ADR-0010 blocker-B1 class by construction.
Scrambled wire order across all 34 real frames never changed a result. The
negative-zero and trailing-zero collapses still hold identically for
`OrderBookSnapshotV1` after the shared helper was rewritten, and every value in
both real fixtures still parses and is preserved **exactly**. `price_change.v1`
is **not** the first payload model with a free-text field: every string that
reaches `to_record()` is a pattern-validated identifier, an enum, or a
canonical decimal rendering — so the store-level OSC-52 exposure STATUS names
as its most important open item is **not** opened by this slice.

**Scope deliberately excluded, and why.** No WebSocket transport, no capture
loop, no sequence allocator, no manifest. `parse_price_change_group` normalizes
for **one** requested token per call, because a frame carries entries for the
unsubscribed binary sibling and fan-out would force a sequence-allocation policy
M3 replay determinism would inherit. A `(frame, token)` group carrying more than
one distinct hash is refused outright — never observed in either live capture,
and refusing an unobserved shape is preferred to inventing a grouping policy
that would then have to be reproduced byte-for-byte forever. All four
constraints are filed in `docs/BACKLOG.md` with their measurements.

**Process facts, recorded rather than smoothed over.** Four consecutive
subagent runs on this slice ended without delivering a report. In each case the
work products were verified directly instead of treating silence as success —
which is how the HIGH finding above was recovered: the adversarial agent had
left **5 tests failing on purpose** to pin a real defect, exactly as instructed,
and a silent completion would have looked identical to a clean run. A green
suite plus a silent agent remains the most dangerous combination in this
repository, now twice demonstrated. Separately, one security review was
invalidated by my own coordination error: I edited `orderbook.py` while a
read-only reviewer was measuring it. Read-only does not mean immune to
concurrent edits, and the remaining security verification was completed
directly rather than re-delegated.

## M2 slice: public CLOB REST order-book adapter and normalization

The public CLOB REST order-book snapshot adapter and its normalization step —
the first complete vertical slice of M2: real recorded bytes go in one end
and a deduplicated, identity-stable observation lands in durable storage at
the other. New: `src/argos/sources/clob.py` (`ClobClient.get_book`,
`ClobHealth`, `ClobBookNotFoundError`, `ClobBookBadRequestError`),
`src/argos/ingestion/clob_book.py` (`normalize_clob_book`,
`MAX_NORMALIZABLE_BYTES`), `tests/test_clob_client.py`,
`tests/test_clob_book_ingestion.py`, `tests/test_clob_adapter_adversarial.py`,
`tests/fixtures/clob/` (a byte-identical copy of the recorded real capture,
with provenance sidecar). Changed: `src/argos/domain/orderbook.py`,
`src/argos/sources/__init__.py`, `src/argos/ingestion/__init__.py`.

Quality gate: PASS — ruff, ruff format, mypy strict, **951 tests** (up from
886 when the adapter first landed within this slice, 841 before the slice).

**Scope deliberately excluded, and why.** `ingest_sequence` is a
caller-supplied parameter to `normalize_clob_book`, not allocated by it —
`normalize_clob_book` is a pure, synchronous normalization step, not the
capture loop. No capture loop, sequence allocator, scheduler, manifest
writer, or CLI exists in this slice. The WebSocket research found a frame can
carry entries for an *unsubscribed* sibling token, so assigning sequence
numbers before subscription filtering would make `ingest_sequence` depend on
connection topology, and M3 replay determinism would inherit that dependency.
Allocation is left as a decision the capture-loop slice must make
deliberately rather than inherit by accident.

**What actually moves on the M2 exit criteria.** "Duplicate source event does
not create a second accepted observation" now has **end-to-end evidence, not
just store-level evidence**: a real recorded response fed through
`ClobClient` → `normalize_clob_book` → `SQLiteEventStore` produces one
observation row and two delivery rows (`accepted=1, duplicate=1`) for a
redelivery. There is still no capture loop running against live traffic, so
this is described precisely rather than declared closed outright — see the
updated exit-criteria table below. "Invalid messages enter a rejection ledger
with reason and raw hash" moves the same way: a malformed real-shaped body
now produces a `RejectedObservationV1` written to the ledger through the
adapter path, not only through a hand-built envelope. Everything else in the
exit-criteria table stays open: no WebSocket adapter, no capture manifest, no
capture CLI, no projection.

**Security review: PASS_WITH_FINDINGS, no blocker.** The central question was
whether the new adapter inherits the two M1 HIGH fixes (the gzip-bomb
resource-exhaustion shape and the retry-deadline-bypasses-injected-clock
shape) as real properties or only as copied code shape. Re-measured with the
original instruments, the answer is real properties:

- Gzip bomb: 1.2 GiB decompressed from a 1,223,023-byte body is refused,
  `byte_length_so_far=67414112`, in 0.186 s at 174.7 MiB peak RSS —
  indistinguishable from `gamma.py`'s own 0.184 s / 174.8 MiB. The
  `content-length` pre-check refuses before a single byte is streamed; a
  *lying* small `content-length` with a 40 MiB body is still caught
  incrementally.
- Slow drip against a real `RealPacer`: the deadline held at 1.00 s (1
  attempt) and 23.00 s (3 attempts), not just the zero-retry path proven at
  M1 close, and fired *inside* a real backoff sleep with
  `cancelled_inside_backoff=True` — the pre-M2 pacing correction is inherited
  as a real property, not only copied shape.
- Jitter: the adapter-owned seeded RNG is immune to `random.seed()` on the
  module global (verified by seeding it to 12345 and 999 between runs) and
  bounded — across 200 seeds × 10 attempt numbers the maximum was exactly
  `MAX_BACKOFF_SECONDS`, so jitter cannot outrun the deadline budget at any
  legal configuration.
- Token id: all 18 tried hostile ids (`../../admin`, `123%2f..%2fadmin`,
  `https://evil.example/x`, CR-LF injection, 121 digits, a trailing newline,
  Arabic-Indic/fullwidth/mathematical digit spellings, ZWSP-separated digits)
  are refused with **zero requests sent**. `fullmatch` on `[0-9]{1,120}`
  blocks the trailing-newline bypass a bare `re.match` would allow; the id
  lands in a query parameter, not the URL path.
- The execution boundary is confirmed absent; no new dependencies; the only
  headers sent are `accept` and `user-agent`; the one log call carries
  path/attempts/status only, never a payload or a URL.

**Findings fixed in this slice**, all with regression tests:

| Severity | Finding | Fix |
|---|---|---|
| HIGH | Unbounded CPU and RSS in `normalize_clob_book`, in the gap between the client's 32 MiB response cap and the envelope's 4 MiB canonical-payload cap. Measured, all sized to pass the client cap: a 31 MiB `timestamp` cost 21.18 s CPU and was **accepted**; a 31 MiB `market` on a rejected payload cost 32.37 s and 571 MiB RSS; 900,000 book levels peaked at 1,266 MiB. Cause: `neutralize_and_bound` bounds the stored value but neutralizes the whole input first, and both the accepted and rejected paths run it twice. A `Pacer` cannot bound it — a cancel scope cannot interrupt synchronous CPU work, measured at 14.21 s elapsed against a 0.50 s deadline with `cancelled_caught` false. The same class the earlier M2 security review closed at `build_observation_envelope`, relocated one layer upstream | `MAX_NORMALIZABLE_BYTES` checked before any text is read, returning a rejection rather than raising (`src/argos/ingestion/clob_book.py`). Reproduced at 8 MiB: **5.48 s → 0.02 s**, and the result changed from an accepted observation to a counted rejection |
| MEDIUM | `normalize_clob_book` raised on two attacker-reachable inputs, contradicting its own docstring and producing no ledger entry: a 257-character `hash`, or one containing a newline, reached `_validate_identifier` *outside* the module's `try` and propagated a raw pydantic `ValidationError`. Reproduced on a 3.7 KB payload — no resource cost needed | Validated inside `_extract_source_hash`; the 256-character boundary is exact and pinned by a regression test |
| MEDIUM | The M1 HIGH properties held but nothing pinned them against a future edit to `clob.py` | Added deadline-fires-during-backoff, slow-drip, redirect-never-followed, and seeded-RNG-independence tests |

**Adversarial testing found one defect, now fixed: negative zero split one
economic price into two identities.** `_normalize_decimal` never
special-cased the sign of zero: `Decimal("-0")` survives `normalize()` with
exponent 0, so the quantize branch never fired, and it rendered as canonical
text `"-0"`. Since identity hashes rendered canonical text rather than the
`Decimal` value, the same price spelled two ways minted two `observation_id`s.
Reachable because `Decimal('-0') >= Decimal('0')` is `True`, so it passes the
`ge=MIN_PRICE` validator on exactly the two fields whose range includes the
boundary — `last_trade_price` and a level's `price`; `tick_size`/
`min_order_size` are `gt=0` and immune. Fixed by collapsing any zero onto
positive zero in `_normalize_decimal` (`src/argos/domain/orderbook.py`); the
fix is general, not a patch for the literal spelling — `-0`, `-0.0`, `-0E+5`,
`-0.00000`, and `0E+3` all render `"0"`. **This is the second independent
instance of the ADR-0010 decimal-normalization class**, after the
trailing-zero case `OrderBookSnapshotV1` already closed — a pattern, not an
isolated incident.

**What held up under adversarial attack, worth recording as a negative
result.** A fully *scrambled* wire order still re-derives correct
`best_bid`/`best_ask` — the code genuinely does not trust wire order.
Determinism survived duplicate JSON keys, whitespace and key-order
differences, exponent notation, and leading-zero/plus-sign spellings, while a
genuinely different book still mints a different id. Every malformed shape
returns a rejection rather than raising, including a hostile ESC sequence in
`market`, which is neutralized in the stored `condition_id`. A 404 is raised
*before* any payload reaches the normalizer, proven structurally. On an
A → B → A′ revert, three distinct observations are minted, verified by
reading `_observation_identity`'s call site: the distinction is driven by
`event_time` advancing, not by incidental raw-byte differences —
`raw_payload_sha256` plays no role in identity at all.

**Process fact worth recording.** The adversarial-testing agent run on this
slice ended without delivering a report. The test file it had written was run
directly rather than treating the silence as "no findings found"; 1 of 54
tests was failing, and investigating it by hand turned up the negative-zero
defect above. A green suite plus a silent agent is exactly where a real
defect can slip through unrecorded.

## M2 slice: idempotent SQLite event store and delivery record (ADR-0011)

Committed as `25c6f05`. Specified by ADR-0011
(`docs/adr/0011-sqlite-event-store-and-delivery-record.md`), which closes two
decisions this slice inherited as open: the SQLite/WAL-vs-append-log
comparison `docs/12_TECH_STACK.md` required, and the delivery-record shape
ADR-0010 explicitly left for "the store slice" to decide before it writes a
row. New: `src/argos/store/event_store.py` (`EventStore` protocol,
`SQLiteEventStore`, `open_sqlite_event_store`,
`Disposition`/`CompletionStatus`,
`DeliveryRecord`/`RejectionRecord`/`CaptureRunRecord`/`CaptureRunCounts`),
`tests/test_event_store.py`, `tests/test_event_store_adversarial.py`. Changed:
`src/argos/store/raw_archive.py`, `src/argos/store/__init__.py`,
`src/argos/domain/text.py`, `tests/test_boundaries.py`,
`tests/test_observation_envelope.py`, `tests/test_raw_archive.py`.

Quality gate: PASS — ruff, ruff format, mypy strict on 33 source files,
**841 tests** (up from 779 at the observation-identity slice).

**Engine decision, and the honest part of it.** Volume was measured from
artifacts already in the repository — 1.05 accepted observations/second/token
over the 36.1 s live WebSocket capture; 4,362 B per full envelope record, of
which 1,274 B is overhead independent of payload, so the fixed overhead is
roughly 6x the payload itself for a `price_change` delta; 345 B of embedded
provenance per record, ~31 MB/day/token duplicated at the measured rate — but
**volume did not decide the engine**: both candidates clear the ~1-2/s
requirement by roughly four orders of magnitude (~11.9k events/s measured at
`synchronous=FULL`, no batching). Atomicity decided it instead: "idempotent
and observable" (`docs/02_ARCHITECTURE.md`) is a two-write operation — detect
the duplicate, record its arrival — that must land together or not at all.
SQLite does that in one transaction; an append log needs a journal to close
the crash window between two appends, which is re-implementing SQLite worse.
`WITHOUT ROWID` was rejected on a measurement reproduced independently for
this ADR: **4,681 B/row against 1,456 B/row**, because a >1 KB record spills
to overflow pages inside an index B-tree. Parquet is rejected outright for
M2 — new large dependency, columnar and wrong for row-at-a-time append, no
unique key at all — reconsiderable only as an M4 export format.

The volume evidence is recorded as thin by ADR-0011 itself, and STATUS
repeats that rather than softening it: n = 1 token, 36 seconds, one
connection, no reconnect, an upper-tail market during a live match, no
quiet-market sample, and REST volume not measured at all (no polling cadence
chosen yet). ADR-0011 treats a future capture contradicting the
extrapolation by an order of magnitude as a reason to revisit retention and
compaction, not the engine choice, because volume was not what decided it.

**A contradiction in ADR-0011 itself, found and corrected during
implementation.** Section 5 as first written described `capture_run` as one
row with `ended_at NULL` meaning "not closed", while the same ADR's own
Consequences forbid any `UPDATE` inside `argos.store` — closing a run by
setting a column on an existing row is itself a mutation. The ADR now records
this as a dated Correction (2026-08-11) rather than a silent fix:
`capture_run` is append-only, closing inserts a second row, "open" is a
derived read, and two partial unique indexes make double-open and
double-close impossible at the schema level. The cost, recorded rather than
hidden: `capture_run_id` is no longer unique in that table, so
`delivery.capture_run_id` cannot be a SQL foreign key to it, and "the named
run was opened and is still open" is a Python check inside the transaction —
genuinely weaker than a database constraint. Security review assessed this
specific weakening as **weaker, not exploitable**: `BEGIN IMMEDIATE`
serializes writers, and a measured 3-process race against it produced a
consistent store.

**Two M2 exit criteria now have store-level evidence** (see the updated M2
exit-criteria table below), while remaining explicit that no adapter or
capture loop feeds them yet: `append_observation` inserts zero second
observation rows and exactly one `delivery` row with `disposition="duplicate"`
for a redelivery, both writes in one `BEGIN IMMEDIATE` transaction;
`append_rejection`/`iter_rejections` persist the rejection ledger, keyed
`(capture_run_id, ingest_sequence)` rather than on `rejection_id` alone. "An
interrupted capture closes or marks its manifest incomplete" now has its
**store half** only: `capture_run` is append-only and a run with no closing
row is a queryable signal via `iter_open_capture_runs`. No capture loop
exists, so none of the three is marked closed.

**Review verdicts.** Independent testing added 25 adversarial tests: real
multi-connection races (duplicate insert, close-vs-close, append-vs-close,
delivery-vs-rejection sequence contention) each produced exactly one winner;
a simulated crash mid-transaction left no partial row in either direction,
including the duplicate path; append-only held behaviourally; three
real-fixture-derived malformed entries sharing one `rejection_id` all
survived; a byte-identical round trip held including a `PRESENT` event_time
the existing suite had never exercised. Security review returned
**PASS_WITH_FINDINGS, no blocker**: no SQL injection (every statement is a
module constant; a `DROP TABLE` payload round-tripped as a value), foreign
keys genuinely on, atomicity holds, `synchronous=FULL` really syncs (17
`fdatasync` calls measured on ext4, **0 on tmpfs** — recorded because it means
the durability guarantee is filesystem-dependent), no wall clock, lazy
iterators, no execution/wallet/credential/authenticated surface, and no new
dependency.

**Findings fixed in this slice**, all with regression tests:

- `BEGIN IMMEDIATE` sat outside `_transaction`'s own `try` — found
  independently by *both* reviews and reproduced before the fix. The
  statement most likely to fail during a real capture ("database is locked"
  against a concurrent writer; "Cannot operate on a closed database") escaped
  as a bare `sqlite3.OperationalError`. Now inside the try, with the
  underlying sqlite message preserved in the error's `context`.
- `capture_run_id` bypassed the identifier contract that
  `ObservationEnvelopeV1._validate_identifier` already enforces for the same
  field name elsewhere — OSC 52 and RLO survived into
  `iter_open_capture_runs`, the exact "interrupted capture" report a future
  renderer prints, and 20,000,000 characters were accepted in 0.168 s. The M2
  security-review HIGH finding relocated to a new boundary; now refused via
  `_validate_capture_run_id`.
- Six read-path corruption shapes (`json.JSONDecodeError`, `ValueError`,
  `TypeError`) escaped the taxonomy the module documented as `StorageError` —
  the M1 `read_raw_payload` finding reopened in the same package. A
  `_decoding` context manager now converts any decode failure to
  `StorageError`.
- The append-only boundary check under-covered badly: the forbidden-token set
  was `UPDATE|DELETE|ALTER`, and `REPLACE INTO`, `INSERT OR REPLACE`, `DROP
  INDEX`, `PRAGMA writable_schema`, `ATTACH`, and `VACUUM` all passed it.
  Security review demonstrated `REPLACE INTO observation` actually destroying
  an immutable row — and `INSERT OR REPLACE` is the idiom a future author
  most plausibly reaches for to make a write "idempotent", the very concept
  this store is built on. The token set is extended; the test now documents
  that the check is literal-only, not a general guarantee.
- The WAL durability claim was prose, not a property: `PRAGMA
  journal_mode=WAL` discards its result, and SQLite returns the mode actually
  in effect rather than erroring — verified that `:memory:` silently reports
  `memory`. ADR-0011 hangs the interrupted-capture criterion on WAL recovery,
  so all three pragmas are now read back and a mismatch raises `StorageError`.
- `raw_archive._write_atomically`'s `os.replace` and its new directory
  `fsync` ran outside the guard: the failure escaped as a bare `OSError`
  *after* the rename it exists to make durable had already happened, so the
  call reported failure while the file was on disk. Both are now inside the
  guard, and the error message states that distinction explicitly.
- A stale `.partial` file left by a real process kill permanently wedged that
  hash (a bare `FileExistsError`, no self-healing) and defeated the
  documented sidecar-repair path. A stale *regular* file is now cleared and
  the write proceeds.

**Two things worth recording as process facts, not just outcomes.** The
first fix for the stale-`.partial` finding above introduced a security
regression: an unconditional unlink would have silently downgraded the M1
symlink guard from "refuse" to "delete and proceed", because
`O_CREAT|O_EXCL` reports a planted symlink as the same `FileExistsError` as a
stale regular file. The pre-existing symlink regression test caught it
immediately; the final version clears only a regular file and refuses
anything else with `ImmutabilityViolationError`. Separately, a finding
neither review caught, found while writing a regression test:
`is_clean_identifier` delegated wholly to `is_display_control`, which
deliberately exempts `\n`/`\t` (correct for prose, where the M1 defence is
sanitizer *plus* block-quoting) — so **a newline passed identifier
validation on `market_id`, `condition_id`, `token_id`, `source_sequence`,
`source_hash`, `capture_run_id` and siblings through two security reviews**,
and a newline is the original M1 attack that forged
`review status: human_reviewed` into an audit. `is_clean_identifier` now
refuses `\n`/`\t`/`\r` independently of `is_display_control`. The existing
test's docstring had claimed newlines were refused while asserting only ESC
and RLO — a claim outrunning its assertion, a pattern that recurs often
enough in this repository to name here rather than treat as a one-off.

## M2 slice: observation identity and rejection ledger (ADR-0010)

Slice: the canonical `ObservationEnvelopeV1` + `RejectedObservationV1`
contracts (`src/argos/domain/observation.py`), serving two M2 exit criteria —
"duplicate source event does not create a second accepted observation" and
"invalid messages enter a rejection ledger with reason and raw hash" — plus a
public CLOB REST research slice (`docs/research/m2-clob-rest-book.md`,
committed as `da08d1d`) that fed the identity design with measurements against
the real endpoint rather than assumption.

Quality gate: PASS — ruff, ruff format, mypy strict on 32 source files,
**710 tests** (up from 594 at the pre-M2 slice).

The architecture review returned **BLOCK**. Every finding was reproduced
before acting on it; all five blockers are fixed and re-verified:

- **B1** — identity material was joined on `\x1f`, so a source-controlled
  separator could shift a field boundary and collide two different
  observations. Fixed with a length-prefixed injective encoding
  (`_digest` in `src/argos/domain/observation.py`). Independent adversarial
  testing found the same class of collision on the `market_id`/`condition_id`
  and `condition_id`/`token_id` boundaries and in the rejection ledger; all are
  covered by regression tests in `tests/test_observation_envelope_adversarial.py`.
- **B5** — the envelope held live Python objects, so `to_record()` dumped in
  JSON mode and `Decimal('0.5')` reloaded as `'0.5'`: the replayed envelope
  differed from the live one while carrying the same `observation_id` — core
  invariant 5 broken, and M3's identical-hash criterion runs through this
  object. Fixed by storing the canonical JSON form and restoring types via
  `read_payload`.
- **B4** — any mapping could previously be labelled with any
  `payload_schema_version`; the mismatch would have surfaced only when
  `read_payload` ran during replay, against a capture that cannot be re-taken.
  Fixed by having `build_observation_envelope` take the typed `VersionedModel`
  rather than a mapping plus a version string.
- **B2** — `SourceProvenanceV1.http_status` was mandatory, which would have
  forced the WebSocket adapter (a same-milestone deliverable) to invent an
  HTTP status inside the provenance contract. Now nullable.
- **B3** — `docs/04_DATA_CONTRACTS.md` specified a clock-skew tolerance and
  quality flag that did not exist in code — the M1 "specified contract
  silently dropped" pattern repeating. Implemented as
  `ObservationQualityFlag.EVENT_TIME_AHEAD_OF_RECEIPT`; the doc and the code
  now agree.

Also fixed from the same reviews:

- **N8** — the sanitizer had been duplicated into the domain on a justification
  that was false (`compiler/audit.py` already imports from `argos.domain`),
  and its docstring claimed a parity test that did not exist. Now a shared
  `argos.domain.text` module used by both `compiler.audit` and
  `domain.observation`.
- Adversarial testing found that Unicode tag characters (U+E0000-U+E007F) and
  U+200B/U+FEFF are Unicode category `Cf`, not `Cc`, so they survived the
  original control-character sanitizer into stored records — an invisible-text
  smuggling channel. Now neutralized in `argos.domain.text`; ZWJ/ZWNJ are
  deliberately kept because they are load-bearing in Indic and Perso-Arabic
  scripts and cannot reorder or hide surrounding text.
- Adversarial testing reproduced that pydantic's `model_copy(update=...)` does
  **not** re-validate, so a bare mutable dict could land in a stored payload
  and a later external mutation would reach `to_record()`. Fixed structurally
  on `VersionedModel.model_copy`, which now copies through validation.
- **N1** — `source_sequence="absent"` could forge the identity of
  `source_sequence=None`; fixed by the same length-prefixed encoding as B1.
- **N2** — identity is derived only from validated field values, and
  `recompute_observation_id` makes it auditable rather than trust-only.
- **N6** — envelope `source` is now cross-checked against `provenance.source`
  at construction, so an envelope cannot attribute itself to a different
  source than the bytes it points at.

Research findings recorded as project reality (from real recorded public
payloads against `clob.polymarket.com`, not from documentation or assumption
— see `docs/research/m2-clob-rest-book.md` for the fixtures and the
three-poll timing experiment):

- the CLOB book response has no sequence number, only a millisecond
  `timestamp` and a content `hash`;
- `timestamp` tracks the book's last change, not the response time — verified
  by polling one token three times, five seconds apart;
- both `bids` and `asks` end at top of book (ascending / descending
  respectively), the reverse of the naive `[0]` reading;
- 404 is ambiguous across a closed market, an unknown token, and a
  syntactically valid token that never had a book.

## M2 slice: typed order-book snapshot payload (`OrderBookSnapshotV1`)

Committed as `1901b4e`. The first typed payload named by
`ObservationEnvelopeV1.payload_schema_version` (`"order_book_snapshot.v1"`,
`src/argos/domain/orderbook.py`), built and checked against the real REST
`/book` fixture the earlier research slice recorded, not only constructed
examples.

- Every `Decimal` price/size/tick-size/last-trade-price field is normalized on
  the way in (`_normalize_decimal`), closing the ADR-0010 "consequences"
  constraint directly on the first payload that could have hit it:
  `Decimal("0.430")` and `Decimal("0.43")` now serialize to identical
  canonical text, checked against the real recorded two-precision case
  (`tests/test_orderbook_snapshot.py::test_the_same_price_at_two_text_precisions_produces_identical_canonical_json`,
  `::test_observation_identity_collapses_across_a_cosmetic_decimal_reformat`).
- Floats and bools masquerading as `Decimal` are refused outright on every
  price/size field, even if the caller's own JSON decoding produced one
  (`tests/test_orderbook_snapshot.py::test_a_float_price_is_refused` and
  siblings). Wire order (bids ascending, asks descending — the reverse of the
  naive `[0]` reading) is never trusted: every construction path, not only
  parsing, is re-validated into "best level first" order and refuses rather
  than silently re-sorts an out-of-order snapshot.
- Anomalies — a dropped zero-size level, an off-tick price, a crossed book, a
  locked book — are recorded as counted, reasoned `OrderBookAnomaly` entries
  rather than raised or silently dropped, per core invariant 14. A *duplicate*
  price level on one side has no non-arbitrary resolution and refuses the
  whole snapshot instead. This vocabulary (`OrderBookAnomalyKind`) is
  deliberately kept separate from `ObservationQualityFlag`, which lives on the
  envelope and describes a different class of defect (delivery, not payload
  content).
- Zero-size levels were never observed in the REST fixture
  (`tests/test_orderbook_snapshot.py::test_zero_size_levels_were_never_observed_in_the_real_fixture`),
  matching the REST research note's suspicion. The WebSocket research below is
  what actually observed one live, on the delta stream specifically.

No adapter or store consumes this model yet; it is a domain payload type
built and tested against a recorded fixture, the same relationship
`MarketDefinitionV1` had to the Gamma fixtures at M1.

## M2 security review: observation and order-book contracts

Verdict: **PASS_WITH_FINDINGS, no blocker.** Findings closed in `1fb057c`.
Both original M1 attack classes — the resource-exhaustion shape and the
newline/OSC-52 rendering-forgery shape — were re-measured through the real
audit/envelope pipeline rather than re-read, and neither regressed from
consolidating the sanitizer into the shared `argos.domain.text` module (see
the ADR-0010 slice, "N8" above): the review ran a differential across all
1,112,064 legal Unicode codepoints and found 0 codepoints the consolidated
sanitizer stopped neutralizing and 130 gained, i.e. the move is a strict
superset of the sanitizer it replaced. This is recorded here as the review's
own measurement; no such full-codespace sweep is committed as a repository
test today.

| Severity | Finding | Fix |
|---|---|---|
| HIGH | Building an envelope traversed the normalized payload four times; a 31.8 MiB payload (legal under the source client's own 32 MiB response cap) cost 2.84 s CPU and 750 MiB RSS, and the `Pacer` deadline could not bound it because a cancel scope cannot interrupt synchronous CPU work — measured at 1.03 s elapsed against a 0.50 s deadline with `cancelled_caught` false. The M1 gzip-bomb class, relocated downstream of the byte cap that fixed it | Capped at `MAX_PAYLOAD_CANONICAL_BYTES` (4 MiB) and serialized once; the same payload is now refused in 0.17 s at 129 MiB peak RSS (`src/argos/domain/observation.py::build_observation_envelope`, `tests/test_observation_envelope_adversarial.py::test_an_oversized_payload_is_refused_inside_the_taxonomy`) |
| HIGH | `market_id`, `condition_id`, `token_id`, `source_sequence`, `source_hash` stored verbatim — ESC, OSC 52, RLO, and newlines all survived — and unbounded; one measured at 20,000,000 characters beside a `detail` capped at 4,043. The M1 finding recurring | The envelope now refuses a hostile or oversized identifier outright (`_validate_identifier`); the rejection ledger neutralizes and bounds them instead, deliberately, because refusing there would mean the rejection itself could not be written (`tests/test_observation_envelope_adversarial.py::test_rejection_detail_neutralizes_zero_width_and_unicode_tag_characters`, `tests/test_observation_envelope.py::test_a_hostile_identifier_is_refused_from_an_accepted_observation`, `::test_the_rejection_ledger_bounds_a_hostile_identifier_instead_of_refusing_it`) |
| MEDIUM | Truncation is lossy and identity was derived from the truncated value: two payloads differing only past the cap shared one `observation_id` while their raw hashes differed | `neutralize_and_bound`'s truncation suffix now carries a digest of the full neutralized text, restoring injectivity for anything that hashes the stored value (`src/argos/domain/text.py::neutralize_and_bound`, `tests/test_observation_envelope_adversarial.py::test_truncated_text_stays_distinguishable_and_recomputable`) |
| MEDIUM | U+061C ALM was missing from the bidi mark set the docstring claimed to cover; U+FFF9-FFFB interlinear annotation is display forgery of the class the sanitizer already closes for bidi | Both now neutralized in `argos.domain.text.is_display_control` |
| MEDIUM | pydantic's bare `ValueError` past nesting depth 254, and `freeze`/`thaw`'s `RecursionError`, escaped the ARGOS error taxonomy — both trivially reachable from hostile JSON | Both now surface as `ContractViolationError` (`src/argos/domain/observation.py::_canonical_payload`, `tests/test_observation_envelope_adversarial.py::test_a_pathologically_nested_payload_fails_inside_the_taxonomy`) |
| LOW | The rejection ledger had `recompute_observation_id`'s auditability but no equivalent of its own — a forged `rejection_id` could not be checked against the stored fields | `recompute_rejection_id` added (`src/argos/domain/observation.py::recompute_rejection_id`) |

Deliberately **not** fixed this slice, each filed in `docs/BACKLOG.md` with its
own reasoning rather than dropped silently:

- the variation-selector/ZWJ/ZWNJ covert channel (a 31-byte instruction was
  demonstrated surviving into a rendered audit with zero visible glyph
  difference) — a detection problem for the consuming layer, not something a
  sanitizer can close, and left explicitly unsolved;
- `SourceProvenanceV1.http_status` being nullable makes "non-HTTP transport"
  indistinguishable from "adapter forgot to set it" — no transport
  discriminator exists yet;
- `schema_version` uniqueness is unenforced across `VersionedModel`
  subclasses;
- neither `observation_id` nor `rejection_id` is enforced by a validator — a
  forged id round-trips through `from_record` while `recompute_*` disagrees;
- `SourceProvenanceV1.endpoint` still has no redaction contract for a future
  credential-in-query mistake, now with a larger blast radius since it
  persists per observation rather than per run manifest;
- `RejectedObservationV1.detail` retains newlines by design, and the M1
  defence was sanitizer **plus** block-quoting — only the sanitizer carried
  across, because no ledger renderer exists yet to block-quote into.

## M2 research: public market WebSocket channel

`docs/research/m2-clob-websocket.md`, committed as `f508fbb`. Scope is the
public, unauthenticated market channel only, per
`.claude/rules/no-execution.md`. Two live captures against real traffic for
one actively-trading token (~85 combined seconds), not documentation alone,
answer the three questions the REST research note left open:

- **No sequence number** — confirmed absent by direct observation across both
  captures, not only by absence from the documentation. Gap detection on this
  channel, like REST, has to be built on `(timestamp, hash)` reconciliation.
- **A `price_change` delta's `hash` is the hash of the resulting book state**,
  and reconciles **exactly** with what REST `/book` returns for the same
  state — confirmed twice independently, within the WebSocket stream itself
  and against concurrent REST polls for the same token. This is exact content
  identity, not heuristic matching.
- **Zero-size means removal**, observed directly on live traffic, including
  three removals batched into one update — the M2 exit criterion's convention,
  with real evidence, on the WebSocket delta stream specifically (never
  observed on REST, matching the REST note's suspicion).

**Consequence for ingestion design.** `(timestamp, hash)` identifies a
*post-state*, not a wire message: six `price_changes` entries shared one hash
in a single message, and two distinct frames 196 microseconds apart carried an
identical `(timestamp, hash)` pair. An adapter must not assume a 1:1 mapping
between a WebSocket message and a book transition. This makes the residual
collision ADR-0010 already documents — "two source messages that are
genuinely different events but share every stable field... remain
indistinguishable" — **reachable in practice for this source, not merely
theoretical**. Filed as a constraint the store and the WebSocket adapter must
both handle, in `docs/BACKLOG.md`.

Also recorded: REST `/book` returned 403 for Python's default `urllib`
User-Agent while `curl`'s default UA and a browser-like UA both succeeded.
**UNVERIFIED as a general rule** — only two User-Agent strings were tried —
but the capture adapter must set an explicit, reasonable User-Agent and must
not read "no auth header" as "no client-identification requirement."

No WebSocket adapter exists yet; this is a research document only, the same
status the REST research note had at its own stage. The document's own
UNVERIFIED list (idle-timeout duration, reconnect behavior, rate limits,
maximum token ids per connection, `operation: subscribe/unsubscribe` on an
open connection, the three `custom_feature_enabled` event types, whether the
observed `min_order_size`/`neg_risk` omission on one live `book` event is
systematic) is unresolved and must not be assumed by the capture loop.

## M2 exit criteria

Tracking `docs/07_MILESTONES.md`. Two criteria now have **end-to-end**
evidence from the CLOB REST adapter slice (a real recorded response through
`ClobClient` → `normalize_clob_book` → `SQLiteEventStore`), not merely
store-level evidence; one has payload-level evidence on real recorded frames;
one has store-only partial evidence; the rest have no adapter or capture loop
yet to produce evidence against and are listed as open rather than implied
closed. No WebSocket adapter, no capture manifest, and no capture CLI exist
yet — nothing runs either adapter continuously against live traffic yet, and
no criterion is marked closed on the strength of a payload model alone.

| Criterion | Status | Evidence |
|---|---|---|
| Duplicate source event does not create a second accepted observation | **End-to-end evidence; no capture loop runs it against live traffic yet** | `_observation_identity` collides an identical redelivery onto one `observation_id`, on the real recorded CLOB payload as well as a constructed one (`docs/research/m2-clob-rest-book.md`, "Consequence for `ObservationEnvelopeV1`"). `SQLiteEventStore.append_observation` enforces it at the store: a redelivery inserts zero second `observation` rows and exactly one `delivery` row with `disposition="duplicate"`, both writes inside one `BEGIN IMMEDIATE` transaction (`tests/test_event_store.py`, `tests/test_event_store_adversarial.py`, including real multi-connection race tests). The CLOB REST adapter slice closes the remaining gap end to end: a real recorded response fed through `ClobClient` → `normalize_clob_book` → `SQLiteEventStore` produces one observation row and two delivery rows (`accepted=1, duplicate=1`) for a redelivery. **Not yet closed**: no capture loop runs either adapter continuously against live traffic |
| Zero-size level update is represented as removal | **End-to-end evidence into the store; no transport receives a live frame yet** | Represented structurally, not by convention: `PriceLevelChangeKind.REMOVE` holds **if and only if** `size == 0`, validated on every construction path including replay and `from_record`, so a record cannot claim one and carry the other (`src/argos/domain/pricechange.py`). Both genuine zero-size entries in the recorded live capture now travel the full path — `normalize_clob_price_change` -> `build_observation_envelope` -> `SQLiteEventStore` — and land as `REMOVE` in a stored payload (`0.17` bid side, `0.83` ask side on the sibling), verified independently of the slice's own tests. `OrderBookSnapshotV1` separately implements the REST-snapshot side, where the same value is deliberately a counted anomaly rather than a removal. **Not yet closed**: no WebSocket transport exists, so the frames are replayed from a recorded capture rather than received from a socket |
| Reconnect does not reset ingest sequence or silently lose manifest state | **Closed structurally; never exercised against a live socket** | `run_capture` owns the counter for the whole run, so a reconnect inside the transport is transparent to it — the transport keeps yielding from one async generator. Restarting the loop cannot silently restart the sequence either: re-opening the same `capture_run_id` is refused with `StorageError` by the store's partial unique index, so the property is structural rather than a check that could be forgotten. Verified independently of the slice's own tests, on the real recorded capture: sequences span both the delivery and rejection ledgers with no gaps and no reuse (exactly 1..38), and are identical across two runs. Manifest state cannot be lost silently: `capture_run` is append-only and a run with no closing row is a queryable signal. **Not yet closed**: no capture has run against a live socket, and a real network reconnect has never been exercised — the research note still records reconnect behaviour as UNVERIFIED |
| Invalid messages enter a rejection ledger with reason and raw hash | **End-to-end evidence; no capture loop runs it against live traffic yet** | `RejectedObservationV1` carries `reason: RejectionReason`, `detail`, and `raw_payload_sha256`; `build_rejected_observation` derives a deterministic `rejection_id` so redelivery of the same invalid bytes for the same reason collapses rather than growing the ledger unbounded. `SQLiteEventStore.append_rejection`/`iter_rejections` persist it, keyed `(capture_run_id, ingest_sequence)` rather than on `rejection_id` alone, so two genuinely different malformed entries in one frame that happen to share one `rejection_id` (ADR-0011 section 7) both survive instead of one silently overwriting the other. The CLOB REST adapter slice closes the remaining gap end to end: a malformed real-shaped body fed through `normalize_clob_book` produces a `RejectedObservationV1` written to the ledger (`tests/test_clob_book_ingestion.py`). The WebSocket source now has the same evidence on its own path: `normalize_clob_price_change` turns every `ValueError` the domain raises — plus its own `entry_hash` length and display-control checks, deliberately inside its own `try` — into a `RejectedObservationV1` rather than an escaping exception, so a malformed real-shaped frame reaches the ledger with a reason and the raw hash (`tests/test_clob_price_change_ingestion.py`). **Not yet closed**: no capture loop runs either adapter continuously against live traffic |
| Book snapshot plus deltas reconstruct a tested projection | Open | Both input payload models now exist — `OrderBookSnapshotV1` (snapshot) and `PriceChangeV1` (delta) — and the research note established that a delta's `hash` is the hash of the *resulting* book state and reconciles exactly with REST for the same state, which is what a projection would verify against. **Not yet closed**: no projection module and no WebSocket adapter exist |
| No authenticated/user channel or trading code exists | Holds | Unchanged from M0-M1; the CLOB REST adapter is read-only by construction — it sends no credentials and exposes only the public `GET /book` endpoint (`src/argos/sources/clob.py` module docstring, ADR-0007) |
| An interrupted capture closes or marks its manifest incomplete | **Both halves now exist; never exercised against a live socket** | Store half (unchanged): `capture_run` is append-only, closing inserts a second row, and "not yet closed" is a derived read via `iter_open_capture_runs`, with partial unique indexes making double-open/double-close impossible (ADR-0011 section 5). Loop half (new): `run_capture` opens the run before consuming a frame and always closes it — `COMPLETED` on clean exhaustion, `FAILED` recorded and then re-raised on an exception. A process killed outright runs neither branch and writes no closing row, which `iter_open_capture_runs` reports as interrupted — that absence is the intended signal, not a gap, since a dying process cannot be trusted to describe its own death. **Not yet closed**: no capture loop has run against live traffic, so no real interruption has ever been observed |

## Known limitations from the M2 observation-identity slice

Recorded here rather than discovered late by the store or adapter slices that
build on this one. Full reasoning in `docs/adr/0010-observation-identity.md`
and `docs/BACKLOG.md`.

- **Closed by `OrderBookSnapshotV1` (`1901b4e`).** The identity slice noted
  that no payload model yet normalizes `Decimal` scale, so `Decimal("0.430")`
  and `Decimal("0.43")` minted different `observation_id`s — reproduced
  directly
  (`tests/test_observation_envelope_adversarial.py::test_decimal_trailing_zero_precision_changes_identity`)
  and confirmed live: the CLOB endpoint really does report the same price at
  two precisions across `/book` (`"0.430"`) and `/last-trade-price`
  (`"0.43"`). `OrderBookSnapshotV1._normalize_decimal` now closes this on
  every price/size/tick-size/last-trade-price field it carries (see "M2
  slice: typed order-book snapshot payload" above). The general form of the
  constraint stands: **every future payload model must do the same**, this
  only closes it for the first one.
- `ObservationEnvelopeV1` has no `supersedes_observation_id`. Identity depends
  on the normalized payload and deliberately excludes `parser_version`, so
  reprocessing the same raw bytes under a corrected parser mints a new,
  unlinked identity. ADR-0004 requires superseding records as the correction
  mechanism; this field does not exist yet.
- **Closed by the ADR-0011 event-store slice.** The store's delivery-record
  shape was undecided at this slice's close: identity excludes
  `capture_run_id` and `ingest_sequence` by design (both would make a
  duplicate unable to collide), which meant a collapsed duplicate had nowhere
  to record its own arrival, and `RejectedObservationV1` could not point at
  an accepted twin it duplicates. ADR-0011 decided it: one `delivery` row per
  arrival, keyed `(capture_run_id, ingest_sequence)`, carrying a
  `disposition` of `accepted_new` or `duplicate` (see "M2 slice: idempotent
  SQLite event store" above).
- No `payload_schema_version` -> model registry exists. `read_payload` takes an
  explicit `model` argument today; M3 dispatch across multiple payload types
  will need something less ad hoc.
- `read_payload` hard-matches exactly one `payload_schema_version` rather than
  accepting a set via `ensure_supported_version`, so no reader can yet accept
  more than one payload version.
- The research doc's UNVERIFIED list (rate limits, the `/books` batch
  endpoint, response headers, zero-size REST levels, halted-market behaviour)
  is unresolved. None of it should be assumed by the capture loop.
- The WebSocket research note found `(timestamp, hash)` identifies a
  post-state, not a wire message, and can span more than one frame. This makes
  ADR-0010's already-documented residual identity collision reachable in
  practice for this source, not merely theoretical — see "M2 research: public
  market WebSocket channel" above and `docs/BACKLOG.md`.
- The security review closed in `1fb057c` left several findings deliberately
  unfixed with their own reasoning — the variation-selector/ZWJ covert
  channel, `SourceProvenanceV1.http_status`'s missing transport discriminator,
  unenforced `schema_version` uniqueness, and `SourceProvenanceV1.endpoint`'s
  redaction gap (now with a larger blast radius at the store — see below).
  **Partially closed:** `observation_id`/`rejection_id` verification is no
  longer trust-only. `SQLiteEventStore` calls `recompute_observation_id`/
  `recompute_rejection_id` on both write and read (ADR-0011 section 8). Full
  list in "M2 security review" above and `docs/BACKLOG.md`.

Quality gate: PASS — ruff, ruff format, mypy strict on 32 source files,
**779 tests** (up from 710 at the observation-identity slice).

## Known limitations from the M2 event-store slice

Recorded here rather than discovered late by the adapter or capture-loop
slices that build on this one. Full reasoning in
`docs/adr/0011-sqlite-event-store-and-delivery-record.md` and
`docs/BACKLOG.md`.

- Duplicate arrivals silently drop `quality_flags`: the observation row is
  written only on first arrival and `delivery` has no quality column, so two
  arrivals of one observation that legitimately differ in `quality_flags`
  (the flag derives from `received_time`, which identity excludes) lose the
  later one — a counted defect discarded, against
  `.claude/rules/data-integrity.md`.
- `open_sqlite_event_store`'s path handling is strictly weaker than the raw
  archive beside it: no `resolve()`, no containment check, no `O_NOFOLLOW`
  equivalent, no mode. Measured: a symlinked db path was followed and the
  `-wal`/`-shm` side files were created beside the symlink target, all at
  0644, while the archive writes 0600 and checks `is_relative_to(root)`.
  Requires local write access.
- The store imposes no size bound of its own; `MAX_PAYLOAD_CANONICAL_BYTES`
  lives only in `build_observation_envelope`, and `from_record` accepts
  anything. Measured: a 32 MiB record wrote in 0.441 s at 210 MiB peak RSS
  and read back at 334 MiB. No exposure today because the builder is the
  only production write path. `source_frame_offset` is also unvalidated
  (`-1` accepted; `2**63` raises a bare `OverflowError`).
- The database schema has no identity and no version: `PRAGMA user_version`
  is never set or read, and `CREATE TABLE IF NOT EXISTS` opens a
  differently-shaped pre-existing file silently, failing at the first write
  mid-capture with a generic message. The engineering rule "every public
  schema and persistent record is versioned" is satisfied for records but
  not for the schema itself.
- `write_raw_payload` does `mkdir(parents=True)` but `_fsync_directory`
  syncs only `path.parent`, so for the first payload of a new source the
  file is durable inside a directory whose own entry may not be. Also
  `mkdir` mode is 0755 around files written 0600.
- `SourceProvenanceV1.endpoint` redaction is an existing gap; a credential in
  a query string is now greppable in the database file, once per observation
  (~91k rows/day/token extrapolated) rather than once per run manifest — a
  larger blast radius than when the gap was first filed.
- Payload text is opaque to the store by design (ADR-0011 section 3), so it
  is unneutralized: `get_observation()` faithfully returns live ESC, BEL,
  RLO, ZWSP, and Unicode tag characters out of a payload field. No exposure
  today because `OrderBookSnapshotV1` is all `Decimal`; the first payload
  model with a free-text field reopens the M1 OSC-52 rendering-forgery class
  at the store's own read boundary, against durably stored text — filed as
  the most important open item from this slice (`docs/BACKLOG.md`, L7).
- The denormalized filter columns on `observation`/`rejection` are never
  cross-checked against `record` on read. Not exploitable today since no
  method queries by them; becomes real the moment a query-by-`market_id`
  method ships.
- Nothing detects a `delivery`/`observation` row naming a capture run that
  was never opened. The Python referential check binds only callers going
  through `SQLiteEventStore` and is never re-checked afterward.

## Known limitations from the M2 CLOB REST adapter slice

Recorded here rather than discovered late by the WebSocket adapter or
capture-loop slices that build on this one. Full list, with severity and
trigger, in `docs/BACKLOG.md`.

- A 3xx response with a JSON body is accepted as a successful observation:
  `clob.py` treats every status below 400 as success, and a measured 302
  carrying `{"market":"pwn"}` was accepted with `provenance.http_status=302`.
  The redirect is never followed and the host never changes
  (`follow_redirects=False`), so the body can only come from the host already
  contacted, and provenance records the 302 honestly — auditable after the
  fact, and not a regression: `gamma.py` behaves identically.
- Cross-adapter backoff correlation: `ClobClient` and `GammaClient` at the
  shared default `source_jitter_seed=0` produce byte-identical backoff
  sequences, as do two `ClobClient` instances. The thundering-herd item
  already in the backlog from the pre-M2 security review is now confirmed
  cross-adapter as well as cross-market. Reproducibility remains the right
  trade for a public unauthenticated endpoint; the scope of the existing item
  widened rather than a new risk appearing.
- A substituted response (the source's `asset_id` disagreeing with the
  requested token) is refused, but labelled `MALFORMED_PAYLOAD` — identical
  to a JSON parse failure — so an operator cannot count "the source returned
  a different token's book" as a distinct outcome. Separately,
  `condition_id` on an accepted envelope comes only from the response with no
  cross-check against the M1 Gamma metadata, so ARGOS durably stores a
  token-to-condition binding the source alone asserts.
- `SourceProvenanceV1.endpoint`'s already-backlogged redaction gap now
  materializes once per CLOB observation as well as once per Gamma page. This
  adapter introduces no *new* leak channel — the only query parameter is a
  `[0-9]{1,120}`-validated token id — but the gap is more urgent than before
  simply because it now recurs on a second, higher-volume source.
- Blind spots left by adversarial testing, not yet closed by a test: `market`/
  `asset_id` explicitly `null` in the response is hand-traced through the code
  but has no regression test; a non-string `hash` type is read-verified only,
  not exercised by a test. The sharpest one — **every future payload model
  must reuse the shared decimal normalization rather than reimplement it** —
  is **closed as a structural fact** by the `price_change.v1` slice: the
  helpers are now public and shared (`parse_wire_decimal` /
  `normalize_decimal`, `src/argos/domain/orderbook.py`) and `PriceChangeV1`
  calls them rather than duplicating the logic, so the ADR-0010 decimal class
  did not recur a third time. The reuse turned out to matter in the opposite
  direction too: making one helper serve two schemas is what put enough
  adversarial pressure on it to expose the taxonomy escape and the ambient
  decimal-context dependence described in the `price_change.v1` slice above —
  both of which were already reachable on this REST adapter and neither of
  which two prior security reviews had found.

## Pre-M2 slice: pacing separated from timekeeping (ADR-0009)

Closed the one obligation STATUS previously recorded as gating M2. See
`docs/adr/0009-pacing-separate-from-timekeeping.md` for the full reasoning.
No M2 source adapter was written in this slice; this was the gate preceding
one.

- `Clock` now exposes `now()` only; `@runtime_checkable` is dropped;
  `ReplayClock.sleep` and `LiveClock.sleep` are removed. A new
  `argos.clock.pacing` module holds a `Pacer` protocol (`wait`,
  `move_on_after`) and `RealPacer`. Adapters take a `Pacer` alongside a
  `Clock`.
- Retry jitter is now drawn from an adapter-owned `random.Random`, seeded by
  the new `source_jitter_seed` setting, via a `wait_base` subclass — tenacity's
  `wait_exponential_jitter` has no RNG injection hook. This closes an
  already-committed M1 defect: order-dependent hidden global state, where one
  market's backoff depended on how many sibling markets had already retried in
  the same capture loop, which is the CLAUDE.md-prohibited "hidden global
  state that changes output based on asset processing order".
- **Correction to a claim STATUS previously made in the M1 review-findings
  table.** `GammaClient._get` bounded requests with `anyio.move_on_after`,
  which reads the event loop's monotonic clock, not the injected `Clock`.
  Reproduced directly: five virtual hours of `ReplayClock.sleep` inside a
  0.05 s deadline scope left `cancelled_caught = False`. So the M1 security fix
  "one overall deadline bounds the request, retries included" was only ever
  exercised on the zero-retry path — the existing test pinned
  `http_max_attempts=1`, and its own docstring conceded a virtual clock could
  not demonstrate the property on the retry path. The deadline now runs on the
  injected `Pacer` and is genuinely exercised across backoff, covered by
  `tests/test_gamma_client.py::test_the_overall_deadline_can_fire_during_backoff`.
- Four new boundary tests in `tests/test_boundaries.py`: `Clock` exposes only
  `now`; `ReplayClock` has no pacing methods; no domain/projections/baselines/
  evaluation module imports a pacing name; no `sources`/`ingestion` module
  calls `anyio.sleep`/`anyio.move_on_after` directly (mutation-tested: an
  injected `anyio.sleep()` in `gamma.py` makes it fail, so the check is not
  vacuous).
- `RunManifest` is now `run_manifest.v2`: `mode` is a `RunMode` StrEnum
  (discover/audit/capture/replay/inspect); `working_tree`
  (`WorkingTreeStatus` clean/dirty/unknown) joins `code_revision`;
  `schema_versions` and `input_provenance` (reusing `SourceProvenanceV1`) close
  the rest of core invariant 13. A model validator forbids claiming
  clean/dirty without a proven revision. No `v1` manifest was ever persisted,
  so no migration was written — this is a breaking schema change, not a
  compatible extension.
- Quality gate: PASS — ruff, ruff format, mypy strict on 29 source files,
  594 tests (up from 536).

## M1 closure

| Review | Verdict |
|---|---|
| Architecture | BLOCK → **APPROVE_WITH_FOLLOWUPS** after both blockers were fixed and re-verified against the reviewer's own reproductions |
| Security | **PASS** — both blocking findings re-measured with the original instruments: the gzip bomb peaks at 93 MiB instead of 1.2 GiB, the endless drip stops on the deadline (see correction above: fully exercised only after the pre-M2 pacing slice) |
| Testing | Coverage report acted on; suite 75 → 536 tests across M0 and M1, no xfail |

## M1 exit criteria

| Criterion | Evidence |
|---|---|
| Repeated normalization of the same raw payload is deterministic | `tests/test_gamma_normalizer.py::test_normalization_is_deterministic` |
| Malformed outcome/token mapping cannot enter active selection | Mismatched lengths, duplicate tokens, a condition id in a token slot, and a non-decimal token id all raise `quarantined_mapping` before a `MarketDefinitionV1` exists |
| Original question, description, resolution source, and dates are retained | `MarketDefinitionV1` and `CompiledMarketContractV1` carry them verbatim, linked by `raw_payload_sha256` |
| Compiler never marks a contract human-reviewed automatically | A field validator rejects `human_reviewed` outright, so no code path can set it |
| Network adapter tests use fixtures; unit tests need no internet | `tests/test_gamma_client.py` runs entirely on respx; fixtures carry provenance sidecars and `tests/test_fixtures.py` fails if one drifts from its hash |
| At least one ambiguity test and one token-mapping failure test | `tests/test_compiler.py` (7 ambiguity cases) and `tests/test_gamma_normalizer.py` (8 mapping-failure cases) |

Verified against the live public API on 2026-08-07: `argos markets discover` and
`argos markets audit` both work end to end. Findings from the real payloads are
recorded in `docs/14_POLYMARKET_NOTES.md`.

## M1 review findings

Fixed in this milestone. Two reviews returned BLOCK; both blockers and the
high-severity findings are closed and covered by regression tests.

| Source | Finding | Fix |
|---|---|---|
| Architecture (blocking) | `contract_id` derived from the *page* hash, so one market had two identities depending on which endpoint returned it, and an unrelated sibling's volume minted a new one | Derive from the market's own rule-bearing content; verified across both recorded fixtures |
| Architecture (blocking) | `CompiledMarketContractV1` silently dropped `subject_entities` and `qualifying_event` from its specified contract | Present and empty, like the conditions, with the compatibility decision recorded |
| Security (high) | The 32 MiB cap ran after `.content` buffered and decompressed the body — a 611 KB gzip response reached 1.2 GiB of RSS and *then* reported refusal | Stream with an incremental check and a `content-length` pre-check |
| Security (high) | httpx's read timeout is per chunk, so a slow-drip response hung indefinitely and the retry budget never applied | One overall deadline bounds the request, retries included — **at M1 close this was only proven on the zero-retry path** (`anyio.move_on_after` reads the event loop's monotonic clock, not the injected `Clock`, and the regression test pinned `http_max_attempts=1`); genuinely closed across backoff in the pre-M2 pacing slice via `Pacer.move_on_after`, see above |
| Security + testing (high) | A newline in a market question forged whole sections of the audit, including `review status: human_reviewed`; OSC 52 in a description wrote to the reviewer's clipboard | Sanitize control characters, always block-quote, label source text as untrusted; recorded in `docs/05_RESEARCH_PROTOCOL.md` |
| Security (medium) | `provenance.source` built an archive path unchecked, so `"../outside"` wrote outside the archive | Constrained on the contract and re-checked at the write |
| Security (medium) | `get_market` interpolated an unvalidated id into the URL path, and httpx normalizes dot segments | Validated against an id pattern before any request |
| Testing (medium) | `normalize_markets` iterated a string character by character and raised a bare `TypeError` on a scalar body | A non-list body is refused whole with a reason |
| Testing (medium) | `read_raw_payload` leaked `FileNotFoundError`/`JSONDecodeError`, and a lost sidecar could never be repaired | Both inside the taxonomy; re-archiving restores a missing sidecar; writes are atomic, sidecar first |
| Testing (low-med) | `SourceHealth.retries` counted only on the exhausted-budget path, so a recovered capture reported `retries=0` | Counted per retry |
| Testing (low) | Duplicate token detection compared strings, so `"007"` and `"7"` passed as distinct | Compared by integer value |
| Both | Dead code presenting as a decision: an unreachable `MACHINE_CHECKED` branch and an unreachable condition-id/token-id check | Removed, with the real guarantee documented where it is actually enforced |

## Known gaps at M1

- Discovery fetches a single page; pagination and a documented stopping rule are
  carried into M2.
- Normalized and quarantined records are reported but not persisted — only the
  raw payload is archived. The durable store is an M2 deliverable.
- The raw archive in `argos.store` is deliberately minimal and makes no
  durability or replay claim.
- A discovery run emits no run manifest yet, so a sample is not yet linked to the
  configuration that produced it.

## Known limitations from the pre-M2 pacing slice

- `RunManifest.input_provenance` is an unbounded tuple of `SourceProvenanceV1`.
  That is acceptable at M1 discovery scale (one record per fetched page/market)
  but an M2 capture manifest must **not** embed one provenance record per
  ingested event — event volumes make that unbounded in a way discovery's
  never was. The M2 capture-manifest design must reference the event store
  instead of enumerating provenance inline. Filed as a constraint, not yet a
  fix, so it is not discovered late.
- There is no `VirtualPacer`. Accelerated and stepwise replay pacing is M3
  scheduler work (`argos.replay`, per ADR-0009's consequences section) and was
  deliberately not built in this slice, to avoid scope drift into a milestone
  that has not started.

## Pre-M2 security review

Verdict: **PASS, no blockers.** Both M1 high-severity properties were
re-measured rather than re-read, and both survive the pacer rewrite: a 1.2 GiB
gzip bomb is refused at 45 MB accumulated with peak RSS 116 MiB, and the
overall deadline bounded a 3-attempt call at 20.90 s against a 20.9 s budget.
The seeded jitter cannot exceed `MAX_BACKOFF_SECONDS` at any legal
configuration, so it cannot outrun the deadline budget.

Two MEDIUM findings landed on the `working_tree` field this slice introduced,
and both are **fixed here** rather than deferred, because a manifest that
positively asserts the wrong provenance is worse than one that admits it does
not know:

| Finding | Reproduced | Fix |
|---|---|---|
| `git status --porcelain` reported **clean** for a genuinely modified tree — via `assume-unchanged`/`skip-worktree` index bits, and via `status.showUntrackedFiles=no` injected through `GIT_CONFIG_*` (also a real developer performance setting, so the accidental path is the likely one) | Yes — file on disk read `TAMPERED` while status printed nothing | Index bits are treated as unknowable (`UNKNOWN`, never `CLEAN`); untracked mode pinned on the command line; the git environment is scrubbed |
| `GIT_DIR` defeated the M0 toplevel check: git reports cwd as `--show-toplevel` while `HEAD` comes from a foreign repository | Yes — returned a throwaway repository's HEAD instead of this one's | Same environment scrub, applied to both git calls |

Six regression tests cover these, including one asserting a genuinely clean
tree is still reported `CLEAN` — the guard must not be degenerate. Provenance
probes now run with `--no-optional-locks`, so collecting provenance no longer
writes to `.git/index`.

Three LOW findings are recorded in `docs/BACKLOG.md` rather than fixed, each
with no current exposure: the shared jitter seed becoming a thundering herd
once a capture loop exists, `SourceProvenanceV1.endpoint` persisting a full
URL with no redaction contract, and a validator message that could embed a
path. Predictable retry timing against a public unauthenticated endpoint was
assessed and is **not** treated as a risk; reproducibility is the right trade.

## M0 checklist

- [x] Run `/bootstrap` and create initial task plan.
- [x] Generate `uv.lock` from a clean environment.
- [x] Create package boundaries from `02_ARCHITECTURE.md`.
- [x] Implement immutable settings and execution-disabled validation.
- [x] Implement Clock protocol, LiveClock, and ReplayClock contracts.
- [x] Establish structured logging and error taxonomy.
- [x] Confirm CI and local quality commands pass.
- [x] Add ADR index.
- [x] Architecture review complete.
- [x] Security review complete.
- [x] Milestone summary committed.

## Evidence

| Exit criterion | Evidence |
|---|---|
| `uv sync --all-groups` succeeds from a clean checkout | `uv.lock` committed; sync run on Python 3.13 with the 3.12 floor from `pyproject.toml` |
| `ruff check`, `ruff format --check`, `mypy`, `pytest` pass | `uv run python scripts/claude/quality_gate.py` → PASS (250 tests) |
| Domain package imports no HTTP/database/UI packages | `tests/test_boundaries.py` — AST scan for infrastructure imports, outer-layer imports, environment reads, and wall-clock calls |
| `LiveClock` and `ReplayClock` contracts have tests | `tests/test_clock.py` and `tests/test_properties.py` — UTC anchoring across the tz database, DST folds, regression refusal, non-finite durations, reproducibility |
| Configuration rejects `ARGOS_EXECUTION_ENABLED=true` | `tests/test_config.py` — every truthy spelling (`true/True/TRUE/1/yes/on/t/y`) rejected with `argos.execution_prohibited`; falsy spellings load; `maybe` is a config error, not a silent false |
| Architecture and security agents report no blockers | Architecture: **APPROVE_WITH_FOLLOWUPS** after two blocking findings were fixed and re-verified. Security: **PASS**, no blockers. Testing: coverage report acted on; suite 75 → 250 tests |

Additional guardrails added beyond the checklist:

- `tests/test_boundaries.py::test_no_execution_surface_in_source` scans declared
  names across `src/argos` for wallet/order/credential identifiers;
- unrecognized `ARGOS_*` environment variables fail the load instead of being
  silently ignored;
- `RunManifest` (`run_manifest.v1`) records config fingerprint, code revision, and
  settings snapshot, and is stamped from an injected clock so replay runs
  reproduce it exactly;
- `tests/conftest.py` strips ambient `ARGOS_*` variables, so the execution-prohibition
  test cannot pass for the wrong reason on a developer machine.

## M0 review findings

Fixed in this milestone:

| Source | Finding | Fix |
|---|---|---|
| Architecture (blocking) | `__init_subclass__` used `getattr`, so a subclass inherited its parent's `schema_version` and would write mislabelled records | Check `cls.__dict__` |
| Architecture (blocking) | `frozen=True` left `settings_snapshot` mutable, and the mutation reached `to_record()` | `freeze`/`thaw` with a deep-copyable, picklable, hashable `FrozenDict` |
| Testing | `RunManifest.created_at` accepted naive timestamps and kept non-UTC offsets, so equal instants serialized differently | `ensure_utc` field validator |
| Testing | `nan` slipped past the `seconds < 0` guard and would hang `anyio.sleep` | `InvalidDurationError(ClockError, ValueError)` |
| Security (medium) | git discovers repositories upward, so a wheel install could stamp manifests with a foreign repository's HEAD | Verify `--show-toplevel` before trusting `HEAD` |
| Security (low) | pydantic embeds the offending value in its message, so a credential pasted into the wrong variable reached stderr and CI logs | Report field name and error type only |
| Security (low) | `http_timeout_seconds` and `http_max_attempts` had floors but no ceilings | `le=120` and `le=10` |

Deferred debt is filed in `docs/BACKLOG.md` under "Carried from the M0 closure
reviews", each item tagged with the milestone it must close by.

## Active blockers

None.

## Known limitations at M0

- Storage engine is undecided; the SQLite/WAL vs append-log comparison required by
  `docs/12_TECH_STACK.md` needs real M2 capture volumes and will land as an ADR.
  (Historical, M0-era limitation. Resolved 2026-08-11 by ADR-0011 — SQLite/WAL,
  on atomicity, not volume — see "M2 slice: idempotent SQLite event store"
  above.)
- The `sources`, `ingestion`, `store`, `projections`, `compiler`, `replay`,
  `baselines`, `resolution`, and `evaluation` packages are documented boundaries
  only; they contain no implementation yet.
- **CI has still never run on any ARGOS commit.** `m1-market-discovery` is now
  pushed (10 commits, through the `price_change.v1` slice), but
  `.github/workflows/ci.yml` triggers only on `pull_request` and on `push` to
  `main`, so pushing a feature branch runs nothing — `gh run list` reports zero
  workflow runs for the repository. Every quality gate recorded in this file is
  therefore a **local** result on one machine, at one Python version (3.13,
  against a 3.12 floor), and has never been reproduced on a clean checkout by
  an independent runner. Opening a pull request is what would trigger it; that
  is an owner-visible action and has not been taken. Recorded here as an
  evidence gap rather than a task, because "the gate passes" currently means
  less than it appears to.
- Container immutability on `VersionedModel` is opt-in per field, not structural.
  `RunManifest` opts in; a future subclass with a bare `dict` field would not.
- Branch coverage is unmeasured — `pytest-cov` is not installed and the thresholds
  in `docs/13_TEST_STRATEGY.md` are therefore unverified.

## Next owner action

No action required until the M4 owner gate unless Claude records a true blocker.
