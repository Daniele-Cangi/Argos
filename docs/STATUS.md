# ARGOS status

Last updated: 2026-08-19

## 2026-08-19 owner correction — M4 reopened

The independent owner review in `docs/OWNER_TAKEOVER_M4_REVIEW.md` supersedes
the M4/M4.1 completion claim below. The dated history is retained so the
correction is auditable; it is not current authority.

- Current milestone: **M4 blocked; owner gate not passed.** M5-M8 remain out of
  scope.
- The arrival-weighted v1 evaluator is superseded by ADR-0013. The exported v2
  path emits on target information-state changes and persists a digest-bound
  bundle containing policy, trajectory, resolution, optional contract,
  forecasts, scores, decisions and exclusions.
- Finality, exact resolution cutoff, persisted contract identity and capture/
  replay integrity are prerequisites for scoring. The committed real sample
  lacks an exact CLOB cutoff and a persisted compiled contract, so its honest
  result is **zero admissible scored points and no headline claim**, not 75
  samples.
- Counts now distinguish arrivals, information states, forecast points, scored
  points and resolved targets. ADR-0014 makes two targets a structural floor,
  not calibration sufficiency; only a predeclared multi-target sample,
  weighting and sufficiency rule can support that claim.
- `EvaluationRunBundleV2` now rejects digest-valid internal contradictions by
  cross-validating report policy/resolution/contract claims, counts and child
  digests, evaluation links, and scored-versus-excluded membership against the
  actual sibling records.
- The prospective branch versions forward to `EvaluationRunBundleV3`: frozen
  protocol, market/contract/target persistence receipts, ordered lifecycle
  polls, first-final cutoff evidence and the original resolution are one
  cross-validated boundary. `ProspectiveExperimentBundleV1` gives each resolved
  target one contribution per method and publishes measurement and calibration
  verdicts separately (ADR-0015).
- A bounded public preflight found 37 market-channel frames / 40 stored events
  with no rejection or unknown type. No standalone `last_trade_price` event was
  observed; the schema remains unmodeled and the pilot predeclares target-level
  exclusion if one occurs. These probe frames are not admitted experiment data.
- Initial book-snapshot last trade is preserved separately from book state.
  Midpoint, last trade and the conditional displayed-price rule are distinct
  baselines. Standalone last-trade events remain unmodeled until a pinned raw
  fixture establishes the exact schema.
- Windows is now a first-class CI platform. The final local prospective
  bundle-hardening gate passes Ruff, format, strict mypy, and **1,597 tests**
  (one symlink-capability skip). The branch-coverage gate passes at 95% aggregate;
  `bundle.py` is 96.47% and `run_v2.py` 92.11%, both above their 90% floors.
  PR #2 was merged as `bd2ca1c` only after final GitHub Actions run
  `32279416650` passed on Windows and Ubuntu. GitHub branch protection/status
  enforcement is not configured, so this was an observed operational gate,
  not a repository-enforced barrier.

The smallest next data experiment is not merely a larger capture. It must
persist each compiled contract before its first forecast and record either a
verifiable source terminal time or the first observed final settlement with
separate source/retrieval times and non-reconstructed provenance. Its sample,
target weighting, stopping and sufficiency rules must be predeclared.

## Current state

- Current milestone: **M4 — complete, plus an M4.1 owner-gate hardening pass.
  Stopped at the owner gate.** The hardening pass fixed four confirmed defects
  and characterized five findings that need an owner decision without choosing
  one; its full record is `docs/HANDOFF_M4.md` section 12, which is not
  repeated here. Suite now **1,582 tests** on **58 source files**, and the
  gates are reproduced on GitHub Actions (run `32195822692`) as well as
  locally. The canonical repository is `Daniele-Cangi/Argos`, and **M0 through
  M4.1 are merged on `main`** as of `a8371d5e` — that branch is the single
  source of truth, and the owner review starts there. M0 and M1 are
  closed. **M2 is functionally complete and is closed on evidence rather than
  on an independent verdict** — see "M2 closure" below, which does not claim
  more than that, and "M3 readiness audit" for what was re-derived from `main`
  rather than read off this file.
- Every M2 deliverable in `docs/07_MILESTONES.md` exists: the public CLOB REST
  order-book adapter, the public market WebSocket transport, both typed payload
  models plus the WebSocket `book` model, bounded seeded reconnect backoff, the
  idempotent append-only SQLite event store (ADR-0011), the capture loop with
  health counters, a bounded blocking frame buffer, the `argos capture market`
  CLI, and a raw frame archive. Three live captures ran against real Polymarket
  traffic on 2026-08-15 (45 s, 60 s, 40 s).
- Autonomous target: **complete M0-M4**
- Owner gate: **required after M4**
- Execution capability: **prohibited and absent**
- External evidence / LLM forecasting: **not started**

## Current objective

**None. Implementation stops here** — `docs/OWNER_REVIEW_GATE.md` is the gate
after M4 and `CLAUDE.md` forbids continuing into M5 merely because M4 passes.
`docs/HANDOFF_M4.md` is the owner package.

The one thing that would most change what this repository can claim is not code:
a real sample. Ten to thirty liquid markets resolving within a week, captured
continuously and then evaluated, would turn a working pipeline into a result. It
needs no new code and is filed in `docs/BACKLOG.md` as the first M4 carry-over.

## M4 — baseline probability and evaluation (2026-08-18)

Quality gate at M4 close, and left as the dated record it is: PASS — ruff,
ruff format, mypy strict on **57 source files**, **1,536 tests** (up from 1,415
at M3). Both figures describe 2026-08-18, before the M4.1 pass added
`argos/evaluation/numeric.py` and its tests; the current figures are in the
"Current state" bullet above.

**M4 closes with a real evaluation, not a constructed one**, and that was not
guaranteed when the milestone started. The market ARGOS captured 40 seconds of
on 2026-08-10 — *National Bank Open: Diana Shnaider vs Iga Swiatek* — has since
resolved. So the whole chain runs on data this repository actually holds: the
recorded capture, replayed through M3 into an order book, read as a midpoint
baseline, scored against the settlement the CLOB published after the match.

### The research that changed the design, twice

Two findings came out of measuring the live public API rather than reading its
documentation, and each one broke the obvious implementation.

**`closed == true` does not mean resolved.** Two samples of the same endpoint,
differing only in ordering, disagree almost completely: oldest-first (n=900),
**0.4%** of closed markets carry an exact 1/0 outcome while **93.2%** carry a
fractional price and 5.1% carry `["0","0"]`; most-recently-ended (n=500), 100%
carry an exact 1/0. Market 40 is *"Will Trump win the 2020 U.S. presidential
election"*, closed, at `0.0000000436`/`0.9999999` — a question whose outcome is
not in doubt and whose payload does not encode it. A normalizer validated
against either sample alone would look correct and be wrong about the other, and
a naive evaluator over the id-ordered one would have scored 93% of its markets
against a price. Only an exact 1/0 pair is accepted; everything else is a
counted refusal.

**Gamma cannot resolve ARGOS's own capture.** A `condition_ids` query for the
captured market returns *nothing at all*, while the CLOB returns a complete
resolved record carrying an explicit `"winner": true` per token. So the resolution
path that would have been built first — Gamma, inferring from `outcomePrices` —
would have had **zero coverage of the one market this repository has captured**.
Both paths ship; the CLOB one is primary because it states the settlement rather
than leaving it to be inferred, and it cross-checks price against the winner flag
rather than preferring one silently.

A third finding removed work rather than adding it: Polymarket's displayed price
**is** the midpoint, on 91 of 91 two-sided markets measured. So
`docs/05_RESEARCH_PROTOCOL.md`'s "displayed-price proxy" and "midpoint" are one
quantity here, and implementing both would have produced two identical numbers
reported as independent baselines that agree.

Four payloads are committed with provenance sidecars, one per observed shape;
the full note, including its UNVERIFIED list and the HTTP 422 paging ceiling, is
`docs/research/m4-gamma-resolution.md`.

### The exit criteria

| Criterion | How it is closed |
|---|---|
| Midpoint is never labeled executable price | Structurally: `best_bid`/`best_ask` carry sizes and are the only executable fields, and `midpoint` is `None` whenever both sides do not exist rather than falling back to the side that does. 9 of the 100 highest-volume open markets have no two-sided book, so that fallback would have been nine invented prices per hundred markets. A quote whose midpoint disagrees with its own sides is refused |
| Unresolved markets are not scored as negatives | By the type. `score_forecast` takes a `WinningOutcome`, which has exactly two members; an undetermined market produces a `ResolutionRefusal` and never a resolution, so there is no call site at which "unresolved" could be passed as 0 |
| Probability values validated, log-loss clipping declared | `ForecastEvaluationV1` refuses metrics that disagree with its own inputs, and carries `log_loss_epsilon` plus `log_loss_was_clipped` per forecast. At ε=1e-6 a confidently wrong market scores 13.8 and at 1e-3 it scores 6.9 — a report that omits ε has not reported log loss |
| Baseline evaluation reproducible from stored records | Two CLI runs agree on the state hash, every count and every metric. Nothing is fetched during evaluation: both inputs are records |
| Reports include missing data and sample counts | Missingness is split into abstentions and unresolved, because "the baseline declined" and "the market has not resolved" are different and only the first is a property of ARGOS. Every calibration bin reports its count, empty ones included, with `observed_rate` `None` rather than 0 |
| No advanced predictive engine to flatter metrics | Only market baselines exist. `argos.forecasting` does not |

### The evaluation, and why its numbers mean less than they look

38 midpoint and 37 persistence forecasts, scored against outcome 0: Brier
**0.081225**, log loss **0.3355** (ε = 1e-6, **0 clipped**), ECE **0.285**.

Those numbers are one constant repeated. **The top of book never moved during
the capture** — best bid 0.28, best ask 0.29, midpoint 0.285 across all 38
states, with all 34 deltas touching deeper levels. So the effective sample size
is 1, and midpoint and persistence agreeing *perfectly* is an artifact rather
than corroboration. The report detects a method whose every score is identical
and says exactly that, in its own required `limitations` field, alongside: the
scores are uncalibrated market baselines and not ARGOS probabilities; the
forecasts are successive states of one order book and heavily autocorrelated;
and one market is not a sample.

### Two defects the output showed and the tests did not

**Persistence scored 0 of 38.** The carried-forward value was only updated inside
the non-abstaining branch, so a baseline that can score only after something has
been carried forward could never start. Every test passed throughout — a
baseline that silently never fires produces no failure, only an empty column.
Found by reading the printed report.

**Then the fix exposed the constant-score problem above**, because midpoint and
persistence came back byte-identical. That is the second time in this session
that the interesting finding was in the *output* rather than in a red test, and
it is the reason the constant-score limitation is now generated automatically
rather than left for a reader to notice.

### What M4 deliberately does not do

- **No category cohort.** `docs/07_MILESTONES.md` asks for it "when data
  permits"; it needs Gamma metadata, and Gamma does not cover the captured
  market. Spread bucket and time-to-resolution ship.
- **No base-rate baseline.** It needs resolved data grouped by category, and the
  qualifier is the operative part.
- **No `p_yes`, anywhere.** Every score this milestone produces is
  `raw_score` with `calibration_status=uncalibrated`, and the validator refuses
  a `p_yes` without a calibration version.

## M4 closure reviews (2026-08-18)

Same label as the M2 closure reviews, for the same reason: **performed by the
author of the code, not independently.** `docs/OWNER_REVIEW_GATE.md`'s review
box is deliberately left unticked because of it.

**Architecture — APPROVE.** The four new packages sit where
`docs/02_ARCHITECTURE.md` puts them, and the dependency direction holds:
`argos.evaluation` imports `argos.replay` and `argos.resolution` and nothing
imports it back. The one judgement worth recording is that `evaluate_capture`
drives a replay rather than reading the store directly, so the whole chain a
score depends on is the chain M3 made deterministic — a scorer with its own
reader would have been a second ordering nobody tested.

**Security — PASS, no new findings.** No new network surface: the evaluation
path fetches nothing, and that is a property of the code rather than of how it
is invoked. No new execution surface (the boundary scans pass unchanged). The
one dependency added is `pytest-cov`, dev-only. The resolution normalizers parse
untrusted source text and route it through the existing
`neutralize_and_bound`; `ResolutionV1.resolution_source` is the only free-text
field either produces, and it is bounded at 200 characters.

**Testing — 1,536 tests at M4 close, and three findings worth the space.**

The two defects above were found by reading output, not by a failing assertion,
which is the third time this repository has recorded that pattern.

The third came from the coverage gate itself, and it earned its keep twice in
two days. It caught `argos/evaluation/run.py` at **77.78%** against its 90%
floor and `clob_resolution.py` at 70% — because the real end-to-end evaluation
exercises exactly one happy path (one market, one token, a two-sided book at
every state), so the abstention path, the time-to-resolution buckets and *every
refusal the CLOB normalizer can produce* had never run. A module whose only test
is its happy path is a module whose refusals have never run, and refusals are
most of what those two modules do. 23 tests close it; the gate now passes.

What no coverage number can check, and what the constant-score finding shows, is
whether a test exercises a property or merely reaches a line.

**Documentation — complete.** `docs/RUNBOOK.md` gains the evaluation command
and states the `closed != resolved` finding where an operator will meet it;
`docs/04_DATA_CONTRACTS.md` was reconciled with the code at M2 closure and the
M3 amendments are recorded there; `docs/HANDOFF_M4.md` carries all eleven
sections `docs/10_HANDOFF.md` specifies.

## M3 — deterministic replay (2026-08-18)

Specified by **ADR-0012**, which decides the six things ADR-0003 deliberately
left open: what a replay is scoped to, what happens to a duplicate arrival, what
a watermark does to a late one, what the clock is advanced to, what the output
hash covers, and whether pacing can reach it. Each of those has a defensible
answer that produces a *different* hash from the other defensible answers, so
leaving them implicit would have meant "identical input produces identical
output hash" was satisfied by whatever the first implementation happened to do.

Quality gate: PASS — ruff, ruff format, mypy strict on 49 source files,
**1,415 tests** (up from 1,361 at M2 closure).

### The exit criteria, and what closes each

| Criterion | Evidence |
|---|---|
| Identical input + code + config produces an identical output hash across at least three runs | Three replays of the recorded capture produce one hash **and one set of counts** — the contract requires both, so both are compared (`tests/test_replay.py::test_three_replays_of_one_capture_agree_exactly`) |
| Replay never reads the wall clock inside domain logic | The whole call is driven with a `ReplayClock` for the manifest's own timestamps too, so anything reaching for real time would leave a moment neither clock was ever set to. Plus the existing AST boundary scans |
| Late and invalid event behaviour is deterministic and counted | A late arrival is marked and **still applied in arrival order**, and the state hash is identical whether the watermark called it late or not — marking changes counts, never state. Rejections are replayed as arrivals, tallied by reason, and reach no projection |
| Changing a source event produces a predictable hash change | One price level's `size` is changed in the *source bytes* of the last recorded frame, so the change travels normalization, identity, storage and replay. The hash changes; every arrival and dispatch count stays identical, which is what makes the difference attributable to the book rather than to a different amount of input |
| A live adapter can be replaced by a replay source without changing domain handlers | The capture loop and the replay scheduler drive the *same* `ObservationDispatcher`, and the two paths are compared directly: same frames, one from a fake wire and one from storage, identical state hash and identical dispatch counts |
| Replay performance is measured but correctness takes precedence | Measured and printed, deliberately not asserted against a threshold — a wall-clock assertion in a unit suite is the flaky timing test `docs/13_TEST_STRATEGY.md` forbids |

### The golden replay, and why it is anchored rather than merely pinned

`GOLDEN_STATE_HASH = 2a7fcb6a…` over 38 arrivals (4 `book` snapshots, 34
`price_change` deltas; 38 on time, 0 late, 0 undatable, 0 duplicates, 0
rejections). A stable hash of the *wrong* state would still be stable, so the
value is anchored to something the source itself asserted: the capture carries
four full `book` snapshots with deltas between them, giving **three independent
checkpoints** where the state built from deltas alone must already equal the
book the source is about to restate. All three hold, checked *before* each
snapshot is applied — comparing afterwards would be vacuous, because a snapshot
replaces state wholesale.

