# Backlog

Work top to bottom unless a milestone dependency requires reordering.

## ADR-0019 resilient validation campaign

- [x] Define a versioned technical-scenario result and aggregate that preserve
      `PASSED`, `FAILED`, `INCOMPLETE` and `NOT_RUN` independently.
- [x] Replace the external one-off monitor with a repository-tracked resumable
      monitor that enforces exclusive poll ownership and validates its last
      durable checkpoint before continuing.
- [x] Add deterministic network, process, storage and terminal-state fault
      adapters; unit tests must not depend on real outages or settlement.
- [x] Add a repository-tracked T1 executor that runs the shipped capture and
      replay commands, content-addresses its artifacts, and derives the result
      from durable counts, raw evidence, database immutability and two replay
      digests.
- [x] Add a repository-tracked T2 stability executor with process-tree memory,
      artifact growth and cadence samples under frozen resource bounds.
- [ ] Execute the bounded T1-T8 matrix in
      `docs/research/m4-resilient-validation-plan.md` before freezing another
      live predictive campaign.
- [ ] Add an append-only late-resolution record that can score a previously
      frozen forecast without reconstructing a historical first-observed
      cutoff.
- [ ] Build the next asynchronous cohort with at least 10-20 intended targets;
      retain ADR-0014's 30-resolved-target and dispersion requirements for any
      calibration claim.

## Owner priority — M4 reopened 2026-08-19

This section supersedes the historical “M4 — closed” and “Carried from M4”
sections below without deleting their audit trail.

- [x] Replace transport-arrival forecast emission with target
      information-state-transition emission through `ReplaySession`; persist
      one decision for every arrival.
- [x] Enforce final-only resolution, exact cutoff, post-resolution exclusion,
      persisted contract identity and capture/replay integrity as explicit
      scoring admissibility rules.
- [x] Persist `EvaluationRunBundleV2` with canonical evidence digest, nested
      version checks, trajectory/resolution/contract identity, forecasts,
      evaluations, decisions and exclusions.
- [x] Reject digest-valid but internally contradictory v2 bundles by checking
      report claims, child counts/digests, evaluation links, and exclusive
      scored/excluded membership against the actual sibling records.
- [x] Separate arrivals, target information states, forecast points, scored
      points and resolved-target counts. Keep one-target calibration out of the
      headline.
- [x] Replace the ambiguous two-target calibration threshold with a structural
      floor in `EvaluationPolicyV2`; require a predeclared protocol to define
      sample size, weighting and scientific sufficiency (ADR-0014).
- [x] Preserve `last_trade_price` supplied by initial REST/WebSocket book
      snapshots as auxiliary evidence, outside the order-book state hash.
- [x] Restore the distinct displayed-price method: midpoint for spread at most
      0.10, last trade for wider spreads.
- [x] Make the declared local baseline portable: Windows tzdata, guarded
      `O_NOFOLLOW`, platform-correct archive durability behavior and a
      Windows/Ubuntu CI matrix.
- [x] Meet per-file branch-coverage thresholds for the corrected contracts and
      evaluator. Final prospective bundle-hardening local Windows result:
      1,597 passed, one privilege-dependent symlink skip; `bundle.py` 96.47%
      and `run_v2.py` 92.11% against their 90% floors.
- [x] Merge canonical PR #2 only after its complete Windows/Ubuntu CI matrix
      passed, including Ubuntu coverage (`32279416650`). This was operational
      discipline; GitHub branch protection/status enforcement is not currently
      configured.
- [x] Pin a real standalone `last_trade_price` WebSocket fixture before
      modeling that event. The frozen V2 pilot observed exact public payloads
      on both selected targets; commit `9904b54` models the event as auxiliary
      evidence without changing the order-book state hash or retroactively
      reinterpreting those captures.
- [x] Introduce V3 prospective protocol/receipt/target/lifecycle/cutoff
      contracts and a digest-valid-but-false-semantics adversarial suite.
- [x] Separate the M4 measurement verdict from calibration sufficiency and
      implement equal-target, last-admissible-point aggregation (ADR-0015).
