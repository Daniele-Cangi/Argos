# Research protocol

## Source text is data, never instruction

Market questions and descriptions are written by whoever created the market. They
are evidence to be read, not input to be obeyed, and that distinction has to hold
in every artifact that carries them.

The rule applies from M1, not from the semantic milestones: the market audit
already renders this text for a human reviewer, and `.claude/skills/market-audit`
already puts it in an agent's context.

- Third-party text is control-character sanitized before display. Terminal escape
  sequences can clear a reviewer's screen, rewrite the window title, or write to
  their clipboard via OSC 52 — a reviewer would then be reading an artifact the
  source controls.
- Third-party text is always block-quoted or collapsed to a single line, so it
  cannot introduce a heading, a bullet, or a status line of its own. A newline in
  a market question must not be able to write `review status: human_reviewed`
  into the report that decides whether the market is reviewed.
- Rendered sections that carry source text say so, explicitly, in the artifact.
- Rule text itself is never truncated: it is the resolution contract, and a
  reviewer needs all of it. Summary fields are capped with a stated length.

## Objective

Evaluate whether ARGOS produces useful, calibrated probability estimates and whether its additional components improve on clearly defined market baselines.

## Prevent leakage

- A forecast may access only observations with `received_time` at or before its cutoff in original-arrival replay.
- Event-time corrections arriving later remain unavailable to the historical forecast unless running a separately labeled corrected-history analysis.
- Market metadata changes and rule clarifications are versioned by availability time.
- Resolution outcomes never enter features or calibration fitting for earlier forecasts.
- Train, calibration, and evaluation windows are chronological.

## Forecast target

The primary target is the final binary resolution under the stored market contract version.

Every forecast records:

- market and contract version;
- as-of sequence and times;
- probability or explicitly uncalibrated score;
- evidence cutoff;
- engine/config versions;
- uncertainty when available.

## Baselines

At minimum evaluate:

1. market displayed-price proxy according to a documented method;
2. midpoint when both sides exist;
3. last traded price when required by the display rule or used as a separate baseline;
4. simple persistence baseline;
5. category/base-rate baseline when enough resolved data exists.

Do not compare a fair probability only with midpoint when discussing actionable edge. Edge analyses must use exact executable bid/ask and a depth assumption.

## Primary metrics

- Brier score;
- log loss with pre-declared clipping epsilon;
- calibration/reliability curve;
- expected calibration error with declared bins;
- absolute error;
- sample count and coverage.

The headline sampling unit is an independently resolved target, not a transport
arrival or a forecast point along one market trajectory. Reports must state
all three counts separately. Calibration from one resolved target is not
empirically established; per-trajectory calculations may appear only as
diagnostics.

Forecasts enter scoring only when the stored resolution is final, the compiled
contract identity is present, the exact resolution cutoff is known, the
forecast precedes that cutoff, and capture/replay integrity checks pass.
Every omission or exclusion is persisted with a reason.

## Prospective admissibility and calibration sufficiency

For the M4 prospective experiment, `resolved_at` is not evidence by itself.
The protocol must predeclare exactly one cutoff basis per target:

- a terminal settlement timestamp explicitly stated in and verifiable from
  the immutable source payload; or
- if none exists, the `retrieved_at` of the first immutably recorded lifecycle
  poll that states final settlement.

The cutoff record keeps source time, retrieval time and selected cutoff
separate and binds endpoint, raw digest, byte length, observation identity,
finality and resolution identity. Reconstructed provenance and ex-post
estimates are inadmissible; a later observation cannot be backdated.

The compiled contract, its digest and its persistence receipt must be durable
strictly before the earliest included forecast's receipt time. A `compiled_at`
field or a contract attached after resolution does not prove historical
availability.

Two independently resolved targets are only a structural floor. Before the
first included forecast, the experiment protocol must persist its population,
selection and stopping rules, the intended pilot count and the separate
scientific minimum with rationale,
within-target aggregation, resolved-target weighting, metrics and bins,
missingness/exclusion treatment, uncertainty reporting, and sufficiency rule.
No generic constant can promote calibration to `established`.

