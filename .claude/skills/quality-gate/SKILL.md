---
name: quality-gate
description: Run deterministic ARGOS code-quality checks and obtain independent architecture, security, and testing reviews before milestone closure.
allowed-tools: Read, Glob, Grep, Edit, Write, Bash, Agent
---

Run the full quality gate:

1. `python scripts/claude/quality_gate.py`
2. Review git diff and ensure generated data/secrets are absent.
3. Ask test-engineer to map tests to the changed acceptance criteria.
4. Ask architecture-guardian for a closure verdict.
5. Ask security-reviewer for a closure verdict.
6. Fix blockers; rerun the gate.
7. Write a concise milestone evidence section in `docs/STATUS.md` with commands and artifact paths.

A milestone cannot close on `APPROVE_WITH_FOLLOWUPS` unless every follow-up is placed in BACKLOG with owner, priority, and reason. It cannot close on `BLOCK`.