- [x] Preflight bounded public discovery, lifecycle and WebSocket capture; do
      not admit the probe frames into the prospective sample.
- [x] Run the smallest prospective multi-target experiment with contracts and
      receipts persisted before capture and predeclared selection, stopping,
      cutoff, weighting, missingness and sufficiency rules. The V2 pilot
      selected two targets and captured each separately, but both produced a
      standalone `last_trade_price` that the frozen revision did not model.
      The predeclared rule permanently excludes both: measurement is
      `M4_BLOCKED`, calibration is `CALIBRATION_NOT_EVALUABLE`, and there are
      zero contributions. This is a completed negative experiment, not a pass.
- [x] Complete lifecycle polling for the excluded V2 targets. The frozen
      `2026-08-20T06:00:00Z` deadline elapsed without a first-final cutoff; at
      the last valid in-window poll (ordinal 70), both markets were still
      `proposed`. Aggregate `observation_complete` closes selected-target
      accounting, while the missing cadence coverage before the deadline keeps
      `lifecycle_record_complete` false. A host-clock jump caused one append-only
      poll (ordinal 71) after the deadline. It is preserved and reported but
      inadmissible as cutoff evidence, and changed no exclusion, contribution or
      experiment verdict.
- [x] Harden the prospective operational boundary without running a new
      experiment (ADR-0017): bind deadline, cadence and capture limits in
      `ProspectiveExperimentProtocolV2`; require `retrieved_at` and selected
      cutoff at or before the deadline; bind the exact capture manifest in
      `EvaluationRunBundleV4`; enforce limits and per-target capture separation
      in `ProspectiveExperimentBundleV3`; and publish the immutable historical
      V2 aggregate plus receipt index under
      `experiments/m4-pilot-20260819/proof/`.
- [x] Run and close the fresh V3 experiment without reusing V1/V2 targets.
      Both frozen captures completed cleanly with the standalone last-trade
      schema already modeled. Each target has 98 contiguous persisted
      lifecycle observations and zero observed admissible cutoffs. Host
      suspension left an approximately 3h37 unobserved tail before the
      `2026-08-21T06:00:00Z` deadline, so target accounting is complete while
      lifecycle continuity is incomplete. Publish the receipt-bound terminal
      chain as `ProspectiveExperimentBundleV4`, verify its exact committed
      bytes through `ProspectiveClaimArtifactIndexV1`, and retain
      `M4_BLOCKED` / `CALIBRATION_NOT_EVALUABLE`. Do not infer settlement
      state during the unobserved tail (ADR-0018).
- [x] Abort V4 before observation when its start boundary elapsed before
      protocol freeze. Persist the abort record; create no targets, captures,
      lifecycle evidence, cutoff or result, and reuse none of its inputs.
- [x] Run and close the fresh V5 experiment. Both bounded captures completed
      cleanly and each target has 129 contiguous, receipt-bound lifecycle
      observations through approximately `07:59Z`. No admissible cutoff was
      observed before the frozen `2026-08-22T08:00:00Z` deadline. Publish the
      exact terminal V4 bundle and receipt index with
      `TARGET_ACCOUNTING_COMPLETE`, `LIFECYCLE_CONTINUITY_COMPLETE`,
      `NO_ADMISSIBLE_CUTOFF_OBSERVED`, `M4_BLOCKED` and
      `CALIBRATION_NOT_EVALUABLE`.
- [ ] Close the owner-authorized M4 V6 experiment after its frozen
      `2026-08-24T22:00:00Z` deadline. Its fresh protocol and two captures are
      valid, and each target has 34 contiguous receipt-bound lifecycle
      observations through approximately `00:56Z`. A DNS lookup failure then
      terminated the monitor and left a cadence-breaking unobserved tail; do
      not restart, rescue or infer settlement from silence. Materialize the
      terminal V4 bundle after the deadline with lifecycle continuity
      incomplete and no observed admissible cutoff.
