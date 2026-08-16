# Architecture

## Style

Use a **modular monolith with ports and adapters** for the research core. Avoid microservices before throughput or isolation requirements justify them.

The design separates deterministic domain behavior from network, clock, persistence, and interface concerns.

```text
                       PUBLIC POLYMARKET SOURCES
                Gamma REST | CLOB REST | Market WebSocket
                                  |
                                  v
                         Source-specific adapters
                                  |
                                  v
                     Canonical ObservationEnvelope
                                  |
                    +-------------+-------------+
                    |                           |
                    v                           v
             Immutable event store      Live event dispatcher
                    |                           |
                    |                           v
                    |                   Deterministic core
                    |                           |
                    +-----------> Replay source|
                                                v
                                      Market state projections
                                                |
                     +--------------------------+----------------------+
                     |                          |                      |
                     v                          v                      v
              Market baseline            Contract compiler      Resolution resolver
                     |                          |                      |
                     +--------------------------+----------------------+
                                                v
                                      Forecast/evaluation ledger
```

## Packages

The intended package boundaries are:

```text
argos.domain          versioned domain contracts and invariants
argos.clock           Clock protocol, LiveClock, ReplayClock, plus the
                      live-only Pacer protocol and RealPacer (ADR-0009)
argos.config          validated immutable configuration and run manifests
argos.sources         source-specific REST/WebSocket clients
argos.ingestion       retries, reconnect, normalization, dedupe, backpressure
argos.store           event-store and ledger protocols plus local adapters
argos.projections     deterministic state built from observations
argos.compiler        market-rule contract representation and review flow
argos.replay          capture readers, replay scheduler, watermarks, run hashes
argos.baselines       market midpoint, executable quote, and naive baselines
argos.resolution      lifecycle and winning-outcome normalization
argos.evaluation      scoring rules, calibration bins, cohort reports
argos.cli             operator commands only; no domain logic
```

Future packages after the owner gate may include `evidence`, `reson`, `forecasting`, `reliability`, `fusion`, `policy`, `api`, and `ui`.

## Dependency direction

```text
cli / adapters / storage
          |
          v
application services
          |
          v
domain contracts and pure functions
```

Domain code must not import HTTP clients, database implementations, Typer, environment variables, or wall-clock functions.

## Live and replay identity

Live mode and replay mode must construct the same canonical event objects and call the same application service.

Only these components may differ:

- event source;
- clock implementation;
- pacing policy;
- output storage location.

Do not create `if replay:` branches inside forecasting or state logic.

Pacing and timekeeping are separate ports (ADR-0009): `Clock` exposes only
`now()`. Real elapsed time — waiting and deadlines — is a `Pacer`
(`argos.clock.pacing`), used only by live source adapters for retry backoff
and connection deadlines. A live adapter's retry pacing has no replay
counterpart: a replay run issues no request, so there is nothing to retry or
wait for. The "pacing policy" that legitimately differs between live and
replay is the *scheduler's* pacing (real-time vs. accelerated vs. stepwise
replay), which is separate M3 work in `argos.replay` and must never influence
the output hash.

## Temporal model

Every source observation records at least:

- `event_time`: time assigned by the source to the event;
- `received_time`: time ARGOS received it;
- `ingest_sequence`: local monotonic order of accepted input;
- optional source sequence/hash.

Deterministic replay reproduces `ingest_sequence`. Domain windows use `event_time` under an explicit late-event/watermark policy. Reordering late data into the past is forbidden unless running a separately labeled corrected-history experiment.

## Persistence model

- Append-only observations.
- Unique deterministic event ID where source fields permit it; otherwise content hash plus source key.
- Duplicate insert is idempotent and observable.
- Normalized payload and raw payload/hash are both retained.
- Forecasts, decisions, resolutions, and evaluations are append-only records linked by IDs and versions.
- Corrections create superseding records; they do not overwrite history.

## Failure model

Each adapter exposes health and counters for:

- reconnects;
- parsing failures;
- invalid timestamps;
- duplicates;
- gaps;
- backpressure/dropped input;
- stale subscriptions;
- REST fallback use;
- schema version mismatch.

The system must fail loudly when it cannot prove the integrity of a capture.

## Scaling principle

Start with a local implementation and stable protocols. Replace storage or transport only behind interfaces and only after profiling. Do not introduce Kafka, Redis, Kubernetes, or distributed consensus during M0-M4.