Six deltas follow the last snapshot, so the final state is deliberately **not**
equal to it. Asserting that it was would have been the more obvious test and the
wrong one; the first draft of this test made exactly that mistake and failed.

### Three findings from building it

**The replay clock cannot be seeded from the capture-run row.** `run.started_at`
and an arrival's `received_time` come from two different clocks — the capture
loop's and the transport's — and nothing in the store obliges them to agree.
Seeding from the run row and then advancing to an earlier arrival raises
`ClockRegressionError` and kills the replay; seeding from it and *skipping* the
advance would leave the virtual clock ahead of the capture, which is the leakage
this milestone exists to prevent. Reproduced against the recorded capture, whose
frames predate the injected run start by four days. The clock is now seeded from
the first arrival, which makes it a function of the data being replayed.

**A test fixture was manufacturing rejections the shipped path cannot
produce.** Feeding the recorded fixture's frames straight into `run_capture`
yields four `malformed_payload` rejections — they are the server's plain-text
`PONG` replies, which `ClobMarketWsClient._classify` consumes and never yields
onward. The M3 readiness audit had already recorded this as a probe artifact; it
would have become a *committed* artifact if the golden hash had been computed
over a capture that cannot happen. The fixture loader filters them and says why.

**A duplicate delivery must not be re-applied, and "harmless" is nearly true.**
Re-applying a `price_change` group is not idempotent: a second `REMOVE` names a
level the projection no longer holds, and `OrderBookProjection` correctly
records `REMOVE_OF_ABSENT_LEVEL` — an anomaly whose whole job is to signal a
missed delta. Replaying duplicates would manufacture that signal out of the
deduplication mechanism itself, and make the anomaly count a function of how
often the *source* resent. The dispatcher owns the refusal rather than each
caller, so live and replay cannot drift apart on it.

### One projection change, and the rule it had to respect

`apply_snapshot`/`apply_delta` now accept `event_time=None`. An observation
whose source timestamp did not parse is accepted by the envelope contract and
must still reach the book: refusing it would lose a real book state, and
substituting `received_time` would be the silent timestamp replacement
`.claude/rules/data-integrity.md` forbids. It is applied, excluded from ordering
comparisons, forbidden from overwriting the last applied event time — assigning
`None` through would have silently disabled lateness detection for everything
after it — and counted as **undatable**, which is a third value precisely
because "the source sent no timestamp" and "the source sent one and it was not
late" are different facts about the source.

### What M3 deliberately does not do

- **No multi-run replay.** `ingest_sequence` means nothing between capture runs,
  and a cross-run order would have to be invented. Filed rather than guessed.
- **No `supersedes_observation_id`.** Still not needed: a replay reads one run's
  records as captured, and nothing here can yet re-normalize a capture under a
  corrected parser, which is the only operation that mints an unlinked second
  identity.
- **No buffering watermark.** ADR-0003 forbids reordering late data into the
  past outside a separately labelled experiment, so the watermark marks and
  nothing else happens to a late event.

## M2 closure reviews (2026-08-17)

**Read the label before the verdicts.** These four reviews were performed by the
same author who is building M3, not by an independent reviewer. M0 and M1 closed
on independent verdicts; M2 does not, and this section does not pretend
otherwise. The alternative was a fourth attempt at a delegation that failed
seven times in this milestone, where silence would again have been
indistinguishable from a clean result. Every finding below names what was
*measured* rather than what was read, because that is the only part of a
non-independent review worth anything.

### Architecture — APPROVE_WITH_FOLLOWUPS

**A1, fixed in this slice — a capture could not be reproduced from its own
manifest.** Core invariant 13 says every run records its configuration.
`RunManifest` recorded `Settings`, and a capture's real inputs are not settings:
the subscribed token ids, the stopping bounds and the raw-archive flag all
arrive as command-line arguments and were in **no durable artifact anywhere**.
`subscribed_token_ids` is the one that matters. `argos.ingestion.capture` fans
out over the *configured* token set, sorted — its own docstring calls that "the
load-bearing one for M3", because it is what keeps `ingest_sequence` allocation
a function of configuration rather than of connection topology — and that set
existed only in the operator's shell history. Closed by `run_parameters` on
`run_manifest.v5`, recorded deduplicated and sorted, in the form the loop
actually used.

**A2, fixed in this slice — a specified contract that was never implemented.**
`docs/04_DATA_CONTRACTS.md` specifies `CaptureManifestV1` with eighteen fields.
No such record exists; the capture-loop slice decided the manifest is
`RunManifest` plus the store's `capture_run` rows, which is a good decision that
was never written back into the specification. This is precisely the shape of
the M1 architecture review's blocking finding and of ADR-0010's B3: a specified
contract silently dropped. The document now says which record carries each
field, and names the four that are genuinely unimplemented — `market_filter`
and `selected_markets` (capture takes explicit token ids; there is no filter to
record), `host_metadata`/`clock_metadata` (nothing reads them), and
`reconnect_count`/`gap_warnings` (reconnects are counted in memory only, and
this channel has no sequence number, so an always-empty `gap_warnings` would
read as "no gaps" when the truth is "cannot tell").

**A3, open, and it is M3's first deliverable — there is no dispatcher.** Core
invariant 5 says live and replay use the same domain handlers. That is currently
true only because there is *one* path, not because two share one: the capture
loop writes to the store and stops, and every projection this repository has
driven was driven by a test. Nothing is wrong with the M2 code; what is wrong is
reading the exit-criteria table as evidence of a shared handler, which it is
not. Filed under "Now — M3".

**A4 — verified, no action.** Package boundaries and dependency direction hold
(the AST boundary tests pass, including the ones added this session). All four
adapters plus the capture loop expose the health counters
`docs/02_ARCHITECTURE.md`'s failure model requires (`SourceHealth`, `ClobHealth`,
`ClobWsHealth`, `CaptureHealth`). The only wall-clock read in `src/` is
`LiveClock.now`. The store's append-only guarantee survives the new schema
stamping: `PRAGMA` writes file-header fields, not rows, and the forbidden-SQL
boundary test still passes.

### Security — PASS_WITH_FINDINGS, no blocker

**S1, accepted with the reasoning recorded.** Three statements in
`event_store.py` interpolate into SQL with an f-string. SQLite cannot
parameterize a pragma's name or its value, so there is no parameterized
alternative; all three interpolate module constants — `_EXPECTED_TABLES` keys
and two module integers — and no caller input reaches any of them. Recorded
rather than left for a future reviewer to re-derive, because "f-string in SQL"
is a pattern that should always be justified in place.

**S2, verified negative, now pinned by a test.** R4 made
`archive_relative_location` compose a value that lives in a durable record and
that a consumer will join back onto an archive root — one layer further out than
`write_raw_payload`'s containment check, which only guards the write. A
separator or `..` in it would escape at read time. It cannot occur:
`SourceProvenanceV1.source` is `^[a-z0-9][a-z0-9_-]{0,31}$` and `raw_sha256` is
validated 64-character hex, so the composed string admits no slash, backslash or
dot. Asserted at the *contract*, not at the composition, so relaxing the pattern
breaks the test rather than silently reopening the hole.

**S3, accepted.** The adopt-an-unstamped-store path (R3) lets ARGOS write into a
pre-existing correctly-shaped database. Reaching it requires local write access
to the database path, which is the same precondition as the already-filed **L2**
symlink item; it is not an escalation, and refusing instead would strand every
capture taken before today for no gain.

**S4, accepted and documented at the point of use.** The schema registry (R2)
makes "dispatch on whatever the record claims to be" the easy path, which is the
silent coercion `.claude/rules/data-integrity.md` forbids.
`read_declared_payload`'s docstring states that resolving is not accepting and
that a consumer must still refuse what it does not handle, with a count.

**S5 — no exposure from narrowing the fingerprint.** R1 removed `data_dir` and
`log_level` from `config_fingerprint`. Both are still recorded verbatim in
`settings_snapshot`, so nothing became unauditable; only the hash was scoped.

No new network surface, no execution surface (the boundary scans pass), and the
one dependency added — `pytest-cov` — is dev-only.

### Testing — branch coverage measured for the first time

`pytest-cov` was not installed, so `docs/13_TEST_STRATEGY.md`'s coverage
thresholds had been **unverifiable since M0** and were filed as such. They are
now measured, and they pass:

| Area | Threshold | Measured |
|---|---|---|
| `argos.domain` (contracts) | 90% | 95-99% across eight modules |
| `argos.sources` (adapters) | 80% | 94-96% across three modules |
| `argos.replay` (ordering) | 90% | no module yet — M3 |
| `argos.evaluation` (scoring) | 90% | no module yet — M4 |

Whole-project branch coverage is 96% (3,346 statements, 762 branches, 101
statements and 57 branch arcs unexercised). Enforcement is per area rather than
aggregate, because that document's own first sentence about thresholds is "do
not optimize for a vanity global coverage number", and an empty area is reported
as empty rather than passing silently. It runs in CI and behind
`--coverage` locally, not in the default gate: the suite takes ~80 s and the
same suite under coverage takes ~610 s, and a gate that slow stops being run
between slices.

**A fourth instance of the "claim outruns its assertion" pattern this file
already names three times.** Enabling the schema registry showed that a stub in
`tests/test_observation_envelope.py` had been declaring
`"order_book_snapshot.v1"` — the real payload model's version — inside the test
module for `read_payload`, which is the function whose version check that
collision defeats. The M2 security review had *described* this exact attack; the
repository was carrying an instance of it in the test file for the affected
function, and had been for four slices.

### Documentation — two stale claims, both fixed

`docs/RUNBOOK.md` documented no `capture` command at all, several slices after
`argos capture market` shipped and three live captures ran — the milestone's
last deliverable, the only one that opens a socket, and the one an operator is
most likely to reach for. It also described `argos manifest` as emitting
`run_manifest.v1`, three versions out of date. Both corrected, and the capture
section states the things an operator can get wrong: the mandatory bound, what
Ctrl-C does versus a killed process, and what `--no-raw-archive` costs.

`docs/04_DATA_CONTRACTS.md` is reconciled with the code (A2 above).
`docs/STATUS.md`'s own two stale present-tense claims were corrected by the
readiness audit, above.

## M2 closure finding: captures were discarding the raw bytes

Found while verifying M2 for closure, after the architecture review agent
terminated without a report and the check was done by hand instead. **Both
live captures taken so far stored `raw_payload_sha256` for every observation
and kept none of the bytes it hashes.** `raw_payload_location` was `None` on
every record, and no raw payload file existed anywhere on disk.

This breaks an explicit engineering rule in `CLAUDE.md` — *"Store raw payload
plus normalized payload and schema/compiler version"* — and hollows out core
invariant 7: normalization is supposed to *never replace* the source payload,
but the source payload was not retained at all. Three consequences, none
cosmetic:

- the stored hash was an **unverifiable claim**: a digest of bytes nobody had;
- a historical capture could **never be re-normalized under a corrected
  parser**, which is precisely the correction mechanism ADR-0004 requires;
- the milestone is named "CLOB capture and immutable event store", and its
  captures were not reproducible.

I treated this as blocking for M2 closure rather than filing it.

**Fixed**: `run_capture` takes a `raw_archive_dir` and archives each frame once,
before fan-out, so every record derived from one frame points at the same
archived bytes — which is the truth. The CLI archives beside the database by
default (`--no-raw-archive` exists, and its help text says outright that
disabling it produces a capture that cannot be re-normalized and whose stored
hash can never be checked). An archive failure is deliberately fatal to the run
rather than counted: continuing would keep minting records that silently cannot
be reproduced.

**Verified on a third live capture, 40 seconds, 2026-08-15**: 135 observations,
**135 with an archive location, and 135/135 stored `raw_payload_sha256` values
recomputed from the archived bytes and matched**. The hash is now a checkable
fact rather than an assertion. Cost measured rather than guessed: 131,948 bytes
of archive for 40 seconds on one active token, roughly **285 MB/day/token**
uncompressed — content-addressed, so a redelivered frame costs nothing extra.

**A research finding this capture produced for free.** One event was rejected as
`unknown_event_type` with the detail *"no payload model is wired for event_type
'last_trade_price' yet"*. `docs/research/m2-clob-websocket.md` lists
`last_trade_price` as **never observed** — no such message arrived in its ~85
seconds of combined capture, so its field shape was documentation-only. It does
arrive on live traffic. The research note's UNVERIFIED list is now one item
shorter, and a `last_trade_price` payload model is a real M4 need (that document
already flags it as the M4-relevant event type), filed rather than built here.

## M2 slice: the WebSocket `book` payload — a stored capture becomes self-sufficient

`src/argos/domain/wsbook.py` (`WsBookSnapshotV1`, `ws_book_snapshot.v1`) and
`src/argos/ingestion/clob_ws_book.py` (`normalize_clob_ws_book`), dispatched
from `capture.py`. Closes what the previous slice named the sharpest remaining
M2 gap.

Quality gate: PASS — ruff, ruff format, mypy strict on 43 source files,
**1,316 tests** (up from 1,249).

**Why a second book schema rather than reusing `OrderBookSnapshotV1`.** The
WebSocket `book` event is a different wire schema, measured, not assumed: the
snapshot delivered on subscribe omits `min_order_size` and `neg_risk`, and the
three in-stream ones omit `tick_size` and `last_trade_price` as well, carrying
ids, timestamp, hash and levels alone. Weakening the shipped REST contract to
absorb a WebSocket quirk would degrade the model that *does* have those fields
guaranteed. `tick_size`/`last_trade_price` are optional here, and their absence
is a fact about the message rather than a parser failure.

**Verified independently of the slice's own tests, reading only from the
store.** Recorded frames driven through the capture loop into
`SQLiteEventStore`, then read back: 38 events, **38 accepted, 0 rejections**
(previously 4 `unknown_event_type`), stored as 4 `ws_book_snapshot.v1` plus 34
`price_change.v1`. Seeding `OrderBookProjection` from a *stored* snapshot and
applying the *stored* deltas reconstructs all three later stored snapshots
exactly, with zero anomalies. This is the exit criterion at its strongest form —
a full round trip through durable storage, not payloads held in memory.

**A second live capture, 60 seconds, 2026-08-15**, on a token discovered
through the public Gamma endpoint:

```
loop_health : frames=19 events=19 accepted=18 duplicate=1 rejected=0
              decode_failures=0 unknown_event_type=0
store_counts: accepted=18 duplicate=1 rejected=0
```

Two things this run adds that no replay could:

- **`book` events are now accepted on live traffic** — zero rejections, against
  four in the previous live run. The stored capture holds
  `ws_book_snapshot.v1` and `price_change.v1` together, and the projection
  seeds from the live stored snapshot and applies 18 live deltas with no
  anomalies.
- **A genuine duplicate arrived from the source and collapsed**: one
  `duplicate` delivery against 18 accepted, with 18 observation rows. The
  "duplicate source event does not create a second accepted observation"
  criterion now has **live** evidence, not only replayed and constructed
  evidence.

**Stated precisely rather than overclaimed**: only one `book` event arrived in
those 60 seconds, so the live run has no *second* snapshot to reconcile
against. The three-checkpoint reconstruction remains evidenced on the recorded
capture; the live run shows the pipeline accepting and storing both payload
kinds and projecting from them, not a live reconciliation.

## M2 slice: the capture CLI, and the first live capture ARGOS has ever run

`argos capture market` (`src/argos/cli.py`, `tests/test_capture_cli.py`), plus
`RunManifest` bumped to `run_manifest.v3` with a `capture_run_id`. The last M2
deliverable, and the first time any ARGOS code has opened a socket.

Quality gate: PASS — ruff, ruff format, mypy strict on 41 source files,
**1,249 tests** (up from 1,240).

**A real capture ran against live Polymarket traffic on 2026-08-15**, 45
seconds, two tokens discovered through the public Gamma endpoint:

```
loop_health : frames_consumed=25 events_seen=26 accepted=22 rejected=4
              duplicate=0 decode_failures=0 not_applicable=22 unknown_event_type=4
store_counts: accepted=22 duplicate=0 rejected=4
```

