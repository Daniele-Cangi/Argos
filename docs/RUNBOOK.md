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
```

Individually:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
```

Run the full gate before closing a milestone. Unit tests never touch the network.

## Governance checks

```bash
uv run python scripts/claude/validate_bootstrap.py      # agents and skills load
```

## CLI

```bash
uv run argos status                  # current milestone from docs/STATUS.md
uv run argos version
uv run argos config                  # validated settings + config fingerprint
uv run argos manifest --mode inspect # run manifest record (schema run_manifest.v1)
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
