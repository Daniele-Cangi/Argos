# ADR-0004: Immutable source-linked event storage

- Status: Accepted
- Date: 2026-08-06

## Context

Normalization bugs, source schema changes, and rule clarifications are inevitable. Overwriting normalized data would make past forecasts impossible to audit.

## Decision

Store append-only observation envelopes linked to raw payload hashes/locations. Duplicates are idempotent and counted. Corrections and compiler updates create superseding records.

## Consequences

- Reprocessing and audit are possible.
- Storage volume is higher.
- Retention and compaction must preserve provenance.