- [x] Harden future prospective public reads without rewriting V6 evidence:
      use three total attempts with deterministic 1 s / 2 s backoff for
      transport failures and transient HTTP statuses, mint no evidence for a
      failed attempt, and keep persistent outages visible. Deterministic tests
      reproduce a one-shot DNS failure, retry exhaustion, transient 503 and
      non-retryable 404.
- [x] Abort V7 at the owner's request before its observation window. It has no
      captures or lifecycle observations and supplies no evidence to V8.
- [x] Run and close the fresh V8 experiment without reusing predecessor
      evidence. Both captures completed cleanly and each target retained 554
      contiguous, receipt-bound lifecycle observations. The monitor failed
      after the last polls near `20:55Z`, leaving an approximately 64-minute
      unobserved tail before the frozen deadline. Publish the exact terminal
      V4 bundle and receipt index with `TARGET_ACCOUNTING_COMPLETE`,
      `LIFECYCLE_CONTINUITY_INCOMPLETE`, `NO_ADMISSIBLE_CUTOFF_OBSERVED`,
      `M4_BLOCKED` and `CALIBRATION_NOT_EVALUABLE`; do not reinterpret V8 under
      ADR-0019.

## Must close before M3 — from the M3 readiness audit (2026-08-17)

Reconstructed from `main` and from measurement, not from this file's own
previous state; see `docs/STATUS.md`, "M3 readiness audit". Each item below is
a genuine blocker for *deterministic replay* specifically, and each names the
measurement that made it one. Everything else this audit touched is either
already closed (marked `[x]` in place, with the evidence) or is real but does
not block M3, and stays where it was filed.

- [x] **R1 — `config_fingerprint` covers `data_dir`, an output location.**
      Measured: two `Settings` differing only in `data_dir` fingerprint
      `a555c764…` and `2dee89e1…`. `docs/02_ARCHITECTURE.md` names "output
      storage location" as a component that legitimately *differs* between live
      and replay, and `docs/04_DATA_CONTRACTS.md` requires
      `ReplayManifestV1.config_sha256` plus "repeated replay of identical
      input, code, config, and mode must produce identical state hash". As it
      stands, two replays of one capture into two directories are the same
      experiment recorded under two configurations. This is the "**Before M3**"
      item carried from the M0 closure reviews, now due.
      **Closed**: every settings field declares a `FingerprintScope`
      (`experiment` or `environment`) on the field itself, the fingerprint
      hashes only the experiment-scoped view, and there is no default — an
      unclassified field raises rather than being guessed at either way.
      `data_dir` and `log_level` are the two environment-scoped fields;
      `settings_snapshot` still records both verbatim, so nothing is lost.
      `RunManifest` is bumped to `run_manifest.v4` because the meaning of
      `config_fingerprint` changed and three `v3` manifests really exist.
- [x] **R2 — no `payload_schema_version` -> model registry.** `read_payload`
      takes the model as an argument, so a replay dispatcher over heterogeneous
      payloads has to grow an `if/elif` chain on version strings — in the one
      module whose whole purpose is that live and replay run the *same*
      handlers. Carried from the ADR-0010 slice as "**Before M3 dispatch**",
      now due. Closing it also closes the M2 security review's unenforced
      `schema_version` uniqueness item, which is the same map viewed from the
      other side.
      **Closed**: `VersionedModel.__init_subclass__` registers each version and
      refuses a second claimant, `resolve_schema` turns a stored version into
      the model that declares it, and `read_declared_payload` is the typed
      accessor for a reader that does not know the payload type in advance.
      The uniqueness half was not hypothetical — a stub declaring
      `order_book_snapshot.v1` was sitting inside the test module for
      `read_payload` itself, and is now the test that asserts the refusal.
