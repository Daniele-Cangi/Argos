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
- [ ] `_write_atomically` renames without an `fsync`, so a power loss can make the
      rename durable before the contents. Acceptable for an archive that disclaims
      durability; the M2 event store cannot inherit it.
- [ ] Duplicate market ids inside one page are accepted twice with no dedup counter;
      it belongs with the M2 idempotent store.
- [ ] The Hypothesis market strategy generates only payloads the normalizer accepts,
      so it exercises the accounting invariants and not the quarantine path.

### Carried into M2 from M1

- [ ] Paginate discovery. `list_markets` fetches one page; a research sample
      larger than one page needs cursor handling and a documented stopping rule.
- [ ] Persist `MarketDefinitionV1` and `QuarantinedMarketV1` records. They are
      currently computed and reported but only the raw payload is archived.
- [ ] The raw archive in `argos.store` is deliberately minimal and is not the
      event store M2 requires; decide whether it survives or is absorbed.
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
- [ ] CLOB snapshot adapter.
- [ ] WebSocket lifecycle and subscriptions.
- [ ] Canonical market-data payloads (order book, price change, etc. — typed
      `VersionedModel`s that `build_observation_envelope` takes as `payload`).
- [ ] Event store, including the delivery-record shape decision below.
- [ ] Capture manifest and health metrics.
- [ ] Capture CLI and integration fixture.

### Carried from the M2 observation-identity slice (ADR-0010)

- [ ] **Before the first payload model lands** — normalize `Decimal` scale
      inside the payload model, not just at the boundary. `Decimal("0.430")`
      and `Decimal("0.43")` currently serialize to different canonical text and
      mint different `observation_id`s
      (`tests/test_observation_envelope_adversarial.py::test_decimal_trailing_zero_precision_changes_identity`).
      The CLOB endpoint really reports the same price at two precisions across
      `/book` and `/last-trade-price` (`docs/research/m2-clob-rest-book.md`,
      "Decimal hygiene"), so this is a live hazard on the very first payload
      type, not a theoretical one.
- [ ] **Before reprocessing under a corrected parser is possible** — add
      `supersedes_observation_id` to `ObservationEnvelopeV1`. Identity excludes
      `parser_version` by design, so reprocessing the same raw bytes under a
      fixed parser mints a new, unlinked identity today. ADR-0004 requires
      superseding records as the correction mechanism; the field does not
      exist yet.
- [ ] **Before the store writes a row** — decide the delivery-record shape.
      `observation_id` deliberately excludes `capture_run_id` and
      `ingest_sequence` (both would make a duplicate unable to collide,
      making the M2 duplicate-detection exit criterion unreachable), so a
      collapsed duplicate currently has nowhere to record its own
      `received_time`/`ingest_sequence`, and `RejectedObservationV1` cannot
      point at the accepted twin it duplicates.
      `docs/02_ARCHITECTURE.md` requires duplicate inserts to be idempotent
      **and observable**; this is a storage-shape question the identity ADR
      deliberately left open (ADR-0010, "Consequences").
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
- [ ] Neither `observation_id` nor `rejection_id` is enforced by a validator: a
      forged id round-trips through `from_record` while `recompute_*` disagrees.
      The recompute functions exist; nothing obliges a reader to call them. The
      store slice is the right place to make verification mandatory on read.
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
