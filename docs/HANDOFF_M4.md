# ARGOS handoff — Owner Review Gate A (after M4)

Structure follows `docs/10_HANDOFF.md` exactly.

## 1. Executive state

- **Commit**: see section 11; the branch head moves with each hardening
  slice, so it is stated once, where the git state is.
- **Milestones completed**: M0, M1, M2, M3, M4, plus an M4.1 owner-gate
  hardening pass (section 12).
- **What works.** ARGOS discovers public Polymarket markets and compiles their
  rules into a reviewable contract; captures the public CLOB market WebSocket
  into an idempotent, append-only SQLite event store with a content-addressed
  archive of every raw frame; replays a stored capture deterministically in
  original arrival order through the *same* dispatcher a live capture drives,
  producing a stable state hash; reads market baselines off the replayed order
  book as explicitly uncalibrated scores; normalizes a market settlement from
  either the CLOB or Gamma, refusing far more than it accepts; and scores those
  baselines against a real settlement with declared log-loss clipping,
  calibration bins that always report their counts, and a limitations field that
  cannot be left empty.
- **Execution and authentication code is absent.** No wallet, signing, order,
  cancellation, balance, allowance, private-key, mnemonic, authenticated
  user-channel or position-management code exists anywhere in `src/`. This is
  enforced mechanically, not by review: `tests/test_boundaries.py` scans every
  declared name in the source tree for execution tokens and every string literal
  for authenticated endpoint markers, and `ARGOS_EXECUTION_ENABLED=true` is
  rejected at configuration load with `argos.execution_prohibited`.

## 2. How to run

From a clean checkout. Requires Python 3.12+ and `uv`.

```bash
uv sync --all-groups
uv run python scripts/claude/quality_gate.py            # ruff, format, mypy, pytest
uv run python scripts/claude/quality_gate.py --coverage # per-area branch thresholds
uv run python scripts/claude/validate_bootstrap.py      # agents and skills load
```

```bash
uv run argos markets discover --limit 20                # public Gamma, read-only
uv run argos markets audit 2063134                      # human-reviewable report
uv run argos capture market --token-id <id> --max-seconds 45
uv run argos replay capture <capture_run_id> --db .data/capture/events.sqlite3
uv run argos evaluate baseline <capture_run_id> --token-id <id> \
    --db .data/capture/events.sqlite3 \
    --resolution tests/fixtures/clob/market_resolved.raw.json
```

`capture market` requires `--max-seconds` and/or `--max-frames`: a research CLI
must not start an unbounded run against a live public endpoint by accident.
`replay` and `evaluate` touch no network at all.

## 3. Architecture map

**Implemented packages**, all of those `docs/02_ARCHITECTURE.md` names:
`argos.domain`, `argos.clock`, `argos.config`, `argos.sources`,
`argos.ingestion`, `argos.store`, `argos.projections`, `argos.compiler`,
`argos.replay`, `argos.baselines`, `argos.resolution`, `argos.evaluation`,
`argos.cli`.

**Dependency direction** points inwards and is enforced by AST tests: domain
code imports no HTTP client, database, Typer, environment read or wall-clock
call, and no outer-layer sibling. `sqlite3` may not be imported outside
`argos.store`; pacing names may not be imported into domain, projections,
baselines or evaluation.

**Event flow.**

```
public source -> source adapter -> normalizer -> ObservationEnvelopeV1
     -> SQLiteEventStore (+ raw archive)
     -> [live] ObservationDispatcher -> OrderBookProjection
     -> [replay] read_capture_arrivals -> ReplaySession -> the same dispatcher
     -> MarketQuoteV1 -> MarketBaselineForecastV1 -> ForecastEvaluationV1
```

The two arrows into the dispatcher are the same object, not two implementations
of one protocol — that is what makes core invariant 5 a measured property
rather than a claim.

**Storage layout.** One SQLite file per capture (`capture_run`, `observation`,
`delivery`, `rejection`), stamped `application_id=0x41524753`/`user_version=1`
and shape-verified on every open; raw frames content-addressed beside it under
`raw/<source>/<sha256>.raw.json`; a `run_manifest.v5` per capture, a
`replay_manifest.v1` per replay, and an `evaluation_report.v1` per evaluation,
each written next to the database.