- [x] **R3 — the SQLite store has no schema identity or version.** Measured:
      `PRAGMA user_version` and `PRAGMA application_id` are both 0 on a store
      this repository just wrote, and `open_sqlite_event_store` opened a
      database whose `observation` table was a foreign two-column table
      *and let `open_capture_run` succeed on it* — the failure surfaces later,
      mid-run, as a generic SQLite error. Filed as **L4** from the event-store
      slice; it becomes a blocker at M3 because "identical input produces an
      identical output hash" needs "identical input" to be a checkable claim
      about the file being read, and a replay reader is the first code that
      opens a store it did not itself write.
      **Closed**: a fresh store is stamped `application_id = 0x41524753` and
      `user_version = 1`, and every open verifies the *shape* — each table's
      columns, and that both `capture_run` indexes are genuinely UNIQUE and
      partial — before trusting the stamp. Shape is the evidence, the stamp is
      the fast path: an unstamped store of the right shape is adopted rather
      than stranded, because captures taken before 2026-08-17 carry
      `application_id = 0`, while a store stamped by another application is
      refused outright. Two further holes surfaced while building it: an index
      of the right name and the wrong nature (non-unique, non-partial) survives
      `CREATE UNIQUE INDEX IF NOT EXISTS` untouched and silently downgrades
      ADR-0011 section 5's schema-level guarantee to a convention; and a foreign
      *table* named after one of those indexes made schema application fail as a
      bare `sqlite3.OperationalError`, outside the taxonomy, on the one code
      path that runs before any check could catch it.
- [x] **R4 — every stored observation embeds an absolute filesystem path.**
      Found by this audit, not previously filed. `run_capture` stores
      `str(write_raw_payload(...))`, and `write_raw_payload` resolves its
      directory, so `raw_payload_location` durably records a machine-specific
      absolute path inside `observation.record`. The link is not merely
      cosmetic: it is unverified (nothing checks the path exists), it breaks
      silently if the capture directory is moved or shared, and it makes two
      captures of identical bytes on two machines produce different stored
      records. It is also redundant — `read_raw_payload(directory, sha256)`
      globs by hash and never reads this field. The same class as R1, one layer
      down: a physical location baked into a record that should describe
      content.
      **Closed**: `archive_relative_location` is the one place that composes the
      stored value, `write_raw_payload` builds its own target from the same
      function so the layout and the record cannot drift, and `run_capture`
      stores the relative form. `write_raw_payload` still *returns* an absolute
      path, because its other callers are operator-facing CLI reports where the
      whole path is what an operator wants — a different question with a
      different answer.

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
      **Now due, and measured** — see **R1** at the top of this file.
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
- [x] **Constraint on the M2 capture-manifest design** — `RunManifest.input_provenance`
      is an unbounded tuple of `SourceProvenanceV1`, fine at M1 discovery scale (one
      record per fetched page/market). An M2 capture manifest must not embed one
      provenance record per ingested event; it must reference the event store instead.
      Decide the reference shape before the capture manifest is built, not after.
      Closed by the capture-CLI slice and confirmed by the M3 readiness audit:
      the reference shape is `RunManifest.capture_run_id` (`run_manifest.v3`),
      `input_provenance` is empty for a capture run, and a reader wanting a
      summary queries `EventStore.counts_for_capture_run`. Measured on the
      2026-08-15 live capture: 0 `input_provenance` entries.
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
- [x] WebSocket lifecycle and subscriptions. Closed by
      `src/argos/sources/clob_ws.py` (`ClobMarketWsClient`): connect,
      subscribe, `PING`/`PONG` heartbeat, reconnect with bounded seeded
      backoff, bounded blocking frame buffer, health counters. Yields raw
      frames only; no decoding, no sequence allocation, no manifest.
