# Data contracts

These are logical contracts. Claude should implement them as versioned Pydantic models and evolve them through explicit migrations or new versions.

## Common conventions

- IDs are non-empty strings with source namespace where applicable.
- Timestamps are timezone-aware UTC and serialize in RFC 3339 with maximum available precision.
- Prices, probabilities, sizes, and money use `Decimal` at system boundaries.
- Probabilities validate to `[0, 1]`.
- Raw JSON payloads are retained as bytes or canonical JSON plus SHA-256.
- Enums serialize as stable lowercase strings.
- `schema_version` is required on persistent records.
- Unknown source fields remain in the raw payload; parsers do not discard evidence.

## `ObservationEnvelopeV1`

```text
schema_version
observation_id
source                 gamma | clob_rest | clob_market_ws | ...
source_event_type
market_id              optional when source message is lifecycle-wide
condition_id           optional
token_id               optional
event_time
received_time
ingest_sequence
source_sequence        optional
source_hash            optional
quality_flags          defects detected while accepting; empty means checked and clean
payload_schema_version names the VersionedModel describing payload
payload                 normalized payload in canonical JSON form
raw_payload_sha256
raw_payload_location   optional
parser_version
capture_run_id
```

Two amendments recorded against the original specification, both made when the
contract was implemented:

- `payload_type` is implemented as `payload_schema_version`. The source's own
  message-kind label lives in `source_event_type`; a second ARGOS-facing name for
  the same idea would only invite the two to disagree. The field that remains
  names the schema, which is what a reader actually needs to dispatch on.
- `payload` holds the payload's **canonical JSON form**, not live Python objects.
  `to_record()` serializes in JSON mode, so a `Decimal` held live would reload as
  a string: the replayed envelope would differ from the live one while carrying
  the same `observation_id`. Typed access (`read_payload`) restores the declared
  types. See ADR-0010.

Validation:

- `received_time` cannot be missing.
- `ingest_sequence` is positive and unique per capture run.
- `event_time > received_time` is allowed only within configured clock-skew tolerance and must emit a quality flag. Implemented as `ObservationQualityFlag.EVENT_TIME_AHEAD_OF_RECEIPT` against `DEFAULT_CLOCK_SKEW_TOLERANCE`; the observation is still accepted, because a source clock running fast is a fact about the source, but an event-time window must be able to see that the timestamp was never corroborated.
- Invalid source times do not silently fall back to current time; store a parse failure record.

## `MarketDefinitionV1`

```text
market_id
condition_id
slug
event_id               optional
question
description
resolution_source
start_time              optional
end_time                optional
active
closed
archived
restricted              optional
category                 optional
liquidity                optional Decimal
volume                   optional Decimal
open_interest            optional Decimal
outcomes[]               ordered
outcome_token_map        outcome -> token_id
neg_risk                 optional
tick_size                optional Decimal
source_updated_time      optional
raw_payload_sha256
normalized_at
normalizer_version
```

Validation:

- binary market scope requires exactly two outcomes and an unambiguous token map;
- condition ID and token IDs are not interchangeable;
- inconsistent outcome/token arrays create a quarantine record.

## `CompiledMarketContractV1`

```text
contract_id
market_id
source_market_hash
compiler_version
compiled_at
proposition
subject_entities[]
qualifying_event
yes_condition
no_condition
start_boundary          optional
end_boundary            optional
resolution_source
edge_cases[]
clarifications[]
ambiguity_flags[]
ambiguity_score         score, not probability
review_status           unreviewed | machine_checked | human_reviewed | rejected
review_notes[]
supersedes_contract_id  optional
```

The compiler must never invent missing rules. Missing or contradictory material increases ambiguity and blocks `human_reviewed` status.

## Market-data payloads

Implement typed payloads for at least:

- `OrderBookSnapshotV1`: bids, asks, tick size, min order size, neg-risk flag, book hash;
- `PriceLevelChangeV1`: token, side, price, size, best bid/ask when supplied;
- `BestBidAskV1`: best bid, best ask, spread;
- `TradeV1`: token, price, size, side, fee metadata when supplied;
- `TickSizeChangeV1`;
- `MarketCreatedV1`;
- `MarketResolvedV1`.