The loop's own counters and the store's independently derived counts **agree** —
which is why the command prints both rather than one. Verified directly against
the resulting database: 22 observation rows, 22 `accepted_new` deliveries, 4
`unknown_event_type` rejections, two `capture_run` rows (open plus close), and
ingest sequences **contiguous 1..26 across both ledgers with no gaps and no
reuse** — the property previously proven only on replayed frames, now on live
traffic. The manifest carries `run_manifest.v3`, `mode=capture`, the
`capture_run_id`, the code revision, the schema versions, and
`working_tree=dirty`, which was correct: the run was made with uncommitted
changes, and the M0 provenance guard said so rather than flattering the run.

**A required bound, not a default.** `--max-seconds` and/or `--max-frames` is
mandatory; the command refuses to start an unbounded live run by accident.
Reaching a bound closes the run `COMPLETED`; Ctrl-C closes it `FAILED` rather
than leaving it dangling. "Interrupted" stays reserved for a process that dies
outright and therefore never reaches any closing code — which is the only
honest meaning, since a dying process cannot describe its own death.

**Manifest linkage, without the unbounded growth the constraint forbids.**
`RunManifest` gains `capture_run_id` and becomes `run_manifest.v3`; no `v2` was
ever persisted by any code path, so this is a clean bump with no migration, the
same reasoning already recorded for v1→v2. `input_provenance` is deliberately
**empty** for a capture run — measured as 0 entries in the live manifest — because
every ingested event's provenance is already stored once per record in the event
store, and enumerating it here is exactly the unbounded growth the pre-M2 slice
warned about.

**A defect found by the slice's own failing test, and it was real.** Reusing an
existing `capture_run_id` — an ordinary operator mistake the store deliberately
refuses — printed **nothing at all** and crashed with a traceback. `run_capture`
runs inside an `anyio` task group, so the `StorageError` arrived wrapped in an
`ExceptionGroup`, which is not an `ArgosError` and sailed straight past the
handler. This is the "escapes the error taxonomy" class the M2 slices closed
three times inside the library, arriving at the CLI boundary, where the
consequence is not a missing ledger row but an **operator told nothing**. Fixed
in both `_run_or_exit` and the capture command by recursively unwrapping nested
groups — task groups nest, and a store error during a capture is raised two
scopes down.

**A gap the live run made concrete, which no replayed test would have shown so
plainly.** The captured database contains `price_change.v1` payloads only: all
four `book` snapshots in those 45 seconds became `unknown_event_type`
rejections, with the honest detail *"no payload model is wired for event_type
'book' yet"*. So **a stored WebSocket capture is not yet self-sufficient for
reconstruction** — the projection can replay its deltas but has nothing in the
same capture to seed from, and today a seed must come from a REST `/book` poll.
Closing this needs a WebSocket `book` payload model, which the projection slice
already established cannot be `OrderBookSnapshotV1`: in-stream `book` events
carry only ids, timestamp, hash and levels. Filed in `docs/BACKLOG.md` as the
sharpest remaining M2 gap.

## M2 slice: the order-book projection

`src/argos/projections/book.py` (`BookState`, `OrderBookProjection`,
`BookProjectionAnomalyKind`) plus `tests/test_book_projection.py`. First
implementation in the `argos.projections` package, which had been a documented
boundary since M0. Closes the last substantive open M2 exit criterion.

Quality gate: PASS — ruff, ruff format, mypy strict on 41 source files,
**1,240 tests** (up from 1,199).

**The criterion, on real recorded live traffic.** The capture contains 4 full
`book` snapshots for the subscribed token and 34 delta frames. All three
snapshot→deltas→snapshot transitions reconstruct exactly, level for level, and
a single continuous run seeded once and fed all 28 deltas lands exactly on the
final snapshot with zero anomalies. Re-verified through the shipped code path
independently of the slice's own tests.

**Two corrections the implementing agent made to my brief, both right.**

1. I said the WebSocket `book` event omits `min_order_size` and `neg_risk`.
   True only of the snapshot delivered on subscribe. The three *in-stream*
   `book` events also omit `tick_size` and `last_trade_price` — they carry only
   `market`, `asset_id`, `timestamp`, `hash`, `bids`, `asks`. This strengthens
   the design rather than weakening it: a projection requiring `tick_size`
   could not be re-seeded from the live stream at all, which is why `BookState`
   carries ids and levels only. The research note's "UNVERIFIED whether the
   omission is systematic" is now answered: it is systematic, and worse than
   recorded.
2. I asked for both real zero-size entries to be shown removing a level that
   was present. Only one belongs to the subscribed token; the other belongs to
   the binary sibling, for which the capture carries **no** `book` event at
   all, so there is no source snapshot to verify it against. The agent declined
   to claim it and pinned the honest version instead. That is the correct
   answer to a brief that asked for slightly more than the data supports.

**The reconciliation limitation, stated in the code and not only here.** The
research established that a delta's `hash` is the hash of the resulting book
state and reconciles exactly with REST for the same state — but **ARGOS cannot
compute that hash**, because the algorithm is unpublished. So the projection
cannot detect a missed delta from the delta stream alone; divergence is
detectable only when a full snapshot arrives and disagrees. The stored field is
named `source_asserted_hash`, no method compares it to anything ARGOS computes,
and the projection's own `digest()` is a separate, explicitly ARGOS-side value
for M3 golden tests. The two are asserted side by side in a test precisely so
they can never be confused.

**Out-of-order handling is deliberately M2-shallow**, and says so in prose:
deltas apply in arrival order, the last applied event time is recorded, and a
strict regression is *counted* rather than silently repaired. Equal event times
are not treated as a regression, because the research found one logical
transition spanning multiple frames with an identical `(timestamp, hash)`.
Watermarks and a real late-event policy are M3 deliverables and are not
pre-empted here.

**Anomalies are counted, never absorbed** (invariant 14):
`DELTA_BEFORE_SNAPSHOT` (real — the sibling token receives deltas but never a
snapshot), `ASSET_ID_MISMATCH`, `CONDITION_ID_MISMATCH`,
`REMOVE_OF_ABSENT_LEVEL`, `EVENT_TIME_REGRESSION`,
`SNAPSHOT_DISAGREES_WITH_PROJECTION`. Every kind is always reported including
zeros, because an absent counter and a zero counter read identically in a
report.

**A staleness in this file that the agent caught and I had missed.** Two
present-tense claims — the "Current state" bullet and the exit-criteria table
intro — still said no WebSocket adapter or capture loop existed, several slices
after both landed. Corrected. The same sentences inside earlier slice sections
are left as written: those are dated records of what was true at that slice,
not claims about now.

## M2 slice: the capture loop, and three decisions deferred four times

`src/argos/ingestion/capture.py` (`run_capture`, `FrameSource`,
`CaptureHealth`) plus `tests/test_capture_loop.py`. This is where the three
decisions deliberately deferred since the REST adapter slice were finally
made. No CLI.

Quality gate: PASS — ruff, ruff format, mypy strict on 40 source files,
**1,199 tests** (up from 1,182).

**Decision 1 — `ingest_sequence` is allocated per *record produced*, not per
candidate.** A "peek, then commit" allocator: a sequence is consumed only in
the branch that actually writes an observation or a rejection. A `None` from
the normalizer ("this frame was not about this token") is counted and burns
nothing. The alternative — reserve unconditionally, skip writing — was
rejected because it would carve permanent gaps into the ledger for every
unsubscribed-sibling frame, which the research shows is routine traffic on
this source, not an edge case.

**Decision 2 — fan-out iterates the *configured* token set, sorted, never the
frame's own membership.** This is the load-bearing one for M3. A frame carries
entries for the unsubscribed binary sibling, so iterating "tokens present in
this frame" would make sequence numbers a function of what the server happened
to bundle, and replay determinism would inherit that dependence on connection
topology. Verified directly: the same frames produce identical sequences with
and without the sibling subscribed.

**Decision 3 — there is no new manifest concept.** `RunManifest`
(`run_manifest.v2`) plus the store's append-only `capture_run` rows already are
the manifest. `run_capture` writes no `SourceProvenanceV1` of its own; every
one it touches is already embedded inside a stored envelope or rejection. This
honours the pre-M2 constraint that an M2 capture manifest must **not** embed
one provenance record per ingested event — by construction, not by added logic.

**Verified independently of the slice's own tests**, driving the real recorded
capture through a hand-written source:

- 34 real frames, both tokens subscribed → 68 accepted observations; one token
  → 34. Fan-out is exactly the configured set.
- Sequences span **both** ledgers with **no gaps and no reuse**: exactly
  1..38 across deliveries and rejections combined.
- Two identical runs produce identical sequences and identical health counters.
- Re-opening the same `capture_run_id` is refused with `StorageError`, so a
  restarted loop cannot silently restart the sequence and collide — the
  "reconnect does not reset ingest sequence" property is structural, not a
  check that could be forgotten.
- A redelivered burst produces duplicates counted and zero extra observation
  rows.
- The four `book` events in the capture are counted as
  `UNKNOWN_EVENT_TYPE` rejections rather than dropped: no payload model is
  wired for them here, and invariant 14 wants that visible rather than
  convenient.

**A backlog item I wrote was wrong, and the agent was asked to challenge it
rather than satisfy it.** `docs/BACKLOG.md` said this slice must close the
transport's "oversized frame is counted but produces no rejection-ledger row"
gap. It cannot be closed as written, and the reasoning is now recorded rather
than the item quietly dropped: `WebsocketsConnector` passes the same
`MAX_FRAME_BYTES` to `websockets.connect` that `_classify` later checks, and
the library enforces `max_size` during frame reassembly — it raises out of
`recv()` before an oversized payload is ever assembled into a Python string. No
bytes, and therefore no `raw_payload_sha256`, ever reach ARGOS.
`RejectedObservationV1.raw_payload_sha256` is required, so writing a row would
mean **inventing a hash for content ARGOS never received** — fabricated
evidence, against core invariant 7. The counter is the honest maximum. A
consequence worth naming: with the shipped connector, `_classify`'s own size
check is unreachable, and it is defence-in-depth only for an injected connector
with a larger or unenforced `max_size`.

**A known limitation the implementing agent raised unprompted, now measured
rather than assumed.** `run_capture` calls the store's synchronous SQLite
methods directly from async code, with no thread offload, so each write blocks
the event loop — including the transport's independent heartbeat task.
Measured on a real ext4 database: median **0.23 ms** per
`append_observation`, maximum **0.49 ms**, which is 0.005% of the 10-second
heartbeat interval. Real as a class, not material at this scale; filed with the
number so a future slice that batches, or runs on a slower device, has the
baseline rather than an opinion.

## M2 slice: public market WebSocket transport, and two adversarial findings

Two pieces landed together: the transport adapter
(`src/argos/sources/clob_ws.py`, `tests/test_clob_ws.py`) and an independent
adversarial review of the WebSocket ingestion step
(`tests/test_clob_price_change_ingestion_adversarial.py`), whose findings are
fixed here in `src/argos/domain/text.py` and
`src/argos/domain/observation.py`.

Quality gate: PASS — ruff, ruff format, mypy strict on 39 source files,
**1,182 tests** (up from 1,101).

### The transport

`ClobMarketWsClient` connects to the public, unauthenticated market channel,
subscribes with `{"assets_ids": [...], "type": "market"}`, sends the plain-text
`PING` heartbeat every 10 s and consumes the `PONG` reply, and yields raw
frames. All of it follows the REST sibling's shape: injected `Clock` and
`Pacer` (ADR-0009), an adapter-owned seeded `random.Random`, a frozen health
record, no wall clock. Verified independently rather than taken on trust:
backoff is reproducible across two clients sharing a seed, and its maximum
across 200 seeds × 12 attempts is exactly `MAX_BACKOFF_SECONDS`, so jitter
cannot outrun the ceiling at any legal configuration. The connection is behind
a `MarketWebSocket`/`WebSocketConnector` protocol pair, so every test drives a
fake and no test opens a socket.

**The transport decodes nothing.** It yields `MarketFrame(text, received_time,
provenance)` and does not parse JSON, split arrays, or filter by token — core
invariant 7 keeps raw data immutable and normalization a separate versioned
step, which already exists in `argos.ingestion`.

**Backpressure is real, not advisory.** The frame buffer is bounded at 64; when
it fills, the receive loop blocks, so `recv()` is not called again and
backpressure propagates to the TCP receive buffer. Frames are never dropped to
keep up. The heartbeat runs independently, so a slow consumer does not make the
client look dead to the source.

**Two gaps stated rather than hidden.** An oversized frame (above an explicit
1 MiB `max_size`, set rather than inherited from the library default) is
counted on the health record but produces **no rejection-ledger row**, because
no ingestion layer has seen those bytes and this module has no
`capture_run_id`/`ingest_sequence` to write one under — the "counted" half of
`.claude/rules/data-integrity.md` without the "reasoned" half, left for the
capture-loop slice. And a reconnect **may lose messages**: this channel has no
sequence number (confirmed absent by observation), so a gap across a reconnect
is not detectable from the channel alone. Nothing in the module claims
gap-freedom.

This slice does **not** close "reconnect does not reset ingest sequence or
silently lose manifest state": sequence allocation and the manifest are the
capture-loop slice, and neither exists.

### Adversarial findings, both fixed

**HIGH — a lone UTF-16 surrogate escaped the taxonomy with no ledger entry, on
both adapters.** `is_display_control` never inspected Unicode category `Cs`, so
`is_clean_identifier` called a lone surrogate *clean*. It passed the ingestion
check and `ObservationEnvelopeV1._validate_identifier`, then reached
`_observation_identity` → `_digest`, whose `"|".join(parts).encode()` defaults
to strict UTF-8 and raises `UnicodeEncodeError` on an unpaired surrogate —
outside every `try` in the ingestion path. Python's `json` decodes the wire
escape `"\ud800"` into exactly that string without checking pairing, so an
ordinary-looking body reaches ARGOS carrying one, with an entirely honest
`byte_length`. Reproduced on the WebSocket adapter **and on the
already-committed REST adapter**; both now return a rejection. This is the
fourth distinct instance of the "escapes the ARGOS error taxonomy, therefore no
ledger entry" class in M2.

**MEDIUM — the M1 audit-forgery attack, on a durably stored identifier.** The
rejection ledger neutralizes identifiers rather than refusing them (refusing
would mean the rejection itself could not be written), but it did so with the
*prose* rules, and prose deliberately exempts newline and tab. So a newline in
the source's `market` field survived into a stored `condition_id`, carrying
`"line1\nline2: review status: human_reviewed"` — the original M1 attack,
waiting for the first renderer that prints a ledger row without block-quoting.
A new `neutralize_identifier_and_bound` applies the identifier rule as a
*replacement* rather than a refusal.

**One reported finding was rejected on the merits.** A hash spelled in
Arabic-Indic digits was filed as a fourth hostile shape; the code is right and
the expectation was wrong. Those are category `Nd` — ordinary text that cannot
move a cursor, reorder a line, or hide itself — so refusing them would mean
enforcing a *format* on `hash`, which ARGOS deliberately does not do on either
adapter: the research observed 40 hex characters every time but never
established it as a source guarantee, and enforcing it would turn a legitimate
future format change into a total rejection storm. Contrast `token_id`, where a
format **is** enforced, because the research did establish one there. The rule
is "enforce what the evidence supports", not "enforce what looks tidy". The
test now records the decision — its original docstring claimed all four shapes
were "correctly caught" while its own assertion disproved it, the
claim-outruns-assertion pattern again.

## M2 slice: pre-WebSocket decimal and hash hardening

Two defects reported by the owner against `a9b9802`, both **reproduced before
being acted on**, both closed here in a small slice deliberately scoped to
exclude any capture-loop, reconnect, projection or manifest work. Changed:
`src/argos/domain/orderbook.py`, `src/argos/domain/pricechange.py`, and the
three test modules that pin them.

Quality gate: PASS — ruff, ruff format, mypy strict, **1,101 tests** (up from
1,068).

**1. A non-zero decimal could canonicalize to zero — and this one was mine.**
`normalize_decimal` ran `Decimal.normalize()` inside `CANONICAL_DECIMAL_CONTEXT`,
whose `Emin` was finite and inherited from `decimal.DefaultContext`. A
sufficiently small non-zero value therefore **underflowed to zero**: measured,
`parse_wire_decimal("1E-1000064")` returned `Decimal(0)` and rendered as the
canonical text `"0"` — numerically and textually identical to a genuine zero, so
a tiny non-zero price and a real zero produced the **same identity-bearing
payload**.