**Configuration and manifests.** `Settings` is immutable and every field
declares whether it belongs in `config_fingerprint`: experiment-scoped settings
are hashed, `data_dir` and `log_level` are recorded but not hashed, so a replay
writing elsewhere is still the same experiment.

## 4. Contract inventory

| Schema | Version | Implementation | Tests |
|---|---|---|---|
| `RunManifest` | `run_manifest.v5` | `src/argos/config/manifest.py` | `tests/test_manifest.py`, `tests/test_config.py` |
| `SourceProvenanceV1` | `source_provenance.v1` | `src/argos/domain/provenance.py` | `tests/test_fixtures.py`, `tests/test_raw_archive.py` |
| `MarketDefinitionV1` | `market_definition.v1` | `src/argos/domain/market.py` | `tests/test_gamma_normalizer.py` |
| `QuarantinedMarketV1` | `quarantined_market.v1` | `src/argos/domain/market.py` | `tests/test_gamma_normalizer.py` |
| `CompiledMarketContractV1` | `compiled_market_contract.v1` | `src/argos/compiler/contract.py` | `tests/test_compiler.py` |
| `MarketAuditV1` | `market_audit.v1` | `src/argos/compiler/audit.py` | `tests/test_compiler.py`, `tests/test_cli_markets.py` |
| `ObservationEnvelopeV1` | `observation_envelope.v1` | `src/argos/domain/observation.py` | `tests/test_observation_envelope*.py` |
| `RejectedObservationV1` | `rejected_observation.v1` | `src/argos/domain/observation.py` | `tests/test_observation_envelope*.py` |
| `OrderBookSnapshotV1` | `order_book_snapshot.v1` | `src/argos/domain/orderbook.py` | `tests/test_orderbook_snapshot.py` |
| `PriceChangeV1` | `price_change.v1` | `src/argos/domain/pricechange.py` | `tests/test_price_change*.py` |
| `WsBookSnapshotV1` | `ws_book_snapshot.v1` | `src/argos/domain/wsbook.py` | `tests/test_ws_book.py` |
| `ReplayManifestV1` | `replay_manifest.v1` | `src/argos/replay/manifest.py` | `tests/test_replay.py`, `tests/test_replay_cli.py` |
| `MarketQuoteV1` | `market_quote.v1` | `src/argos/baselines/quote.py` | `tests/test_baselines.py` |
| `MarketBaselineForecastV1` | `market_baseline_forecast.v1` | `src/argos/baselines/forecast.py` | `tests/test_baselines.py` |
| `ResolutionV1` | `resolution.v1` | `src/argos/resolution/gamma_resolution.py` | `tests/test_resolution.py` |
| `ForecastEvaluationV1` | `forecast_evaluation.v1` | `src/argos/evaluation/scoring.py` | `tests/test_evaluation.py` |
| `EvaluationReportV1` | `evaluation_report.v1` | `src/argos/evaluation/report.py` | `tests/test_evaluation.py`, `tests/test_evaluate_cli.py` |

Every version is unique across the whole codebase, enforced at import time by
the registry in `argos.domain.versioning`, and
`tests/test_versioning.py::test_every_shipped_contract_is_resolvable_from_its_version`
asserts the exact set.

## 5. Milestone evidence

M0-M2 evidence tables are in `docs/STATUS.md` and are not repeated here. M3 and
M4:

