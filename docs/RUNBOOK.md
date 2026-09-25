# Local runbook

## Environment

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). CI pins 3.12; a newer
interpreter is fine locally as long as the quality gate passes.

```bash
uv sync --all-groups      # create .venv and install runtime + dev groups
uv run argos --help
```

`uv.lock` is committed. Regenerate it only when dependencies change, and record
the reason (see `docs/12_TECH_STACK.md`, "Dependency additions").

## Quality gate

```bash
uv run python scripts/claude/quality_gate.py            # ruff, format, mypy, pytest
uv run python scripts/claude/quality_gate.py --quick    # fast inner loop
uv run python scripts/claude/quality_gate.py --coverage # branch coverage thresholds
```

`--coverage` is deliberately not part of the default gate: the suite runs in
roughly 80 seconds and the same suite under coverage takes roughly 610, and a
gate that slow stops being run between slices. CI runs it as its own step,
where nobody is waiting. It enforces the per-area thresholds in
`docs/13_TEST_STRATEGY.md` — 90% for domain contracts, replay ordering and
scoring rules; 80% for source adapters — rather than one aggregate number,
because that document's own first sentence about thresholds is "do not optimize
for a vanity global coverage number".

Individually:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
```

Run the full gate before closing a milestone. Unit tests never touch the network.

## M4 resilient validation campaigns

ADR-0019 replaces multi-day all-or-nothing technical qualification with a
bounded scenario matrix. The authoritative plan is
`docs/research/m4-resilient-validation-plan.md`.

Do not treat elapsed runtime as evidence by itself. Each scenario must name the
property it tests, its maximum duration, exact inputs or injected fault and its
own pass/fail evidence. Technical passage does not imply predictive accuracy or
calibration. Resolution polling belongs to a separate resumable cohort and an
unresolved target remains pending rather than becoming a fabricated negative.

The repository contains the ADR-0019 scenario-result schema, resumable monitor
and adversarial tests. A complete matrix must pass the end-to-end duration and
frame bounds enforced by `technical_campaign.v1`, and its published claim must
include repository-verifiable evidence bytes. Technical qualification is a
prerequisite for, but does not authorize or substitute for, a frozen predictive
protocol.

## Governance checks

```bash
uv run python scripts/claude/validate_bootstrap.py      # agents and skills load
```

## CLI

```bash
uv run argos status                  # current milestone from docs/STATUS.md
uv run argos version
uv run argos config                  # validated settings + config fingerprint
uv run argos manifest --mode inspect # run manifest record (schema run_manifest.v5)
```

### Market discovery and audit (M1)

These two commands reach the public Gamma API. They are read-only and send no
credentials.

```bash
uv run argos markets discover --limit 20
uv run argos markets discover --min-liquidity 5000 --min-hours-to-end 48 --json
uv run argos markets discover --save-raw          # archive the payload under ARGOS_DATA_DIR
uv run argos markets audit 2063134                # Markdown report for a human reviewer
uv run argos markets audit 2063134 --json         # market_audit.v1 record
```

`discover` always reports its full sample: how many markets Gamma returned, how
many normalized, how many were quarantined and why, and how many the selection
policy excluded and why. The policy itself is included in the JSON output —
a sample without its policy cannot be interpreted later.

`ARGOS_HTTP_TIMEOUT_SECONDS` is a **per-attempt read timeout**, not the wall-clock
bound on a call. The bound is the overall deadline, which budgets for the whole
retry allowance: `timeout × attempts + 10s × (attempts − 1)`. At the defaults
(10s, 5 attempts) a request is bounded at 90 seconds, not 10.

`audit` makes no probability claim. `yes_condition` and `no_condition` are
deliberately empty: extracting them from prose is semantic work gated behind the
owner review after M4.

### Live capture (M2)

`argos capture market` opens a socket to the **public, unauthenticated** CLOB
market channel. It sends no credentials and subscribes only to token ids the
operator names.

```bash
uv run argos capture market --token-id 34691...961 --max-seconds 45
uv run argos capture market --token-id A --token-id B --max-frames 200 --json
uv run argos capture market --token-id A --max-seconds 60 --db /tmp/run.sqlite3
```

**One of `--max-seconds` or `--max-frames` is required.** A research CLI must
not start an unbounded run against a live public endpoint by accident. Reaching
a bound closes the capture run `completed`; Ctrl-C closes it `failed` rather
than leaving it dangling, and exits 130. "Interrupted" is reserved for a
process that dies outright and therefore writes no closing row at all — that is
what `EventStore.iter_open_capture_runs` reports, and it is the only honest
meaning, since a dying process cannot describe its own death.

Every run writes three artifacts beside each other: a SQLite event store
(default `$ARGOS_DATA_DIR/capture/events.sqlite3`), a
`<capture_run_id>.manifest.json` (`run_manifest.v5`), and a content-addressed
archive of every raw frame under `raw/`. `--no-raw-archive` turns the last one
off and its help text says what that costs: a capture that cannot be
re-normalized under a corrected parser, and whose stored `raw_payload_sha256`
can never be checked against anything.

The command prints the loop's own counters *and* the store's independently
derived counts. They must agree; printing one would hide a disagreement, and a
disagreement is exactly the kind of defect that must stay visible.

### Deterministic replay (M3)

Reads a stored capture and rebuilds the market state it implies. Touches no
network and writes nothing but its manifest — a replay that could modify its own
input would not be a replay, and that is asserted by comparing the database
bytes before and after.

```bash
uv run argos replay capture <capture_run_id> --db .data/capture/events.sqlite3
uv run argos replay capture <capture_run_id> --db events.sqlite3 --json
uv run argos replay capture <capture_run_id> --db events.sqlite3 --mode original_arrival
uv run argos replay capture <capture_run_id> --db events.sqlite3 --allowed-lateness-ms 250
```

`--mode` is **pacing only** and cannot change the output hash (ADR-0012 section
7, and a test asserts it over all three modes). `accelerated` runs as fast as
the machine allows; `original_arrival` sleeps the real gaps between arrivals,
for a replay somebody is watching; `stepwise` exists for a caller driving one
arrival at a time through the library.

`--allowed-lateness-ms` is the watermark tolerance. Zero is the default because
no capture in this repository contains an out-of-order arrival, so zero flags
nothing yet observed while flagging any genuine regression the first time it
happens. A late event is **marked and still applied in arrival order** —
reordering it would be forbidden by ADR-0003 — so changing this changes the
counts and never the state hash.

The command prints the state hash, its encoding version, both count sets
(what the capture recorded, and what the dispatcher did with it) and a digest
per reconstructed book, and writes a `replay_manifest.v1` beside the database.

### Baseline evaluation (M4)

Scores market baselines from a stored capture against a recorded settlement.
Reads only, fetches nothing: an evaluation that reached the network could give a
different answer tomorrow from the same arguments.

```bash
uv run argos evaluate baseline <capture_run_id> \
    --token-id <token> --db events.sqlite3 \
    --resolution tests/fixtures/clob/market_resolved.raw.json