The provenance of this defect is worth recording precisely, because it is a
lesson about the fix and not only about the bug. The previous slice *had* a
symmetric exponent bound, and I removed the negative half deliberately, arguing
that only a positive exponent reaches the branch that expands a value into long
plain text while a very negative exponent keeps a short, equally deterministic
scientific rendering. That argument was about **text length**. The defect is
**arithmetic**. The reasoning was locally correct and answered the wrong
question, and it shipped inside the very commit whose purpose was to stop
decimal canonicalization from silently changing values.

Closed three ways, deliberately overlapping:

- `MIN_DECIMAL_EXPONENT = -1000` restores the lower bound — far below anything
  this source can produce (real tick size `0.001`, adjusted exponent `-3`) and
  comfortably preserving the already-committed `1E-50` case;
- `CANONICAL_DECIMAL_CONTEXT` now sets `Emin`/`Emax` **explicitly** instead of
  inheriting them from `decimal.DefaultContext`, which is itself mutable
  process-global state — the same class of dependency the pinned context exists
  to remove, merely relocated from read time to import time;
- a **fail-closed postcondition** refuses any result that is not numerically
  equal to its input. This is the part that matters most: the two bounds are a
  fast, legible refusal, but the postcondition guarantees the property they are
  only *believed* to imply, without depending on anyone having reasoned
  correctly about `Emin`, `prec`, or which branch expands which exponent —
  which is exactly how the underflow survived review the first time.

Zeros return before the exponent bounds are applied, since a zero carries no
magnitude: `0E-100000` is still exactly zero and must not be refused for its
exponent, while every negative-zero spelling still collapses onto positive zero.

**2. A malformed `hash` escaped as a bare `TypeError`.**
`parse_price_change_group` built its hash set — and `sorted()` it while
composing the error message for a *different* refusal — before type-checking
any hash. Reproduced: `{"hash": []}` raises `TypeError: unhashable type: 'list'`,
and a frame mixing `"a"` with `3` raises `TypeError: '<' not supported between
instances of 'str' and 'int'`. A `TypeError` is outside the ARGOS taxonomy, so
it flew past `clob_price_change`'s `except ValueError` and the frame left **no
rejection-ledger entry at all** — the silent drop core invariant 14 forbids, and
the same shape as the `decimal.InvalidOperation` escape closed one module over
two slices ago. Every selected entry's hash is now validated as a non-empty
string before the set is built, and the error message uses a bounded `repr` so
that composing the rejection detail cannot itself become the exhaustion vector.

Pinned at the boundary that actually owes the row: list, dict, null, numeric,
boolean, empty-string and mixed-type hashes each produce a
`RejectedObservationV1` that is really persisted to and read back from the
ledger with its reason and raw hash, not merely a `ValueError` in the domain.

**Acceptance criteria, all verified directly**: `1E-1000064` refused and never
zero; `1E-50` still exact; negative-zero spellings still collapse to positive
zero; no accepted finite non-zero value changes numerically; tiny non-zero and
zero cannot share one canonical identity; every malformed hash type raises
`ValueError`, never `TypeError`; and the WebSocket ingestion path records them
as rejections.

## M2 slice: WebSocket `price_change` ingestion and normalization

The ingestion-layer counterpart to `argos.domain.pricechange`, the same
relationship `clob_book.py` has to `argos.domain.orderbook`. New:
`src/argos/ingestion/clob_price_change.py` (`normalize_clob_price_change`,
`CLOB_WS_PRICE_CHANGE_EVENT_TYPE`), `src/argos/ingestion/wire.py`
(`parse_event_time`, moved out of `clob_book.py` so both adapters share one
millisecond-timestamp parser instead of duplicating it),
`argos.domain.pricechange.NoEntriesForToken`,
`tests/test_clob_price_change_ingestion.py`. Changed:
`src/argos/ingestion/clob_book.py`, `src/argos/ingestion/__init__.py`,
`src/argos/domain/__init__.py`.

Quality gate: PASS — ruff, ruff format, mypy strict on 38 source files,
**1,068 tests** (up from 1,044).

**Still no transport.** This module takes one already-decoded frame. Opening
the socket, subscribing, heartbeat, and reconnect are a separate slice, and it
is the one that introduces a new dependency — kept out of this slice
deliberately so the normalization contract could be settled first.

**Both backlog constraints this slice owed are closed**, each verified
independently of the slice's own tests:

- The byte cap is checked against `provenance.byte_length` before a single key
  is read out of the frame, returning a rejection rather than raising. A
  14,149,053-byte frame carrying 60,000 entries is refused in **0.0002 s**,
  against a parse path measured at 16.15 s for 100,000 entries.
- `entry_hash` is validated inside the module's own `try` — length against
  `MAX_IDENTIFIER_LENGTH` plus `is_clean_identifier` — reproducing the check
  `clob_book._extract_source_hash` already applies rather than the gap. This
  was the third appearance of that same shape.

**A third outcome, and why it is not a rejection.** `normalize_clob_price_change`
returns `ObservationEnvelopeV1 | RejectedObservationV1 | None`. `None` means
the frame carried no entry for the requested token — normal traffic about the
unsubscribed binary sibling, not a defect. Writing a ledger row for each would
flood the rejection ledger with rows describing healthy traffic and destroy its
signal value. It is a **counted non-event**: the capture loop must count it,
and the docstring says so unmissably, because this function has no
health-counter to write to.

**A boundary contract tightened during integration review.** The ingestion
layer originally recognised that case by comparing the exception's *message
text* against a literal copied from the domain module's source. The failure
direction was safe — a text change would produce a noisy false rejection rather
than a silent drop — but the coupling rots invisibly, so it is now a named
`NoEntriesForToken(ValueError)`, where a rename breaks the import instead of
quietly changing behaviour. It subclasses `ValueError` so every other consumer
keeps working with one `except ValueError`.

**End-to-end evidence, verified directly rather than taken from the slice's own
test names.** Real recorded frames through `normalize_clob_price_change` →
`SQLiteEventStore`:

- all 34 real `price_change` frames normalize into accepted envelopes;
- both real zero-size entries reach an envelope payload as `REMOVE`
  (`0.17` on the bid side for the YES token, `0.83` on the ask side for its
  sibling) — the exit criterion now has evidence *through the store*, not only
  at the payload;
- redelivering an identical frame yields **one observation row and a
  `duplicate` delivery** (`accepted_new=3, duplicate=1` across the run
  measured);
- the three real frames sharing one `(timestamp, hash)` for one token, each
  carrying a different level change, mint **three distinct observation rows** —
  the identity hazard proven at the store, not only in the payload's canonical
  JSON.

**No independent security review ran on this slice.** Recorded plainly rather
than implied: the module is closely modelled on an already-reviewed sibling and
was verified directly, but that is not the same as a review, and four
consecutive subagent runs on the preceding slice terminated without delivering
a report. Adversarial testing was commissioned separately.

## M2 slice: the `price_change.v1` typed delta payload

The second and last typed payload M2 needs (`src/argos/domain/pricechange.py`,
`PriceChangeV1`, `PriceLevelChangeV1`, `PriceLevelChangeKind`,
`PriceChangeGroup`, `parse_price_change_group`), built and checked against the
recorded live WebSocket capture — 34 real `price_change` frames for two tokens,
not constructed examples alone. New: `tests/test_price_change.py`,
`tests/test_price_change_adversarial.py`,
`tests/fixtures/clob/ws_market_price_change.{raw,meta}.json`. Changed:
`src/argos/domain/orderbook.py`, `src/argos/domain/__init__.py`,
`tests/test_orderbook_snapshot.py`, `tests/test_clob_adapter_adversarial.py`.

Quality gate: PASS — ruff, ruff format, mypy strict on 36 source files,
**1,044 tests** (up from 951 at the REST adapter slice).

**The exit criterion, closed structurally rather than by convention.** "Zero-size
level update is represented as removal" is now a validated invariant, not a
parsing habit: `kind is REMOVE` **if and only if** `size == 0`, enforced by a
model validator on *every* construction path including replay and
`from_record`. A record claiming `SET` at size 0, or `REMOVE` at nonzero size,
is refused rather than silently re-derived — the same "recomputable, never
drifting" discipline `_validate_anomalies_are_recomputable` already applies to
book anomalies. The two real zero-size entries in the capture (`0.17 BUY` and
`0.83 SELL`, on the two sibling tokens) are pinned as *observed*, not merely
constructible.

Deliberately **unlike** the REST snapshot path: there, a zero-size level is a
counted `OrderBookAnomaly` (`ZERO_SIZE_LEVEL_DROPPED`), because a snapshot
describing a resting order of size zero is malformed. On the delta stream, size
`"0"` is the source's only vocabulary for removal, confirmed on live traffic
rather than inferred from documentation. The two conventions differ on purpose
and the difference is the point.

**An identity hazard measured from the real bytes, stronger than the research
note recorded.** The same `(timestamp, hash)` pair for the **same token**
arrives across up to **three separate frames**, each carrying a *different*
price level change — token `34691…637961`, timestamp `1786387666174`, hash
`5ce704de…`: `0.49/636`, then `0.65/142.85`, then `0.48/17`. Since ADR-0010
derives identity from the canonical payload, a payload carrying only the hash
would have collapsed three genuine deltas onto one `observation_id` and
**silently lost two**. Carrying the level changes in the payload is what keeps
that closed, and a test built on the real frames pins it. Separately measured:
none of the 58 distinct `(timestamp, hash)` pairs in the capture spans more
than one `asset_id`, which is *why* grouping by token yields a well-defined
per-token post-state hash.

**A HIGH defect found by independent adversarial testing — and it was not
confined to this slice.** `normalize_decimal` raised a bare
`decimal.InvalidOperation` — an `ArithmeticError`, **not** a `ValueError` —
from `quantize`, for any value needing more integer digits than the ambient
precision, reachable with a plain 29-digit integer string. Being outside the
ARGOS taxonomy, it flew past `argos.ingestion.clob_book`'s `except ValueError`:
a **214-byte** body carrying `"tick_size": "1E+29"` escaped `normalize_clob_book`
entirely and produced **no rejection ledger entry** — the silent drop core
invariant 14 exists to prevent, on the *already-committed* REST adapter.

Investigating it surfaced the deeper half. `normalize()`, `quantize()` and `%`
all read `decimal.getcontext()`, which is thread-local **mutable global
state**: the wire price `0.123456789012345678901234567890123` rendered as
**three different canonical texts** — and therefore three different
`observation_id`s — under ambient precisions 5 / 28 / 50. That is the hidden
global state CLAUDE.md prohibits outright, and a direct break of core invariant
5, since a replay under a different ambient context would not reproduce the
live identity. Both are closed by pinning `CANONICAL_DECIMAL_CONTEXT` and
refusing an explicit magnitude budget *before* any context-sensitive operation
runs. Re-measured after the fix: identical rendering across 18 ambient contexts
spanning precision, rounding mode and `Emin`/`Emax`, and across a worker thread
with a hostile default context.

Widening the context instead of refusing was considered and rejected as a fresh
resource-exhaustion vector rather than a fix: `"1E+1000000"` is 11 wire bytes
that would render as a one-megabyte canonical integer.

| Severity | Finding | Fix |
|---|---|---|
| HIGH | `decimal.InvalidOperation` escaping the error taxonomy, reachable on the committed REST adapter with a 214-byte body, leaving no ledger entry | `CANONICAL_DECIMAL_CONTEXT` plus explicit budgets in `normalize_decimal`; re-measured, no input to `parse_wire_decimal`, `parse_order_book_snapshot` or `parse_price_change_group` now escapes as a non-`ValueError` |
| HIGH | Canonical form — and therefore `observation_id` — depended on `decimal.getcontext()`, thread-local mutable global state | Same fix; verified independent across 18 ambient contexts and a second thread |
| MEDIUM | A 1,000,001-digit `int` cost **63.12 s** of CPU, because the magnitude budget ran *downstream* of the superlinear `Decimal(int)` conversion it was meant to bound. Not reachable through this project's own JSON decoding today, but only because CPython's `int_max_str_digits` default blocks it — a third-party default this module does not own | `_refuse_oversized_int` checks `bit_length()` first; refusal now costs 0.21 s |
| MEDIUM | An unrecognized **top-level** frame key was silently ignored while entry-level keys were refused — including a key spelled `sequence`, which this channel is confirmed to lack and whose arrival would have been discarded without trace | `EXPECTED_EVENT_KEYS` refuses it |
| MEDIUM | An entry that is not an object, or carries no `asset_id`, was skipped silently — and since parsing runs once per token, it would have been dropped for *every* token, never counted | Refused; found during integration review, not by an agent |

**A committed test was asserting something it never checked.** The old
`test_very_high_precision_beyond_any_real_tick_size_does_not_crash_and_is_preserved`
asserted only `str(...).startswith("0.4444")` — which a *truncated* value
satisfies just as well as a preserved one. Measured: the 61-digit wire value was
silently rounded to 28 digits and the stored value compared **unequal** to the
source. ARGOS durably stored a price the source never sent, called it
preserved, and passed its own adversarial test. Rewritten to assert refusal,
plus a non-degenerate companion asserting exact preservation *within* budget.
This is the "claim outrunning its assertion" pattern STATUS already names as
recurring — third occurrence.

**A committed test also caught a defect in the fix itself.** The exponent budget
was first written symmetrically; only a *positive* exponent reaches the branch
that expands a value into plain text, so the negative half silently narrowed an
already-shipped contract, and
`test_very_high_precision_beyond_any_real_tick_size_does_not_crash_and_is_preserved`
failed on a `1E-50` tick size. The bound is now one-sided, with the reasoning
recorded in the code.

**What held up under attack, recorded as negative results.** No collision was
found inside the `changes` tuple's canonical JSON — real JSON structure, rather
than a custom separator, closes the ADR-0010 blocker-B1 class by construction.
Scrambled wire order across all 34 real frames never changed a result. The
negative-zero and trailing-zero collapses still hold identically for
`OrderBookSnapshotV1` after the shared helper was rewritten, and every value in
both real fixtures still parses and is preserved **exactly**. `price_change.v1`
is **not** the first payload model with a free-text field: every string that
reaches `to_record()` is a pattern-validated identifier, an enum, or a
canonical decimal rendering — so the store-level OSC-52 exposure STATUS names
as its most important open item is **not** opened by this slice.

**Scope deliberately excluded, and why.** No WebSocket transport, no capture
loop, no sequence allocator, no manifest. `parse_price_change_group` normalizes
for **one** requested token per call, because a frame carries entries for the
unsubscribed binary sibling and fan-out would force a sequence-allocation policy
M3 replay determinism would inherit. A `(frame, token)` group carrying more than
one distinct hash is refused outright — never observed in either live capture,
and refusing an unobserved shape is preferred to inventing a grouping policy
that would then have to be reproduced byte-for-byte forever. All four
constraints are filed in `docs/BACKLOG.md` with their measurements.

**Process facts, recorded rather than smoothed over.** Four consecutive
subagent runs on this slice ended without delivering a report. In each case the
work products were verified directly instead of treating silence as success —
which is how the HIGH finding above was recovered: the adversarial agent had
left **5 tests failing on purpose** to pin a real defect, exactly as instructed,
and a silent completion would have looked identical to a clean run. A green
suite plus a silent agent remains the most dangerous combination in this
repository, now twice demonstrated. Separately, one security review was
invalidated by my own coordination error: I edited `orderbook.py` while a
read-only reviewer was measuring it. Read-only does not mean immune to
concurrent edits, and the remaining security verification was completed
directly rather than re-delegated.

## M2 slice: public CLOB REST order-book adapter and normalization

