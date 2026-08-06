# Milestones and exit criteria

Claude Code may progress autonomously through M4. It must close each milestone with architecture, testing, security, and documentation review.

## M0 — Foundation

### Deliverables

- validated Python project and lock file;
- package boundaries matching the architecture;
- immutable settings model and injected clock protocol;
- structured logging and error taxonomy;
- CLI skeleton;
- CI for lint, format, strict typing, and tests;
- ADR index, status, backlog, and runbook;
- no secrets in repository or history introduced by this work.

### Exit criteria

- `uv sync --all-groups` succeeds from a clean checkout;
- `ruff check`, `ruff format --check`, `mypy`, and `pytest` pass;
- domain package imports no HTTP/database/UI packages;
- `LiveClock` and `ReplayClock` contracts have tests;
- configuration rejects `ARGOS_EXECUTION_ENABLED=true` during M0-M4;
- architecture and security agents report no blockers.

## M1 — Market discovery and contract skeleton

### Deliverables

- Gamma API adapter using public endpoints;
- market/event metadata raw capture;
- `MarketDefinitionV1` normalization;
- configurable binary-market selection policy;
- outcome-to-token mapping validation and quarantine path;
- `CompiledMarketContractV1` skeleton preserving source rules;
- market audit CLI producing a human-reviewable report;
- recorded fixtures from official API responses with retrieval metadata.

### Exit criteria

- repeated normalization of the same raw payload is deterministic;
- malformed outcome/token mapping cannot enter active selection;
- original question, description, resolution source, and dates are retained;
- compiler never marks a contract human-reviewed automatically;
- network adapter contract tests use fixtures; unit tests do not require internet;
- at least one explicit ambiguity test and one token-mapping failure test pass.

## M2 — CLOB capture and immutable event store

### Deliverables

- public CLOB REST order-book snapshot adapter;
- public market WebSocket adapter with subscription updates;
- typed canonical payloads for required event types;
- reconnect with bounded exponential backoff and jitter;
- idempotent immutable event store implementation;
- capture manifest and health counters;
- bounded queue/backpressure policy;
- capture CLI for a small selected market set;
- sanitized sample capture fixture committed only if size and licensing are appropriate.

### Exit criteria

- duplicate source event does not create a second accepted observation;
- zero-size level update is represented as removal;
- reconnect does not reset ingest sequence or silently lose manifest state;
- invalid messages enter a rejection ledger with reason and raw hash;
- book snapshot plus deltas reconstruct a tested projection;
- no authenticated/user channel or trading code exists;
- an interrupted capture closes or marks its manifest incomplete.

## M3 — Deterministic replay

### Deliverables

- replay source reading capture records by original ingest order;
- `ReplayClock` controlled only by the replay scheduler;
- explicit late-event and watermark policy;
- same dispatcher/projection handlers in live and replay;
- replay manifest and output state hash;
- accelerated and stepwise replay modes;
- golden replay fixture and deterministic regression test.

### Exit criteria

- identical input + code + config produces identical output hash across at least three runs;
- replay never reads wall clock inside domain logic;
- late and invalid event behavior is deterministic and counted;
- changing a source event produces a predictable hash change;
- live adapter can be replaced by replay source without changing domain handlers;
- replay performance is measured but correctness takes precedence.

## M4 — Baseline probability and evaluation

### Deliverables

- versioned market baseline forecast records;
- midpoint, displayed-price method, last-trade, bid/ask, spread, and quote-time representation;
- final resolution normalization from available public lifecycle data;
- proper scoring evaluator;
- calibration bins with sample counts;
- cohort report by at least category, spread bucket, and time-to-resolution bucket when data permits;
- CLI to evaluate a captured/resolved dataset;
- reproducibility manifest and limitations report.

### Exit criteria

- midpoint is never labeled executable price;
- unresolved markets are not scored as negatives;
- probability values are validated and log-loss clipping is declared;
- baseline evaluation is reproducible from stored records;
- reports include missing data and sample counts;
- no advanced predictive engine is introduced merely to make metrics interesting;
- all quality gates pass;
- `docs/OWNER_REVIEW_GATE.md` checklist is complete;
- `/handoff` produces the owner package and implementation stops.

## M5 — External evidence and semantic intelligence — owner approval required

Potential work only after review:

- evidence-source adapters;
- claim/evidence graph;
- source independence groups;
- LLM extraction under prompt-injection defenses;
- semantic contract assistance and human review UI.

## M6 — RESON change evidence — owner approval required

- multiscale novelty, burst, persistence, and concordance;
- independent raw channels;
- no TECH feedback loop;
- outputs are change evidence, not direct probabilities.

## M7 — Forecasting, reliability, fusion, abstention — owner approval required

- calibrated engine forecasts;
- hierarchical reliability;
- dependency-aware combination;
- disagreement metrics;
- selective prediction.

## M8 — Research API and UI — owner approval required

Only after the core and metrics are stable.
