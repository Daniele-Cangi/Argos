# Codex owner takeover: independent M4 review

- Review date: 2026-08-19
- Canonical starting revision: `743c8151ed52247529529b0964d14bba9b8eb2aa`
- Operating-model revision used for the review: `a8c418ac256cf521dbc1694e773b2bcb6d9c310e`
- Review phase: independent findings frozen before consulting `HANDOFF_M4.md`,
  `OWNER_REVIEW_GATE.md`, `STATUS.md`, or `BACKLOG.md`

This document is an audit trail, not a milestone-closure argument. Findings are
ranked from recorded input and executable behavior upward. The earlier handoff
labels are intentionally absent from this section so they could not frame the
questions tested.

## Operational meaning reconstructed for M4

M4 is the measurement boundary between a replayed information history and a
known market resolution. A trustworthy implementation must create identifiable
forecast records at explicit information cutoffs, link them to the exact
replayed observations and resolution record, apply a declared finality and
inclusion policy, preserve every exclusion, and produce metrics whose sample
unit is not confused with transport-message count.

The current implementation demonstrates working arithmetic, conservative
resolution parsing, deterministic final book reconstruction, and a complete
happy-path CLI on one real capture. It does not yet meet the operational meaning
above.

## Independent findings

### 1. Forecast emission is controlled by transport arrivals, not information changes

`evaluate_capture` emits every configured baseline after every delivery once
the target projection is seeded. It does so even when `dispatch()` reports a
duplicate or when the arrival concerns another token. Consequently retry,
redelivery, subscription breadth, snapshot cadence, and newly supported event
types can change metric weights without changing the target information.

An in-memory adversarial probe over the committed capture measured:

| Input | Accepted | Duplicate | Forecasts | Scored | Final target capture unchanged? |
|---|---:|---:|---:|---:|---|
| committed token-only capture | 38 | 0 | 76 | 75 | reference |
| same capture plus one duplicate final frame | 38 | 1 | 78 | 77 | yes; same final state hash |
| same frames captured for target and sibling tokens | 72 | 0 | 144 | 143 | target arrivals unchanged |

This is an evaluation-method defect and an architecture defect: M4 reimplements
a partial replay loop instead of consuming an auditable replay trajectory with
an explicit forecast-emission policy.

### 2. Non-final and post-settlement forecasts enter headline metrics

The evaluator requires a winning token but does not require
`resolution_status=final`. A valid `ResolutionV1` changed to `proposed` scored
all 75 non-abstaining records in the committed capture.

Forecasts after `resolved_at` are assigned the cohort label
`after_resolution`, then remain in calibration, Brier, and log-loss aggregates.
With a settlement timestamp before the capture, all 75 scored records remained
headline samples. The test named `test_every_lead_bucket_has_a_name` explicitly
codifies this inclusion.

This is an evaluation-policy defect. Detecting leakage but still averaging it
does not prevent leakage.

### 3. Persisted output cannot identify the inputs and records that produced it

The CLI persists only `EvaluationReportV1`; it discards the returned forecast
and per-forecast evaluation records. Baseline forecasts have no `forecast_id`,
capture/run identity, source observation id, information-set digest, market id,
or contract id. Evaluations have no forecast link. Their generated ids are
sequence-based and collide across evaluation runs.

The report records a capture-run label and final projection hash, but not the
resolution id/hash/status/normalizer, replay policy/counts, capture completion
status, ordered input digest, working-tree state, settings snapshot, forecast
schedule, inclusion/exclusion policy, or identities of scored records. A config
fingerprint without the corresponding configuration is not a reconstructable
configuration.

This is missing provenance plus a persistent-contract defect.

### 4. A final-state hash is being used as if it identified a trajectory

ADR-0012 correctly defines `state_hash.v1` as final projected book state and
deliberately excludes counts, order, timestamps, and anomaly history. M4 uses
that hash as the report's sole reconstruction link. Different trajectories can
therefore produce the same hash and different forecast series, while the report
claims the same source state.

This is not a defect in `state_hash.v1`; it is an M4 provenance-model defect.
Evaluation needs its own ordered trajectory/input identity rather than changing
the meaning of the replay final-state hash.

### 5. Headline sample count confuses forecast records with resolved targets

The committed real evaluation reports 38 midpoint samples and 37 persistence
samples, all scored against one binary outcome from one market. Those are
trajectory points, not 75 independent resolved examples. Limitations prose
acknowledges autocorrelation and sometimes constant scores, but aggregate field
names and calibration/ECE still present record counts as sample counts.

At minimum reports must distinguish forecast points, distinct information
states, resolved markets/outcomes, and the unit used to weight headline
metrics. With the available dataset, effective independent outcome count is
one and calibration is not empirically estimable.

