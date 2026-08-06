# Questions and assumptions

Claude updates this file whenever it makes a reversible choice that is not already resolved.

## Resolved defaults

- Language: Python 3.12.
- Package manager: uv.
- Architecture: modular monolith with ports/adapters.
- Initial mode: local, single operator, public read-only APIs.
- Market scope: configurable binary-market filter.
- Storage: implementation chosen in M0 behind protocols; append-only semantics are mandatory.
- Replay: original arrival order, explicit event-time windows and late-event policy.
- Forecast truth: final market resolution under stored rule version.
- UI: deferred until after M4.
- Execution: prohibited until separate owner approval.

## Open for implementation research

- Exact source fields available for source sequence IDs on each WebSocket event.
- Best public mechanism to retrieve final resolution and dispute state consistently.
- Whether Gamma update timestamps are sufficiently reliable for rule-version availability.
- Initial local event-store implementation after a measured throughput test.
- Sanitized fixture retention policy for real source payloads.
- Default liquidity/spread thresholds for the sample market selector.

## Assumption log

Use this format:

```text
Date:
Milestone:
Assumption:
Why it was needed:
Conservative choice:
How to reverse or verify:
Owner impact:
```
