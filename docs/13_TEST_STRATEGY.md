# Test strategy

## Layers

### Unit tests

Pure domain contracts, validation, quote calculations, scoring rules, clocks, ordering, and state transitions. No live network or filesystem outside temporary directories.

### Property tests

Use Hypothesis for:

- probability and Decimal boundary validation;
- order-book level application;
- deduplication/idempotency;
- serialization round trips;
- replay ordering and stable hashes;
- scoring-rule bounds.

### Adapter contract tests

Recorded and hand-built fixtures for Gamma, CLOB REST, and market WebSocket payloads. Include unknown fields, missing fields, invalid timestamps, zero-size updates, and reconnect sequences.

### Integration tests

Local event store + dispatcher + projections + replay. Network is mocked.

### Golden replay tests

A small ordered capture fixture must always produce the declared state hash and record counts. Changes require an explicit reason and golden update review.

### Live smoke tests

Manual/opt-in only. They use public endpoints, small subscriptions, short duration, strict timeouts, and never run in normal unit CI.

## Required failure cases

- malformed market outcome/token mapping;
- duplicate event;
- out-of-order/late event;
- connection loss and reconnect;
- queue saturation;
- invalid Decimal or probability;
- book level removal;
- missing resolution source;
- unresolved market presented to evaluator;
- capture interrupted before clean completion;
- replay with altered configuration.

## Quality thresholds

Do not optimize for a vanity global coverage number. Require:

- 90%+ branch coverage for domain contracts, replay ordering, and scoring rules;
- 80%+ branch coverage for source adapters using fixtures;
- every milestone exit criterion represented by at least one test or auditable artifact;
- no flaky timing sleeps in deterministic tests.

## Test data provenance

Each real fixture includes:

- source endpoint/channel;
- retrieval date;
- redaction note;
- schema/event type;
- raw hash;
- expected parser version.
