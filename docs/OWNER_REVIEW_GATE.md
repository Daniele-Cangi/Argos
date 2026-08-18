# Owner Review Gate A — after M4

Claude must stop implementation when this gate is reached.

## Required repository state

Checked 2026-08-18 at `42a49ae`. Every box below names what was verified rather
than asserting the box.

- [x] Clean working tree or clearly documented local-only artifacts. Working
      tree clean; `.data/`, `.venv/` and the caches are gitignored, and no
      generated capture data is committed.
- [x] M0-M4 marked complete with evidence. `docs/STATUS.md` carries a criterion
      table per milestone; `docs/HANDOFF_M4.md` section 5 links M3 and M4 to the
      test that closes each.
- [x] All CI/local quality gates pass. ruff, ruff format, mypy strict on 57
      source files, **1,513 tests**; the coverage gate enforces
      `docs/13_TEST_STRATEGY.md`'s per-area branch thresholds and passes.
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
- [x] `HANDOFF_M4.md` is complete. All eleven sections per `docs/10_HANDOFF.md`.

## Questions for Daniele and Nexus

1. Which initial market categories should M5 prioritize?
2. Which external evidence sources are acceptable and affordable?
3. Should the market compiler use LLM assistance, and under what human-review policy?
4. Which RESON channels should be independent in the first experiment?
5. What minimum resolved sample is required before calibration/fusion claims?
6. Should the next interface remain CLI/report-first or introduce a research UI?
7. Should execution remain a separate repository permanently?

## Prohibited automatic continuation

Do not start M5-M8 merely because M4 passes. Produce recommendations only.
