# ARGOS project instructions

@docs/CORE_INVARIANTS.md
@docs/STATUS.md

## Mission

Build ARGOS as an evidence-driven Polymarket probability intelligence engine. The core product is not “signals”; it is a reproducible system that distinguishes observations, forecasts, market prices, edge, decisions, abstention, and eventual resolution.

## Operating protocol

1. Read `docs/STATUS.md`, the current milestone in `docs/07_MILESTONES.md`, and relevant ADRs before changing code.
2. Select the smallest unmet acceptance criterion that creates a complete vertical slice.
3. Delegate bounded research, architecture, schema, ingestion, replay, scientific, testing, security, or documentation work to the matching project subagent.
4. Never allow parallel agents to edit the same file set.
5. Implement, test, review, document, and locally commit each slice.
6. Update `docs/STATUS.md`, `docs/BACKLOG.md`, and `docs/DECISION_LOG.md` before ending a meaningful session.
7. Continue autonomously through M4. Stop at the owner gate defined in `docs/OWNER_REVIEW_GATE.md`.

## Hard prohibitions

- No order placement, cancellation, wallet connection, private key, mnemonic, L1/L2 trading authentication, position management, or execution adapter.
- No UI before M4 passes.
- No external-news or LLM forecasting layer before the M4 owner gate.
- No uncalibrated score named `probability`, `confidence`, `p_yes`, `p_no`, or equivalent.
- No midpoint treated as an executable price. Preserve bid, ask, spread, depth, and quote side.
- No silent mutation of market rules, raw source payloads, or historical events.
- No hidden global state that changes output based on asset processing order.
- No use of wall clock directly inside deterministic domain logic. Inject a clock.
- No unsupported claims such as “beats the market”, “predictive edge”, or “information lead” without pre-specified evaluation.

## Engineering rules

- Python 3.12+, strict typing, Pydantic contracts, UTC timestamps, `Decimal` for prices and probabilities at boundaries.
- Domain code is pure where possible. Network, filesystem, clock, and database access are adapters behind protocols.
- Live and replay modes call the same event handlers and state transitions.
- Store raw payload plus normalized payload and schema/compiler version.
- Tests do not use live network calls except explicit smoke tests.
- Every public schema and persistent record is versioned.
- Every rejected or late event receives a reason; never silently drop.
- Prefer small modules and explicit contracts over compatibility wrappers.

## Completion behavior

Do not stop after writing code. A task is complete only when tests, typing, lint, docs, status, and the relevant acceptance criterion are updated. Use `/quality-gate` before declaring a milestone complete and `/handoff` at the M4 owner gate.
