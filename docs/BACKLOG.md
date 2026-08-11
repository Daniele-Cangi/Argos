# Backlog

Work top to bottom unless a milestone dependency requires reordering.

## Now — M0

- [x] Verify Claude settings, agents, skills, and hooks load.
- [x] Lock dependencies.
- [x] Create package module skeleton.
- [x] Settings and run-manifest contract.
- [x] Clock abstraction.
- [x] Error taxonomy and structured logging.
- [x] CI green from clean checkout (verified locally; remote CI run pending).
- [x] ADR index and M0 closure review (architecture + security + testing).

## Carried from the M0 closure reviews

Raised by the architecture, security, and testing reviews of the M0 foundation
slice. None blocks M0; the ones marked **before M1/M2/M3** must close before the
milestone named, because later code would inherit the defect.

- [x] **ADR before M2** — reconcile `Clock.sleep` with ADR-0003's "`ReplayClock`
      controlled only by the replay scheduler". Closed by ADR-0009: `Clock` lost
      `sleep()` entirely (it now exposes only `now()`), and a new `Pacer` port
      owns all real elapsed time. "An adapter moves replay time" is now
      impossible rather than forbidden — `ReplayClock` has no mutating pacing
      method.
- [ ] **Before M3** — decide what `config_fingerprint` covers. It currently includes
      `data_dir`, but `docs/02_ARCHITECTURE.md` allows output location to differ between
      live and replay, so an otherwise identical replay gets a different fingerprint.
- [x] **Before M2** — `RunManifest` needs schema versions (plural), data provenance, and
      a `mode` enum instead of a free-form string, to satisfy invariant 13 in full.
      Closed: `run_manifest.v2` adds `RunMode` (StrEnum), `schema_versions`
      (deduplicated, sorted tuple), and `input_provenance` (sorted tuple of
      `SourceProvenanceV1`). No `v1` manifest was ever persisted, so this is a
      breaking change with no migration, not a compatible extension. See the
      new known limitation on `input_provenance` at capture scale, filed under
      "Carried into M2 from M1" below.
- [x] **Before M2** — record working-tree state alongside `code_revision`; a dirty tree
      currently yields a manifest that misattributes the code that produced the run.
      Closed: `WorkingTreeStatus` (clean/dirty/unknown) joins `code_revision`, and a
      model validator forbids claiming clean/dirty without a proven revision.
- [ ] **Before M2** — make the container freeze structural on `VersionedModel`, via a
      `FrozenPayload` annotated type or a boundary test forbidding bare `dict`/`list`/
      `set` fields on subclasses. Today a subclass must repeat the validator/serializer
      pair, and `ObservationEnvelope.raw_payload` would inherit exactly that gap.
- [ ] Add an explicit re-validating `supersede()` to `VersionedModel`.
      `model_copy(update=...)` skips the freeze and the validators, and ADR-0004 makes
      superseding records the correction mechanism — so it is the API someone will
      reach for.
- [ ] `freeze` passes `set`, `frozenset`, `bytearray`, and arbitrary objects through
      unfrozen, and `thaw` turns a genuine `tuple` into a `list`. Both are harmless for
      `model_dump(mode="json")` payloads; revisit if a non-JSON payload is ever stored.
- [ ] Move `configure_logging` to the composition root (the Typer callback) instead of
      the `manifest` subcommand; `structlog.configure` is process-global.
- [ ] `REPO_ROOT = parents[2]` assumes the src layout; under a wheel install `argos
      status` cannot find `docs/STATUS.md`.
- [ ] Harden the boundary scan: forbid `os`/`pathlib` in `domain`. The `Clock`/`LiveClock`
      half of this item is done as a side effect of ADR-0009: `Clock` now exposes only
      `now()` and is a plain, minimal protocol. What remains: `argos.clock`'s
      `__init__.py` re-exports `Pacer`/`RealPacer` (the `anyio`-backed live pacer) "for
      convenience", so importing the *package* still pulls an anyio-backed
      implementation into the graph — only importing the `Clock` name itself does not.
      The boundary test (`test_no_pacing_import_outside_live_adapters`), not the import
      graph, is what actually keeps pacing out of domain code today.