This is an evaluation-method defect, not an arithmetic defect.

### 6. `last_trade_price` is real auxiliary market evidence and is discarded

The committed live `book` frame contains `last_trade_price="0.280"`; the REST
book fixture contains `"0.430"`. The current WS payload model drops the field,
the book projection has no auxiliary observation state, and evaluation always
calls `quote_from_book_state` without a last trade. The last-trade baseline thus
abstains over every current capture even when an initial recorded snapshot
contains the value. Standalone public `last_trade_price` WebSocket events are
rejected as unknown by capture and have no typed model.

Current official Polymarket documentation describes `last_trade_price` as a
trade event with token, price, size, side, fee, timestamp, and transaction hash;
book snapshots may also carry the most recent trade price. It is not an order-
book level and must not enter `book_state_digest.v1`. It belongs in a separate
auxiliary last-trade observation/projection whose provenance can be used by a
separate baseline.

This is a deferred source event that invalidates the claimed implemented
baseline set, plus a missing-provenance defect.

### 7. The displayed-price baseline was collapsed onto midpoint too broadly

The repository measured equality on 91 currently two-sided markets and inferred
that displayed price and midpoint are one source quantity. Current official
documentation states a conditional display rule: when bid/ask spread exceeds
$0.10 the UI displays last traded price instead of midpoint. The measured
sample did not falsify that branch and cannot justify removing the method.

Displayed-price proxy, midpoint, and last trade therefore require separate
method identities even when two happen to coincide for a narrow-spread quote.
This is a contract and evaluation-method defect.

### 8. Resolution identity and finality are underspecified

`normalize_clob_resolution(..., yes_token_id=...)` lets the caller redefine
which outcome is named YES while minting the same `resolution_id`; the same
source payload can therefore produce two records with one id and different
`winning_outcome`. Scoring currently uses the winning token and avoids inversion,
but the persistent resolution contract is not identity-safe.

Gamma deliberately emits `proposed`, `disputed`, and `unknown` statuses, yet M4
has no declared policy selecting which statuses may enter headline evaluation.
CLOB final records have no `resolved_at`, so temporal admissibility cannot be
proved from those records alone and must be a reasoned refusal or use a stronger
recorded lifecycle source.

This is a contract defect and a scientific-policy decision with a technically
defensible default: final-only headline scoring and explicit refusal when a
pre-resolution cutoff cannot be established.

### 9. Capture integrity and exclusions are not part of evaluation output

Evaluation does not refuse or prominently classify incomplete captures,
sequence gaps, received-time regressions, late inputs, rejections, dispatcher
anomalies, unhandled payloads, or corrupted/weakened persistence. Rejections are
silently skipped by the evaluation loop. `unresolved_count` and
`resolution_refusals` are always zero/empty on the only public entry point
because resolution refusal happens before a report exists.

This launders important missingness out of the artifact and violates the
project's rule that errors are data.

### 10. Existing tests prove implementation consistency, not M4 meaning

The tests strongly cover Decimal arithmetic, round trips, refusal parsing, and
the one real happy path. They do not attack duplicate delivery, irrelevant-token
traffic, non-final scoring, post-settlement inclusion, trajectory identity,
incomplete capture, or persisted child-record provenance. The CLI reproducibility
test compares selected aggregate fields from two runs over the same live objects;
it does not reconstruct an artifact from its recorded identities. The last-trade
test treats universal abstention as success despite the value present in raw
input. High branch coverage did not exercise the named scientific properties.

This is a test-strategy defect and evidence that the owner gate cannot inherit
the prior green verdict.

### 11. The clean-checkout baseline is not portable to the declared Windows environment

On Windows 10 build 26200, Python 3.13.5, uv 0.12.5, SQLite 3.49.1:

- Ruff and format checks pass; fixture provenance/hash checks pass (19 tests).
- mypy fails because `os.O_NOFOLLOW` is not available in Windows types.
- pytest collection fails because the lockfile does not include `tzdata` while
  `test_clock.py` loads `America/New_York`.
- coverage consequently cannot run locally.

The canonical Ubuntu 24.04 / Python 3.12.13 CI at starting revision passes
1,582 tests and the declared branch thresholds (96% aggregate). CI is green but
its single-platform matrix cannot establish clean-checkout portability.

## Independent verdict before handoff comparison

M4 is **not yet a trustworthy measurement layer**. It is a useful arithmetic
and parsing prototype over one real trajectory. Owner gate status is blocked by
forecast-schedule semantics, temporal/finality policy, persistent provenance,
sample-unit semantics, and missing last-trade/displayed-price support. The
smallest corrective direction is to version an auditable evaluation-run bundle
around an explicit information-state transition and inclusion policy, then make
all headline metrics derive only from its persisted included forecast records.

