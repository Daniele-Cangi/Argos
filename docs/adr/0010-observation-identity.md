# ADR-0010: What an observation is, and what its identity means

- Status: Accepted
- Date: 2026-08-10
- Supersedes nothing. Extends ADR-0004.

## Context

ADR-0004 decided that observations are append-only and source-linked. It did not
decide what an observation *is*, and the M2 exit criterion "duplicate source
event does not create a second accepted observation" cannot be satisfied without
that definition: an idempotent insert needs to know what makes two deliveries the
same event.

Three drafts of the identity derivation were written and two were rejected, each
after reproducing a concrete silent-loss failure rather than after review
argument. That history is the substance of this decision and is recorded here so
the rejected options are not re-proposed.

## Decision

### 1. An `observation_id` identifies a *source message*, not a market state

It is derived from every stable discriminator the message carries: the source,
the source's own message-kind label, whichever market/condition/token the message
names, the payload's schema version, the event-time marker, the source's sequence
and content hash when supplied, and a hash of the normalized payload.

They are **combined, not ranked**.

The rejected alternative ranked the source hash above the source sequence above
payload content. Against the real Polymarket CLOB book endpoint that is unsafe,
and the failure was measured, not hypothesized (see
`docs/research/m2-clob-rest-book.md`): the endpoint returns a `hash` covering the
book's content, so a book moving A → B → A returns `hash(A)` twice. The third
observation would have collided with the first and been refused as a duplicate,
erasing the fact that the book was back at A. A source sequence that resets after
a reconnect — which the M2 exit criteria explicitly anticipate — collides two
genuinely different events the same way.

The objection that combining discriminators floods the store with near-duplicates
was also measured and does not hold for this source: polling an unchanged book
returns an identical `timestamp` and `hash`, so re-polls collide onto one
identity as intended.

### 2. `ingest_sequence`, `received_time`, and `capture_run_id` are excluded

The first two differ on every redelivery of the identical event by construction,
so including either would make a duplicate unable to collide and the M2 criterion
unreachable. `capture_run_id` is excluded because a second capture session
observing the same event is observing the same event; scoping identity per run
would admit the same bytes twice.

### 3. The encoding is injective

Fields are length-prefixed before hashing, and absence is a marker no present
value can produce. Joining source-controlled strings on a separator is not
injective: a reproduced collision showed `source_event_type="book\x1fmarket-1"`
with no market producing the same identity as `source_event_type="book"` with
`market_id="market-1\x1f"`. Independent adversarial testing found the same class
on the `market_id`/`condition_id` and `condition_id`/`token_id` boundaries and in
the rejection ledger.

### 4. Identity is derived from validated values, so it can be recomputed

`recompute_observation_id` re-derives the id from the stored record. An identity
that cannot be recomputed cannot be audited, and a store whose keys nobody can
verify is a store that has to be trusted rather than checked.

### 5. The payload is stored in canonical JSON form, typed on the way out

The envelope is not pydantic-generic; the event store and the M3 replay reader
must not become generic to read heterogeneous events. The type erasure is paid
for at the **write** boundary instead: the builder takes a typed
`VersionedModel`, derives `payload_schema_version` from it, and stores its
canonical mapping. A caller therefore cannot label one payload type as another —
a mismatch that previously surfaced only when `read_payload` ran during replay,
against a capture that can no longer be re-taken.

Storing the canonical form also keeps live and replayed envelopes identical.
While the envelope held live objects, `to_record()` dumped in JSON mode and a
`Decimal('0.5')` reloaded as `'0.5'`: the replayed envelope differed from the
live one *while carrying the same `observation_id`*. That is core invariant 5
broken, and M3's identical-hash criterion runs through this object.

## Consequences

- A payload model **must** normalize `Decimal` scale. `Decimal("0.430")` and
  `Decimal("0.43")` serialize to different text and therefore mint different
  identities. The CLOB book endpoint really does report the same price at two
  precisions across two endpoints, so this is a live hazard, not a theoretical
  one. It is a constraint on every future payload model.
- Identity depends on the normalized payload, and `parser_version` is
  deliberately outside the identity material. Reprocessing the same raw bytes
  under a corrected parser therefore mints new identities with no link to the
  originals. ADR-0004 makes superseding records the correction mechanism, but
  `ObservationEnvelopeV1` has no `supersedes_observation_id` field yet. **This is
  an open gap**, filed in `docs/BACKLOG.md` against the store slice.
- Two source messages that are genuinely different but share every stable field —
  same scope, no sequence or hash, identical payload, equal or absent event times
  — remain indistinguishable. That is irreducible without information the source
  did not send. A source whose payload carries its own timestamp does not reach
  it.
- The store must still decide what a *delivery* record is. `docs/02_ARCHITECTURE.md`
  requires duplicate inserts to be idempotent **and observable**, and per-run
  `ingest_sequence` is what M3 replays by. Because identity excludes both, a
  collapsed duplicate currently has nowhere to record its own `received_time` and
  `ingest_sequence`, and `RejectedObservationV1` cannot point at the accepted twin
  it duplicates. **The store slice must decide this before it writes a row**; it
  is not decided here because it is a storage-shape question, not an identity one.
- `SourceProvenanceV1` is embedded per observation rather than referenced by
  `raw_payload_sha256`. Embedding keeps an observation self-contained for audit
  and survives compaction, at the cost of N identical copies when one response
  yields N observations. This is a deliberate choice, revisitable behind the
  store protocol, and it does not conflict with the backlog constraint about
  unbounded provenance — that constraint is about a single run manifest growing
  without limit, and putting provenance on the append-only event is the escape
  from it, not an instance of it.