| Criterion | Evidence |
|---|---|
| M3: identical input/code/config gives an identical hash over ≥3 runs | `tests/test_replay.py::test_three_replays_of_one_capture_agree_exactly` — hash **and** counts compared |
| M3: replay never reads the wall clock in domain logic | `::test_a_replay_reads_no_wall_clock`, plus the AST boundary scans |
| M3: late and invalid event behaviour is deterministic and counted | `::test_a_late_arrival_is_counted_and_still_applied`, `::test_rejections_are_replayed_as_arrivals_and_tallied_by_reason` |
| M3: changing a source event changes the hash predictably | `::test_changing_one_source_event_changes_the_hash_and_nothing_else` — the change is made to the *source bytes* |
| M3: live adapter replaceable by replay source | `::test_live_capture_and_replay_reach_the_same_state` |
| M3: performance measured, correctness first | `::test_replay_throughput_is_measured` — measured and printed, deliberately not thresholded |
| M4: midpoint never labeled executable price | `tests/test_baselines.py::test_a_one_sided_book_has_no_midpoint_rather_than_a_substitute`, `::test_the_quote_keeps_executable_and_derived_prices_in_separate_fields` |
| M4: unresolved markets not scored as negatives | Enforced by the type: `score_forecast` takes `WinningOutcome`, which has no `UNKNOWN`; an undetermined market yields a `ResolutionRefusal`. `tests/test_resolution.py` (7 refusal cases on real payloads) |
| M4: probabilities validated, clipping declared | `ForecastEvaluationV1` refuses metrics that disagree with its own inputs; `log_loss_epsilon` and `log_loss_was_clipped` are on every record. `tests/test_evaluation.py::test_log_loss_reports_whether_it_clipped` |
| M4: evaluation reproducible from stored records | `tests/test_evaluate_cli.py::test_two_runs_of_the_same_evaluation_agree_on_every_metric` |
| M4: reports include missing data and sample counts | `EvaluationReportV1` splits missingness into abstentions and unresolved; every calibration bin reports its count including empty ones |
| M4: no advanced predictive engine added to flatter metrics | Only market baselines exist. `argos.forecasting` does not exist |

## 6. Sample run

- **Market**: `National Bank Open: Diana Shnaider vs Iga Swiatek`, condition
  `0x94a39add…cc23173`. Captured token `34691510…637961` (Shnaider).
- **Capture**: 40 seconds of the public market WebSocket recorded 2026-08-10,
  committed at `tests/fixtures/clob/ws_market_price_change.raw.json` with a
  provenance sidecar. Driven through the real capture path it produces 38
  arrivals — 4 `book` snapshots, 34 `price_change` deltas — with
  `ingest_sequence` contiguous 1..38, 0 duplicates, 0 rejections, 0 event-time
  regressions.
- **Replay hash**: `2a7fcb6a0ff4745f0e360fa927eb58d67a024e244c5905ac182826717cd69a34`
  (`state_hash.v1`), identical across three runs and across all three pacing
  modes. Anchored to three snapshot-to-snapshot checkpoints the source itself
  asserted, not merely pinned.
- **Resolution**: the CLOB market record fetched 2026-08-18, after the match.
  Explicit `"winner": true` on Swiatek's token; the captured token lost.
  Committed at `tests/fixtures/clob/market_resolved.raw.json`.
- **Evaluation summary**: 38 midpoint and 37 persistence forecasts scored
  against outcome 0. Brier **0.081225**, log loss **0.3355** (ε = 1e-6, **0
  clipped**), ECE **0.285**, 1 abstention (`no_prior_forecast`, the first
  arrival).
- **Known limitations, as the report itself states them**: the scores are
  uncalibrated market baselines and not ARGOS probabilities; the forecasts are
  successive states of one order book and heavily autocorrelated; one market is
  not a sample; and **every forecast carries the same score (0.285)**, because
  the top of book never moved during the capture — best bid 0.28, best ask 0.29
  across all 38 states. The metrics are therefore one number repeated, the
  effective sample size is 1, and the perfect agreement between midpoint and
  persistence is an artifact rather than corroboration.

## 7. Scientific status

**What has been evaluated**: that the pipeline runs end to end on real captured
data against a real settlement, and that it is reproducible. Nothing else.

**What has not been evaluated**: whether ARGOS or the market is well calibrated,
whether any baseline beats any other, or whether ARGOS has any edge of any kind.
The sample is one market, one 40-second window, one token, and one constant
score. No claim rests on it and none is made.

**Baseline definitions.** `midpoint` is `(best_bid + best_ask) / 2` and abstains
without both sides. `last_trade` is the last traded price, a separate baseline
rather than a fallback. `persistence` carries the previous midpoint forward and
abstains without one. `docs/05_RESEARCH_PROTOCOL.md`'s "displayed-price proxy"
is **not** implemented separately because it is the same quantity: measured on
91 of 91 two-sided open markets, Polymarket's displayed price equals the
midpoint exactly. Its "category/base-rate baseline" is not implemented because
the resolved data grouped by category does not exist here.

