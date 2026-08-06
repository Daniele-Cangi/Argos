# Definition of done

## A task is done only when

- the linked milestone criterion is named;
- code and contracts are implemented;
- focused tests cover success, failure, and boundary behavior;
- strict typing and lint pass for touched code;
- no network is required for unit tests;
- error/telemetry behavior is explicit;
- relevant docs and examples are updated;
- `STATUS.md` and `BACKLOG.md` reflect reality;
- a small local commit exists;
- limitations are stated.

## A persistent schema is done only when

- versioned;
- round-trip serialization tested;
- invalid boundary values tested;
- canonical representation defined;
- backward/forward compatibility decision recorded;
- raw-source linkage preserved.

## An adapter is done only when

- timeouts, retries, rate limits, reconnect, and cancellation are handled;
- response parsing is tested from fixtures;
- unknown fields do not destroy raw evidence;
- malformed input is quarantined, not silently coerced;
- source and local timestamps are preserved;
- health counters exist;
- no secrets are logged.

## Replay work is done only when

- domain code uses injected clock;
- deterministic ordering is documented;
- late-event behavior is tested;
- golden input produces a stable output hash;
- replay and live call the same handlers.

## Scientific work is done only when

- target and metric are pre-specified;
- baseline is explicit;
- leakage analysis is documented;
- sample count and missingness are reported;
- negative results are retained;
- an uncalibrated score is not renamed probability.

## Milestone closure

Before closing a milestone:

1. run `/quality-gate`;
2. request architecture-guardian review;
3. request security-reviewer review;
4. request test-engineer review;
5. update milestone evidence in `STATUS.md`;
6. create a milestone summary commit.
