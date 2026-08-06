---
name: close-milestone
description: Verify every exit criterion, run the full gate, update evidence, and create a milestone summary commit. Stops at M4 owner gate.
disable-model-invocation: true
allowed-tools: Read, Glob, Grep, Edit, Write, Bash, Agent
---

1. Enumerate every exit criterion for the current milestone.
2. Link each criterion to tests, code, manifests, reports, or review evidence.
3. Run `/quality-gate` behavior in full.
4. Ensure no open blocking findings.
5. Mark the milestone complete in STATUS and move the next milestone to current.
6. Update BACKLOG and DECISION_LOG.
7. Create a milestone summary commit.
8. If the completed milestone is M4, do not start M5. Invoke `/handoff` behavior and stop.