**Data gaps.** One capture, one market. No quiet-market sample. No capture that
spans a resolution. No REST polling cadence chosen, so REST volume is unmeasured.

**Leakage.** The replay clock advances to `received_time`, never `event_time`,
so a replayed forecast sees information at the moment ARGOS learned it rather
than the moment the market moved. Resolutions are read from a file supplied to
the evaluator and never reach a forecast. No calibration model exists, so there
is no calibration-fitting window to leak across. The one residual worth naming:
the evaluation joins forecasts to a resolution fetched *after* the fact, which
is correct for scoring and would be leakage if any calibration were ever fitted
on the same records.

## 8. Security status

- **Secret scan**: no credential-shaped literal in `src/`. The only matches
  across the tree are the *forbidden-token lists* inside tests that assert no
  such header is ever sent.
- **Authenticated API absence**: confirmed by `tests/test_boundaries.py`
  (declared-name scan, endpoint-literal scan) and by the configuration
  validator. Only public unauthenticated endpoints are reachable; the CLOB user
  channel is not referenced anywhere.
- **Dependency review**: nine runtime dependencies (`anyio`, `httpx`, `orjson`,
  `pydantic`, `pydantic-settings`, `structlog`, `tenacity`, `typer`,
  `websockets`) and seven dev (`hypothesis`, `mypy`, `pytest`,
  `pytest-asyncio`, `pytest-cov`, `respx`, `ruff`). One added since M2:
  `pytest-cov`, dev-only, to make the coverage thresholds verifiable.
  `sqlite3` is standard library.
- **Remaining risks**, all filed in `docs/BACKLOG.md` with their reasoning: the
  variation-selector/ZWJ covert channel through stored text (unsolved by design,
  and load-bearing at M5 where an LLM would read that text);
  `SourceProvenanceV1.endpoint` has no redaction contract and now persists once
  per observation; the event store's path handling is weaker than the raw
  archive's (no `resolve()`, no containment check); payload text is unneutralized
  at the store's read boundary. None is exploitable without local write access
  or a source that ARGOS does not currently read.

## 9. Open decisions

| Decision | Options | Recommended default |
|---|---|---|
| Is an n=1 evaluation enough to pass this gate? | (a) accept, with the limitations as stated; (b) require a capture spanning a resolution first | **(b) for any scientific claim, (a) for the gate itself.** The machinery is complete and honest; the sample is not a result. Capturing markets that resolve within days is a small, mechanical next step |
| Which markets to capture for a real sample | (a) high-volume sports, resolving in hours; (b) political//macro, resolving in months | **(a) first.** Fast resolution turns the sample around in days rather than quarters, and sports markets are liquid enough for two-sided books |
| Should the resolution source be CLOB, Gamma, or both? | (a) CLOB only; (b) Gamma only; (c) both, CLOB primary | **(c), already implemented.** Gamma returned *nothing* for ARGOS's own captured market, and the CLOB states the winner instead of leaving it to be inferred |
| Should `p_yes` ever be populated for a market baseline? | (a) never — it is the market's price; (b) after fitting a calibration model on ARGOS's own resolved sample | **(a) until (b) is genuinely possible.** ADR-0006 already forces this, and nothing should relax it to make a report look finished |
| Multi-run replay | (a) leave refused; (b) define a total order over runs | **(a).** `ingest_sequence` means nothing between runs and any order would be invented |
| Whether M5 may read stored market text with an LLM | (a) no; (b) yes, with the covert-channel risk accepted and mitigated at the consuming layer | **Owner decision.** `docs/BACKLOG.md` records a 31-byte instruction surviving into a rendered audit with zero visible glyph difference; a sanitizer cannot close it |

## 10. Recommended next work

Prioritized, and deliberately not started.

1. **Capture a real sample.** Ten to thirty liquid markets resolving within a
   week, captured continuously, then evaluated. This is the only thing standing
   between the current machinery and a result. It needs no new code.
