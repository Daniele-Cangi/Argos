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

## Later — M2

- [ ] CLOB snapshot adapter.
- [ ] WebSocket lifecycle and subscriptions.
- [ ] Canonical market-data payloads.
- [ ] Event store.
- [ ] Capture manifest and health metrics.
- [ ] Capture CLI and integration fixture.

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
