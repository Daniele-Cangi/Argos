# ADR-0008: Public Polymarket APIs first

- Status: Accepted
- Date: 2026-08-06

## Context

Polymarket currently exposes public Gamma, Data, CLOB read endpoints, and a public market WebSocket. Official SDKs also include authenticated trading functionality that is not needed initially.

## Decision

M0-M4 implement or wrap only public read behavior. Source adapters must keep the authentication boundary explicit and must not import trading configuration into domain/application packages.

## Consequences

- No credentials are required for the autonomous milestone set.
- Adapters remain small and auditable.
- Official SDK adoption may be reconsidered if it materially improves schema correctness without expanding permissions.
