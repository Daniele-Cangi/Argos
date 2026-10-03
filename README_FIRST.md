# Read this first

ARGOS is a read-only Polymarket research system, not a starter scaffold or a
trading bot. See [README.md](README.md) for the current implementation boundary.

## Current working boundary

As of 2026-10-03, PR #26 is merged and post-merge Windows/Ubuntu CI passes.
T1-T8 technical qualification is independently verified. The ADR-0020/V2 cohort
has synthetic admission/capture/freeze/replay proof, **not live qualification**.
Operational M4 and Owner Gate A remain open; no live V2 cohort is frozen or
launched, and no calibration or edge claim is established.

The next work is campaign-wide disk enforcement and durable failure/recovery,
followed by the unified early/late outcome and scoring path. A 2-4-slot live
pilot requires its own concrete reviewed protocol and owner approval.

## Governance before implementation

Read [AGENTS.md](AGENTS.md), [core invariants](docs/CORE_INVARIANTS.md),
[current status](docs/STATUS.md), [backlog](docs/BACKLOG.md) and
[ADR-0020](docs/adr/0020-budgeted-cohorts-and-separate-research-claims.md).
The [cohort plan](docs/research/m4-asynchronous-cohort-design.md) names the
remaining acceptance cases.

`AGENTS.md` is authoritative for Codex work. `CLAUDE.md`, `.claude/`,
`START_PROMPT.md` and earlier handoffs remain historical context; do not restart
bootstrap or treat their older milestone-complete wording as the current verdict.
The retained scripts under `scripts/claude/` are executable quality tools, not
an override of current governance. Do not launch M5-M8 from a green CI result.

## Local setup

Use an existing clone with Python 3.12+, Git and `uv`. Check the current branch,
dirty files and merged PRs before creating a dedicated branch from updated main.
Preserve unrelated edits and all raw/proof archives.

On Windows PowerShell, enable UTF-8 output before printing documentation:

```powershell
$env:PYTHONUTF8 = "1"
```

```bash
uv sync --frozen --all-groups
uv run argos --help
uv run python scripts/claude/quality_gate.py --quick
uv run python scripts/claude/quality_gate.py
uv run python scripts/m4_technical_proof.py verify --proof experiments/m4-technical-20260926-v1/proof
```

These verification commands do not initiate live capture or finality polling.
The full branch-coverage gate is separate; Ubuntu CI runs it. Consult
[RUNBOOK.md](docs/RUNBOOK.md) before commands that access public sources.

## Human responsibility and license

The owner remains responsible for concrete semantic review, live protocol/pilot
approval and the M4 exit gate. Keep pending, excluded and failed targets visible;
never rewrite historical evidence to satisfy a new policy. Trading credentials,
wallets and execution remain out of scope.

Original code and documentation use [Apache-2.0](LICENSE), with attribution in
[NOTICE](NOTICE). This does not change the rights or terms of external data,
captured source payloads or third-party dependencies.