The public CLOB REST order-book snapshot adapter and its normalization step —
the first complete vertical slice of M2: real recorded bytes go in one end
and a deduplicated, identity-stable observation lands in durable storage at
the other. New: `src/argos/sources/clob.py` (`ClobClient.get_book`,
`ClobHealth`, `ClobBookNotFoundError`, `ClobBookBadRequestError`),
`src/argos/ingestion/clob_book.py` (`normalize_clob_book`,
`MAX_NORMALIZABLE_BYTES`), `tests/test_clob_client.py`,
`tests/test_clob_book_ingestion.py`, `tests/test_clob_adapter_adversarial.py`,
`tests/fixtures/clob/` (a byte-identical copy of the recorded real capture,
with provenance sidecar). Changed: `src/argos/domain/orderbook.py`,
`src/argos/sources/__init__.py`, `src/argos/ingestion/__init__.py`.

Quality gate: PASS — ruff, ruff format, mypy strict, **951 tests** (up from
886 when the adapter first landed within this slice, 841 before the slice).

**Scope deliberately excluded, and why.** `ingest_sequence` is a
caller-supplied parameter to `normalize_clob_book`, not allocated by it —
`normalize_clob_book` is a pure, synchronous normalization step, not the
capture loop. No capture loop, sequence allocator, scheduler, manifest
writer, or CLI exists in this slice. The WebSocket research found a frame can
carry entries for an *unsubscribed* sibling token, so assigning sequence
numbers before subscription filtering would make `ingest_sequence` depend on
connection topology, and M3 replay determinism would inherit that dependency.
Allocation is left as a decision the capture-loop slice must make
deliberately rather than inherit by accident.

**What actually moves on the M2 exit criteria.** "Duplicate source event does
not create a second accepted observation" now has **end-to-end evidence, not
just store-level evidence**: a real recorded response fed through
`ClobClient` → `normalize_clob_book` → `SQLiteEventStore` produces one
observation row and two delivery rows (`accepted=1, duplicate=1`) for a
redelivery. There is still no capture loop running against live traffic, so
this is described precisely rather than declared closed outright — see the
updated exit-criteria table below. "Invalid messages enter a rejection ledger
with reason and raw hash" moves the same way: a malformed real-shaped body
now produces a `RejectedObservationV1` written to the ledger through the
adapter path, not only through a hand-built envelope. Everything else in the
exit-criteria table stays open: no WebSocket adapter, no capture manifest, no
capture CLI, no projection.

**Security review: PASS_WITH_FINDINGS, no blocker.** The central question was
whether the new adapter inherits the two M1 HIGH fixes (the gzip-bomb
resource-exhaustion shape and the retry-deadline-bypasses-injected-clock
shape) as real properties or only as copied code shape. Re-measured with the
original instruments, the answer is real properties:

- Gzip bomb: 1.2 GiB decompressed from a 1,223,023-byte body is refused,
  `byte_length_so_far=67414112`, in 0.186 s at 174.7 MiB peak RSS —
  indistinguishable from `gamma.py`'s own 0.184 s / 174.8 MiB. The
  `content-length` pre-check refuses before a single byte is streamed; a
  *lying* small `content-length` with a 40 MiB body is still caught
  incrementally.
- Slow drip against a real `RealPacer`: the deadline held at 1.00 s (1
  attempt) and 23.00 s (3 attempts), not just the zero-retry path proven at
  M1 close, and fired *inside* a real backoff sleep with
  `cancelled_inside_backoff=True` — the pre-M2 pacing correction is inherited
  as a real property, not only copied shape.
- Jitter: the adapter-owned seeded RNG is immune to `random.seed()` on the
  module global (verified by seeding it to 12345 and 999 between runs) and
  bounded — across 200 seeds × 10 attempt numbers the maximum was exactly
  `MAX_BACKOFF_SECONDS`, so jitter cannot outrun the deadline budget at any
  legal configuration.
- Token id: all 18 tried hostile ids (`../../admin`, `123%2f..%2fadmin`,
  `https://evil.example/x`, CR-LF injection, 121 digits, a trailing newline,
  Arabic-Indic/fullwidth/mathematical digit spellings, ZWSP-separated digits)
  are refused with **zero requests sent**. `fullmatch` on `[0-9]{1,120}`
  blocks the trailing-newline bypass a bare `re.match` would allow; the id
  lands in a query parameter, not the URL path.
- The execution boundary is confirmed absent; no new dependencies; the only
  headers sent are `accept` and `user-agent`; the one log call carries
  path/attempts/status only, never a payload or a URL.

**Findings fixed in this slice**, all with regression tests:

| Severity | Finding | Fix |
|---|---|---|
| HIGH | Unbounded CPU and RSS in `normalize_clob_book`, in the gap between the client's 32 MiB response cap and the envelope's 4 MiB canonical-payload cap. Measured, all sized to pass the client cap: a 31 MiB `timestamp` cost 21.18 s CPU and was **accepted**; a 31 MiB `market` on a rejected payload cost 32.37 s and 571 MiB RSS; 900,000 book levels peaked at 1,266 MiB. Cause: `neutralize_and_bound` bounds the stored value but neutralizes the whole input first, and both the accepted and rejected paths run it twice. A `Pacer` cannot bound it — a cancel scope cannot interrupt synchronous CPU work, measured at 14.21 s elapsed against a 0.50 s deadline with `cancelled_caught` false. The same class the earlier M2 security review closed at `build_observation_envelope`, relocated one layer upstream | `MAX_NORMALIZABLE_BYTES` checked before any text is read, returning a rejection rather than raising (`src/argos/ingestion/clob_book.py`). Reproduced at 8 MiB: **5.48 s → 0.02 s**, and the result changed from an accepted observation to a counted rejection |
| MEDIUM | `normalize_clob_book` raised on two attacker-reachable inputs, contradicting its own docstring and producing no ledger entry: a 257-character `hash`, or one containing a newline, reached `_validate_identifier` *outside* the module's `try` and propagated a raw pydantic `ValidationError`. Reproduced on a 3.7 KB payload — no resource cost needed | Validated inside `_extract_source_hash`; the 256-character boundary is exact and pinned by a regression test |
| MEDIUM | The M1 HIGH properties held but nothing pinned them against a future edit to `clob.py` | Added deadline-fires-during-backoff, slow-drip, redirect-never-followed, and seeded-RNG-independence tests |

**Adversarial testing found one defect, now fixed: negative zero split one
economic price into two identities.** `_normalize_decimal` never
special-cased the sign of zero: `Decimal("-0")` survives `normalize()` with
exponent 0, so the quantize branch never fired, and it rendered as canonical
text `"-0"`. Since identity hashes rendered canonical text rather than the
`Decimal` value, the same price spelled two ways minted two `observation_id`s.
Reachable because `Decimal('-0') >= Decimal('0')` is `True`, so it passes the
`ge=MIN_PRICE` validator on exactly the two fields whose range includes the
boundary — `last_trade_price` and a level's `price`; `tick_size`/
`min_order_size` are `gt=0` and immune. Fixed by collapsing any zero onto
positive zero in `_normalize_decimal` (`src/argos/domain/orderbook.py`); the
fix is general, not a patch for the literal spelling — `-0`, `-0.0`, `-0E+5`,
`-0.00000`, and `0E+3` all render `"0"`. **This is the second independent
instance of the ADR-0010 decimal-normalization class**, after the
trailing-zero case `OrderBookSnapshotV1` already closed — a pattern, not an
isolated incident.

**What held up under adversarial attack, worth recording as a negative
result.** A fully *scrambled* wire order still re-derives correct
`best_bid`/`best_ask` — the code genuinely does not trust wire order.
Determinism survived duplicate JSON keys, whitespace and key-order
differences, exponent notation, and leading-zero/plus-sign spellings, while a
genuinely different book still mints a different id. Every malformed shape
returns a rejection rather than raising, including a hostile ESC sequence in
`market`, which is neutralized in the stored `condition_id`. A 404 is raised
*before* any payload reaches the normalizer, proven structurally. On an
A → B → A′ revert, three distinct observations are minted, verified by
reading `_observation_identity`'s call site: the distinction is driven by
`event_time` advancing, not by incidental raw-byte differences —
`raw_payload_sha256` plays no role in identity at all.

**Process fact worth recording.** The adversarial-testing agent run on this
slice ended without delivering a report. The test file it had written was run
directly rather than treating the silence as "no findings found"; 1 of 54
tests was failing, and investigating it by hand turned up the negative-zero
defect above. A green suite plus a silent agent is exactly where a real
defect can slip through unrecorded.

## M2 slice: idempotent SQLite event store and delivery record (ADR-0011)

Committed as `25c6f05`. Specified by ADR-0011
(`docs/adr/0011-sqlite-event-store-and-delivery-record.md`), which closes two
decisions this slice inherited as open: the SQLite/WAL-vs-append-log
comparison `docs/12_TECH_STACK.md` required, and the delivery-record shape
ADR-0010 explicitly left for "the store slice" to decide before it writes a
row. New: `src/argos/store/event_store.py` (`EventStore` protocol,
`SQLiteEventStore`, `open_sqlite_event_store`,
`Disposition`/`CompletionStatus`,
`DeliveryRecord`/`RejectionRecord`/`CaptureRunRecord`/`CaptureRunCounts`),
`tests/test_event_store.py`, `tests/test_event_store_adversarial.py`. Changed:
`src/argos/store/raw_archive.py`, `src/argos/store/__init__.py`,
`src/argos/domain/text.py`, `tests/test_boundaries.py`,
`tests/test_observation_envelope.py`, `tests/test_raw_archive.py`.

Quality gate: PASS — ruff, ruff format, mypy strict on 33 source files,
**841 tests** (up from 779 at the observation-identity slice).

**Engine decision, and the honest part of it.** Volume was measured from
artifacts already in the repository — 1.05 accepted observations/second/token
over the 36.1 s live WebSocket capture; 4,362 B per full envelope record, of
which 1,274 B is overhead independent of payload, so the fixed overhead is
roughly 6x the payload itself for a `price_change` delta; 345 B of embedded
provenance per record, ~31 MB/day/token duplicated at the measured rate — but
**volume did not decide the engine**: both candidates clear the ~1-2/s
requirement by roughly four orders of magnitude (~11.9k events/s measured at
`synchronous=FULL`, no batching). Atomicity decided it instead: "idempotent
and observable" (`docs/02_ARCHITECTURE.md`) is a two-write operation — detect
the duplicate, record its arrival — that must land together or not at all.
SQLite does that in one transaction; an append log needs a journal to close
the crash window between two appends, which is re-implementing SQLite worse.
`WITHOUT ROWID` was rejected on a measurement reproduced independently for
this ADR: **4,681 B/row against 1,456 B/row**, because a >1 KB record spills
to overflow pages inside an index B-tree. Parquet is rejected outright for
M2 — new large dependency, columnar and wrong for row-at-a-time append, no
unique key at all — reconsiderable only as an M4 export format.

The volume evidence is recorded as thin by ADR-0011 itself, and STATUS
repeats that rather than softening it: n = 1 token, 36 seconds, one
connection, no reconnect, an upper-tail market during a live match, no
quiet-market sample, and REST volume not measured at all (no polling cadence
chosen yet). ADR-0011 treats a future capture contradicting the
extrapolation by an order of magnitude as a reason to revisit retention and
compaction, not the engine choice, because volume was not what decided it.

**A contradiction in ADR-0011 itself, found and corrected during
implementation.** Section 5 as first written described `capture_run` as one
row with `ended_at NULL` meaning "not closed", while the same ADR's own
Consequences forbid any `UPDATE` inside `argos.store` — closing a run by
setting a column on an existing row is itself a mutation. The ADR now records
this as a dated Correction (2026-08-11) rather than a silent fix:
`capture_run` is append-only, closing inserts a second row, "open" is a
derived read, and two partial unique indexes make double-open and
double-close impossible at the schema level. The cost, recorded rather than
hidden: `capture_run_id` is no longer unique in that table, so
`delivery.capture_run_id` cannot be a SQL foreign key to it, and "the named
run was opened and is still open" is a Python check inside the transaction —
genuinely weaker than a database constraint. Security review assessed this
specific weakening as **weaker, not exploitable**: `BEGIN IMMEDIATE`
serializes writers, and a measured 3-process race against it produced a
consistent store.

**Two M2 exit criteria now have store-level evidence** (see the updated M2
exit-criteria table below), while remaining explicit that no adapter or
capture loop feeds them yet: `append_observation` inserts zero second
observation rows and exactly one `delivery` row with `disposition="duplicate"`
for a redelivery, both writes in one `BEGIN IMMEDIATE` transaction;
`append_rejection`/`iter_rejections` persist the rejection ledger, keyed
`(capture_run_id, ingest_sequence)` rather than on `rejection_id` alone. "An
interrupted capture closes or marks its manifest incomplete" now has its
**store half** only: `capture_run` is append-only and a run with no closing
row is a queryable signal via `iter_open_capture_runs`. No capture loop
exists, so none of the three is marked closed.

**Review verdicts.** Independent testing added 25 adversarial tests: real
multi-connection races (duplicate insert, close-vs-close, append-vs-close,
delivery-vs-rejection sequence contention) each produced exactly one winner;
a simulated crash mid-transaction left no partial row in either direction,
including the duplicate path; append-only held behaviourally; three
real-fixture-derived malformed entries sharing one `rejection_id` all
survived; a byte-identical round trip held including a `PRESENT` event_time
the existing suite had never exercised. Security review returned
**PASS_WITH_FINDINGS, no blocker**: no SQL injection (every statement is a
module constant; a `DROP TABLE` payload round-tripped as a value), foreign
keys genuinely on, atomicity holds, `synchronous=FULL` really syncs (17
`fdatasync` calls measured on ext4, **0 on tmpfs** — recorded because it means
the durability guarantee is filesystem-dependent), no wall clock, lazy
iterators, no execution/wallet/credential/authenticated surface, and no new
dependency.

**Findings fixed in this slice**, all with regression tests:

- `BEGIN IMMEDIATE` sat outside `_transaction`'s own `try` — found
  independently by *both* reviews and reproduced before the fix. The
  statement most likely to fail during a real capture ("database is locked"
  against a concurrent writer; "Cannot operate on a closed database") escaped
  as a bare `sqlite3.OperationalError`. Now inside the try, with the
  underlying sqlite message preserved in the error's `context`.
- `capture_run_id` bypassed the identifier contract that
  `ObservationEnvelopeV1._validate_identifier` already enforces for the same
  field name elsewhere — OSC 52 and RLO survived into
  `iter_open_capture_runs`, the exact "interrupted capture" report a future
  renderer prints, and 20,000,000 characters were accepted in 0.168 s. The M2
  security-review HIGH finding relocated to a new boundary; now refused via
  `_validate_capture_run_id`.
- Six read-path corruption shapes (`json.JSONDecodeError`, `ValueError`,
  `TypeError`) escaped the taxonomy the module documented as `StorageError` —
  the M1 `read_raw_payload` finding reopened in the same package. A
  `_decoding` context manager now converts any decode failure to
  `StorageError`.
- The append-only boundary check under-covered badly: the forbidden-token set
  was `UPDATE|DELETE|ALTER`, and `REPLACE INTO`, `INSERT OR REPLACE`, `DROP
  INDEX`, `PRAGMA writable_schema`, `ATTACH`, and `VACUUM` all passed it.
  Security review demonstrated `REPLACE INTO observation` actually destroying
  an immutable row — and `INSERT OR REPLACE` is the idiom a future author
  most plausibly reaches for to make a write "idempotent", the very concept
  this store is built on. The token set is extended; the test now documents
  that the check is literal-only, not a general guarantee.
- The WAL durability claim was prose, not a property: `PRAGMA
  journal_mode=WAL` discards its result, and SQLite returns the mode actually
  in effect rather than erroring — verified that `:memory:` silently reports
  `memory`. ADR-0011 hangs the interrupted-capture criterion on WAL recovery,
  so all three pragmas are now read back and a mismatch raises `StorageError`.
- `raw_archive._write_atomically`'s `os.replace` and its new directory
  `fsync` ran outside the guard: the failure escaped as a bare `OSError`
  *after* the rename it exists to make durable had already happened, so the
  call reported failure while the file was on disk. Both are now inside the
  guard, and the error message states that distinction explicitly.
- A stale `.partial` file left by a real process kill permanently wedged that
  hash (a bare `FileExistsError`, no self-healing) and defeated the
  documented sidecar-repair path. A stale *regular* file is now cleared and
  the write proceeds.

