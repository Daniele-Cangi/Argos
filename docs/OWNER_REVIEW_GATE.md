# Owner Review Gate A — after M4

Claude must stop implementation when this gate is reached.

## Owner decision update — 2026-08-22

**BLOCKED. The gate still has not passed.** V4 was aborted before observation
because its start boundary elapsed before freeze; it contributed no targets,
captures, lifecycle evidence or result, and none of its inputs were reused.

The fresh V5 experiment completed both bounded captures without rejection,
unknown event or decode failure. Both target chains contain 129 contiguous,
receipt-bound lifecycle observations through approximately `07:59Z`, within
60 seconds of the frozen `08:00Z` deadline. The last observed finality for
both targets was `proposed`, and no admissible cutoff was observed.

The committed terminal claim therefore reports
`TARGET_ACCOUNTING_COMPLETE`, `LIFECYCLE_CONTINUITY_COMPLETE` and
`NO_ADMISSIBLE_CUTOFF_OBSERVED`. Its mechanically derived verdicts remain
`M4_BLOCKED` and `CALIBRATION_NOT_EVALUABLE`. This closes the V3 host-suspension
continuity defect, but it does not satisfy the frozen 2/2 cutoff requirement.

The exact portable claim and receipt index are committed under
`experiments/m4-prospective-20260821-v5/proof/`. The owner authorizes only
preparation of a fresh V6 M4 experiment with a new frozen protocol, fresh
targets and fresh captures. No predecessor rescue, replacement or evidence
reuse is permitted. M5-M8 remain unauthorized.

## Owner decision update — 2026-08-21

**BLOCKED. The gate still has not passed.** The fresh V3 experiment is terminal
and ADR-0018 supersedes any running/polling description for it. Both
preregistered targets are accounted for and both capture paths completed
without rejection, decode failure or unknown event. Each target has 98
contiguous, receipt-bound lifecycle observations, but the last observed status
was only `proposed` near `02:23Z`. No admissible final-settlement cutoff was
observed before the frozen `06:00Z` deadline.

Host suspension left an approximately 3h37 unobserved tail. No post-deadline
poll exists, but absence of such a poll cannot establish the markets' state
during that tail. The terminal claim therefore reports
`TARGET_ACCOUNTING_COMPLETE`, `LIFECYCLE_CONTINUITY_INCOMPLETE` and
`NO_ADMISSIBLE_CUTOFF_OBSERVED` separately. Its mechanically derived
verdicts are `M4_BLOCKED` and `CALIBRATION_NOT_EVALUABLE`.

The exact portable V4 claim and receipt index are committed under
`experiments/m4-prospective-20260820-v3/proof/`; repository tests parse and
semantically validate the real bytes and reject both one-byte mutation and
globally re-digested false terminal claims. Gate A still lacks the frozen 2/2
admissible cutoff evidence required for an end-to-end M4 measurement-layer
decision. M5-M8 remain unauthorized.


## Owner decision — 2026-08-19

**BLOCKED. The gate has not passed.** The independent review in
`docs/OWNER_TAKEOVER_M4_REVIEW.md` reproduced F2, F3, F4, F5 and F8 and found
additional sample-unit, displayed-price, resolution-identity, integrity and
portability defects. ADR-0013 records the corrective policy; ADR-0014 defines
the prospective evidence and calibration-sufficiency boundary; ADR-0015 keeps
measurement-layer validation separate from calibration sufficiency.

The checklist below is the dated 2026-08-18 submission and is retained as
history, not as the current verdict. In particular, its M4-complete and
completed-evaluation boxes are superseded: the 75 scores are trajectory points
against one target, temporal admissibility cannot be proved without a cutoff,
and the artifact did not bind its children.

The corrective implementation now:

- emits only on target information-state changes;
- refuses non-final resolution, excludes post-resolution points and makes an
  unknown cutoff a no-score result;
- persists a digest-bound evaluation bundle and explicit per-arrival decisions
  and per-forecast exclusions;
- rejects a correctly re-digested but internally contradictory bundle by
  cross-validating report claims and child links against sibling records;
- distinguishes arrivals, information states, forecast points and resolved
  targets, with no one-target calibration headline;
- preserves snapshot last trade separately and implements the conditional
  displayed-price rule; and
- runs the ordinary gate on Windows as well as Ubuntu.

The final local prospective bundle-hardening gate passes (1,597 tests plus one
Windows symlink skip; 95% aggregate coverage; `bundle.py` 96.47% and
`run_v2.py` 92.11%). PR #2 was merged as `bd2ca1c` after final run
`32279416650` passed on Windows and
Ubuntu. GitHub branch protection/status enforcement is not configured; the
green-before-merge rule was operational discipline. Gate closure still
requires a bounded prospective
multi-target experiment with contract persistence proven before forecast,
predeclared source-terminal or first-observed-final cutoff evidence, and a
predeclared sample/weighting/sufficiency rule. No M5-M8 work is authorized.

