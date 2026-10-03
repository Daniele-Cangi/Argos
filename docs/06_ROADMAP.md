# Roadmap

## Current position — 2026-10-03

Phase A is still in progress at M4; Owner Gate A has not passed. PR #26 is merged
and its post-merge Windows/Ubuntu CI passes. The T1-T8 technical matrix is
qualified, and V2 admission/capture/freeze/replay is synthetic-tested. Neither
result establishes a completed live V2 cohort, calibration or predictive edge.

Next: actual total-storage/free-space enforcement and durable failure handling,
exclusive resumable early/late finality, then an integrated synthetic scoring
proof. A separately owner-approved 2-4-slot live pilot follows those checks;
pending settlement alone does not hold operational closure open. See
[status](STATUS.md), [backlog](BACKLOG.md) and
[ADR-0020 cohort plan](research/m4-asynchronous-cohort-design.md).
Phases B-F below are future scope, not implemented or automatically authorized.

## Phase A — trustworthy instrument

- M0 Foundation and governance
- M1 Market discovery and semantic contract skeleton
- M2 Public CLOB capture and immutable event store
- M3 Deterministic replay
- M4 Baseline forecasts, resolution ingestion, and evaluation

**Owner Gate A** follows M4.

## Phase B — evidence intelligence

- M5 external evidence model and source provenance;
- semantic extraction with strict untrusted-input isolation;
- market-specific evidence graph;
- market-rule ambiguity workflow.

## Phase C — RESON reborn

- M6 online change detection over independent evidence channels;
- novelty, persistence, burst, and concordance outputs;
- no direct YES/NO prediction in RESON itself.

## Phase D — calibrated forecasting

- M7 domain engines;
- calibration per engine;
- dependency-aware fusion;
- reliability by category/regime/time-to-resolution;
- abstention and disagreement.

## Phase E — research interface

- M8 query API and research dashboard;
- forecast timeline, evidence provenance, market comparison, calibration, and replay inspection.

## Phase F — optional execution

Execution is not implied by the roadmap. If approved, it should be a separate adapter or repository with independent threat model, credentials, risk controls, and human review.