## Comparison after opening the historical owner package

Only after freezing the findings above, the review read `HANDOFF_M4.md`,
`OWNER_REVIEW_GATE.md`, `STATUS.md`, and `BACKLOG.md` in full. The historical
owner package had already named five of the defects:

| Historical label | Independent result | Owner decision |
|---|---|---|
| F2 — evaluator owns a replay-like loop | Reproduced; duplicate and sibling arrivals change weights without changing target information | Emission must follow a declared target information-state transition, not any transport arrival. Reuse the replay session/dispatcher and persist every exclusion. |
| F3 — non-final resolution scores | Reproduced with a `proposed` resolution | Headline scoring is final-only. This is an integrity constraint, not an optional owner preference. |
| F4 — post-resolution forecasts score | Reproduced; all 75 records remained in headline metrics | Post-resolution points are excluded with a reason. If the resolution cutoff is unknown, temporal admissibility is unproved and no headline result may be claimed. |
| F5 — artifact does not bind its evidence | Reproduced and broader than the handoff framing | Persist one digest-bound run bundle containing policy, replay/input identity, resolution identity, forecasts, evaluations, exclusions, settings, code/tree state, and child digests. Adding a few report fields is insufficient. |
| F8 — last trade stops at capture | Reproduced in raw frames and fixtures | Model last trade as an auxiliary observation/projection. Do not change the semantics of the order-book state hash. |

The independent pass additionally found the unresolved sample-unit problem,
the conditional displayed-price rule, resolution-identity instability, missing
capture-integrity/exclusion evidence, portability failures, and tests that do
not establish the claimed measurement semantics. Those are not duplicates of
F2/F3/F4/F5/F8 and remain owner-blocking.

`STATUS.md` and `BACKLOG.md` also contradict the evidence hierarchy: both call
M4 closed while the backlog says the real sample and `last_trade_price` model
are still missing. The implementation checkbox is therefore superseded by this
review. M4 returns to **blocked**, M5-M8 remain out of scope, and the owner gate
does not pass on the existing artifact.

## Revised direction and smallest next experiment

The immediate slice is not a larger market capture. More data fed through an
arrival-weighted, temporally ambiguous, non-reconstructable evaluator would
only produce a larger untrustworthy result.

The smallest justified experiment is an adversarially verifiable evaluation
run over the already committed capture with these success criteria:

1. a duplicate target delivery and irrelevant sibling traffic do not change
   the included target information states or headline result;
2. non-final resolutions are refused, post-resolution forecasts are excluded,
   and a missing resolution cutoff produces no headline claim;
3. a persisted bundle identifies and hashes its ordered input trajectory,
   resolution, policy, forecasts, evaluations, and exclusions, and reproduces
   byte-for-byte from the same committed evidence;
4. counts distinguish arrivals, distinct target information states, forecast
   points, scored forecast points, and resolved targets; calibration is not
   claimed from one resolved target;
5. last-trade evidence is represented separately from book state, and the
   conditional displayed-price method remains distinct from midpoint.

Only after this experiment passes is a bounded prospective sample worth
collecting. The next data experiment should then capture enough independent
resolved markets to estimate a market-weighted comparison, with resolution
time/finality evidence recorded during the run rather than reconstructed after
the fact.

## Corrective implementation result

The ADR-0013 slice implements and adversarially tests all five success criteria
above:

- duplicate and irrelevant sibling traffic leave the target information-state
  and forecast series unchanged while changing the full trajectory hash;
- non-final resolutions are refused, post-resolution points are explicit
  exclusions, and unknown cutoff or missing contract produces zero scores;
- `EvaluationRunBundleV1` round-trips with nested schema validation and
  rejects a changed evidence digest;
- reports separate arrival, information-state, forecast-point, scored-point and
  resolved-target counts and do not publish one-target calibration; and
- initial book last trade is auxiliary evidence, with distinct midpoint,
  last-trade and conditional displayed-price method identities.

The local Windows quality gate passes ruff, strict mypy and 1,593 tests. Branch
coverage passes every declared threshold; the corrected evaluator is 91.77%
against its 90% floor. One symlink test is skipped because the Windows account
lacks symlink privilege and remains active on Ubuntu CI.

Canonical PR #2 independently reproduces the result on GitHub Actions run
`32269750425`: Windows passes in 1m56s and Ubuntu, including coverage, passes
in 3m41s.

This implementation removes the known code-level blockers but does **not**
change the owner verdict. The current historical evidence still lacks an exact
resolution cutoff and persisted contract, so it establishes no headline
result. M4 remains blocked pending the prospective multi-target experiment
described above.