- [ ] Type-check tests, not only `src` — `docs/08_DEFINITION_OF_DONE.md` requires strict
      typing on touched code and the tests are touched code.
- [ ] Add `pytest-cov` and enforce the branch-coverage thresholds in
      `docs/13_TEST_STRATEGY.md`; that criterion is currently unverifiable.
- [ ] `Settings.model_construct` / `model_copy(update=...)` skip the execution-flag
      validator. Harmless today (nothing reads the flag); add regression tests or drop
      the field before any adapter branches on it.
- [ ] Make `snapshot()` structurally safe against a future credential field — exclude
      `SecretStr` fields rather than relying on the docstring, and add a field allowlist
      test so a new setting forces deliberate review.
- [ ] Decide whether `http://` and `ws://` endpoint overrides should be rejected
      outright, and whether a `.env` file should be loaded — `.env.example` implies a
      workflow that `Settings` does not currently support.
- [ ] Test `_code_revision` against a temporary git repository, including the
      detached-HEAD and no-repo branches.
- [ ] Sub-microsecond `ReplayClock.advance_by` truncates to no movement; revisit if M3
      ever schedules sub-µs gaps.

## Now — M1

- [x] Official API research note refresh (verified against the live API 2026-08-07).
- [x] Gamma client port and adapter.
- [x] Raw response fixture format (payload + provenance sidecar, hash-checked).
- [x] MarketDefinition normalization.
- [x] Binary market selector.
- [x] Token/outcome quarantine.
- [x] Contract compiler skeleton.
- [x] Market audit CLI.
- [x] M1 closure review (architecture + security + testing).

### Carried from the M1 closure reviews

- [x] **ADR before M2** — separate pacing from timekeeping on the `Clock` protocol.
      Closed by ADR-0009 (Accepted): `Clock` keeps `now()` only; a new `Pacer` port
      (`wait`, `move_on_after`; `RealPacer` backed by `anyio`) owns all real elapsed
      time and is live-only. Retry jitter is drawn from an adapter-owned, seeded
      `random.Random` via a `wait_base` subclass, closing the unseeded-global-RNG
      hidden-state defect as well. Four boundary tests guard the regression;
      `docs/DECISION_LOG.md` records the decision.
- [ ] Revisit where the `human_reviewed` prohibition lives. Today a field validator
      makes the value unconstructible, which also means the future human-review flow
      cannot build the record and a stored `human_reviewed` contract cannot be
      deserialized. Keep it until that flow exists, then move the check to the
      compiler and let the review flow write the verdict.
- [x] Symlink handling in the archive. Closed in M1 rather than deferred: the
      resolve check refuses a symlinked directory, `os.replace` replaces a symlink
      at the target instead of writing through it, and the temp file is created
      with `O_CREAT | O_EXCL | O_NOFOLLOW`.
- [ ] The rendered audit is asserted by substring, never against a golden file.
- [ ] **Before M2** — `supersedes_contract_id` is never populated by any caller. It
      becomes load-bearing the moment a second contract is persisted for the same
      market: ADR-0004 makes superseding records the correction mechanism, so the
      chain has to start forming when the store lands.
- [x] `_write_atomically` renames without an `fsync`, so a power loss can make the
      rename durable before the contents. Acceptable for an archive that disclaims
      durability; the M2 event store cannot inherit it. Closed in the ADR-0011
      event-store slice: `os.replace` and the new directory `fsync` are now
      inside the write guard.
- [ ] Duplicate market ids inside one page are accepted twice with no dedup counter;
      it belongs with the M2 idempotent store.
- [ ] The Hypothesis market strategy generates only payloads the normalizer accepts,
      so it exercises the accounting invariants and not the quarantine path.

### Carried into M2 from M1

