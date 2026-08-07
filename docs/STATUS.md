# ARGOS status

Last updated: 2026-08-07

## Current state

- Current milestone: **M2 — CLOB capture — in progress**. M0 and M1 closed. The
  pacing-versus-timekeeping ADR that gated M2 (ADR-0009) is resolved and merged;
  no M2 source adapter has been written yet.
- Autonomous target: **complete M0-M4**
- Owner gate: **required after M4**
- Execution capability: **prohibited and absent**
- External evidence / LLM forecasting: **not started**

## Current objective

Build the CLOB snapshot adapter and the event store, using `Pacer` (not
`Clock`) for retry backoff, per ADR-0009.

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
