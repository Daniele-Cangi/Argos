---
name: handoff
description: Produce the mandatory M4 owner handoff with reproducible commands, architecture map, contract inventory, milestone evidence, scientific/security status, and open decisions.
disable-model-invocation: true
allowed-tools: Read, Glob, Grep, Edit, Write, Bash, Agent
---

Use `docs/10_HANDOFF.md` as the exact structure.

Before writing:

1. run the full quality gate;
2. obtain final architecture, security, and test reviews;
3. verify a clean checkout workflow from documentation;
4. run one representative discovery, audit, capture/replay, and baseline evaluation path using public data or committed fixtures;
5. record exact commit and config hashes;
6. inspect for secrets and authenticated/trading code;
7. collect unresolved assumptions and owner decisions.

Write `docs/HANDOFF_M4.md`. Update STATUS to `WAITING_FOR_OWNER_GATE_A`. Do not implement M5.
