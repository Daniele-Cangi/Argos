# ADR-0005: LLMs do not form the forecasting core

- Status: Accepted
- Date: 2026-08-06

## Context

LLMs can parse rules and structure evidence, but asking an LLM directly for an opaque probability creates reproducibility, calibration, provenance, and prompt-injection problems.

## Decision

LLMs may later assist semantic extraction and market-contract compilation under strict provenance and human review. They are not the primary probability generator through the initial advanced phases.

## Consequences

- Forecasts remain measurable and versionable.
- Semantic tooling can still benefit from language models.
- Any later LLM forecast experiment must be isolated and benchmarked as a separate engine.