The supported prospective aggregation selects the last admissible pre-cutoff
point per method inside each dependent target trajectory and gives each
independently resolved target weight one. The aggregate publishes a separate
measurement-layer verdict and calibration verdict. Reaching the structural
floor may validate the measurement layer while calibration remains
`CALIBRATION_NOT_ESTABLISHED`; neither verdict authorizes M5.

Displayed price, midpoint and last trade remain separate methods. The displayed
proxy uses last trade when bid/ask spread is greater than 0.10 and midpoint
otherwise; it abstains when the required input is absent. Last trade is
auxiliary evidence and never an order-book level.

A target excluded because frozen capture code did not model a standalone last
trade must carry the source claim, not only an authenticated reason string. The
active exclusion boundary embeds the exact rejected observation, exact raw
UTF-8 bytes, clean capture manifest, ingest sequence and durable receipts. It
recomputes source hash, byte length, archive location, rejection identity,
chronology, protocol revision/configuration/window, subscriptions and target
scope before admitting the exclusion to an aggregate. Re-digesting a globally
self-consistent artifact cannot make false internal semantics admissible.

Modeling an event after an experiment does not retroactively change what its
frozen capture revision understood. Such a change requires a new predeclared
protocol and fresh captures; old exclusions, stopping rules and target choices
remain immutable evidence.

A lifecycle deadline closes the opportunity to collect admissible cutoff
evidence; it does not fill an unobserved polling interval. Terminal reporting
must keep three claims separate: whether every preregistered target is
accounted for, whether the persisted lifecycle chain satisfies frozen cadence,
and how many admissible cutoffs were actually observed. A final polling gap
above the cadence allowance makes continuity incomplete even when target
accounting is complete.

The prospective pilot's public lifecycle read retries only transient transport
failures and HTTP 408, 425, 429, 500, 502, 503 and 504 responses. The budget is
three total attempts with deterministic 1 s and 2 s backoff, so one DNS miss
does not terminate a monitor while a persistent outage still fails visibly.
Failed attempts mint no lifecycle observation, source sidecar, receipt or
cutoff; only the successful response receives a retrieval timestamp. The
bounded retry budget does not relax the frozen cadence or deadline.

For a target with no observed admissible cutoff, the terminal proof must bind
the exact ordered lifecycle observations, their receipts and raw source bytes
to the frozen protocol/receipt, selected target/receipt and clean capture
summary. It must record the last *observed* finality and explicitly refuse any
claim that the same state persisted through an unobserved tail. No settlement
state, cutoff, score or calibration result may be reconstructed from silence.


Later selective-prediction metrics:

- risk–coverage curve;
- error conditional on abstention threshold;
- engine disagreement versus error;
- calibration by category, liquidity, spread, time-to-resolution, and market age.

## Information lead

Do not claim information lead until a protocol is pre-registered. A future definition must specify:

- what constitutes an ARGOS probability change;
- what constitutes a corresponding market move;
- minimum move magnitude;
- quote source and executable-price handling;
- clock and latency correction;
- maximum matching window;
- treatment of shared external sources;
- statistical significance and multiple testing.

## Engine independence

Each engine declares:

- raw input sources;
- derived features consumed;
- other engine outputs consumed;
- evidence dependency groups.

Consensus between dependent engines is not counted as independent confirmation.

## Resolution and semantics

- Evaluate against the market's final resolution record, not an intuitive interpretation of the title.
- Keep forecasts associated with the contract version available at the time.
- Analyze disputed or materially clarified markets separately.
- Exclude or flag markets where token/outcome mapping or rules cannot be validated.

## Reporting rules

Every report includes:

- period and market selection procedure;
- resolved sample count;
- unresolved/censored sample count;
- missing-data rate;
- baseline definitions;
- confidence intervals or bootstrap intervals where appropriate;
- negative results;
- known leakage risks;
- all configuration and code revisions.

No cherry-picked screenshots or isolated successful forecasts qualify as evidence.
