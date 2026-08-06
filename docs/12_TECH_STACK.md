# Technical stack

## Chosen foundation

- Python 3.12+
- uv for environment, dependency locking, and commands
- Pydantic v2 for versioned boundary contracts
- httpx for REST adapters
- websockets for public market streams
- anyio for structured async orchestration
- Typer for local CLI
- structlog for structured logs
- tenacity only for explicit bounded retry policies
- orjson for canonical/high-throughput JSON handling where appropriate
- pytest, pytest-asyncio, Hypothesis, and respx for tests
- Ruff and strict mypy for quality

## Storage

The architecture requires an `EventStore` protocol. M0 should compare a minimal SQLite/WAL implementation with an append-log/Parquet research path using actual capture-volume measurements. Choose the smallest implementation that satisfies M2 durability and M3 deterministic replay, and record the decision in an ADR.

Do not add a distributed broker or database during M0-M4.

## Frontend

Deferred. The first interface is CLI plus generated Markdown/JSON research reports. A frontend introduced before stable contracts will freeze the wrong abstractions.

## Official SDK policy

The official Polymarket SDKs may be evaluated, but the project should not import authenticated trading functionality into the research core. Lightweight public REST/WebSocket adapters may be clearer for M0-M4. Record the choice in an ADR.

## Dependency additions

Every new dependency requires:

- purpose;
- why stdlib/current dependencies are insufficient;
- maintenance/security assessment;
- license compatibility;
- lock-file update;
- architecture-guardian approval when it affects a boundary.