- [x] **Two gaps the transport stated rather than hid. One is closed; the
      other was my own item, written wrong, and is corrected here rather than
      quietly dropped.** The oversized-frame gap ("counted but no
      rejection-ledger row") is **not closable**: `WebsocketsConnector` passes
      the same `MAX_FRAME_BYTES` to `websockets.connect` that `_classify` later
      checks, and the library enforces `max_size` during frame reassembly,
      raising out of `recv()` before an oversized payload is ever assembled
      into a string. No bytes, therefore no `raw_payload_sha256`, ever reach
      ARGOS, and `RejectedObservationV1.raw_payload_sha256` is required — a row
      would have to invent a hash for content never received, which is
      fabricated evidence against core invariant 7. The counter is the honest
      maximum. Consequence to remember: with the shipped connector `_classify`'s
      size check is unreachable, and is defence-in-depth only for an injected
      connector with a larger or unenforced `max_size`. The reconnect-gap half
      stands unchanged and unclosable from this channel: it has no sequence
      number, confirmed absent by observation.
- [x] Canonical market-data payloads (order book, price change — typed
      `VersionedModel`s that `build_observation_envelope` takes as `payload`).
      Closed for both message kinds M2 needs: `OrderBookSnapshotV1`
      (`order_book_snapshot.v1`) and `PriceChangeV1` (`price_change.v1`,
      `src/argos/domain/pricechange.py`). `tick_size_change` and
      `last_trade_price` have no payload model and no observed live sample —
      listed separately below rather than implied by this item.

### Closed by the pre-WebSocket hardening slice (2026-08-13)

Both reported by the owner against `a9b9802` and reproduced before being acted
on. Recorded here because each is a *recurrence pattern*, not a one-off.

- [x] **Canonicalization could silently change a non-zero value to zero.**
      `normalize_decimal` ran inside a context whose `Emin` was finite and
      inherited from `decimal.DefaultContext`, so `1E-1000064` underflowed to
      `Decimal(0)` and shared a genuine zero's canonical text and identity.
      Introduced by the previous slice's own fix: the lower exponent bound was
      removed on a **text-length** argument while the defect is **arithmetic**.
      Closed by restoring `MIN_DECIMAL_EXPONENT`, setting `Emin`/`Emax`
      explicitly, and adding a fail-closed equality postcondition that does not
      depend on anyone reasoning correctly about `Emin`.
      **Standing lesson for every future canonicalization change:** assert the
      postcondition, do not derive it from the bounds.
- [x] **A malformed `hash` escaped the taxonomy as `TypeError`.** The hash set
      was built, and sorted for an error message, before any type check, so an
      unhashable or mixed-type value bypassed `except ValueError` and left no
      ledger row. Closed by validating each selected hash as a non-empty string
      first, with a bounded `repr` in the message.
      **Standing lesson:** this is the third distinct "escapes the ARGOS error
      taxonomy, therefore no ledger entry" finding in M2, after
      `read_raw_payload` at M1, `_extract_source_hash`, and
      `decimal.InvalidOperation`. Any new parsing boundary should be probed for
      it explicitly rather than waiting for it to be reported.

### Constraints the WebSocket ingestion slice must close, with measurements

Each was measured on the `price_change.v1` slice and left deliberately unfixed
there, because the boundary that must hold it does not exist yet. Recorded with
numbers so the next slice inherits evidence rather than a reminder.

- [x] **A byte cap before parsing, the fourth instance of one class.** Closed by
      `src/argos/ingestion/clob_price_change.py`, which checks
      `provenance.byte_length` against the existing `MAX_NORMALIZABLE_BYTES`
      before reading a single key out of the frame and returns a *rejection*
      rather than raising. Verified independently of the slice's own tests: a
      14,149,053-byte frame carrying 60,000 entries is refused in **0.0002 s**.
      Original finding below, kept for the reasoning and the numbers.
      `parse_price_change_group` bounds neither the `price_changes` array
      length nor any field size, exactly as `parse_order_book_snapshot` does
      not — the cap belongs at the ingestion boundary, where
      `normalize_clob_book` already has `MAX_NORMALIZABLE_BYTES` checked
      against `provenance.byte_length` *before any text is read*. Measured on
      the parse path: **100,000 entries cost 16.15 s CPU / 81.2 MiB**;
      **900,000 entries cost 146.87 s / 731.7 MiB**. No `Pacer` can bound
      this — a cancel scope cannot interrupt synchronous CPU work. No exposure
      today because nothing consumes the model, the same position
      `OrderBookSnapshotV1` was in before `clob_book.py` existed. This is the
      **fourth** boundary this class has appeared at (`build_observation_envelope`,
      `normalize_clob_book`, the event store's missing size bound, now here):
      treat it as a checklist item for every new boundary, not an incident.
- [x] **`entry_hash` must be validated inside the ingestion `try`.** Closed by
      `clob_price_change._validate_entry_hash`, reproducing the check
      `clob_book._extract_source_hash` already applies rather than the gap.
      Original finding below, kept for the reasoning and the measurements.
      `PriceChangeGroup.entry_hash` is unbounded and unsanitized by design at
      the domain layer. Measured: a newline, an OSC 52 sequence, a
      257-character value, and a 20,000,000-character value all parse
      successfully and are then **refused by
      `ObservationEnvelopeV1._validate_identifier`** — so no hostile hash can
      reach an accepted observation. But refusing *at the envelope* raises a
      raw `ValidationError` with **no ledger entry**, which is precisely the
      MEDIUM finding already closed once in
      `argos.ingestion.clob_book._extract_source_hash` (length check plus
      `is_clean_identifier`, inside the module's own `try`). The WebSocket
      ingestion module must reproduce that check, not the gap. Third
      appearance of this shape.
- [ ] **Sequence allocation and cross-token fan-out.** One frame carries
      entries for the unsubscribed binary sibling, and
      `parse_price_change_group` normalizes for **one** requested token per
      call, so a frame yields N records for N tokens. `ingest_sequence` is
      still caller-supplied and unallocated. Whoever allocates it must decide
      the order across tokens within one frame, and M3 replay determinism
      inherits that decision.
- [ ] **A `(frame, token)` group carrying more than one distinct `hash` is
      refused outright.** Never observed in either live capture; refusing an
      unobserved shape was preferred to inventing a grouping policy M3 would
      have to reproduce forever. If a real capture ever produces this
      rejection, revisit with the evidence in hand rather than pre-emptively.
- [ ] No payload model or observed live sample exists for `tick_size_change`
      or `last_trade_price`; neither arrived in ~85 s of combined capture, so
      both remain documentation-only shapes
      (`docs/research/m2-clob-websocket.md`).
- [x] Event store, including the delivery-record shape decision below.
      Closed by ADR-0011 (`docs/adr/0011-sqlite-event-store-and-delivery-record.md`,
      `src/argos/store/event_store.py`). No adapter or capture loop writes
      through it yet.
- [x] Capture manifest and health metrics. Closed by the capture-loop slice:
      the manifest is `RunManifest` plus the store's append-only `capture_run`
      rows (no new concept), and `CaptureHealth` carries the counters.
- [x] Book projection (snapshot plus deltas). Closed by
      `src/argos/projections/book.py`, verified on three real
      snapshot→deltas→snapshot transitions from the recorded live capture.
- [x] Capture CLI. Closed by `argos capture market`; a real 45-second live
      capture ran on 2026-08-15 (22 observations, 4 rejections, sequences
      contiguous across both ledgers).
- [x] **A WebSocket `book` payload model.** Closed by
      `WsBookSnapshotV1` (`ws_book_snapshot.v1`) and `normalize_clob_ws_book`,
      dispatched from the capture loop. A stored capture is now self-sufficient:
      reading only from the event store, a projection seeds from a stored
      snapshot and reconstructs three later stored snapshots exactly. Confirmed
      on live traffic too — the second live capture stored both payload kinds
      with zero rejections. It is a distinct schema from `OrderBookSnapshotV1`
      because the wire schemas genuinely differ.
- [ ] A `last_trade_price` payload model. Observed live on 2026-08-15 during a
      40-second capture, which is new: `docs/research/m2-clob-websocket.md`
      records it as never observed in ~85 seconds and therefore
      documentation-only. It is rejected as `unknown_event_type` today. That
      document already names it as the M4-relevant event type, so this belongs
      with the M4 baseline work rather than M2.
- [ ] Retention and compaction for the raw archive. Measured on live traffic:
      131,948 bytes for 40 seconds on one active token, roughly 285 MB/day/token
      uncompressed. Content-addressed, so redelivered frames cost nothing, but
      nothing prunes.
- [ ] Sanitized sample capture fixture, committed only if size and licensing
      are appropriate.

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
      **Now due** — see **R2** at the top of this file.
- [ ] `read_payload` hard-matches exactly one `payload_schema_version` instead
      of accepting a set via `ensure_supported_version`; no reader can accept
      more than one payload version yet.
- [ ] **Before the capture loop is trusted** — close the research doc's
      UNVERIFIED list: rate limits, the `/books` batch endpoint's existence and
      shape, response headers (caching/rate-limit/`content-encoding`),
      zero-size levels in a REST snapshot (never observed), and behaviour under
      a paused/halted market as distinct from a closed one
      (`docs/research/m2-clob-rest-book.md`, "UNVERIFIED").
- [x] The WebSocket research slice must answer: does the market channel supply
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
      **The condition has been met**: `argos.sources.clob_ws` ships and writes
      `http_status=None` on every frame, so the second value now genuinely
      exists and the ambiguity is live rather than anticipated. Not a *replay*
      blocker — provenance is carried, never dispatched on — so the M3
      readiness audit left it here rather than promoting it. It is the oldest
      item whose stated trigger has actually fired.
- [x] `schema_version` uniqueness is unenforced across `VersionedModel`
      subclasses. Two classes both declaring `order_book_snapshot.v1` defeat
      `read_payload`'s version check — review built a `Trade` out of a book
      envelope with no error. A registry check in `__init_subclass__` closes it.
      Closed exactly that way, as part of **R2**. Worth recording: the
      collision this described already existed in the repository — a stub in
      `tests/test_observation_envelope.py` declared
      `order_book_snapshot.v1`, in the test module for `read_payload` itself,
      and nothing noticed for four slices.
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
- [x] **L4** The database schema has no identity and no version:
      `PRAGMA user_version` is never set or read, and
      `CREATE TABLE IF NOT EXISTS` opens a differently-shaped pre-existing
      file silently, failing at the first write mid-capture with a generic
      message. A future migration will have no version to migrate from. Note
      the engineering rule "Every public schema and persistent record is
      versioned" is satisfied for records but not for the schema.
      **Closed as R3.** **Re-measured by the M3 readiness audit and promoted to a blocker** —
      see **R3** at the top of this file. The audit found it is one step worse
      than recorded here: against a database whose `observation` table was a
      foreign two-column table, `open_sqlite_event_store` *and*
      `open_capture_run` both succeeded, so the run is already open and
      recorded before anything fails.
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
      **Owner action, not reachable from here**: it lives in the home
      directory of the machine that ran that review, which is not the machine
      this milestone is being built on. Left open deliberately so it is not
      marked done by someone who merely could not see it.

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

## M3 — closed 2026-08-18 (ADR-0012)

- [x] Replay source and scheduler. `argos.replay.reader` merges both ledgers on
      `ingest_sequence`; `argos.replay.session` is the only thing that moves a
      `ReplayClock`.
- [x] Event-time watermark policy. Observational: it marks a late event and
      applies it in arrival order, never reordering, buffering or dropping
      (ADR-0012 section 4). `allowed_lateness` defaults to zero because no
      capture in this repository contains an out-of-order arrival.
- [x] Golden replay and hash. `2a7fcb6a…` over 38 arrivals, anchored to three
      snapshot-to-snapshot checkpoints the source itself asserted rather than
      merely pinned.
- [x] `VirtualPacer` for accelerated/stepwise replay pacing, living in
      `argos.replay`, and never influencing the output hash — asserted over all
      three modes.
- [x] A dispatcher that live and replay both call.
      `argos.projections.dispatch.ObservationDispatcher`, driven by
      `run_capture` and by the replay scheduler, and compared directly: same
      frames from a fake wire and from storage, identical state hash.
- [x] Decide what a **duplicate delivery** means to a replayed projection.
      Counted as an arrival, never re-applied, and the refusal lives in the
      dispatcher rather than in each caller so the two cannot drift.
- [x] Decide the replay order **across** capture runs. Refused: a replay is
      scoped to exactly one `capture_run_id`, because a cross-run order would
      have to be invented and ADR-0003's "stable tie-breaking" is a warning
      against exactly that. Reopen with evidence, not by generalization.

### Carried from M3

- [ ] Multi-run replay, if a real need appears. It needs a stated total order
      over runs; `started_at` is not one, because two captures can overlap.
- [ ] `ObservationDispatcher` holds every applied `observation_id` in memory —
      roughly 45 bytes each, about 4 MB/day/token at the volume ADR-0011
      extrapolates. Fine at M3 scale; a session that outgrows it needs a bounded
      structure, not a caller-side check.
- [ ] The replay reader resolves one observation per delivery
      (`get_observation` per row). Correct and deterministic, and an N+1 read
      against the store. Measured at M3 scale it is irrelevant; a capture two
      orders of magnitude larger may want a batched read *behind the same port*,
      never a query from `argos.replay`.
- [x] ~~`RealTimePacer` is not exercised by any test, deliberately.~~
      **That claim was an overstatement, and the coverage gate caught it**:
      `argos/replay/pacing.py` measured 78.26% against its 90% floor within
      hours of the threshold being enforced. Waiting out a capture's real
      inter-arrival gaps would indeed be the flaky timing test
      `docs/13_TEST_STRATEGY.md` forbids; asserting that `wait(0)` does not
      sleep and that a millisecond wait returns is not, because nothing asserts
      *how long* anything took. Closed by `tests/test_replay_pacing.py`.

## M4 — closed 2026-08-18

- [x] Baseline quotes and forecasts (`MarketBaselineForecastV1`). Scores, never
      probabilities: `raw_score` populated, `p_yes` null, and a `p_yes` without a
      calibration version refused by a validator rather than by convention.
- [x] Resolution normalization (`ResolutionV1`) from public lifecycle data, on
      **two** sources. The CLOB states the winner; Gamma requires inferring it
      from a price and usually cannot. Both refuse far more than they accept.
- [x] Proper scores and calibration report (`ForecastEvaluationV1`). Log-loss
      clipping declared on every record and counted per forecast; every
      calibration bin reports its count including the empty ones.
- [x] M4 handoff (`docs/HANDOFF_M4.md`, all eleven sections).

### Carried from M4

- [ ] **The only thing between this machinery and a result: a real sample.**
      Ten to thirty liquid markets resolving within a week, captured
      continuously, then evaluated. It needs no new code. The current evaluation
      is one market, one 40-second window, and one constant score — the top of
      book never moved (0.28/0.29 across all 38 states), so its effective sample
      size is 1 and midpoint and persistence agree perfectly as an artifact.
- [ ] A `last_trade_price` payload model. Observed live on 2026-08-15;
      `docs/research/m2-clob-websocket.md` names it the M4-relevant event type.
      The M3 dispatcher counts it as `unhandled_payload`, so its arrival is
      already visible rather than silent, and wiring it adds the second real
      baseline.
- [ ] A category cohort dimension. `docs/07_MILESTONES.md` asks for category,
      spread bucket and time-to-resolution "when data permits". Spread bucket and
      time-to-resolution ship; category needs Gamma metadata, and Gamma does not
      cover the one market ARGOS has captured.
- [ ] The `category/base-rate` baseline from `docs/05_RESEARCH_PROTOCOL.md`. Not
      implemented because it needs resolved data grouped by category, and the
      qualifier "when enough resolved data exists" is the operative part.
- [ ] `ResolutionV1.resolved_at` is `None` on the CLOB path: that record states
      the winner but not when. Every time-to-resolution cohort therefore lands in
      the `unknown` bucket, which is reported rather than hidden.

## Explicitly not in backlog before owner gate

- frontend;
- news scraping;
- LLM probability estimates;
- RESON implementation;
- wallet or order execution;
- deployment complexity.