2. **A `last_trade_price` payload model.** Observed live on 2026-08-15 and
   currently counted as `unhandled_payload` by the dispatcher — so its arrival is
   already visible, and wiring it adds the second real baseline.
3. **Retention and compaction for the raw archive** (~285 MB/day/token measured,
   content-addressed, nothing prunes) before any long capture.
4. **Redaction contract for `SourceProvenanceV1.endpoint`**, while the record
   count is still small.
5. Only then M5. Evidence sources, claim/evidence graph, and independence
   groups — with the covert-channel decision above made first, because an LLM
   layer reads exactly the stored text that channel lives in.

## 11. Git status

- **Canonical repository**: `Daniele-Cangi/Argos`. It is the only repository
  this work is pushed to; nothing is pushed to any other copy.
- **Branch**: `m3-deterministic-replay`, ahead of `main`, pushed to `origin`.
  **Pull request #1 is the open review surface and must not be merged** without
  owner review.
- **Head at the time of writing**: the M4.1 hygiene commit, which is the last
  of the four M4.1 slices. `git log --oneline main..HEAD` is the authoritative
  list; this document deliberately no longer pins a hash that goes stale on the
  next commit.
- **Uncommitted changes**: none.
- **Continuous integration**: **green.** GitHub Actions run **`32195822692`**
  completed successfully on pull request #1. Every step passed: checkout,
  `uv sync --all-groups`, Ruff, format check, mypy, pytest, and the
  branch-coverage thresholds. Section 12 records the numbers and states exactly
  what a green pipeline does and does not establish.
- **Open TODOs**: none in source. `docs/BACKLOG.md` carries every deferred item
  with its reasoning, including one (`~/.cache/argos-sec-probe/e.sqlite3`) that
  is an owner cleanup on a different machine.
- **Local-only generated artifacts**: `.data/`, `.venv/`, `.hypothesis/`,
  `.mypy_cache/`, `.pytest_cache/`, `__pycache__/` — all gitignored. No
  generated capture data is committed. The committed fixtures are recorded
  source payloads with provenance sidecars, hash-checked by
  `tests/test_fixtures.py`.

## 12. M4.1 owner-gate hardening

A review pass over the M4 package. It fixed only defects whose correct
resolution was already determined by an accepted invariant or ADR, and
characterized the rest without choosing. Nothing here expands a milestone.

### Verification environment

Two independent environments, which is what makes the numbers below more than a
local claim.

**Locally**, and the developer machine:

| | |
|---|---|
| OS | Ubuntu 24.04 (WSL 1) on Windows 11 |
| Python | 3.12.3 — the declared floor |
| uv | 0.12.5 |
| SQLite | 3.45.1 |
| Commit verified | `3ebb592`, the M4.1 hygiene commit |
| Suite | **1,580 tests**, all passing |
| Coverage gate | PASS against every per-area threshold in `docs/13_TEST_STRATEGY.md` |

Run **from a fresh clone into a fresh environment**, not from the working tree:
`git clone --config core.autocrlf=true`, a new `uv sync --frozen`, then
`ruff check` (pass), `ruff format --check` (110 files already formatted),
`mypy --strict src` (**no issues in 58 source files**), `pytest` (1,580 passed
in 76.94 s) and the coverage gate (PASS). The clone carries
`core.autocrlf=true` in its own config, and it contains **zero** CRLF files:
`tests/test_fixtures.py` passes there, 19 tests, 9 of 9 raw fixtures matching
their recorded hashes under exactly the configuration that corrupted them
before `.gitattributes` was scoped.

**On GitHub Actions**, run **`32195822692`**, at commit `26559f5`, on
`ubuntu-latest` and **Python 3.12.13** — a different operating-system image and
a different patch release from the local run, from a clean `actions/checkout`
and a fresh `uv sync --all-groups`. Every step passed:

| Step | Result |
|---|---|
| Checkout | clean, `actions/checkout@v4` |
| `uv sync --all-groups` | success, CPython 3.12.13 |
| Ruff | all checks passed |
| Format check | 180 files already formatted |
| Mypy strict | **no issues found in 58 source files** |
| Pytest | **1,580 passed in 36.85 s** |
| Branch coverage thresholds | pass, 1,580 passed in 126.39 s |

