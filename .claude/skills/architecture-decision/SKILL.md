---
name: architecture-decision
description: Create a structured ADR for a durable ARGOS architecture, contract, storage, dependency, security, or research-policy choice.
disable-model-invocation: true
allowed-tools: Read, Glob, Grep, Edit, Write, Agent
---

Before writing an ADR:

1. Confirm the decision is durable and affects a boundary, persistent contract, dependency direction, storage, clock/replay semantics, safety policy, or scientific protocol.
2. Ask architecture-guardian for alternatives and consequences.
3. Use the next numeric ID in `docs/adr/`.
4. Include: title, status, date, context, decision, alternatives, consequences, reversibility, and follow-up.
5. Update `docs/adr/0000-index.md` and `docs/DECISION_LOG.md`.
6. Do not rewrite old accepted ADRs; supersede them.
