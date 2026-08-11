# ARGOS status

Last updated: 2026-08-11

## Current state

- Current milestone: **M2 — CLOB capture — in progress**. M0 and M1 closed. The
  pacing-versus-timekeeping ADR that gated M2 (ADR-0009) is resolved and merged.
  Five M2 vertical slices have landed: the canonical `ObservationEnvelopeV1` /
  `RejectedObservationV1` contracts and their identity derivation (ADR-0010);
  the first typed payload, `OrderBookSnapshotV1`; a security review of both
  contracts (verdict **PASS_WITH_FINDINGS**, no blocker, findings closed in
  `1fb057c`); a public CLOB WebSocket market-channel research note built from
  live capture, not documentation alone; and the idempotent, append-only
  SQLite event store and delivery record specified by ADR-0011, committed as
  `25c6f05`. **No CLOB adapter, no WebSocket adapter, and no capture CLI exist
  yet** — the store enforces idempotent insert, the rejection ledger, and
  interrupted-run detection at the schema level, but nothing feeds it real
  traffic yet.
- Autonomous target: **complete M0-M4**
- Owner gate: **required after M4**
- Execution capability: **prohibited and absent**
- External evidence / LLM forecasting: **not started**

## Current objective

Build the CLOB REST snapshot adapter, using `Pacer` (not `Clock`) for retry
backoff per ADR-0009, `ObservationEnvelopeV1` / `RejectedObservationV1`
(ADR-0010) as the boundary contract it produces, and the SQLite `EventStore`
(ADR-0011) as the boundary it writes to. The event store itself is built and
reviewed (`src/argos/store/event_store.py`); no adapter or capture loop calls
it yet, so the store's schema-level guarantees have no real traffic behind
them.

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

Tracking `docs/07_MILESTONES.md`. Two criteria now have store-level evidence
from the ADR-0011 event-store slice, one criterion has store-only partial
evidence, and the rest have no adapter or capture loop yet to produce
evidence against and are listed as open rather than implied closed. No CLOB
adapter, no WebSocket adapter, and no capture CLI exist yet — the store
enforces these properties at the schema level; nothing feeds it real traffic
yet.

| Criterion | Status | Evidence |
|---|---|---|
| Duplicate source event does not create a second accepted observation | **Store-level evidence; no adapter feeds it yet** | `_observation_identity` collides an identical redelivery onto one `observation_id`, on the real recorded CLOB payload as well as a constructed one (`docs/research/m2-clob-rest-book.md`, "Consequence for `ObservationEnvelopeV1`"). `SQLiteEventStore.append_observation` now enforces it: a redelivery inserts zero second `observation` rows and exactly one `delivery` row with `disposition="duplicate"`, both writes inside one `BEGIN IMMEDIATE` transaction (`tests/test_event_store.py`, `tests/test_event_store_adversarial.py`, including real multi-connection race tests). **Not yet closed**: no CLOB or WebSocket adapter and no capture loop write through this store against real traffic |
| Zero-size level update is represented as removal | Open | Not yet built. `OrderBookSnapshotV1` implements the REST-snapshot side of the convention (a zero-size level is dropped and recorded as an anomaly), and the WebSocket research note now confirms the delta-stream convention directly on live traffic, including a batch of three removals in one update (`docs/research/m2-clob-websocket.md`, "Priority question 3"). **Not yet closed**: no `price_change` payload model, WebSocket adapter, or capture loop exists to apply this to a real delta stream |
| Reconnect does not reset ingest sequence or silently lose manifest state | Open | No WebSocket adapter and no capture manifest exist yet |
| Invalid messages enter a rejection ledger with reason and raw hash | **Store-level evidence; no adapter feeds it yet** | `RejectedObservationV1` carries `reason: RejectionReason`, `detail`, and `raw_payload_sha256`; `build_rejected_observation` derives a deterministic `rejection_id` so redelivery of the same invalid bytes for the same reason collapses rather than growing the ledger unbounded. `SQLiteEventStore.append_rejection`/`iter_rejections` now persist it, keyed `(capture_run_id, ingest_sequence)` rather than on `rejection_id` alone, so two genuinely different malformed entries in one frame that happen to share one `rejection_id` (ADR-0011 section 7) both survive instead of one silently overwriting the other. **Not yet closed**: nothing writes to it from real input — no CLOB or WebSocket adapter produces rejections yet |
| Book snapshot plus deltas reconstruct a tested projection | Open | No projection, no adapter |
| No authenticated/user channel or trading code exists | Holds | Unchanged from M0-M1; this slice added no network code at all — only domain contracts, a research note, and (in the event-store slice) a storage adapter built against a local SQLite file, not a network source |
| An interrupted capture closes or marks its manifest incomplete | **Store half only** | `capture_run` is append-only — opening inserts a row, closing inserts a second row, and "not yet closed" is a derived read via `iter_open_capture_runs`, with two partial unique indexes making double-open/double-close impossible at the schema level (ADR-0011 section 5, including its dated Correction for why the table is append-only rather than update-in-place). **Not yet closed**: no capture loop or manifest writer exists to open or close a real run |

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
- CI has not yet run on the remote branch for this slice; the gate is verified
  locally only, and the branch has not been pushed.
- Container immutability on `VersionedModel` is opt-in per field, not structural.
  `RunManifest` opts in; a future subclass with a bare `dict` field would not.
- Branch coverage is unmeasured — `pytest-cov` is not installed and the thresholds
  in `docs/13_TEST_STRATEGY.md` are therefore unverified.

## Next owner action

No action required until the M4 owner gate unless Claude records a true blocker.