**Two things worth recording as process facts, not just outcomes.** The
first fix for the stale-`.partial` finding above introduced a security
regression: an unconditional unlink would have silently downgraded the M1
symlink guard from "refuse" to "delete and proceed", because
`O_CREAT|O_EXCL` reports a planted symlink as the same `FileExistsError` as a
stale regular file. The pre-existing symlink regression test caught it
immediately; the final version clears only a regular file and refuses
anything else with `ImmutabilityViolationError`. Separately, a finding
neither review caught, found while writing a regression test:
`is_clean_identifier` delegated wholly to `is_display_control`, which
deliberately exempts `\n`/`\t` (correct for prose, where the M1 defence is
sanitizer *plus* block-quoting) — so **a newline passed identifier
validation on `market_id`, `condition_id`, `token_id`, `source_sequence`,
`source_hash`, `capture_run_id` and siblings through two security reviews**,
and a newline is the original M1 attack that forged
`review status: human_reviewed` into an audit. `is_clean_identifier` now
refuses `\n`/`\t`/`\r` independently of `is_display_control`. The existing
test's docstring had claimed newlines were refused while asserting only ESC
and RLO — a claim outrunning its assertion, a pattern that recurs often
enough in this repository to name here rather than treat as a one-off.

## M2 slice: observation identity and rejection ledger (ADR-0010)

Slice: the canonical `ObservationEnvelopeV1` + `RejectedObservationV1`
contracts (`src/argos/domain/observation.py`), serving two M2 exit criteria —
"duplicate source event does not create a second accepted observation" and
"invalid messages enter a rejection ledger with reason and raw hash" — plus a
public CLOB REST research slice (`docs/research/m2-clob-rest-book.md`,
committed as `da08d1d`) that fed the identity design with measurements against
the real endpoint rather than assumption.

Quality gate: PASS — ruff, ruff format, mypy strict on 32 source files,
**710 tests** (up from 594 at the pre-M2 slice).

The architecture review returned **BLOCK**. Every finding was reproduced
before acting on it; all five blockers are fixed and re-verified:

- **B1** — identity material was joined on `\x1f`, so a source-controlled
  separator could shift a field boundary and collide two different
  observations. Fixed with a length-prefixed injective encoding
  (`_digest` in `src/argos/domain/observation.py`). Independent adversarial
  testing found the same class of collision on the `market_id`/`condition_id`
  and `condition_id`/`token_id` boundaries and in the rejection ledger; all are
  covered by regression tests in `tests/test_observation_envelope_adversarial.py`.
- **B5** — the envelope held live Python objects, so `to_record()` dumped in
  JSON mode and `Decimal('0.5')` reloaded as `'0.5'`: the replayed envelope
  differed from the live one while carrying the same `observation_id` — core
  invariant 5 broken, and M3's identical-hash criterion runs through this
  object. Fixed by storing the canonical JSON form and restoring types via
  `read_payload`.
- **B4** — any mapping could previously be labelled with any
  `payload_schema_version`; the mismatch would have surfaced only when
  `read_payload` ran during replay, against a capture that cannot be re-taken.
  Fixed by having `build_observation_envelope` take the typed `VersionedModel`
  rather than a mapping plus a version string.
- **B2** — `SourceProvenanceV1.http_status` was mandatory, which would have
  forced the WebSocket adapter (a same-milestone deliverable) to invent an
  HTTP status inside the provenance contract. Now nullable.
- **B3** — `docs/04_DATA_CONTRACTS.md` specified a clock-skew tolerance and
  quality flag that did not exist in code — the M1 "specified contract
  silently dropped" pattern repeating. Implemented as
  `ObservationQualityFlag.EVENT_TIME_AHEAD_OF_RECEIPT`; the doc and the code
  now agree.

Also fixed from the same reviews:

- **N8** — the sanitizer had been duplicated into the domain on a justification
  that was false (`compiler/audit.py` already imports from `argos.domain`),
  and its docstring claimed a parity test that did not exist. Now a shared
  `argos.domain.text` module used by both `compiler.audit` and
  `domain.observation`.
- Adversarial testing found that Unicode tag characters (U+E0000-U+E007F) and
  U+200B/U+FEFF are Unicode category `Cf`, not `Cc`, so they survived the
  original control-character sanitizer into stored records — an invisible-text
  smuggling channel. Now neutralized in `argos.domain.text`; ZWJ/ZWNJ are
  deliberately kept because they are load-bearing in Indic and Perso-Arabic
  scripts and cannot reorder or hide surrounding text.
- Adversarial testing reproduced that pydantic's `model_copy(update=...)` does
  **not** re-validate, so a bare mutable dict could land in a stored payload
  and a later external mutation would reach `to_record()`. Fixed structurally
  on `VersionedModel.model_copy`, which now copies through validation.
- **N1** — `source_sequence="absent"` could forge the identity of
  `source_sequence=None`; fixed by the same length-prefixed encoding as B1.
- **N2** — identity is derived only from validated field values, and
  `recompute_observation_id` makes it auditable rather than trust-only.
- **N6** — envelope `source` is now cross-checked against `provenance.source`
  at construction, so an envelope cannot attribute itself to a different
  source than the bytes it points at.

Research findings recorded as project reality (from real recorded public
payloads against `clob.polymarket.com`, not from documentation or assumption
— see `docs/research/m2-clob-rest-book.md` for the fixtures and the
three-poll timing experiment):

- the CLOB book response has no sequence number, only a millisecond
  `timestamp` and a content `hash`;
- `timestamp` tracks the book's last change, not the response time — verified
  by polling one token three times, five seconds apart;
- both `bids` and `asks` end at top of book (ascending / descending
  respectively), the reverse of the naive `[0]` reading;
- 404 is ambiguous across a closed market, an unknown token, and a
  syntactically valid token that never had a book.

## M2 slice: typed order-book snapshot payload (`OrderBookSnapshotV1`)

Committed as `1901b4e`. The first typed payload named by
`ObservationEnvelopeV1.payload_schema_version` (`"order_book_snapshot.v1"`,
`src/argos/domain/orderbook.py`), built and checked against the real REST
`/book` fixture the earlier research slice recorded, not only constructed
examples.

- Every `Decimal` price/size/tick-size/last-trade-price field is normalized on
  the way in (`_normalize_decimal`), closing the ADR-0010 "consequences"
  constraint directly on the first payload that could have hit it:
  `Decimal("0.430")` and `Decimal("0.43")` now serialize to identical
  canonical text, checked against the real recorded two-precision case
  (`tests/test_orderbook_snapshot.py::test_the_same_price_at_two_text_precisions_produces_identical_canonical_json`,
  `::test_observation_identity_collapses_across_a_cosmetic_decimal_reformat`).
- Floats and bools masquerading as `Decimal` are refused outright on every
  price/size field, even if the caller's own JSON decoding produced one
  (`tests/test_orderbook_snapshot.py::test_a_float_price_is_refused` and
  siblings). Wire order (bids ascending, asks descending — the reverse of the
  naive `[0]` reading) is never trusted: every construction path, not only
  parsing, is re-validated into "best level first" order and refuses rather
  than silently re-sorts an out-of-order snapshot.
- Anomalies — a dropped zero-size level, an off-tick price, a crossed book, a
  locked book — are recorded as counted, reasoned `OrderBookAnomaly` entries
  rather than raised or silently dropped, per core invariant 14. A *duplicate*
  price level on one side has no non-arbitrary resolution and refuses the
  whole snapshot instead. This vocabulary (`OrderBookAnomalyKind`) is
  deliberately kept separate from `ObservationQualityFlag`, which lives on the
  envelope and describes a different class of defect (delivery, not payload
  content).
- Zero-size levels were never observed in the REST fixture
  (`tests/test_orderbook_snapshot.py::test_zero_size_levels_were_never_observed_in_the_real_fixture`),
  matching the REST research note's suspicion. The WebSocket research below is
  what actually observed one live, on the delta stream specifically.

No adapter or store consumes this model yet; it is a domain payload type
built and tested against a recorded fixture, the same relationship
`MarketDefinitionV1` had to the Gamma fixtures at M1.

## M2 security review: observation and order-book contracts

Verdict: **PASS_WITH_FINDINGS, no blocker.** Findings closed in `1fb057c`.
Both original M1 attack classes — the resource-exhaustion shape and the
newline/OSC-52 rendering-forgery shape — were re-measured through the real
audit/envelope pipeline rather than re-read, and neither regressed from
consolidating the sanitizer into the shared `argos.domain.text` module (see
the ADR-0010 slice, "N8" above): the review ran a differential across all
1,112,064 legal Unicode codepoints and found 0 codepoints the consolidated
sanitizer stopped neutralizing and 130 gained, i.e. the move is a strict
superset of the sanitizer it replaced. This is recorded here as the review's
own measurement; no such full-codespace sweep is committed as a repository
test today.

