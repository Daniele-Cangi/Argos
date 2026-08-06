---
name: continue-milestone
description: Resume ARGOS from STATUS, choose the next unmet acceptance criterion, delegate it safely, validate it, document it, and make a local commit.
disable-model-invocation: true
allowed-tools: Read, Glob, Grep, Edit, Write, Bash, Agent
---

1. Read `docs/STATUS.md`, `docs/BACKLOG.md`, current milestone criteria, relevant contracts, and ADRs.
2. Verify the working tree. If unrelated uncommitted changes exist, isolate the task and do not overwrite them.
3. Choose one smallest high-value unmet criterion with no unresolved dependency.
4. State the criterion and planned files.
5. Delegate bounded research or implementation to the matching specialist.
6. Integrate and run focused tests.
7. Ask test-engineer for an independent boundary/failure test pass when behavior changed.
8. Run the relevant quality checks.
9. Update status, backlog, assumptions/decision log, and limitations.
10. Inspect diff and create one local commit.
11. Continue with the next slice unless at a true blocker or M4 owner gate.