Those are the figures for `26559f5`, the commit CI ran. The branch has since
gained two tests — the missing backslash case and the guard on it, both
described below — so the current count is **1,582**.

**What that establishes, and what it does not.** A green pipeline is
**independent execution**: the gates reproduce on a machine this session does
not control, on a Python patch release that has never run here, from a checkout
that shares nothing with the working tree. It is **not an independent
architectural or security review**. No reviewer read this code. The checkbox in
`docs/OWNER_REVIEW_GATE.md` stays unticked, and running CI again will not tick
it.

### One finding from the pull-request reviewer

The automated reviewer on pull request #1 raised one issue, reproduced before
being acted on and confirmed real: in `tests/test_raw_archive.py`, the hostile
`source` value written `"a\b"` is not `a`-backslash-`b`. Python reads `\b` as
U+0008 BACKSPACE, so the **Windows path separator that the test's own docstring
named was never exercised** — while the test passed, for a real reason, because
the pattern rejects a backspace too.

That is the *"claim outruns its assertion"* pattern this repository has now
recorded five times, and the same shape as F7: a test passing without exercising
the property it names. Both spellings are now present as separate cases, and a
guard test asserts the characters directly (`chr(92)`, length 3, and the two
values being distinct). The guard is **mutation-tested**: collapsing the
spelling back makes it fail with `assert '\\' in 'a\x08'` while the other 40
tests in the file still pass — which is precisely why the original defect went
unnoticed.

### Confirmed bugs, fixed

| Finding | What was measured | Correction, and what determined it |
|---|---|---|
| **F1** ambient `Decimal` context reached stored evaluation records | The same inputs produced **three different serialized artifacts** at ambient precisions 6, 28 and 50 (`mean_score` `0.123457` vs `0.123456789`), propagating into every mean, the ECE and every cohort slice | Pinned `EVALUATION_DECIMAL_CONTEXT`. Determined by `docs/DECISION_LOG.md` 2026-08-12, which closed this exact class once already in `CANONICAL_DECIMAL_CONTEXT` |
| **F1b** degenerate log-loss epsilon accepted | `epsilon = 0.5` collapses the clipping interval to a point so every forecast scores `ln 2`; `epsilon = 0.9` inverts it so every score is *replaced*. Both were accepted silently | Refused in `(0, 0.5)` at the public boundary, before any arithmetic |
| **F1c** non-positive `bin_count` | `0` raised a bare `decimal.InvalidOperation` outside the ARGOS taxonomy; `-3` was accepted and produced a report with **zero bins** whose ECE was computed over nothing | Refused as `ContractViolationError`. Determined by the taxonomy rule this repository has applied five times |
| **F6** store identity verified column names only | Six of seven weakened databases were **accepted**: no `UNIQUE (observation_id)`, no `UNIQUE (capture_run_id, ingest_sequence)` on either ledger, no `delivery -> observation` foreign key, neither `CHECK` | Whole-DDL comparison against the schema this build would create, plus foreign-key enforcement. Determined by ADR-0011's "append-only is enforced mechanically, not by convention" |
| **F6b** a refused open mutated the database first | Opening a foreign-stamped file refused it *after* switching its journal mode to WAL, creating four tables and three indexes inside it, and leaving `-wal`/`-shm` beside it | The stamp check moved ahead of every write. A regression test compares bytes, pragmas, object list and directory contents across the refused open |
| **F7** the late-arrival test was a tautology | `assert late >= 0`, on a capture containing **zero** out-of-order arrivals | Replaced with eleven tests on a genuinely out-of-order capture built from unedited real frames. Determined by `docs/13_TEST_STRATEGY.md` and the explicit prohibition on such assertions |

### Findings characterized, not decided

Each is reproduced, and each needs an owner or architectural choice that no
accepted ADR forces. **No policy has been silently adopted for any of them.**

#### F2 — the evaluator walks its own replay loop

`evaluate_capture()` calls `read_capture_arrivals()` directly; it does not
compose `ReplaySession` or `replay_capture`.

