# M4 resilient validation plan

This plan operationalizes ADR-0019. It authorizes no live experiment by itself.
Every live campaign still needs a frozen protocol, clean revision and explicit
owner start decision.

## Objectives

The next work separates three questions that V8 coupled:

1. **Can the evidence path run correctly?**
2. **Can it recover honestly from faults?**
3. **Are enough final outcomes available to evaluate predictive quality?**

The first two are answered by bounded technical scenarios. The third is an
asynchronous research cohort and is never inferred from technical passage.

## Technical scenario matrix

| ID | Scenario | Maximum duration | Required setup | Pass evidence |
|---|---|---:|---|---|
| T1 | Functional capture and replay | 10 min | two public tokens, separate database, raw archive | completed manifest, zero unexplained rejection, immutable database, identical replay digest |
| T2 | Stability | 2 h | representative active market | bounded memory/disk growth, complete accounting, no unexplained cadence gap |
| T3 | Endurance | 6 h | active market plus periodic checkpoints | checkpoint chain, bounded growth, restartable terminal state |
| T4 | Network loss | 60 min | controlled adapter failure | bounded retries, no fabricated observation, explicit gap, successful idempotent resume |
| T5 | Process termination | 60 min | forced stop between persisted polls | exclusive replacement owner, next ordinal correct, no duplicate request owner |
| T6 | Storage refusal | 60 min | controlled write failure | no partial receipt or published claim, stable error code, recoverable prior evidence |
| T7 | Terminal state matrix | 60 min | recorded fixtures for every lifecycle state | correct handling of unknown, proposed, disputed, final and administrative close |
| T8 | Cross-volume paths | 60 min | equivalent workspaces on C: and D: | identical semantic result and path-independent evidence identities |

Live network access is unnecessary for T4–T7. Faults use deterministic adapters
or recorded fixtures; tests never wait on real outages or settlements.

T3's six-hour bound covers the timeline from runner start through the durable
terminal checkpoint. The live capture request is therefore 21,540 seconds,
reserving 60 seconds for orderly source shutdown and terminal checkpoint
persistence. The evidence contract freezes both values and the assessor rejects
any measured start-to-terminal-checkpoint duration above 21,600 seconds.
Post-capture raw indexing and result serialization are outside that measured
window and cannot change the already persisted checkpoint chain.

## Scenario result contract

Each scenario must eventually emit a versioned result containing:

- campaign and scenario identifiers;
- code revision and configuration fingerprint;
- declared maximum duration and resource bounds;
- exact input/fixture identities;
- injected fault and its activation point, when applicable;
- start, last durable checkpoint and end times;
- artifact identities and receipts;
- expected and observed outcome;
- `PASSED`, `FAILED`, `INCOMPLETE` or `NOT_RUN`;
- limitations and follow-up action.

The aggregate is a partition of every declared scenario. It cannot omit or
silently retry a failed scenario.

## Asynchronous resolution cohort

The predictive cohort is separate from the technical matrix:

- freeze each target, contract and forecast before its event;
- fetch finality periodically through a single exclusive poll owner;
- allow safe resume from the last persisted ordinal;
- retain source time, retrieval time and arrival order separately;
- mark unresolved targets `PENDING_RESOLUTION` at review dates;
- append a versioned final-outcome receipt when finality becomes admissible;
- score only resolved targets while reporting both intended and resolved counts.

The cohort should contain at least 10–20 targets before drawing operational
comparisons and at least 30 resolved targets, with ADR-0014's category and
YES/NO minima, before evaluating calibration.

## Implementation order

1. Publish ADR-0019 and this plan.
2. Add a versioned scenario/result boundary with adversarial validator tests.
3. Replace the one-off PowerShell owner with a repository-tracked resumable
   monitor using exclusive ownership and deterministic checkpoints.
4. Add deterministic fault adapters and execute T1–T8 locally.
5. Freeze a new multi-target asynchronous protocol only after the technical
   campaign passes.

## V8 disposition

V8 is not rewritten. Its two captures and 554 observations per target remain
valid recorded evidence. Its last successful lifecycle observations are near
`2026-09-19T20:55Z`; the monitor failure at `21:00:23Z` leaves an unobserved
tail before the `22:00Z` deadline. V8 therefore supports technical findings but
not an end-to-end measurement pass or predictive score.
