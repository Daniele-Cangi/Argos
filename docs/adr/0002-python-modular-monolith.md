# ADR-0002: Python modular monolith

- Status: Accepted
- Date: 2026-08-06

## Context

The initial work is data normalization, replay, statistics, and research. Distributed services would add failure modes without proven throughput need.

## Decision

Use Python 3.12 and a modular monolith with ports/adapters. Keep domain, application, and infrastructure dependencies directed inward.

## Consequences

- Fast iteration and strong scientific ecosystem.
- Simple local operation and testing.
- Storage/transport can later be replaced behind protocols after profiling.