*Reproduction.* Redelivering every frame of the recorded capture doubles
`forecast_count` 76 → 152 and `scored_count` 75 → 151 while the state hash is
**unchanged** (`2a7fcb6a…`). A duplicate delivery, an unhandled payload, and an
arrival for a different token all emit a forecast from an unchanged book.

*Affected.* `src/argos/evaluation/run.py`; `EvaluationReportV1.forecast_count`,
`scored_count`, `calibration`, `cohorts`.

*Options.* **(a)** Emit one forecast per arrival — current behaviour; the
metrics then weight a market by how chatty its feed was, and a resend inflates
them. **(b)** Emit one forecast per *state change* of the target book; counts
become a property of the market rather than of the transport, and the
autocorrelation limitation shrinks but does not vanish. **(c)** Emit per arrival
and record which arrivals changed state, deciding at report time.

*Invariants.* All three preserve determinism (ADR-0012) and invariant 14. (b)
and (c) change every recorded count and therefore every stored evaluation
artifact. (a) is what the shipped numbers were produced under.

*Second question, separable.* Whether the evaluator should compose
`ReplaySession` rather than re-walking arrivals. Composing it would put the
scheduler's clock, pacing and counters on the evaluation path; re-walking keeps
them out but means two code paths read the same arrivals. Neither is forced by
ADR-0012, which is about replay determinism and is satisfied by both.

#### F3 — a non-final resolution can be scored

*Reproduction.* A `ResolutionV1` with `resolution_status` `proposed`, `disputed`
or `unknown` and a populated winning token scores all 75 forecasts, exactly as a
`final` one does. The status does not appear in `EvaluationReportV1` at all.

*Why it matters more than it looks.* `proposed` is the **modal** real-world
state: 458 of 500 recently-closed markets carry only `["proposed"]`
(`docs/research/m4-gamma-resolution.md`). A finality filter would exclude most
of the available sample; no filter scores outcomes that can still be disputed.

*Affected.* `src/argos/evaluation/run.py`, `src/argos/evaluation/report.py`,
`ResolutionV1.resolution_status`.

*Options.* **(a)** Score any determined outcome, current behaviour. **(b)** Score
only `final`. **(c)** Score any, and record the status distribution on the
report so a reader can weight it. **(d)** Make finality a caller parameter with
no default.

*Invariants.* All preserve "unresolved markets are not scored as negatives" —
an undetermined market still produces a `ResolutionRefusal` and never reaches
scoring. (a) as it stands does *not* satisfy `.claude/rules/scientific-claims.md`
fully, because the report omits a fact a reader needs; (c) is the minimum that
does, and is a reporting change rather than a policy one.

#### F4 — forecasts made after resolution enter the headline metrics

*Reproduction.* With `resolved_at` set before every arrival, all 75 forecasts
still contribute to `scored_count`, `mean_brier` (0.081225), `mean_log_loss`,
the ECE (0.285) and the calibration bins. Only the `time_to_resolution` cohort
marks them, in an `after_resolution` bucket.

*Affected.* `src/argos/evaluation/run.py` (`_lead_bucket`), the calibration and
cohort reports.

*Options.* **(a)** Retain and mark, current behaviour. **(b)** Exclude from
headline metrics and report separately. **(c)** Refuse the evaluation outright
when any forecast post-dates the resolution, treating it as a join error.

*Invariants.* `docs/05_RESEARCH_PROTOCOL.md`'s leakage rules forbid a forecast
seeing information after its cutoff; a forecast made after settlement is not
leakage by that definition, but it is not evidence about forecasting either.
(b) and (c) change recorded metrics. Note the CLOB resolution path leaves
`resolved_at` as `None`, so today every forecast lands in `unknown` and this
question is latent rather than active.

#### F5 — the evaluation artifact does not bind its own inputs

*Reproduction.* `EvaluationReportV1` binds `source_capture_run_ids`,
`source_state_hash`, `config_fingerprint`, `evaluator_version`,
`log_loss_epsilon`, `calibration_bin_count` and `code_revision`. It binds
**none** of: the resolution id, the resolution's source payload hash, the
evaluated token, the replay manifest, the forecast-emission policy, the ordered
forecast records, or the ordered evaluation records.

