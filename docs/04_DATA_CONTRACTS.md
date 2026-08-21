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

Four amendments recorded against this specification when M3 implemented it
(`src/argos/replay/manifest.py`, ADR-0012):

- `replay_mode` gains **`stepwise`**. `docs/07_MILESTONES.md` names stepwise a
  deliverable in the same breath as accelerated; dropping it to fit a two-value
  enum would be the "specified contract silently dropped" failure that blocked
  M1.
- `working_tree` joins `code_revision`, for the reason already accepted for
  `RunManifest`: a dirty tree makes a revision string misattribute the code that
  produced a run, and a replay whose entire claim is reproducibility is the
  worst place to leave that ambiguous.
- `input_event_count` is implemented as **`input_arrival_count`**. What is
  counted is *arrivals* — a duplicate is one more arrival of the same event —
  and that distinction is the whole reason the delivery record exists.
- `source_completion_status` is added. Replaying an interrupted capture is
  legitimate and often the point, but a manifest that did not say so would
  present a partial capture's state hash as though it described a complete one.

**And one correction to the sentence above.** The mode is *not* an input to the
hash. Two replays of one capture in all three modes produce one hash — asserted
directly, because ADR-0009 requires that scheduler pacing never influence the
output hash. The mode is recorded because it describes how the run was
performed, not because it changes what the run produced. What must be identical
across runs is `output_state_hash` and `output_record_counts`; `started_at` and
`finished_at` legitimately differ, and do.

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

## Corrected M4 evaluation evidence

ADR-0013 supersedes the v1 arrival-weighted composition while retaining its
arithmetic record types for compatibility.

`MarketBaselineForecastV2` adds a stable forecast id, evaluation-run link,
source capture and observation ids, target information-state hash, market id
and optional compiled-contract id. The stable forecast identity does not
include the evaluation-run id.

`ForecastEvaluationV2` links an evaluation to the exact forecast,
evaluation run and compiled contract.

`EvaluationRunBundleV1` remains readable as the first corrected claim boundary.
ADR-0014 advances the active boundary to `EvaluationRunBundleV2`, containing:

- `EvaluationPolicyV2`, where two targets are named only as a structural floor
  and calibration claims require a predeclared multi-target protocol;
- the exact `ResolutionV1` and optional `CompiledMarketContractV1`;
- `EvaluationReportV2`;
- ordered v2 forecasts and evaluations;
- one `EvaluationDecisionV1` per replay arrival;
- explicit `EvaluationExclusionV1` records; and
- a canonical digest over every nested, independently versioned record.

The prospective boundary is `EvaluationRunBundleV3`. It embeds and
cross-validates:

- `ProspectiveExperimentProtocolV1` plus its durable persistence receipt;
- the selected `MarketDefinitionV1`, `CompiledMarketContractV1` and
  `ProspectiveTargetV1`, each with canonical digest and receipt;
- a contiguous chain of receipt-bound `LifecycleObservationV1` records;
- `ResolutionCutoffEvidenceV1` naming the first final observation or the
  predeclared source-terminal alternative, plus its receipt;
- the original `ResolutionV1`, `EvaluationReportV3`, forecasts, evaluations,
  per-arrival decisions and exclusions; and
- a canonical V3 digest over every nested record.

`EvaluationReportV3.resolution_cutoff` means the cutoff proven by the cutoff
record. It does not change the historical meaning of V2 and does not reinterpret
`ResolutionV1.resolved_at`. Source time, retrieval time and selected cutoff are
separate fields. Contract, market, target and protocol receipts must all precede
every admitted forecast.

`EvaluationReportV2` records separate arrival, target-information-state,
forecast-point, scored-point, resolved-target and headline-eligible-target
counts. It also binds trajectory, resolution, contract, settings, revision,
working-tree, replay-integrity and child-record digests. A final state hash is
not a trajectory identity.

The v2 bundle validator cross-checks those report claims against its sibling
records before accepting the outer evidence digest: policy version;
resolution identity, digest, status, normalizer and cutoff; contract identity
and digest; child counts and digests; forecast/evaluation scope; and exclusive
scored-versus-excluded classification. Recomputing the outer digest over an
internally contradictory bundle does not make it a valid claim.