- [ ] Paginate discovery. `list_markets` fetches one page; a research sample
      larger than one page needs cursor handling and a documented stopping rule.
- [ ] Persist `MarketDefinitionV1` and `QuarantinedMarketV1` records. They are
      currently computed and reported but only the raw payload is archived.
- [x] The raw archive in `argos.store` is deliberately minimal and is not the
      event store M2 requires; decide whether it survives or is absorbed.
      Decided in ADR-0011 section 4: it survives. Raw bytes stay
      content-addressed on disk; the event store references them by
      `raw_payload_sha256`/`raw_payload_location` rather than absorbing them
      into a row.
- [ ] Discovery does not yet emit a run manifest linking sample to configuration.
- [ ] **Constraint on the M2 capture-manifest design** — `RunManifest.input_provenance`
      is an unbounded tuple of `SourceProvenanceV1`, fine at M1 discovery scale (one
      record per fetched page/market). An M2 capture manifest must not embed one
      provenance record per ingested event; it must reference the event store instead.
      Decide the reference shape before the capture manifest is built, not after.
- [ ] M2's CLOB REST and WebSocket adapters take a `Pacer` (per ADR-0009), not `Clock`,
      for retry/backoff/reconnect waits, and their own seeded `random.Random` for
      jitter. A shared bounded-retry helper is justified once the second client exists.

### Carried from the pre-M2 security review

- [ ] **Before the M2 capture loop** — `source_jitter_seed` lives on `Settings`, so
      every `GammaClient` in a process draws the *identical* backoff sequence. Today
      the CLI builds one client per invocation, so it is inert. A capture loop
      constructing one client per market would have them all retry at the same
      instants — a self-synchronized thundering herd against an already-degraded
      endpoint, which is worse politeness than the module-global RNG it replaced.
      Mix a per-client discriminator into the seed when the loop lands, and keep it
      recorded so the run stays reproducible. Not a security issue: predictable
      retry timing against a public unauthenticated read endpoint has no attacker
      value, and reproducibility is the right trade.
- [ ] **Before the first source with a credential in its URL** —
      `SourceProvenanceV1.endpoint` stores the full request URL including the query
      string, and `input_provenance` now persists it in the run manifest. A planted
      `?token=SUPERSECRET` round-trips verbatim. No exposure today (Gamma is
      unauthenticated and nothing populates the field yet), but unlike
      `settings_snapshot` this persisted surface carries no "exclude credentials"
      contract. Stripping the query string is the wrong fix — `limit`/`offset` are
      legitimate provenance. Needs a redaction rule at the boundary that writes it.
      Note a static boundary test cannot catch this: the risk is runtime URL
      content, not a declared field name.
- [ ] The `_working_tree_needs_a_trusted_revision` model validator embeds the input
      dict in its pydantic message. Unreachable from the CLI (every
      `_code_revision` failure path returns `(None, UNKNOWN)`) and pydantic
      truncated the value before `data_dir` in review, but it is one more place a
      filesystem path could reach stderr.

## Now — M2

- [x] `ObservationEnvelopeV1` / `RejectedObservationV1` canonical contracts and
      deterministic identity derivation (ADR-0010).
- [x] Public CLOB REST order-book research note, from recorded live payloads
      (`docs/research/m2-clob-rest-book.md`).
- [x] CLOB snapshot adapter. Closed: `src/argos/sources/clob.py`
      (`ClobClient`) and `src/argos/ingestion/clob_book.py`
      (`normalize_clob_book`) — a real recorded response now goes through
      `ClobClient` → `normalize_clob_book` → `SQLiteEventStore` end to end.
      `ingest_sequence` allocation, a capture manifest, and a capture CLI
      remain open, listed separately below.
- [ ] WebSocket lifecycle and subscriptions.
- [ ] Canonical market-data payloads (order book, price change, etc. — typed
      `VersionedModel`s that `build_observation_envelope` takes as `payload`).
