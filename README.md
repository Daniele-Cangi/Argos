# ARGOS

[![CI on main](https://github.com/Daniele-Cangi/Argos/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Daniele-Cangi/Argos/actions/workflows/ci.yml?query=branch%3Amain)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue)](pyproject.toml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Technical T1-T8: verified](https://img.shields.io/badge/Technical_T1--T8-verified-green)](experiments/m4-technical-20260926-v1/README.md)
[![M4: in progress](https://img.shields.io/badge/M4-in_progress-yellow)](docs/STATUS.md)

**Replayable, evidence-driven probability-intelligence research for Polymarket.**

ARGOS discovers public binary markets, preserves their rules and source payloads,
captures CLOB books and trades, and replays the original arrival sequence through
the same domain handlers. It builds explicit market-baseline **scores** and
evaluates admissible forecasts against recorded final outcomes. Calibration,
predictive usefulness and trading edge are not established.

The system is read-only with respect to trading: no wallet, private trading
channel, order placement or execution adapter. Advanced forecasting and external
evidence engines are future owner-gated work, not implemented capability.

## Current state — 2026-10-03

PRs [#25](https://github.com/Daniele-Cangi/Argos/pull/25) and
[#26](https://github.com/Daniele-Cangi/Argos/pull/26) are merged. Post-merge
[Windows/Ubuntu CI](https://github.com/Daniele-Cangi/Argos/actions/runs/37128655607)
passed at `6dcc138`. The live CI badge tracks `main`; the technical and M4 badges
are descriptive labels linked to their evidence/status, not automated verdicts.

| Area | Verified boundary |
|---|---|
| Discovery, capture and replay | Public adapters, immutable raw/EventStore records, shared live/replay handlers and reproducibility checks. |
| Technical qualification | T1-T8 all PASSED; 14,638 original proof files independently verified. This is not prospective or predictive evidence. |
| M4 cohort implementation | V2 declaration, reviewed partial admission and bounded capture-to-freeze owner with no-network synthetic integration and fault tests. |
| Current persistent records | V3 baseline forecasts, V2 frozen snapshots and V3 terminal journals; separate book/trigger clocks, anchored arrivals and version-pinned nested records. |
| Operational M4 / Owner Gate A | Not closed. No live ADR-0020/V2 cohort is frozen or launched; runtime disk/failure handling and the unified outcome/scoring path remain open. |
| Preliminary / scientific results | No completed live V2 cohort evaluation; calibration NOT_ESTABLISHED and no predictive or edge claim. Historical negative results remain unchanged. |

Local verification of the source merged through PR #26: **2,535 tests passed,
two Windows symlink-privilege skips**. The affected 257-test branch-coverage set
measured accounting 97%, journal/replay 94%, and snapshots 91%. Ruff, formatting,
strict mypy and proof verification passed. These dated measurements are not a
claim that future commits are already verified.

## Next implementation steps

1. Enforce actual campaign-wide disk budgets and reserved free space, including
   raw bytes, SQLite/WAL, receipt/arrival artifacts, logs and failure records.
2. Complete durable crash/failure accounting and exclusive recovery without
   duplicate ownership, target replacement or recapture.
3. Join original snapshots to early/late finality and produce separate operational,
   pending/outcome and per-method descriptive scoring reports.
4. Prove the vertical path with small synthetic fault fixtures; then obtain owner
   approval for a separately frozen **2-4-slot live integration pilot**.

The pilot uses bounded captures (first of at most 120 seconds, 500 frames or the
declared byte cap) and normally 180-minute finality monitoring. Pending outcomes
remain pending: operational review does not require waiting for every settlement.
Neither the existing preflight nor this README authorizes a live launch.

## Local setup and verification

Requires Python 3.12+, Git and `uv`. On Windows PowerShell, set
`$env:PYTHONUTF8 = "1"` before running commands that print Unicode documentation.

```bash
uv sync --frozen --all-groups
uv run argos --help
uv run python scripts/claude/quality_gate.py --quick
uv run python scripts/claude/quality_gate.py
uv run python scripts/m4_technical_proof.py verify --proof experiments/m4-technical-20260926-v1/proof
```

The default gate runs lint, format, typing and tests. The separate
`quality_gate.py --coverage` gate enforces per-module branch-coverage thresholds;
Ubuntu CI runs it. Verification does not start a live experiment.
See the [runbook](docs/RUNBOOK.md) for individual CLI commands and their effects.

## Documentation and governance

- [Start here](README_FIRST.md): setup and safe continuation.
- [AGENTS.md](AGENTS.md) and [core invariants](docs/CORE_INVARIANTS.md): authoritative constraints.
- [Current status](docs/STATUS.md) and [backlog](docs/BACKLOG.md): dated evidence and open work.
- [Roadmap](docs/06_ROADMAP.md), [milestones](docs/07_MILESTONES.md) and [Owner Gate A](docs/OWNER_REVIEW_GATE.md).
- [ADR-0020](docs/adr/0020-budgeted-cohorts-and-separate-research-claims.md) and [M4 cohort plan](docs/research/m4-asynchronous-cohort-design.md): operational, descriptive and scientific claims kept separate.
- [Technical proof](experiments/m4-technical-20260926-v1/README.md): immutable T1-T8 qualification evidence.

`CLAUDE.md`, `.claude/` and earlier handoffs are retained as historical context;
they do not override `AGENTS.md` or superseding owner decisions. M5-M8 remain
owner-gated. No LSTM or other advanced engine is part of the current M4 scope.

## License

Original ARGOS code and documentation are licensed under the
[Apache License, Version 2.0](LICENSE); see [NOTICE](NOTICE) for attribution.
Third-party dependencies, recorded source payloads, market text and external
data retain their own rights and terms. Their inclusion as provenance evidence
does not relicense them as ARGOS software.
