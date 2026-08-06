# Read this first

This package is the operating system for starting **ARGOS — a Polymarket Probability Intelligence Engine** with Claude Code.

It is deliberately more restrictive than a normal starter repository. The goal is to let a colleague and Claude Code advance independently through a well-defined research foundation without drifting into a dashboard, a generic AI agent, or an execution bot.

## What this package gives you

- a repository-level `CLAUDE.md` with non-negotiable invariants;
- a lead agent and specialized project subagents under `.claude/agents/`;
- reusable Claude Code skills for bootstrapping, continuing milestones, ADRs, quality gates, market audits, and handoff;
- deterministic safety hooks;
- a complete product, architecture, domain, research, security, testing, and milestone specification;
- a minimal Python scaffold and CI pipeline that Claude can extend;
- an explicit autonomy boundary: Claude may build through **M4**, then must stop for owner review.

## First use

1. Copy the complete contents of this package into a new empty private Git repository.
2. Install Python 3.12+, `uv`, Git, and a current Claude Code release.
3. From the repository root, run:

   ```bash
   uv sync
   claude doctor
   claude
   ```

4. Inside Claude Code, verify project configuration with `/status` and inspect agents with `/agents`.
5. Run:

   ```text
   /bootstrap
   ```

6. Then paste the contents of `START_PROMPT.md` as the first project prompt.

## Human responsibility

Claude is allowed to edit and commit locally. A human remains responsible for:

- repository creation and access control;
- reviewing any permission prompt;
- rotating and storing credentials;
- pushing branches if project policy requires it;
- approving the owner gate after M4;
- deciding whether later phases may include external evidence, LLM extraction, or execution.

## Non-negotiable boundary

The autonomous target is a **read-only research core** with market discovery, market-rule normalization, public market-data capture, immutable storage, deterministic replay, baseline probability evaluation, and a complete handoff.

It must not place orders, connect a wallet, request private trading credentials, or implement an execution adapter.
