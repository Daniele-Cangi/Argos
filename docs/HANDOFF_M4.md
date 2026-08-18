# ARGOS handoff — Owner Review Gate A (after M4)

Structure follows `docs/10_HANDOFF.md` exactly.

## 1. Executive state

- **Commit**: `42a49ae` on branch `m3-deterministic-replay`.
- **Milestones completed**: M0, M1, M2, M3, M4.
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

- **Branch**: `m3-deterministic-replay`, ahead of `main`. Not pushed.
- **Uncommitted changes**: none at `42a49ae`; this document is the next commit.
- **Open TODOs**: none in source. `docs/BACKLOG.md` carries every deferred item
  with its reasoning, including four that are closed-with-evidence and one
  (`~/.cache/argos-sec-probe/e.sqlite3`) that is an owner cleanup on a different
  machine.
- **Local-only generated artifacts**: `.data/` (captures), `.venv/`,
  `.hypothesis/`, `.mypy_cache/`, `.pytest_cache/`, `__pycache__/` — all
  gitignored. No generated capture data is committed. The committed fixtures are
  recorded source payloads with provenance sidecars, hash-checked by
  `tests/test_fixtures.py`.
