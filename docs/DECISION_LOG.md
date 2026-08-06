# Decision log

Accepted architecture decisions live in `docs/adr/`. Use this file for small operational choices that do not merit an ADR.

| Date | Milestone | Decision | Reason | Reversible? | Reference |
|---|---|---|---|---|---|
| 2026-08-06 | Bootstrap | Autonomous implementation stops after M4 | Owner review before evidence/forecasting expansion | Yes | ADR-0007 |
| 2026-08-06 | M0 | Schema version is a `ClassVar` on `VersionedModel`, re-attached by `to_record()` | Version describes the class, not a mutable field; a subclass that omits it fails at import time | Yes | `src/argos/domain/versioning.py` |
| 2026-08-06 | M0 | Unrecognized `ARGOS_*` environment variables fail the load | pydantic-settings ignores them, so a typo would silently keep a default and a stray credential-shaped variable would go unnoticed | Yes | `src/argos/config/settings.py` |
| 2026-08-06 | M0 | `ensure_utc` rejects naive datetimes instead of assuming UTC | A naive timestamp is ambiguous evidence; assuming UTC would corrupt event-time reasoning | Yes | `src/argos/clock/base.py` |
| 2026-08-06 | M0 | `ReplayClock.advance_to` accepts an equal instant but rejects backwards moves | Several observations legitimately share one event time; regression is a determinism bug | Yes | `tests/test_clock.py` |
| 2026-08-06 | M0 | Architecture and no-execution boundaries enforced by AST tests, not review alone | A rule that only exists in prose drifts; `tests/test_boundaries.py` fails the build instead | Yes | `tests/test_boundaries.py` |
| 2026-08-06 | M0 | Storage engine choice deferred to M2 | The SQLite/WAL vs append-log comparison needs real capture volumes; deciding now would be speculation | Yes | `docs/12_TECH_STACK.md` |