*What that means concretely.* Two evaluations of the same capture against two
*different* resolutions, or for two different tokens, produce reports that are
indistinguishable except by their run id and their numbers. The 75 individual
`ForecastEvaluationV1` records exist in memory and are never persisted, so the
report cannot be re-derived from what it stores.

*Affected.* `src/argos/evaluation/report.py`, `src/argos/evaluation/run.py`.

*Options.* **(a)** Add the identifying fields (token, resolution id, resolution
payload hash, methods, emission policy) — a schema change, no new concept.
**(b)** Also persist the ordered forecast and evaluation records, making the
report self-contained and much larger. **(c)** Derive an `evaluation_id` from
the inputs the way `observation_id` is derived (ADR-0010), so two evaluations of
different things cannot collide.

*Invariants.* Core invariant 13 ("every run records configuration, code
revision, schema versions, and data provenance") is **not** currently satisfied
for an evaluation run: the resolution is data provenance and is unrecorded. This
is the one item here where an invariant already points at a direction; it does
not, on its own, choose between (a), (b) and (c).

#### F8 — `last_trade_price` never reaches the evaluation

*The complete path, and where it stops.* The transport yields the frame
unchanged (`argos.sources.clob_ws` decodes nothing). `argos.ingestion.capture._consume_event`
dispatches exactly two event types, `price_change` and `book`; **everything else
becomes an `UNKNOWN_EVENT_TYPE` rejection there.** That is the first and only
rejection point: it never reaches normalization, the store, the dispatcher,
replay, `MarketQuoteV1`, or a baseline.

*Downstream state, for context.* No `last_trade_price` payload model is
registered. `argos.projections.dispatch._HANDLER_FOR` keys three models and
would count an unregistered one as `unhandled_payload`. `MarketQuoteV1` already
*has* a `last_trade_price` field and `BaselineMethod.LAST_TRADE` already exists;
nothing supplies either, so that baseline abstains on every arrival today.

*Integration points, exactly.* (1) a `last_trade_price.vN` `VersionedModel` in
`argos.domain`; (2) a normalizer in `argos.ingestion`; (3) a branch in
`_consume_event`; (4) a handler in `_HANDLER_FOR`; (5) a decision about whether
a trade belongs in `BookState` — it is not a resting order, so it likely does
not, which then requires a place to carry it into `quote_from_book_state`.

*Why it is not implemented in this pass.* Point (5) can change projected state,
and therefore `state_hash.v1`, the golden replay hash, and every stored
evaluation artifact bound to it. That is milestone work, not hardening.

*Classification.* Deferred deliverable. It was observed live on 2026-08-15 and
`docs/research/m2-clob-websocket.md` already names it the M4-relevant event
type.

### Self-reviews

Architecture, security and testing were re-reviewed after the corrections.
**They are SELF-REVIEWS** — performed by the author of the code — and they do
**not** satisfy the independent-review checkbox in
`docs/OWNER_REVIEW_GATE.md`, which remains unticked.

- **Architecture (self-review) — APPROVE.** `argos.evaluation.numeric` sits
  below the rest of the package and is imported by it, not the reverse. The
  event-store change is inside `argos.store` and adds no import. F2's second
  replay loop is left as an open architectural question rather than resolved by
  fiat.
- **Security (self-review) — PASS, one improvement, no new findings.** The
  improvement is real: a refused open no longer writes to a database ARGOS does
  not own, which was a genuine unauthorized-modification path requiring only
  that an operator point the tool at the wrong file. No new network or execution
  surface; the boundary scans pass unchanged; no dependency added.
- **Testing (self-review) — 1,582 tests.** Four findings worth recording: the
  late-event test was a tautology and is replaced; the hostile-`source` list
  named a backslash it never contained; the coverage gate again caught a module
  the happy path never reached; and F1's determinism defect was invisible to the
  whole suite because every test ran at the same ambient precision. The general
  lesson is the same one in three of those four — a suite that never varies a
  global cannot see a dependency on it, and a test that never contains the
  character it names cannot refuse it. Both were found by reading, not by a red
  test.
