---
name: bootstrap
description: Initialize ARGOS correctly from a new repository, validate Claude project configuration, build the M0 task plan, and begin the first vertical slice.
disable-model-invocation: true
allowed-tools: Read, Glob, Grep, Edit, Write, Bash, Agent
---

Bootstrap ARGOS in this exact order:

1. Run `python scripts/claude/validate_bootstrap.py`.
2. Read `CLAUDE.md`, `docs/CORE_INVARIANTS.md`, `docs/00_VISION.md`, `docs/02_ARCHITECTURE.md`, `docs/07_MILESTONES.md`, `docs/08_DEFINITION_OF_DONE.md`, all accepted ADRs, and `docs/STATUS.md`.
3. Inspect `git status`, branch, and recent commits. Do not alter history.
4. Run `uv sync --all-groups` and create/update `uv.lock`.
5. Run `python scripts/claude/quality_gate.py --quick`.
6. Ask the architecture-guardian and security-reviewer for a bootstrap sanity review. They must not edit files.
7. Convert M0 into a dependency-ordered task list in `docs/STATUS.md` and `docs/BACKLOG.md`.
8. Select the smallest M0 vertical slice. Delegate implementation to the appropriate specialist, integrate it, test it, update docs, and commit locally.
9. Continue using the normal lead protocol. Do not stop merely because bootstrap succeeded.

Do not add any credential or authenticated Polymarket feature. Do not create a frontend. Do not broaden dependencies during bootstrap without an ADR-level reason.
