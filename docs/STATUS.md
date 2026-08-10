# ARGOS status

Last updated: 2026-08-10

## Current state

- Current milestone: **M2 — CLOB capture — in progress**. M0 and M1 closed. The
  pacing-versus-timekeeping ADR that gated M2 (ADR-0009) is resolved and merged.
  A first M2 vertical slice has landed: the canonical `ObservationEnvelopeV1` /
  `RejectedObservationV1` contracts and their identity derivation (ADR-0010),
  plus a public CLOB REST research note. **No CLOB adapter, no WebSocket
  adapter, no event store, and no capture CLI exist yet** — this slice is the
  contract the store and adapters will be built against, not the adapters
  themselves.
- Autonomous target: **complete M0-M4**
- Owner gate: **required after M4**
- Execution capability: **prohibited and absent**
- External evidence / LLM forecasting: **not started**

## Current objective

Build the CLOB REST snapshot adapter and the idempotent event store, using
`Pacer` (not `Clock`) for retry backoff per ADR-0009, and `ObservationEnvelopeV1`
/ `RejectedObservationV1` (ADR-0010) as the boundary contract the adapter writes
to and the store reads from. The store must additionally decide the open
delivery-record question ADR-0010 left unresolved (see below) before it writes
a row.

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

## M2 exit criteria

Tracking `docs/07_MILESTONES.md`. Two criteria have contract-level evidence
from this slice; the rest have no adapter, store, or capture loop yet to
produce evidence against, and are listed as open rather than implied closed.

| Criterion | Status | Evidence |
|---|---|---|
| Duplicate source event does not create a second accepted observation | **Contract-level evidence only** | `_observation_identity` collides an identical redelivery onto one `observation_id` (`tests/test_observation_envelope.py::test_identity_is_stable_across_redelivery`) and does so on the real recorded CLOB payload, not only a constructed one (`docs/research/m2-clob-rest-book.md`, "Consequence for `ObservationEnvelopeV1`"). **Not yet closed**: no event store exists to perform the idempotent insert itself — the identity a store would key on is proven stable, the store is not built |
| Zero-size level update is represented as removal | Open | Not yet built. The REST research doc found zero-size levels were **never observed** in a snapshot (UNVERIFIED for REST) and is, on current evidence, a property of the WebSocket delta stream, which has not been researched or built |
| Reconnect does not reset ingest sequence or silently lose manifest state | Open | No WebSocket adapter and no capture manifest exist yet |
| Invalid messages enter a rejection ledger with reason and raw hash | **Contract-level evidence only** | `RejectedObservationV1` carries `reason: RejectionReason`, `detail`, and `raw_payload_sha256`; `build_rejected_observation` derives a deterministic `rejection_id` so redelivery of the same invalid bytes for the same reason collapses rather than growing the ledger unbounded (`tests/test_observation_envelope.py::test_rejection_identity_is_stable_across_redelivery`). **Not yet closed**: nothing writes to a ledger yet — there is no store and no adapter producing rejections from real input |
| Book snapshot plus deltas reconstruct a tested projection | Open | No projection, no adapter |
| No authenticated/user channel or trading code exists | Holds | Unchanged from M0-M1; this slice added no network code at all — only domain contracts and a research note built from public unauthenticated GET requests (`docs/research/m2-clob-rest-book.md`, header) |
| An interrupted capture closes or marks its manifest incomplete | Open | No capture loop or manifest exists yet |

## Known limitations from the M2 observation-identity slice

Recorded here rather than discovered late by the store or adapter slices that
build on this one. Full reasoning in `docs/adr/0010-observation-identity.md`
and `docs/BACKLOG.md`.

- No payload model yet normalizes `Decimal` scale, and `Decimal("0.430")` and
  `Decimal("0.43")` serialize to different canonical text and therefore mint
  different `observation_id`s — reproduced directly
  (`tests/test_observation_envelope_adversarial.py::test_decimal_trailing_zero_precision_changes_identity`)
  and confirmed live: the CLOB endpoint really does report the same price at
  two precisions across `/book` (`"0.430"`) and `/last-trade-price`
  (`"0.43"`).
- `ObservationEnvelopeV1` has no `supersedes_observation_id`. Identity depends
  on the normalized payload and deliberately excludes `parser_version`, so
  reprocessing the same raw bytes under a corrected parser mints a new,
  unlinked identity. ADR-0004 requires superseding records as the correction
  mechanism; this field does not exist yet.
- The store's delivery-record shape is undecided. Identity excludes
  `capture_run_id` and `ingest_sequence` by design (both would make a
  duplicate unable to collide), which means a collapsed duplicate currently
  has nowhere to record its own arrival, and `RejectedObservationV1` cannot
  point at an accepted twin it duplicates. `docs/02_ARCHITECTURE.md` requires
  duplicate inserts to be idempotent **and observable** — this slice defines
  identity, not the delivery record, and the store slice must decide it before
  writing a row.
- No `payload_schema_version` -> model registry exists. `read_payload` takes an
  explicit `model` argument today; M3 dispatch across multiple payload types
  will need something less ad hoc.
- `read_payload` hard-matches exactly one `payload_schema_version` rather than
  accepting a set via `ensure_supported_version`, so no reader can yet accept
  more than one payload version.
- The research doc's UNVERIFIED list (rate limits, the `/books` batch
  endpoint, response headers, zero-size REST levels, halted-market behaviour)
  is unresolved. None of it should be assumed by the capture loop.

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