| Severity | Finding | Fix |
|---|---|---|
| HIGH | Building an envelope traversed the normalized payload four times; a 31.8 MiB payload (legal under the source client's own 32 MiB response cap) cost 2.84 s CPU and 750 MiB RSS, and the `Pacer` deadline could not bound it because a cancel scope cannot interrupt synchronous CPU work — measured at 1.03 s elapsed against a 0.50 s deadline with `cancelled_caught` false. The M1 gzip-bomb class, relocated downstream of the byte cap that fixed it | Capped at `MAX_PAYLOAD_CANONICAL_BYTES` (4 MiB) and serialized once; the same payload is now refused in 0.17 s at 129 MiB peak RSS (`src/argos/domain/observation.py::build_observation_envelope`, `tests/test_observation_envelope_adversarial.py::test_an_oversized_payload_is_refused_inside_the_taxonomy`) |
| HIGH | `market_id`, `condition_id`, `token_id`, `source_sequence`, `source_hash` stored verbatim — ESC, OSC 52, RLO, and newlines all survived — and unbounded; one measured at 20,000,000 characters beside a `detail` capped at 4,043. The M1 finding recurring | The envelope now refuses a hostile or oversized identifier outright (`_validate_identifier`); the rejection ledger neutralizes and bounds them instead, deliberately, because refusing there would mean the rejection itself could not be written (`tests/test_observation_envelope_adversarial.py::test_rejection_detail_neutralizes_zero_width_and_unicode_tag_characters`, `tests/test_observation_envelope.py::test_a_hostile_identifier_is_refused_from_an_accepted_observation`, `::test_the_rejection_ledger_bounds_a_hostile_identifier_instead_of_refusing_it`) |
| MEDIUM | Truncation is lossy and identity was derived from the truncated value: two payloads differing only past the cap shared one `observation_id` while their raw hashes differed | `neutralize_and_bound`'s truncation suffix now carries a digest of the full neutralized text, restoring injectivity for anything that hashes the stored value (`src/argos/domain/text.py::neutralize_and_bound`, `tests/test_observation_envelope_adversarial.py::test_truncated_text_stays_distinguishable_and_recomputable`) |
| MEDIUM | U+061C ALM was missing from the bidi mark set the docstring claimed to cover; U+FFF9-FFFB interlinear annotation is display forgery of the class the sanitizer already closes for bidi | Both now neutralized in `argos.domain.text.is_display_control` |
| MEDIUM | pydantic's bare `ValueError` past nesting depth 254, and `freeze`/`thaw`'s `RecursionError`, escaped the ARGOS error taxonomy — both trivially reachable from hostile JSON | Both now surface as `ContractViolationError` (`src/argos/domain/observation.py::_canonical_payload`, `tests/test_observation_envelope_adversarial.py::test_a_pathologically_nested_payload_fails_inside_the_taxonomy`) |
| LOW | The rejection ledger had `recompute_observation_id`'s auditability but no equivalent of its own — a forged `rejection_id` could not be checked against the stored fields | `recompute_rejection_id` added (`src/argos/domain/observation.py::recompute_rejection_id`) |

Deliberately **not** fixed this slice, each filed in `docs/BACKLOG.md` with its
own reasoning rather than dropped silently:

- the variation-selector/ZWJ/ZWNJ covert channel (a 31-byte instruction was
  demonstrated surviving into a rendered audit with zero visible glyph
  difference) — a detection problem for the consuming layer, not something a
  sanitizer can close, and left explicitly unsolved;
- `SourceProvenanceV1.http_status` being nullable makes "non-HTTP transport"
  indistinguishable from "adapter forgot to set it" — no transport
  discriminator exists yet;
- `schema_version` uniqueness is unenforced across `VersionedModel`
  subclasses;
- neither `observation_id` nor `rejection_id` is enforced by a validator — a
  forged id round-trips through `from_record` while `recompute_*` disagrees;
- `SourceProvenanceV1.endpoint` still has no redaction contract for a future
  credential-in-query mistake, now with a larger blast radius since it
  persists per observation rather than per run manifest;
- `RejectedObservationV1.detail` retains newlines by design, and the M1
  defence was sanitizer **plus** block-quoting — only the sanitizer carried
  across, because no ledger renderer exists yet to block-quote into.

## M2 research: public market WebSocket channel

`docs/research/m2-clob-websocket.md`, committed as `f508fbb`. Scope is the
public, unauthenticated market channel only, per
`.claude/rules/no-execution.md`. Two live captures against real traffic for
one actively-trading token (~85 combined seconds), not documentation alone,
answer the three questions the REST research note left open:

- **No sequence number** — confirmed absent by direct observation across both
  captures, not only by absence from the documentation. Gap detection on this
  channel, like REST, has to be built on `(timestamp, hash)` reconciliation.
- **A `price_change` delta's `hash` is the hash of the resulting book state**,
  and reconciles **exactly** with what REST `/book` returns for the same
  state — confirmed twice independently, within the WebSocket stream itself
  and against concurrent REST polls for the same token. This is exact content
  identity, not heuristic matching.
- **Zero-size means removal**, observed directly on live traffic, including
  three removals batched into one update — the M2 exit criterion's convention,
  with real evidence, on the WebSocket delta stream specifically (never
  observed on REST, matching the REST note's suspicion).

**Consequence for ingestion design.** `(timestamp, hash)` identifies a
*post-state*, not a wire message: six `price_changes` entries shared one hash
in a single message, and two distinct frames 196 microseconds apart carried an
identical `(timestamp, hash)` pair. An adapter must not assume a 1:1 mapping
between a WebSocket message and a book transition. This makes the residual
collision ADR-0010 already documents — "two source messages that are
genuinely different events but share every stable field... remain
indistinguishable" — **reachable in practice for this source, not merely
theoretical**. Filed as a constraint the store and the WebSocket adapter must
both handle, in `docs/BACKLOG.md`.

Also recorded: REST `/book` returned 403 for Python's default `urllib`
User-Agent while `curl`'s default UA and a browser-like UA both succeeded.
**UNVERIFIED as a general rule** — only two User-Agent strings were tried —
but the capture adapter must set an explicit, reasonable User-Agent and must
not read "no auth header" as "no client-identification requirement."

No WebSocket adapter exists yet; this is a research document only, the same
status the REST research note had at its own stage. The document's own
UNVERIFIED list (idle-timeout duration, reconnect behavior, rate limits,
maximum token ids per connection, `operation: subscribe/unsubscribe` on an
open connection, the three `custom_feature_enabled` event types, whether the
observed `min_order_size`/`neg_risk` omission on one live `book` event is
systematic) is unresolved and must not be assumed by the capture loop.

## M2 exit criteria

Tracking `docs/07_MILESTONES.md`.

**Re-checked by the M3 readiness audit (2026-08-17), and two rows below were
stale in the direction that flatters the milestone.** The table's introduction
said "no capture CLI exists, and no capture has ever run against a live socket"
several slices after both had landed, and the zero-size row said "no WebSocket
transport exists" for the same reason. Those sentences are corrected here; the
identical sentences *inside* the dated slice sections further down are left
alone, because those are records of what was true at that slice, not claims
about now. This is the third time in this milestone that a present-tense claim
in this file outlived the thing it described, which is why the audit
re-derived the state from `main` instead of reading it here.

As it actually stands: the CLI exists, three live captures have run, and the
remaining honest qualifications are narrower and specific — a real network
reconnect has never happened, and no capture has ever been interrupted by a
process dying. Those two are marked "never exercised live" below and are not
claimed as closed.

| Criterion | Status | Evidence |
|---|---|---|
| Duplicate source event does not create a second accepted observation | **End-to-end evidence; no capture loop runs it against live traffic yet** | `_observation_identity` collides an identical redelivery onto one `observation_id`, on the real recorded CLOB payload as well as a constructed one (`docs/research/m2-clob-rest-book.md`, "Consequence for `ObservationEnvelopeV1`"). `SQLiteEventStore.append_observation` enforces it at the store: a redelivery inserts zero second `observation` rows and exactly one `delivery` row with `disposition="duplicate"`, both writes inside one `BEGIN IMMEDIATE` transaction (`tests/test_event_store.py`, `tests/test_event_store_adversarial.py`, including real multi-connection race tests). The CLOB REST adapter slice closes the remaining gap end to end: a real recorded response fed through `ClobClient` → `normalize_clob_book` → `SQLiteEventStore` produces one observation row and two delivery rows (`accepted=1, duplicate=1`) for a redelivery. **Now closed on live traffic**: a 60-second live capture on 2026-08-15 received a genuine duplicate from the source and collapsed it — 18 observation rows, 18 `accepted_new` deliveries and 1 `duplicate` delivery, with no second observation minted |
| Zero-size level update is represented as removal | **Closed structurally and end-to-end into the store; not observed on a live capture** | Represented structurally, not by convention: `PriceLevelChangeKind.REMOVE` holds **if and only if** `size == 0`, validated on every construction path including replay and `from_record`, so a record cannot claim one and carry the other (`src/argos/domain/pricechange.py`). Both genuine zero-size entries in the recorded live capture now travel the full path — `normalize_clob_price_change` -> `build_observation_envelope` -> `SQLiteEventStore` — and land as `REMOVE` in a stored payload (`0.17` bid side, `0.83` ask side on the sibling), verified independently of the slice's own tests. `OrderBookSnapshotV1` separately implements the REST-snapshot side, where the same value is deliberately a counted anomaly rather than a removal. **Corrected 2026-08-17**: the earlier "no WebSocket transport exists" qualification was stale — the transport, the capture loop and the CLI all shipped afterwards, and three live captures have run. The accurate residual is narrower: the two zero-size entries that carry this criterion come from the *recorded* capture, and no zero-size entry has been observed in a live ARGOS capture, because none of the three live runs happened to contain one. The `kind is REMOVE` ⟺ `size == 0` validator makes the representation structural either way |
| Reconnect does not reset ingest sequence or silently lose manifest state | **Closed structurally; never exercised against a live socket** | `run_capture` owns the counter for the whole run, so a reconnect inside the transport is transparent to it — the transport keeps yielding from one async generator. Restarting the loop cannot silently restart the sequence either: re-opening the same `capture_run_id` is refused with `StorageError` by the store's partial unique index, so the property is structural rather than a check that could be forgotten. Verified independently of the slice's own tests, on the real recorded capture: sequences span both the delivery and rejection ledgers with no gaps and no reuse (exactly 1..38), and are identical across two runs. Manifest state cannot be lost silently: `capture_run` is append-only and a run with no closing row is a queryable signal. **Not yet closed**: no capture has run against a live socket, and a real network reconnect has never been exercised — the research note still records reconnect behaviour as UNVERIFIED |
| Invalid messages enter a rejection ledger with reason and raw hash | **End-to-end evidence; no capture loop runs it against live traffic yet** | `RejectedObservationV1` carries `reason: RejectionReason`, `detail`, and `raw_payload_sha256`; `build_rejected_observation` derives a deterministic `rejection_id` so redelivery of the same invalid bytes for the same reason collapses rather than growing the ledger unbounded. `SQLiteEventStore.append_rejection`/`iter_rejections` persist it, keyed `(capture_run_id, ingest_sequence)` rather than on `rejection_id` alone, so two genuinely different malformed entries in one frame that happen to share one `rejection_id` (ADR-0011 section 7) both survive instead of one silently overwriting the other. The CLOB REST adapter slice closes the remaining gap end to end: a malformed real-shaped body fed through `normalize_clob_book` produces a `RejectedObservationV1` written to the ledger (`tests/test_clob_book_ingestion.py`). The WebSocket source now has the same evidence on its own path: `normalize_clob_price_change` turns every `ValueError` the domain raises — plus its own `entry_hash` length and display-control checks, deliberately inside its own `try` — into a `RejectedObservationV1` rather than an escaping exception, so a malformed real-shaped frame reaches the ledger with a reason and the raw hash (`tests/test_clob_price_change_ingestion.py`). **Not yet closed**: no capture loop runs either adapter continuously against live traffic |
| Book snapshot plus deltas reconstruct a tested projection | **Closed on real recorded traffic** | `argos.projections.book` reconstructs the book from a snapshot plus `price_change` deltas. Verified on the recorded live capture, which contains 4 full `book` snapshots for the subscribed token and 34 delta frames: all three snapshot→deltas→snapshot transitions reconstruct **exactly**, level for level (`book@msg0 +13 → book@msg16`, `+7 → book@msg26`, `+8 → book@msg37`), and a single continuous run seeded once at msg0 and fed all 28 deltas lands exactly on msg37 with zero anomalies — re-verified independently of the slice's own tests. The zero-size removal convention is exercised through the projection, not merely at the payload. **Known limitation, not a gap in the test**: ARGOS cannot compute the source's own `hash` (algorithm unpublished), so a missed delta is undetectable from the delta stream alone; divergence surfaces only when a full snapshot arrives and disagrees, which is what these three transitions measure |
| No authenticated/user channel or trading code exists | Holds | Unchanged from M0-M1; the CLOB REST adapter is read-only by construction — it sends no credentials and exposes only the public `GET /book` endpoint (`src/argos/sources/clob.py` module docstring, ADR-0007) |
| An interrupted capture closes or marks its manifest incomplete | **Both halves now exist; never exercised against a live socket** | Store half (unchanged): `capture_run` is append-only, closing inserts a second row, and "not yet closed" is a derived read via `iter_open_capture_runs`, with partial unique indexes making double-open/double-close impossible (ADR-0011 section 5). Loop half (new): `run_capture` opens the run before consuming a frame and always closes it — `COMPLETED` on clean exhaustion, `FAILED` recorded and then re-raised on an exception. A process killed outright runs neither branch and writes no closing row, which `iter_open_capture_runs` reports as interrupted — that absence is the intended signal, not a gap, since a dying process cannot be trusted to describe its own death. **Not yet closed**: no capture loop has run against live traffic, so no real interruption has ever been observed |

## Known limitations from the M2 observation-identity slice

Recorded here rather than discovered late by the store or adapter slices that
build on this one. Full reasoning in `docs/adr/0010-observation-identity.md`
and `docs/BACKLOG.md`.

- **Closed by `OrderBookSnapshotV1` (`1901b4e`).** The identity slice noted
  that no payload model yet normalizes `Decimal` scale, so `Decimal("0.430")`
  and `Decimal("0.43")` minted different `observation_id`s — reproduced
  directly
  (`tests/test_observation_envelope_adversarial.py::test_decimal_trailing_zero_precision_changes_identity`)
  and confirmed live: the CLOB endpoint really does report the same price at
  two precisions across `/book` (`"0.430"`) and `/last-trade-price`
  (`"0.43"`). `OrderBookSnapshotV1._normalize_decimal` now closes this on
  every price/size/tick-size/last-trade-price field it carries (see "M2
  slice: typed order-book snapshot payload" above). The general form of the
  constraint stands: **every future payload model must do the same**, this
  only closes it for the first one.
- `ObservationEnvelopeV1` has no `supersedes_observation_id`. Identity depends
  on the normalized payload and deliberately excludes `parser_version`, so
  reprocessing the same raw bytes under a corrected parser mints a new,
  unlinked identity. ADR-0004 requires superseding records as the correction
  mechanism; this field does not exist yet.
- **Closed by the ADR-0011 event-store slice.** The store's delivery-record
  shape was undecided at this slice's close: identity excludes
  `capture_run_id` and `ingest_sequence` by design (both would make a
  duplicate unable to collide), which meant a collapsed duplicate had nowhere
  to record its own arrival, and `RejectedObservationV1` could not point at
  an accepted twin it duplicates. ADR-0011 decided it: one `delivery` row per
  arrival, keyed `(capture_run_id, ingest_sequence)`, carrying a
  `disposition` of `accepted_new` or `duplicate` (see "M2 slice: idempotent
  SQLite event store" above).
- No `payload_schema_version` -> model registry exists. `read_payload` takes an
  explicit `model` argument today; M3 dispatch across multiple payload types
  will need something less ad hoc.
- `read_payload` hard-matches exactly one `payload_schema_version` rather than
  accepting a set via `ensure_supported_version`, so no reader can yet accept
  more than one payload version.
- The research doc's UNVERIFIED list (rate limits, the `/books` batch
  endpoint, response headers, zero-size REST levels, halted-market behaviour)
  is unresolved. None of it should be assumed by the capture loop.
- The WebSocket research note found `(timestamp, hash)` identifies a
  post-state, not a wire message, and can span more than one frame. This makes
  ADR-0010's already-documented residual identity collision reachable in
  practice for this source, not merely theoretical — see "M2 research: public
  market WebSocket channel" above and `docs/BACKLOG.md`.
- The security review closed in `1fb057c` left several findings deliberately
  unfixed with their own reasoning — the variation-selector/ZWJ covert
  channel, `SourceProvenanceV1.http_status`'s missing transport discriminator,
  unenforced `schema_version` uniqueness, and `SourceProvenanceV1.endpoint`'s
  redaction gap (now with a larger blast radius at the store — see below).
  **Partially closed:** `observation_id`/`rejection_id` verification is no
  longer trust-only. `SQLiteEventStore` calls `recompute_observation_id`/
  `recompute_rejection_id` on both write and read (ADR-0011 section 8). Full
  list in "M2 security review" above and `docs/BACKLOG.md`.

Quality gate: PASS — ruff, ruff format, mypy strict on 32 source files,
**779 tests** (up from 710 at the observation-identity slice).

## Known limitations from the M2 event-store slice

Recorded here rather than discovered late by the adapter or capture-loop
slices that build on this one. Full reasoning in
`docs/adr/0011-sqlite-event-store-and-delivery-record.md` and
`docs/BACKLOG.md`.

- Duplicate arrivals silently drop `quality_flags`: the observation row is
  written only on first arrival and `delivery` has no quality column, so two
  arrivals of one observation that legitimately differ in `quality_flags`
  (the flag derives from `received_time`, which identity excludes) lose the
  later one — a counted defect discarded, against
  `.claude/rules/data-integrity.md`.
- `open_sqlite_event_store`'s path handling is strictly weaker than the raw
  archive beside it: no `resolve()`, no containment check, no `O_NOFOLLOW`
  equivalent, no mode. Measured: a symlinked db path was followed and the
  `-wal`/`-shm` side files were created beside the symlink target, all at
  0644, while the archive writes 0600 and checks `is_relative_to(root)`.
  Requires local write access.
- The store imposes no size bound of its own; `MAX_PAYLOAD_CANONICAL_BYTES`
  lives only in `build_observation_envelope`, and `from_record` accepts
  anything. Measured: a 32 MiB record wrote in 0.441 s at 210 MiB peak RSS
  and read back at 334 MiB. No exposure today because the builder is the
  only production write path. `source_frame_offset` is also unvalidated
  (`-1` accepted; `2**63` raises a bare `OverflowError`).
- The database schema has no identity and no version: `PRAGMA user_version`
  is never set or read, and `CREATE TABLE IF NOT EXISTS` opens a
  differently-shaped pre-existing file silently, failing at the first write
  mid-capture with a generic message. The engineering rule "every public
  schema and persistent record is versioned" is satisfied for records but
  not for the schema itself.
- `write_raw_payload` does `mkdir(parents=True)` but `_fsync_directory`
  syncs only `path.parent`, so for the first payload of a new source the
  file is durable inside a directory whose own entry may not be. Also
  `mkdir` mode is 0755 around files written 0600.
- `SourceProvenanceV1.endpoint` redaction is an existing gap; a credential in
  a query string is now greppable in the database file, once per observation
  (~91k rows/day/token extrapolated) rather than once per run manifest — a
  larger blast radius than when the gap was first filed.
- Payload text is opaque to the store by design (ADR-0011 section 3), so it
  is unneutralized: `get_observation()` faithfully returns live ESC, BEL,
  RLO, ZWSP, and Unicode tag characters out of a payload field. No exposure
  today because `OrderBookSnapshotV1` is all `Decimal`; the first payload
  model with a free-text field reopens the M1 OSC-52 rendering-forgery class
  at the store's own read boundary, against durably stored text — filed as
  the most important open item from this slice (`docs/BACKLOG.md`, L7).
- The denormalized filter columns on `observation`/`rejection` are never
  cross-checked against `record` on read. Not exploitable today since no
  method queries by them; becomes real the moment a query-by-`market_id`
  method ships.
- Nothing detects a `delivery`/`observation` row naming a capture run that
  was never opened. The Python referential check binds only callers going
  through `SQLiteEventStore` and is never re-checked afterward.

## Known limitations from the M2 CLOB REST adapter slice

Recorded here rather than discovered late by the WebSocket adapter or
capture-loop slices that build on this one. Full list, with severity and
trigger, in `docs/BACKLOG.md`.

- A 3xx response with a JSON body is accepted as a successful observation:
  `clob.py` treats every status below 400 as success, and a measured 302
  carrying `{"market":"pwn"}` was accepted with `provenance.http_status=302`.
  The redirect is never followed and the host never changes
  (`follow_redirects=False`), so the body can only come from the host already
  contacted, and provenance records the 302 honestly — auditable after the
  fact, and not a regression: `gamma.py` behaves identically.
- Cross-adapter backoff correlation: `ClobClient` and `GammaClient` at the
  shared default `source_jitter_seed=0` produce byte-identical backoff
  sequences, as do two `ClobClient` instances. The thundering-herd item
  already in the backlog from the pre-M2 security review is now confirmed
  cross-adapter as well as cross-market. Reproducibility remains the right
  trade for a public unauthenticated endpoint; the scope of the existing item
  widened rather than a new risk appearing.
- A substituted response (the source's `asset_id` disagreeing with the
  requested token) is refused, but labelled `MALFORMED_PAYLOAD` — identical
  to a JSON parse failure — so an operator cannot count "the source returned
  a different token's book" as a distinct outcome. Separately,
  `condition_id` on an accepted envelope comes only from the response with no
  cross-check against the M1 Gamma metadata, so ARGOS durably stores a
  token-to-condition binding the source alone asserts.
- `SourceProvenanceV1.endpoint`'s already-backlogged redaction gap now
  materializes once per CLOB observation as well as once per Gamma page. This
  adapter introduces no *new* leak channel — the only query parameter is a
  `[0-9]{1,120}`-validated token id — but the gap is more urgent than before
  simply because it now recurs on a second, higher-volume source.
- Blind spots left by adversarial testing, not yet closed by a test: `market`/
  `asset_id` explicitly `null` in the response is hand-traced through the code
  but has no regression test; a non-string `hash` type is read-verified only,
  not exercised by a test. The sharpest one — **every future payload model
  must reuse the shared decimal normalization rather than reimplement it** —
  is **closed as a structural fact** by the `price_change.v1` slice: the
  helpers are now public and shared (`parse_wire_decimal` /
  `normalize_decimal`, `src/argos/domain/orderbook.py`) and `PriceChangeV1`
  calls them rather than duplicating the logic, so the ADR-0010 decimal class
  did not recur a third time. The reuse turned out to matter in the opposite
  direction too: making one helper serve two schemas is what put enough
  adversarial pressure on it to expose the taxonomy escape and the ambient
  decimal-context dependence described in the `price_change.v1` slice above —
  both of which were already reachable on this REST adapter and neither of
  which two prior security reviews had found.

## Pre-M2 slice: pacing separated from timekeeping (ADR-0009)

Closed the one obligation STATUS previously recorded as gating M2. See
`docs/adr/0009-pacing-separate-from-timekeeping.md` for the full reasoning.
No M2 source adapter was written in this slice; this was the gate preceding
one.

- `Clock` now exposes `now()` only; `@runtime_checkable` is dropped;
  `ReplayClock.sleep` and `LiveClock.sleep` are removed. A new
  `argos.clock.pacing` module holds a `Pacer` protocol (`wait`,
  `move_on_after`) and `RealPacer`. Adapters take a `Pacer` alongside a
  `Clock`.
- Retry jitter is now drawn from an adapter-owned `random.Random`, seeded by
  the new `source_jitter_seed` setting, via a `wait_base` subclass — tenacity's
  `wait_exponential_jitter` has no RNG injection hook. This closes an
  already-committed M1 defect: order-dependent hidden global state, where one
  market's backoff depended on how many sibling markets had already retried in
  the same capture loop, which is the CLAUDE.md-prohibited "hidden global
  state that changes output based on asset processing order".
- **Correction to a claim STATUS previously made in the M1 review-findings
  table.** `GammaClient._get` bounded requests with `anyio.move_on_after`,
  which reads the event loop's monotonic clock, not the injected `Clock`.
  Reproduced directly: five virtual hours of `ReplayClock.sleep` inside a
  0.05 s deadline scope left `cancelled_caught = False`. So the M1 security fix
  "one overall deadline bounds the request, retries included" was only ever
  exercised on the zero-retry path — the existing test pinned
  `http_max_attempts=1`, and its own docstring conceded a virtual clock could
  not demonstrate the property on the retry path. The deadline now runs on the
  injected `Pacer` and is genuinely exercised across backoff, covered by
  `tests/test_gamma_client.py::test_the_overall_deadline_can_fire_during_backoff`.
- Four new boundary tests in `tests/test_boundaries.py`: `Clock` exposes only
  `now`; `ReplayClock` has no pacing methods; no domain/projections/baselines/
  evaluation module imports a pacing name; no `sources`/`ingestion` module
  calls `anyio.sleep`/`anyio.move_on_after` directly (mutation-tested: an
  injected `anyio.sleep()` in `gamma.py` makes it fail, so the check is not
  vacuous).
- `RunManifest` is now `run_manifest.v2`: `mode` is a `RunMode` StrEnum
  (discover/audit/capture/replay/inspect); `working_tree`
  (`WorkingTreeStatus` clean/dirty/unknown) joins `code_revision`;
  `schema_versions` and `input_provenance` (reusing `SourceProvenanceV1`) close
  the rest of core invariant 13. A model validator forbids claiming
  clean/dirty without a proven revision. No `v1` manifest was ever persisted,
  so no migration was written — this is a breaking schema change, not a
  compatible extension.
- Quality gate: PASS — ruff, ruff format, mypy strict on 29 source files,
  594 tests (up from 536).

## M1 closure

| Review | Verdict |
|---|---|
| Architecture | BLOCK → **APPROVE_WITH_FOLLOWUPS** after both blockers were fixed and re-verified against the reviewer's own reproductions |
| Security | **PASS** — both blocking findings re-measured with the original instruments: the gzip bomb peaks at 93 MiB instead of 1.2 GiB, the endless drip stops on the deadline (see correction above: fully exercised only after the pre-M2 pacing slice) |
| Testing | Coverage report acted on; suite 75 → 536 tests across M0 and M1, no xfail |

## M1 exit criteria

| Criterion | Evidence |
|---|---|
| Repeated normalization of the same raw payload is deterministic | `tests/test_gamma_normalizer.py::test_normalization_is_deterministic` |
| Malformed outcome/token mapping cannot enter active selection | Mismatched lengths, duplicate tokens, a condition id in a token slot, and a non-decimal token id all raise `quarantined_mapping` before a `MarketDefinitionV1` exists |
| Original question, description, resolution source, and dates are retained | `MarketDefinitionV1` and `CompiledMarketContractV1` carry them verbatim, linked by `raw_payload_sha256` |
| Compiler never marks a contract human-reviewed automatically | A field validator rejects `human_reviewed` outright, so no code path can set it |
| Network adapter tests use fixtures; unit tests need no internet | `tests/test_gamma_client.py` runs entirely on respx; fixtures carry provenance sidecars and `tests/test_fixtures.py` fails if one drifts from its hash |
| At least one ambiguity test and one token-mapping failure test | `tests/test_compiler.py` (7 ambiguity cases) and `tests/test_gamma_normalizer.py` (8 mapping-failure cases) |

Verified against the live public API on 2026-08-07: `argos markets discover` and
`argos markets audit` both work end to end. Findings from the real payloads are
recorded in `docs/14_POLYMARKET_NOTES.md`.

## M1 review findings

Fixed in this milestone. Two reviews returned BLOCK; both blockers and the
high-severity findings are closed and covered by regression tests.

| Source | Finding | Fix |
|---|---|---|
| Architecture (blocking) | `contract_id` derived from the *page* hash, so one market had two identities depending on which endpoint returned it, and an unrelated sibling's volume minted a new one | Derive from the market's own rule-bearing content; verified across both recorded fixtures |
| Architecture (blocking) | `CompiledMarketContractV1` silently dropped `subject_entities` and `qualifying_event` from its specified contract | Present and empty, like the conditions, with the compatibility decision recorded |
| Security (high) | The 32 MiB cap ran after `.content` buffered and decompressed the body — a 611 KB gzip response reached 1.2 GiB of RSS and *then* reported refusal | Stream with an incremental check and a `content-length` pre-check |
| Security (high) | httpx's read timeout is per chunk, so a slow-drip response hung indefinitely and the retry budget never applied | One overall deadline bounds the request, retries included — **at M1 close this was only proven on the zero-retry path** (`anyio.move_on_after` reads the event loop's monotonic clock, not the injected `Clock`, and the regression test pinned `http_max_attempts=1`); genuinely closed across backoff in the pre-M2 pacing slice via `Pacer.move_on_after`, see above |
| Security + testing (high) | A newline in a market question forged whole sections of the audit, including `review status: human_reviewed`; OSC 52 in a description wrote to the reviewer's clipboard | Sanitize control characters, always block-quote, label source text as untrusted; recorded in `docs/05_RESEARCH_PROTOCOL.md` |
| Security (medium) | `provenance.source` built an archive path unchecked, so `"../outside"` wrote outside the archive | Constrained on the contract and re-checked at the write |
| Security (medium) | `get_market` interpolated an unvalidated id into the URL path, and httpx normalizes dot segments | Validated against an id pattern before any request |
| Testing (medium) | `normalize_markets` iterated a string character by character and raised a bare `TypeError` on a scalar body | A non-list body is refused whole with a reason |
| Testing (medium) | `read_raw_payload` leaked `FileNotFoundError`/`JSONDecodeError`, and a lost sidecar could never be repaired | Both inside the taxonomy; re-archiving restores a missing sidecar; writes are atomic, sidecar first |
| Testing (low-med) | `SourceHealth.retries` counted only on the exhausted-budget path, so a recovered capture reported `retries=0` | Counted per retry |
| Testing (low) | Duplicate token detection compared strings, so `"007"` and `"7"` passed as distinct | Compared by integer value |
| Both | Dead code presenting as a decision: an unreachable `MACHINE_CHECKED` branch and an unreachable condition-id/token-id check | Removed, with the real guarantee documented where it is actually enforced |

## Known gaps at M1

- Discovery fetches a single page; pagination and a documented stopping rule are
  carried into M2.
- Normalized and quarantined records are reported but not persisted — only the
  raw payload is archived. The durable store is an M2 deliverable.
- The raw archive in `argos.store` is deliberately minimal and makes no
  durability or replay claim.
- A discovery run emits no run manifest yet, so a sample is not yet linked to the
  configuration that produced it.

## Known limitations from the pre-M2 pacing slice

- `RunManifest.input_provenance` is an unbounded tuple of `SourceProvenanceV1`.
  That is acceptable at M1 discovery scale (one record per fetched page/market)
  but an M2 capture manifest must **not** embed one provenance record per
  ingested event — event volumes make that unbounded in a way discovery's
  never was. The M2 capture-manifest design must reference the event store
  instead of enumerating provenance inline. Filed as a constraint, not yet a
  fix, so it is not discovered late.
- There is no `VirtualPacer`. Accelerated and stepwise replay pacing is M3
  scheduler work (`argos.replay`, per ADR-0009's consequences section) and was
  deliberately not built in this slice, to avoid scope drift into a milestone
  that has not started.

## Pre-M2 security review

Verdict: **PASS, no blockers.** Both M1 high-severity properties were
re-measured rather than re-read, and both survive the pacer rewrite: a 1.2 GiB
gzip bomb is refused at 45 MB accumulated with peak RSS 116 MiB, and the
overall deadline bounded a 3-attempt call at 20.90 s against a 20.9 s budget.
The seeded jitter cannot exceed `MAX_BACKOFF_SECONDS` at any legal
configuration, so it cannot outrun the deadline budget.

Two MEDIUM findings landed on the `working_tree` field this slice introduced,
and both are **fixed here** rather than deferred, because a manifest that
positively asserts the wrong provenance is worse than one that admits it does
not know:

| Finding | Reproduced | Fix |
|---|---|---|
| `git status --porcelain` reported **clean** for a genuinely modified tree — via `assume-unchanged`/`skip-worktree` index bits, and via `status.showUntrackedFiles=no` injected through `GIT_CONFIG_*` (also a real developer performance setting, so the accidental path is the likely one) | Yes — file on disk read `TAMPERED` while status printed nothing | Index bits are treated as unknowable (`UNKNOWN`, never `CLEAN`); untracked mode pinned on the command line; the git environment is scrubbed |
| `GIT_DIR` defeated the M0 toplevel check: git reports cwd as `--show-toplevel` while `HEAD` comes from a foreign repository | Yes — returned a throwaway repository's HEAD instead of this one's | Same environment scrub, applied to both git calls |

Six regression tests cover these, including one asserting a genuinely clean
tree is still reported `CLEAN` — the guard must not be degenerate. Provenance
probes now run with `--no-optional-locks`, so collecting provenance no longer
writes to `.git/index`.

Three LOW findings are recorded in `docs/BACKLOG.md` rather than fixed, each
with no current exposure: the shared jitter seed becoming a thundering herd
once a capture loop exists, `SourceProvenanceV1.endpoint` persisting a full
URL with no redaction contract, and a validator message that could embed a
path. Predictable retry timing against a public unauthenticated endpoint was
assessed and is **not** treated as a risk; reproducibility is the right trade.

## M0 checklist

- [x] Run `/bootstrap` and create initial task plan.
- [x] Generate `uv.lock` from a clean environment.
- [x] Create package boundaries from `02_ARCHITECTURE.md`.
- [x] Implement immutable settings and execution-disabled validation.
- [x] Implement Clock protocol, LiveClock, and ReplayClock contracts.
- [x] Establish structured logging and error taxonomy.
- [x] Confirm CI and local quality commands pass.
- [x] Add ADR index.
- [x] Architecture review complete.
- [x] Security review complete.
- [x] Milestone summary committed.

## Evidence

| Exit criterion | Evidence |
|---|---|
| `uv sync --all-groups` succeeds from a clean checkout | `uv.lock` committed; sync run on Python 3.13 with the 3.12 floor from `pyproject.toml` |
| `ruff check`, `ruff format --check`, `mypy`, `pytest` pass | `uv run python scripts/claude/quality_gate.py` → PASS (250 tests) |
| Domain package imports no HTTP/database/UI packages | `tests/test_boundaries.py` — AST scan for infrastructure imports, outer-layer imports, environment reads, and wall-clock calls |
| `LiveClock` and `ReplayClock` contracts have tests | `tests/test_clock.py` and `tests/test_properties.py` — UTC anchoring across the tz database, DST folds, regression refusal, non-finite durations, reproducibility |
| Configuration rejects `ARGOS_EXECUTION_ENABLED=true` | `tests/test_config.py` — every truthy spelling (`true/True/TRUE/1/yes/on/t/y`) rejected with `argos.execution_prohibited`; falsy spellings load; `maybe` is a config error, not a silent false |
| Architecture and security agents report no blockers | Architecture: **APPROVE_WITH_FOLLOWUPS** after two blocking findings were fixed and re-verified. Security: **PASS**, no blockers. Testing: coverage report acted on; suite 75 → 250 tests |

Additional guardrails added beyond the checklist:

- `tests/test_boundaries.py::test_no_execution_surface_in_source` scans declared
  names across `src/argos` for wallet/order/credential identifiers;
- unrecognized `ARGOS_*` environment variables fail the load instead of being
  silently ignored;
- `RunManifest` (`run_manifest.v1`) records config fingerprint, code revision, and
  settings snapshot, and is stamped from an injected clock so replay runs
  reproduce it exactly;
- `tests/conftest.py` strips ambient `ARGOS_*` variables, so the execution-prohibition
  test cannot pass for the wrong reason on a developer machine.

## M0 review findings

Fixed in this milestone:

| Source | Finding | Fix |
|---|---|---|
| Architecture (blocking) | `__init_subclass__` used `getattr`, so a subclass inherited its parent's `schema_version` and would write mislabelled records | Check `cls.__dict__` |
| Architecture (blocking) | `frozen=True` left `settings_snapshot` mutable, and the mutation reached `to_record()` | `freeze`/`thaw` with a deep-copyable, picklable, hashable `FrozenDict` |
| Testing | `RunManifest.created_at` accepted naive timestamps and kept non-UTC offsets, so equal instants serialized differently | `ensure_utc` field validator |
| Testing | `nan` slipped past the `seconds < 0` guard and would hang `anyio.sleep` | `InvalidDurationError(ClockError, ValueError)` |
| Security (medium) | git discovers repositories upward, so a wheel install could stamp manifests with a foreign repository's HEAD | Verify `--show-toplevel` before trusting `HEAD` |
| Security (low) | pydantic embeds the offending value in its message, so a credential pasted into the wrong variable reached stderr and CI logs | Report field name and error type only |
| Security (low) | `http_timeout_seconds` and `http_max_attempts` had floors but no ceilings | `le=120` and `le=10` |

Deferred debt is filed in `docs/BACKLOG.md` under "Carried from the M0 closure
reviews", each item tagged with the milestone it must close by.

## Active blockers

None.

## Known limitations at M0

- Storage engine is undecided; the SQLite/WAL vs append-log comparison required by
  `docs/12_TECH_STACK.md` needs real M2 capture volumes and will land as an ADR.
  (Historical, M0-era limitation. Resolved 2026-08-11 by ADR-0011 — SQLite/WAL,
  on atomicity, not volume — see "M2 slice: idempotent SQLite event store"
  above.)
- The `sources`, `ingestion`, `store`, `projections`, `compiler`, `replay`,
  `baselines`, `resolution`, and `evaluation` packages are documented boundaries
  only; they contain no implementation yet.
- **CI ran for the first time on 2026-08-15 and passed**, on pull request #2
  (`m1-market-discovery` -> `main`, 34 commits): ruff, ruff format, mypy strict
  and **1,316 tests in 27.60 s**, green at the first attempt. Two things this
  settles that no local run could. It ran on **Python 3.12.13** — the declared
  floor in `pyproject.toml`, never previously executed anywhere, while every
  local gate had run on 3.13. And it ran from a clean checkout with
  `uv sync --all-groups`, which is M0's own first exit criterion and had until
  now been verified only on this machine. Every gate recorded in this file
  before that date remains a single-machine, single-version result; from here
  they are reproduced independently.
- Container immutability on `VersionedModel` is opt-in per field, not structural.
  `RunManifest` opts in; a future subclass with a bare `dict` field would not.
- Branch coverage is unmeasured — `pytest-cov` is not installed and the thresholds
  in `docs/13_TEST_STRATEGY.md` are therefore unverified.

## Next owner action

**The M4 owner gate is reached. Implementation has stopped.**
`docs/HANDOFF_M4.md` is the owner package; `docs/OWNER_REVIEW_GATE.md` carries
the checklist with every box's evidence, and the one box left deliberately
unticked is the independent architecture/security/test review — the only thing
this gate asks for that was not obtained.

The seven questions in `docs/OWNER_REVIEW_GATE.md` need answers before M5, and
`docs/HANDOFF_M4.md` section 9 adds six more with a recommended default for
each. Nothing in M5-M8 may start on the strength of M4 passing.