The smallest frozen two-target pilot has now executed against public sources.
Both separately captured targets emitted a real standalone
`last_trade_price` event that revision `877060b` did not model. Under the
predeclared target-level exclusion and no-replacement rules, both targets are
permanently excluded. The proof-backed V2 aggregate therefore reports zero
contributions, `M4_BLOCKED` and `CALIBRATION_NOT_EVALUABLE`; later finality
cannot promote those verdicts. The frozen lifecycle deadline elapsed without
any first-final cutoff. At the last valid in-window poll (ordinal 70), both
markets remained `proposed`. Historical aggregate `observation_complete` means
closed selected-target accounting, not continuous lifecycle coverage. A
host-clock jump caused the monitor to append poll 71 after the deadline. That
deviation is preserved for audit but inadmissible as cutoff evidence; the gap
leaves `lifecycle_record_complete = false` even though
`experiment_closed_by_frozen_deadline = true`, and affected no exclusion,
contribution or verdict.

ADR-0016 advances exclusions to an exact-raw, rejection-, manifest-, receipt-
and target-bound claim. Standalone trade modeling added after capture is for a
future protocol only and does not rescue or reinterpret the pilot. Gate A stays
blocked and no M5-M8 work is authorized.

ADR-0017 binds future deadlines, cadence and capture limits in protocol V2;
V4 target bundles bind the exact capture manifest and refuse post-deadline
cutoffs; V3 aggregates enforce those limits. The historical V2 aggregate is
published byte-for-byte with a verifiable index under
`experiments/m4-pilot-20260819/proof/`. No new experiment has been run.

## Required repository state

Checked 2026-08-18, and re-checked after the M4.1 hardening pass
(`docs/HANDOFF_M4.md` section 12). Every box below names what was verified
rather than asserting the box; the commit is not pinned here because it moves
with each slice, and `git log --oneline main..HEAD` is authoritative.

- [x] Clean working tree or clearly documented local-only artifacts. Working
      tree clean; `.data/`, `.venv/` and the caches are gitignored, and no
      generated capture data is committed.
- [x] M0-M4 marked complete with evidence. `docs/STATUS.md` carries a criterion
      table per milestone; `docs/HANDOFF_M4.md` section 5 links M3 and M4 to the
      test that closes each.
- [x] All CI/local quality gates pass. ruff, ruff format, mypy strict on **58
      source files**, **1,582 tests**; the coverage gate enforces
      `docs/13_TEST_STRATEGY.md`'s per-area branch thresholds and passes.
      **Reproduced on CI**: GitHub Actions run `32195822692`, commit
      `26559f5`, `ubuntu-latest` / Python 3.12.13 — checkout, uv sync, Ruff,
      format, mypy strict (58 source files), pytest (1,580 tests) and the
      branch-coverage thresholds all passed. That is **independent
      execution**, not an independent architectural or security review; the
      review box below stays unticked regardless of how often CI is green.
- [x] No execution, wallet, private key, authenticated channel, or order code.
      Enforced mechanically by `tests/test_boundaries.py` (declared-name and
      endpoint-literal scans) and by the configuration validator, not by review.
- [x] Public-source endpoints and schemas re-verified. Re-measured against live
      public traffic on 2026-08-18 and recorded in
      `docs/research/m4-gamma-resolution.md`, which found that `closed == true`
      does not imply a resolution and that Gamma does not cover ARGOS's own
      captured market.
- [x] Sample discovery, capture, replay, and evaluation commands work from clean
      checkout. `docs/RUNBOOK.md` carries all four; replay and evaluation are
      driven end to end through `CliRunner` in the suite, and discovery and
      capture were verified against live traffic at M1 and M2.
- [x] Golden replay hash is stable. `2a7fcb6a…`, identical across three runs and
      all three pacing modes, and anchored to three snapshot-to-snapshot
      checkpoints the source itself asserted.
- [x] At least one completed baseline evaluation exists, without edge claims.
      38 midpoint and 37 persistence forecasts from the real capture, scored
      against the real settlement. **No edge claim is made, and the report's own
      limitations field states why one could not be**: every forecast carries the
      same score, so the effective sample size is 1.
- [ ] Architecture, security, and test reviews have no critical blocker. **The
      reviews found no critical blocker, and they are not independent** — they
      were performed by the same author who wrote the code. See
      `docs/STATUS.md`, "M2 closure reviews" and "M4 closure reviews". This box
      is left unticked deliberately: an independent pass is the one thing this
      gate asks for that has not been obtained, and ticking it would be the
      claim the whole repository's discipline exists to prevent.

      **A green CI run does not tick this box.** CI is independent
      *execution* — it proves the gates reproduce on a machine nobody here
      controls. It reads nothing, judges nothing, and has no opinion about the
      architecture or the threat model. The one automated reviewer that did
      read the diff found a real defect on its first pass
      (`docs/HANDOFF_M4.md` section 12), which is evidence that reading this
      code finds things, not evidence that it has been reviewed.
- [x] `HANDOFF_M4.md` is complete. All eleven sections `docs/10_HANDOFF.md`
      specifies, plus a twelfth recording the M4.1 hardening pass.

## Questions for Daniele and Nexus

1. Which initial market categories should M5 prioritize?
2. Which external evidence sources are acceptable and affordable?
3. Should the market compiler use LLM assistance, and under what human-review policy?
4. Which RESON channels should be independent in the first experiment?
5. What predeclared sample, dispersion and uncertainty rule is sufficient for
   calibration claims in the prospective experiment?
6. Should the next interface remain CLI/report-first or introduce a research UI?
7. Should execution remain a separate repository permanently?

## Prohibited automatic continuation

Do not start M5-M8 merely because M4 passes. Produce recommendations only.
