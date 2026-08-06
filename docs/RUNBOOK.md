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
