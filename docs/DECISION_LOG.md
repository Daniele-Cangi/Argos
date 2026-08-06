# Decision log

Accepted architecture decisions live in `docs/adr/`. Use this file for small operational choices that do not merit an ADR.

| Date | Milestone | Decision | Reason | Reversible? | Reference |
|---|---|---|---|---|---|
| 2026-08-06 | Bootstrap | Autonomous implementation stops after M4 | Owner review before evidence/forecasting expansion | Yes | ADR-0007 |
| 2026-08-06 | M0 | Schema version is a `ClassVar` on `VersionedModel`, re-attached by `to_record()` | Version describes the class, not a mutable field; a subclass that does not declare its **own** version fails at import time (checked via `cls.__dict__`, not `getattr`, so a subclass cannot inherit and mislabel its records) | Yes | `src/argos/domain/versioning.py` |
| 2026-08-06 | M0 | Container fields on `VersionedModel` are deep-frozen on validation | `frozen=True` blocks only attribute assignment; a `dict` field stays mutable and the mutation reaches `to_record()` — invariant 7 needs the object itself to be unable to lie | Yes | `freeze`/`thaw` in `src/argos/domain/versioning.py` |
| 2026-08-06 | M0 | `RunManifest.created_at` is normalized by a field validator, not only by `build_run_manifest` | A manifest read back from disk must be as trustworthy as one just built, and the same instant must always serialize identically or M3 hashes diverge | Yes | `src/argos/config/manifest.py` |
| 2026-08-06 | M0 | `InvalidDurationError` subclasses both `ClockError` and `ValueError` | `nan` slipped past the `seconds < 0` guard and would hang `anyio.sleep`; the failure needs a countable code while staying catchable by stdlib-shaped callers | Yes | `src/argos/clock/base.py` |
| 2026-08-06 | M0 | Configuration errors report field names and error types only, never the offending value | pydantic embeds `input_value` in its message; an operator who pastes a credential into the wrong `ARGOS_*` variable must not see it echoed into stderr or CI logs | Yes | `docs/09_SECURITY.md` |
| 2026-08-06 | M0 | `_code_revision` verifies the git toplevel before trusting `HEAD` | git discovers repositories upward, so a wheel install inside an unrelated checkout would stamp manifests with foreign provenance — silently, since git succeeds | Yes | `src/argos/cli.py` |
| 2026-08-06 | M0 | Unrecognized `ARGOS_*` environment variables fail the load | pydantic-settings ignores them, so a typo would silently keep a default and a stray credential-shaped variable would go unnoticed | Yes | `src/argos/config/settings.py` |
| 2026-08-06 | M0 | `ensure_utc` rejects naive datetimes instead of assuming UTC | A naive timestamp is ambiguous evidence; assuming UTC would corrupt event-time reasoning | Yes | `src/argos/clock/base.py` |
| 2026-08-06 | M0 | `ReplayClock.advance_to` accepts an equal instant but rejects backwards moves | Several observations legitimately share one event time; regression is a determinism bug | Yes | `tests/test_clock.py` |
| 2026-08-06 | M0 | Architecture and no-execution boundaries enforced by AST tests, not review alone | A rule that only exists in prose drifts; `tests/test_boundaries.py` fails the build instead | Yes | `tests/test_boundaries.py` |
| 2026-08-07 | M1 | `MarketDefinitionV1` validates structure only; binary-ness is a selection policy | A three-outcome market is not malformed, it is out of scope — conflating the two would hide real normalization failures behind a scope filter | Yes | `src/argos/domain/selection.py` |
| 2026-08-07 | M1 | A timeout that exhausts the retry budget raises `SourceTimeoutError`, an outage raises `SourceUnavailableError` | They are different operational stories and lead to different fixes | Yes | `src/argos/sources/gamma.py` |
| 2026-08-07 | M1 | Retry backoff sleeps on the injected clock | Tests and replay must not spend real seconds; retry *timing* is deliberately excluded from any output hash | Yes | ADR-0003 |
| 2026-08-07 | M1 | `contract_id` is derived from the source hash, not generated | Recompiling identical evidence must not invent a new identity | Yes | `src/argos/compiler/contract.py` |
| 2026-08-07 | M1 | `CONDITIONS_NOT_EXTRACTED` is always flagged at compiler v1 | `yes_condition`/`no_condition` need semantic extraction, which is gated behind the owner review; a flag is honest where a guess would silently redefine resolution (invariant 15) | Yes | ADR-0005 |
| 2026-08-07 | M1 | `ambiguity_score` is an integer count of flags | Invariant 8 forbids an uncalibrated score that could be read as a probability; a count above 1 cannot be | Yes | `docs/04_DATA_CONTRACTS.md` |
| 2026-08-07 | M1 | A missing liquidity value is treated as zero against a floor, not as "unknown, pass" | Absent evidence must not let a market through a filter it was never measured against | Yes | `src/argos/domain/selection.py` |
| 2026-08-07 | M1 | `--min-liquidity` is parsed as a string into `Decimal` | A float CLI option would round money before it reached the policy | Yes | `src/argos/cli.py` |
| 2026-08-06 | M0 | Storage engine choice deferred to M2 | The SQLite/WAL vs append-log comparison needs real capture volumes; deciding now would be speculation | Yes | `docs/12_TECH_STACK.md` |
