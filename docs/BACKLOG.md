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

- [ ] **ADR before M2** — reconcile `Clock.sleep` with ADR-0003's "`ReplayClock`
      controlled only by the replay scheduler". Today any holder of the protocol can
      advance replay time, and `ReplayClock.sleep` never awaits while `LiveClock.sleep`
      yields — a live/replay divergence once the dispatcher is concurrent. Decide before
      M2 retry/backoff code is written against `clock.sleep`.
- [ ] **Before M3** — decide what `config_fingerprint` covers. It currently includes
      `data_dir`, but `docs/02_ARCHITECTURE.md` allows output location to differ between
      live and replay, so an otherwise identical replay gets a different fingerprint.
- [ ] **Before M2** — `RunManifest` needs schema versions (plural), data provenance, and
      a `mode` enum instead of a free-form string, to satisfy invariant 13 in full.
- [ ] **Before M2** — record working-tree state alongside `code_revision`; a dirty tree
      currently yields a manifest that misattributes the code that produced the run.
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
- [ ] Harden the boundary scan: forbid `os`/`pathlib` in `domain`, and split the `Clock`
      protocol from `LiveClock` so importing the protocol does not pull a wall-clock
      implementation into the graph.
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
- [ ] M1 closure review (architecture + security + testing).

### Carried from the M1 closure reviews

- [ ] **ADR before M2** — separate pacing from timekeeping on the `Clock` protocol.
      `GammaClient` sleeps its backoff on the injected clock, `ReplayClock.sleep`
      advances virtual time, and `wait_exponential_jitter` draws from an unseeded
      global RNG — so an adapter given the scheduler's clock would move replay time
      nondeterministically and break M3's identical-output-hash criterion. The
      client's docstring warns against it; the type system does not. Options: an
      injected `sleep` callable defaulting to `anyio.sleep`, or seeded jitter plus
      an enforced prohibition.
- [ ] Revisit where the `human_reviewed` prohibition lives. Today a field validator
      makes the value unconstructible, which also means the future human-review flow
      cannot build the record and a stored `human_reviewed` contract cannot be
      deserialized. Keep it until that flow exists, then move the check to the
      compiler and let the review flow write the verdict.
- [ ] `read_raw_payload` follows a pre-existing symlink at the target path, and the
      archive does not use `O_NOFOLLOW`. Requires an attacker who already has write
      access to the data dir, so it is low, but the M2 store should close it.
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
