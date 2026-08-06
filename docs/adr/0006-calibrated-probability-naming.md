# ADR-0006: Probability naming requires calibration

- Status: Accepted
- Date: 2026-08-06

## Context

ARGOS v1 used names such as `p_move` and confidence for monotonic transformations of heuristics. This made outputs look probabilistic without empirical calibration.

## Decision

Uncalibrated model output is named `raw_score`. `p_yes` is populated only when an explicit calibration model and version exist. Reports always disclose calibration state and sample count.

## Consequences

- Early modules may expose scores rather than probabilities.
- Scientific language remains honest.
- Calibration becomes a first-class subsystem rather than cosmetic post-processing.
