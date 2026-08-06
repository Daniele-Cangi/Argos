# ADR-0003: Deterministic arrival-order replay

- Status: Accepted
- Date: 2026-08-06

## Context

Source event time and the time ARGOS learns an event are different. Reordering late events into their source timestamp can give historical models information earlier than it was actually available.

## Decision

Capture a monotonic local ingest sequence and replay original arrival order. Preserve event time for windows and use an explicit watermark/late-event policy. Corrected-history analysis, if added, is a separately labeled experiment.

## Consequences

- Historical forecasts match the knowledge available to live ARGOS.
- Late-data behavior is explicit.
- Reproducibility requires capture manifests and stable tie-breaking.