- [x] Event store, including the delivery-record shape decision below.
      Closed by ADR-0011 (`docs/adr/0011-sqlite-event-store-and-delivery-record.md`,
      `src/argos/store/event_store.py`). No adapter or capture loop writes
      through it yet.
- [ ] Capture manifest and health metrics.
- [ ] Capture CLI and integration fixture.

### Carried from the M2 observation-identity slice (ADR-0010)

- [x] **Before the first payload model lands** — normalize `Decimal` scale
      inside the payload model, not just at the boundary. `Decimal("0.430")`
      and `Decimal("0.43")` currently serialize to different canonical text and
      mint different `observation_id`s
      (`tests/test_observation_envelope_adversarial.py::test_decimal_trailing_zero_precision_changes_identity`).
      The CLOB endpoint really reports the same price at two precisions across
      `/book` and `/last-trade-price` (`docs/research/m2-clob-rest-book.md`,
      "Decimal hygiene"), so this is a live hazard on the very first payload
      type, not a theoretical one. Closed by `OrderBookSnapshotV1._normalize_decimal`.
      **Not fully closed as a general guarantee**: the same function had a
      second, independent instance of this class — negative zero
      (`Decimal("-0")`) rendering as `"-0"` rather than `"0"` — found by
      adversarial testing during the CLOB REST adapter slice and fixed there.
      Two independent instances in one function make this a pattern to guard
      against on every future payload model (see the CLOB REST adapter slice
      backlog item below), not a closed incident.
- [ ] **Before reprocessing under a corrected parser is possible** — add
      `supersedes_observation_id` to `ObservationEnvelopeV1`. Identity excludes
      `parser_version` by design, so reprocessing the same raw bytes under a
      fixed parser mints a new, unlinked identity today. ADR-0004 requires
      superseding records as the correction mechanism; the field does not
      exist yet.
- [x] **Before the store writes a row** — decide the delivery-record shape.
      `observation_id` deliberately excludes `capture_run_id` and
      `ingest_sequence` (both would make a duplicate unable to collide,
      making the M2 duplicate-detection exit criterion unreachable), so a
      collapsed duplicate currently has nowhere to record its own
      `received_time`/`ingest_sequence`, and `RejectedObservationV1` cannot
      point at the accepted twin it duplicates.
      `docs/02_ARCHITECTURE.md` requires duplicate inserts to be idempotent
      **and observable**; this is a storage-shape question the identity ADR
      deliberately left open (ADR-0010, "Consequences"). Closed by ADR-0011
      section 5: one `delivery` row per arrival, keyed `(capture_run_id,
      ingest_sequence)`, carrying `observation_id`, `received_time`, and a
      `disposition` of `accepted_new`/`duplicate`.
- [ ] **Before M3 dispatch** — no `payload_schema_version` -> model registry
      exists. `read_payload` takes an explicit `model` argument; dispatch
      across multiple payload types during replay will otherwise grow ad hoc.
- [ ] `read_payload` hard-matches exactly one `payload_schema_version` instead
      of accepting a set via `ensure_supported_version`; no reader can accept
      more than one payload version yet.
- [ ] **Before the capture loop is trusted** — close the research doc's
      UNVERIFIED list: rate limits, the `/books` batch endpoint's existence and
      shape, response headers (caching/rate-limit/`content-encoding`),
      zero-size levels in a REST snapshot (never observed), and behaviour under
      a paused/halted market as distinct from a closed one
      (`docs/research/m2-clob-rest-book.md`, "UNVERIFIED").
