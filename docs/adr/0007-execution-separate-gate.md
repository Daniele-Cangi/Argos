# ADR-0007: Execution is outside the autonomous scope

- Status: Accepted
- Date: 2026-08-06

## Context

The owner wants Claude Code to advance autonomously while humans resume after a structured research foundation exists. Execution introduces secrets, financial risk, legal concerns, and irreversible side effects.

## Decision

Autonomous work stops after M4. No execution code may be implemented. Any future execution work requires a new owner-approved ADR and preferably a separate adapter/repository.

## Consequences

- Project hooks and security review block obvious drift.
- Owner review can evaluate the research core independently.
- No private trading credentials are needed.
