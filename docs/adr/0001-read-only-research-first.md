# ADR-0001: Research-only, read-only first

- Status: Accepted
- Date: 2026-08-06

## Context

The previous ARGOS prototype mixed monitoring, heuristic decision labels, notifications, and evaluation before establishing replayable data and calibrated targets. Polymarket provides a cleaner ground truth, but adding execution early would contaminate architecture and safety.

## Decision

M0-M4 use public read-only data and produce research records only. There is no wallet, authenticated channel, order, position, or execution code.

## Consequences

- Core contracts remain useful whether execution is later added or not.
- Research claims can be evaluated before capital/risk concerns.
- Some executable-edge questions are represented using public quotes and depth, but no order is sent.