- [ ] The WebSocket research slice must answer: does the market channel supply
      a sequence number or only `timestamp`/`hash`; does a delta carry the hash
      of the book state it produces; does zero-size mean removal on the delta
      stream (this is where the corresponding M2 exit criterion actually
      lives, since it was never observed on REST)
      (`docs/research/m2-clob-rest-book.md`, "Next questions for the WebSocket
      research slice"). **Answered** in `docs/research/m2-clob-websocket.md`
      against live traffic: no sequence number; a delta's `hash` is the hash of
      the resulting book state and reconciles exactly with REST; zero-size means
      removal, observed directly.

### Carried from the M2 security review (verdict PASS_WITH_FINDINGS)

Deliberately **not** fixed in the slice that found them, each with the reason.
None is a blocker; all are recorded so they are chosen rather than forgotten.

- [ ] **The covert channel through stored text is open and is not closable
      here.** Security review demonstrated a 31-byte instruction encoded into
      variation selectors (U+FE00-FE0F, U+E0100-E01EF) surviving into a rendered
      audit with a zero-glyph visible difference — higher bandwidth than the tag
      block this slice did close, and the technique that became prominent
      *because* tag-block filtering became common. ZWJ/ZWNJ carry the same
      channel at one bit per codepoint. Not filtered because variation selectors
      are load-bearing for CJK ideographic variants and emoji presentation, and
      ZWJ/ZWNJ for Indic and Perso-Arabic scripts: filtering them would corrupt
      legitimate market text while an encoder routes around it through the ~60
      remaining format characters, or through the Hangul fillers, which are
      invisible but category `Lo` and so are not reachable by a category sweep
      at all. **This is a detection problem for the layer that consumes the
      text, and must be treated as unsolved by anything that reads these
      records.** It becomes load-bearing at the M4 gate and at M5, where an
      external-evidence or LLM layer would read exactly this stored text — see
      `docs/adr/0005-no-llm-forecast-core.md`. `src/argos/domain/text.py`
      states the residual in its own docstring rather than claiming a closed
      channel.
- [ ] `SourceProvenanceV1.http_status` is now nullable, which makes "non-HTTP
      transport" indistinguishable from "the adapter forgot to set it". There is
      no transport discriminator, so one silent-substitution risk was replaced by
      another inside the contract whose job is provenance. Close when the
      WebSocket adapter lands and a transport field has a real second value.
- [ ] `schema_version` uniqueness is unenforced across `VersionedModel`
      subclasses. Two classes both declaring `order_book_snapshot.v1` defeat
      `read_payload`'s version check — review built a `Trade` out of a book
      envelope with no error. A registry check in `__init_subclass__` closes it.
- [x] Neither `observation_id` nor `rejection_id` is enforced by a validator: a
      forged id round-trips through `from_record` while `recompute_*` disagrees.
      The recompute functions exist; nothing obliges a reader to call them. The
      store slice is the right place to make verification mandatory on read.
      Closed by the ADR-0011 event store (section 8): `SQLiteEventStore` calls
      `recompute_observation_id`/`recompute_rejection_id` on both write and
      read.
- [ ] `RejectedObservationV1.detail` retains newlines by design, and the M1
      defence was the sanitizer **plus** block-quoting — only the sanitizer
      carried across. No ledger renderer exists yet, so this is a claim-versus-
      property gap to close *before* one is written: either the renderer's line
      discipline becomes a contract obligation, or the docstring's forgery claim
      is softened. Do not write a ledger renderer without resolving this.
- [ ] `SourceProvenanceV1.endpoint` redaction — severity unchanged by review
      (the same URL set, duplicated, not new URLs), but a credential ever landing
      in a query string would now be copied into every observation record rather
      than one page record, and retroactive redaction scales with it. Close while
      the record count is still small.

### Carried from the WebSocket research

- [ ] **ADR-0010's residual collision is reachable in practice, not
      theoretical.** `(timestamp, hash)` identifies a post-state, not a wire
      message: six `price_changes` entries shared one hash inside a single
      message, and two distinct frames 196 microseconds apart carried an
      identical `(timestamp, hash)` pair. Measured against the recorded capture,
      the combined identity separates all 68 real entries, while the *rejected*
      hash-ranked draft would have silently dropped 10 of them — so content
      hashing is load-bearing on this source, not belt-and-braces. What remains
      open is the case where two frames are byte-identical in content as well:
      the store and the WebSocket adapter must both decide what a delivery is
      when one book transition arrives across more than one frame.
- [ ] The capture adapter must set an explicit `User-Agent`. REST `/book`
      returned 403 for Python's default urllib UA while curl and a browser-like
      UA succeeded (UNVERIFIED as a general rule — two strings tried, not a
      study). Do not read "no auth header" as "no client identification
      expected".

### Carried from the M2 event-store slice (ADR-0011)

None is a blocker; security review returned PASS_WITH_FINDINGS and every
finding it raised was fixed in the slice itself. These are new items found
while building and reviewing the store, recorded so they are chosen rather
than discovered late by an adapter or the capture CLI.

- [ ] **L1** Duplicate arrivals silently drop `quality_flags`: the observation
      row is written only on first arrival and `delivery` has no quality
      column, so two arrivals of one observation that legitimately differ in
      `quality_flags` (the flag derives from `received_time`, which identity
      excludes) lose the later one. A counted defect discarded —
      `.claude/rules/data-integrity.md`. Cheap fix (a column on `delivery`)
      but it is a schema change and the schema is unversioned (see L4).
- [ ] **L2** `open_sqlite_event_store` path handling is strictly weaker than
      the raw archive beside it: no `resolve()`, no containment check, no
      `O_NOFOLLOW` equivalent, no mode. Measured: a symlinked db path was
      followed and the `-wal`/`-shm` side files were created beside the
      symlink target, all at 0644, while the archive writes 0600 and checks
      `is_relative_to(root)`. Requires local write access, hence LOW.
- [ ] **L3** The store imposes no size bound of its own;
      `MAX_PAYLOAD_CANONICAL_BYTES` lives only in `build_observation_envelope`,
      and `from_record` accepts anything. Measured: a 32 MiB record wrote in
      0.441 s at 210 MiB peak RSS and read back at 334 MiB. No exposure today
      because the builder is the only production write path. Also
      `source_frame_offset` is unvalidated (`-1` accepted; `2**63` raises a
      bare `OverflowError`).
- [ ] **L4** The database schema has no identity and no version:
      `PRAGMA user_version` is never set or read, and
      `CREATE TABLE IF NOT EXISTS` opens a differently-shaped pre-existing
      file silently, failing at the first write mid-capture with a generic
      message. A future migration will have no version to migrate from. Note
      the engineering rule "Every public schema and persistent record is
      versioned" is satisfied for records but not for the schema.
- [ ] **L5** `write_raw_payload` does `mkdir(parents=True)` but
      `_fsync_directory` syncs only `path.parent`, so for the first payload of
      a new source the file is durable inside a directory whose own entry may
      not be. Also `mkdir` mode is 0755 around files written 0600.
- [ ] **L6** `SourceProvenanceV1.endpoint` redaction — already in the
      backlog, but re-filed with the new blast radius: measured, a credential
      in a query string is now greppable in the database file, once per
      observation (~91k rows/day/token extrapolated) rather than once per run
      manifest, and retention/compaction are explicitly out of scope.
- [ ] **L7 — the most important one, and it must be filed before a renderer
      exists.** Payload text is opaque to the store by design (ADR-0011
      section 3), so it is unneutralized: `get_observation()` faithfully
      returns live ESC, BEL, RLO, ZWSP and a U+E0041 tag character out of a
      payload field. No exposure today because `OrderBookSnapshotV1` is all
      `Decimal`. The first payload model with a free-text field reopens the
      M1 OSC-52 rendering-forgery class at the store's read boundary, against
      durably stored text. Two obligations: every future payload model with a
      free-text field must neutralize it the way envelope identifiers and
      ledger `detail` are; and any renderer over `iter_rejections`/
      `get_observation` must block-quote as well as sanitize, because the
      sanitizer is the only M1 defence that carried across (see the M2
      security review findings above).
- [ ] The denormalized filter columns on `observation`/`rejection` are never
      cross-checked against `record` on read. Not exploitable today since no
      method queries by them; becomes real the moment a query-by-`market_id`
      method ships (plausibly M3).
- [ ] Nothing detects a `delivery`/`observation` row naming a run that was
      never opened — the Python referential check binds only callers going
      through `SQLiteEventStore` and is never re-checked. Recommend a cheap
      integrity-check query rather than trying to restore the foreign key.
- [ ] A leftover artifact from the security review,
      `~/.cache/argos-sec-probe/e.sqlite3`, could not be removed (the
      permission system denied `rm`). It is outside the repository and
      affects no commit, but note it as a manual cleanup item.

### Carried from the M2 CLOB REST adapter slice

None is a blocker; security review returned PASS_WITH_FINDINGS and every
finding it raised was fixed in the slice itself (see `docs/STATUS.md`, "M2
slice: public CLOB REST order-book adapter and normalization"). These are new
items found while building and adversarially testing the adapter, recorded so
they are chosen rather than discovered late by the WebSocket adapter or the
capture-loop slice.

- [ ] **C1** A 3xx response with a JSON body is accepted as a successful
      observation — `clob.py` treats every status below 400 as success. A 302
      carrying `{"market":"pwn"}` was accepted with
      `provenance.http_status=302`. The redirect is not followed and the host
      never changes (`follow_redirects=False`), so the body can only come
      from the host already contacted, and provenance records the 302
      honestly — auditable after the fact. Not a regression introduced here:
      `gamma.py` behaves identically.
- [ ] **C2** Cross-adapter backoff correlation: `ClobClient` and
      `GammaClient` at the shared default `source_jitter_seed=0` produce
      byte-identical backoff sequences, as do two `ClobClient` instances. The
      thundering-herd item already in the backlog (pre-M2 security review) is
      now confirmed cross-adapter as well as cross-market. Reproducibility
      remains the right trade for a public unauthenticated endpoint; record
      that the scope widened.
- [ ] **C3** A substituted response is refused (the response `asset_id` is
      cross-checked against the requested `token_id`) but labelled
      `MALFORMED_PAYLOAD`, identical to a JSON parse failure, so an operator
      cannot count "the source returned a different token's book" as a
      distinct outcome. Separately, `condition_id` on an accepted envelope
      comes only from the response with no cross-check against the M1 Gamma
      metadata, so ARGOS durably stores a token-to-condition binding the
      source alone asserts.
- [ ] **C4** `SourceProvenanceV1.endpoint` — already in the backlog (L6 in
      the event-store slice above), but this adapter introduces no new leak
      channel: the only query parameter is a `[0-9]{1,120}`-validated token
      id. The already-backlogged redaction gap now materializes once per CLOB
      observation as well as once per Gamma page — more urgent because it
      recurs on a second source, not qualitatively worse.
- [ ] Blind spots left by adversarial testing, not yet closed by a test:
      `market`/`asset_id` explicitly `null` in the response is hand-traced
      through the code but has no regression test; a non-string `hash` type
      is read-verified only. The sharpest one — **every future payload model
      must reuse `OrderBookSnapshotV1._normalize_decimal` rather than
      reimplement decimal normalization** — with the `price_change`
      WebSocket delta model as the concrete next place this can reappear.

## Later — M3

- [ ] Replay source and scheduler.
- [ ] Event-time watermark policy.
- [ ] Golden replay and hash.
- [ ] `VirtualPacer` for accelerated/stepwise replay pacing (per ADR-0009's
      consequences section), living in `argos.replay`, and never influencing the
      output hash. Deliberately not built pre-M2 to avoid scope drift.

## Later — M4

- [ ] Baseline quotes and forecasts.
- [ ] Resolution normalization.
- [ ] Proper scores and calibration report.
- [ ] M4 handoff.

## Explicitly not in backlog before owner gate

- frontend;
- news scraping;
- LLM probability estimates;
- RESON implementation;
- wallet or order execution;
- deployment complexity.