A zero size on a price-level update represents removal and must remain distinguishable from missing size.

## `CaptureManifestV1`

```text
capture_run_id
started_at
ended_at              optional
code_revision
config_sha256
schema_versions
source_urls
market_filter
subscribed_token_ids
selected_markets
host_metadata
clock_metadata
reconnect_count
accepted_count
duplicate_count
rejected_count
gap_warnings[]
completion_status
```

**Implemented as three existing records, not as a class with this name** —
decided in the M2 capture-loop slice, recorded here in the M2 closure so the
specification and the code stop disagreeing. `RunManifest` (`run_manifest.v5`)
carries `code_revision`, `config_sha256` (as `config_fingerprint`),
`schema_versions`, `source_urls` (inside `settings_snapshot`),
`subscribed_token_ids` (inside `run_parameters`), and `capture_run_id`; the
store's append-only `capture_run` rows carry `started_at`, `ended_at` and
`completion_status`; and `EventStore.counts_for_capture_run` *derives*
`accepted_count`/`duplicate_count`/`rejected_count` by query, so a summary can
never silently disagree with the rows it summarizes (ADR-0011 section 5).

Four fields are genuinely **not implemented**, and saying so is the point of
this note:

- `market_filter` and `selected_markets` — capture takes explicit token ids
  today; no selection policy runs inside a capture, so recording one would be
  recording a filter that did not filter anything;
- `host_metadata` and `clock_metadata` — nothing reads them, and a manifest
  field nobody reads is a field that quietly stops being true;
- `reconnect_count` and `gap_warnings[]` — the transport counts reconnects on
  `ClobWsHealth`, which is in-memory and not persisted, and this channel has no
  sequence number, so ARGOS cannot detect a gap at all
  (`docs/research/m2-clob-websocket.md`). A `gap_warnings` field would be
  permanently empty and would read as "no gaps", which is a stronger claim than
  "cannot tell".

The M1 architecture review's blocking finding was a specified contract silently
dropped, and ADR-0010's B3 was the same shape. This note exists so that this one
is dropped *loudly*.

## `ReplayManifestV1`

```text
replay_run_id
source_capture_run_id
code_revision
config_sha256
started_at
finished_at
replay_mode            original_arrival | accelerated_arrival
input_first_sequence
input_last_sequence
input_event_count
output_state_hash
output_record_counts
late_event_policy
result_status
```

Repeated replay of identical input, code, config, and mode must produce identical state hash and record counts.

## `EngineForecastV1` — introduced only after baseline contracts exist

```text
forecast_id
engine_id
engine_version
market_id
contract_id
as_of_event_time
as_of_ingest_sequence
data_cutoff
p_yes                  nullable unless calibrated
raw_score               optional
calibration_status      uncalibrated | calibrated | degraded
calibration_version     optional
uncertainty_low         optional
uncertainty_high        optional
evidence_ids[]
dependency_groups[]
feature_snapshot_hash
config_sha256
valid_until             optional
```

If `calibration_status=uncalibrated`, use `raw_score` and keep `p_yes` null.

## `MarketBaselineForecastV1`

Record at least:

- displayed/midpoint proxy and method;
- best YES bid/ask;
- best NO bid/ask;
- last trade if used;
- spread;
- observation time;
- exact token IDs;
- liquidity/depth assumption;
- baseline probability source.

## `ResolutionV1`

```text
resolution_id
market_id
condition_id
resolved_at
winning_outcome
winning_token_id        optional
resolution_source
source_payload_sha256
clarification_present
resolution_status       proposed | disputed | final | unknown
normalizer_version
```

## `ForecastEvaluationV1`

```text
evaluation_id
forecast_id
resolution_id
outcome_yes             0 or 1
brier_score
log_loss                optional when p is clipped by declared epsilon
absolute_error
cohort_dimensions
created_at
evaluator_version
```