`ProspectiveExperimentBundleV1` is the multi-target boundary. It embeds the V3
target bundles, persistent target exclusions, one last-admissible contribution
per target and method, and a recomputable aggregate report. Duplicate targets
cannot contribute twice. It publishes measurement-layer and calibration
verdicts independently (ADR-0015).

ADR-0016 versions that aggregate boundary forward when an exclusion claims that
frozen capture code rejected a standalone `last_trade_price` event:

- `CaptureRejectionEvidenceV1` embeds the clean capture `RunManifest`, exact
  `RejectedObservationV1`, ingest sequence, exact UTF-8 source bytes and
  content-addressed archive location. It recomputes the rejection identity,
  byte length, SHA-256 and archive path, then strictly parses the bytes as
  `LastTradePriceV1` and checks source, event type, reason, chronology,
  condition and token scope. The manifest must prove raw archival, the frozen
  code revision, configuration fingerprint and subscribed token pair.
- `ProspectiveTargetExclusionV2` embeds that proof and its persistence receipt,
  plus the target and target receipt. It permits only the predeclared
  `unmodeled_standalone_last_trade_price` reason and fixes `excluded_at` to the
  proven source receipt time.
- `ProspectiveExperimentBundleV2` accepts only proof-backed V2 exclusions. It
  cross-checks each capture revision and configuration against the frozen
  protocol, requires the rejection to fall inside the observation window, and
  rederives contributions, report, verdicts and the canonical evidence digest.

ADR-0017 versions the operational boundary forward without changing those
historical records:

- `ProspectiveExperimentProtocolV2` carries the typed lifecycle deadline,
  polling interval, per-target seconds/frames, separate-database requirement,
  exact two-token subscription requirement and raw-archive policy.
- `EvaluationRunBundleV4` embeds the exact `RunManifest`, binds it into a V4
  digest and requires both cutoff retrieval and selected cutoff at or before
  the protocol deadline. Manifest run identity, clean revision, configuration,
  token set and capture bounds must agree with the protocol and target.
- `ProspectiveExperimentBundleV3` requires V4 included bundles and V2
  proof-backed exclusions under one V2 protocol, validates capture limits and
  prevents two targets from sharing a capture run.
- `ProspectiveClaimArtifactIndexV1` publishes a repository-relative aggregate
  path with schema, evidence digest, byte length, SHA-256 and original receipt.
  Verification rejects traversal/symlinks, hashes exact bytes, parses the
  versioned bundle and rechecks receipt and semantic identity.

ADR-0018 adds a proof-bearing terminal boundary for a different negative
failure mode: clean frozen captures whose selected targets have no observed
admissible cutoff by the lifecycle deadline.

- `CaptureRunSummaryV1` binds each completed capture manifest, ledger hash and
  byte length, delivery/rejection digests, frame/accepted counts, schema counts
  and zero rejection/decode/unknown counts.
- `LifecyclePollEvidenceV1` binds each `LifecycleObservationV1` to its
  persistence receipt and exact UTF-8 source payload, rechecking payload hash,
  length, source endpoint and interpreted finality.
- `ProspectiveTargetTerminalEvidenceV1` binds the protocol identity/receipt,
  selected target/receipt, capture summary and ordered poll chain. It derives
  the last observed finality, final/maximum cadence gaps and continuity, fixes
  admissible cutoff count to zero, and structurally forbids replacement,
  rescue or a claim of observation through an unobserved tail.
- `ProspectiveTerminalReportV1` reports target accounting, lifecycle
  continuity and resolution admissibility as independent typed claims.
- `ProspectiveExperimentBundleV4` receipt-binds every terminal target,
  revalidates it against protocol V2, derives the report and hashes the complete
  portable claim.

A terminal V4 bundle proves that no admissible cutoff was observed in its
persisted chain. It does not prove that the external market did not settle
during an unobserved interval. `ProspectiveClaimArtifactIndexV1` supports V4
without changing its existing schema meaning.


Experiment deadline closure, selected-target accounting and lifecycle polling
continuity are distinct result fields. Aggregate `observation_complete` must
not be read as proof that every cadence interval was observed.

The V1 schemas remain readable with their historical meaning. They are not the
active claim boundary for proof-backed exclusions and are not silently
reinterpreted.
