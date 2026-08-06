# ARGOS status

Last updated: 2026-08-06

## Current state

- Current milestone: **M0 — Foundation** (implementation complete, closure reviews pending)
- Autonomous target: **complete M0-M4**
- Owner gate: **required after M4**
- Execution capability: **prohibited and absent**
- External evidence / LLM forecasting: **not started**

## Current objective

Close M0: obtain architecture, security, and testing reviews for the foundation
slice, then start M1 market discovery.

## M0 checklist

- [x] Run `/bootstrap` and create initial task plan.
- [x] Generate `uv.lock` from a clean environment.
- [x] Create package boundaries from `02_ARCHITECTURE.md`.
- [x] Implement immutable settings and execution-disabled validation.
- [x] Implement Clock protocol, LiveClock, and ReplayClock contracts.
- [x] Establish structured logging and error taxonomy.
- [x] Confirm CI and local quality commands pass.
- [x] Add ADR index.
- [ ] Architecture review complete.
- [ ] Security review complete.
- [ ] Milestone summary committed.

## Evidence

| Exit criterion | Evidence |
|---|---|
| `uv sync --all-groups` succeeds from a clean checkout | `uv.lock` committed; sync run on Python 3.13 with the 3.12 floor from `pyproject.toml` |
| `ruff check`, `ruff format --check`, `mypy`, `pytest` pass | `uv run python scripts/claude/quality_gate.py` → PASS (75 tests) |
| Domain package imports no HTTP/database/UI packages | `tests/test_boundaries.py::test_domain_imports_no_infrastructure` (AST scan of `src/argos/domain`) |
| `LiveClock` and `ReplayClock` contracts have tests | `tests/test_clock.py` (11 tests: protocol conformance, UTC enforcement, regression rejection, reproducibility) |
| Configuration rejects `ARGOS_EXECUTION_ENABLED=true` | `tests/test_config.py::test_execution_enabled_is_rejected*` → `argos.execution_prohibited` |
| Architecture and security agents report no blockers | **pending** |

Additional guardrails added beyond the checklist:

- `tests/test_boundaries.py::test_no_execution_surface_in_source` scans declared
  names across `src/argos` for wallet/order/credential identifiers;
- unrecognized `ARGOS_*` environment variables fail the load instead of being
  silently ignored;
- `RunManifest` (`run_manifest.v1`) records config fingerprint, code revision, and
  settings snapshot, and is stamped from an injected clock so replay runs
  reproduce it exactly.

## Active blockers

None.

## Known limitations at M0

- Storage engine is undecided; the SQLite/WAL vs append-log comparison required by
  `docs/12_TECH_STACK.md` needs real M2 capture volumes and will land as an ADR.
- The `sources`, `ingestion`, `store`, `projections`, `compiler`, `replay`,
  `baselines`, `resolution`, and `evaluation` packages are documented boundaries
  only; they contain no implementation yet.
- CI has not yet run on the remote branch for this slice.

## Next owner action

No action required until the M4 owner gate unless Claude records a true blocker.