```

`--resolution` is a **path to a recorded payload**, not a URL. Two shapes are
accepted: the CLOB market record, which states the winner outright via a
per-token `winner` flag, and the Gamma market record, which requires inferring
it from `outcomePrices`. The CLOB shape is tried first. If neither yields a
resolution, *both* refusal reasons are printed — "this is not a CLOB record" and
"this is not a resolved Gamma record" are different problems with different
fixes.

**`closed == true` does not mean resolved.** Measured over 900 closed markets,
93.2% carry a fractional `outcomePrices` that is a last price rather than a
settlement, and 5.1% carry `["0","0"]` with no determinable outcome
(`docs/research/m4-gamma-resolution.md`). Only an exact 1/0 pair is accepted, and
everything else is refused with a counted reason.

Scores are **uncalibrated market baselines, never ARGOS probabilities**
(ADR-0006): the record populates `raw_score` and leaves `p_yes` null, and a
`p_yes` without a calibration version is refused by a validator. The report's
`limitations` field is required to be non-empty and is printed in the human
output, not only under `--json` — a report whose caveats are one flag away is a
report whose caveats get dropped.

## Configuration

Settings come from `ARGOS_*` environment variables; `.env.example` lists them.
Configuration is immutable once loaded and every `ARGOS_*` variable must map to a
known field — an unrecognized one fails the load instead of being ignored.

`ARGOS_EXECUTION_ENABLED=true` is rejected with `argos.execution_prohibited`.
That is intentional through M4 (ADR-0007).

## Reading a failure

Every deliberate failure carries a stable code, for example:

```json
{"error_code": "argos.execution_prohibited", "message": "...", "context": {"adr": "ADR-0007"}}
```

Codes are defined in `src/argos/errors.py`. Counters and rejection ledgers key off
`error_code` and `RejectionReason`, never off free-text messages.
