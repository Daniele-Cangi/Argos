# Product scope

## Initial user

The initial user is the research owner operating ARGOS locally or in a controlled server environment. Multi-user product features are out of scope through M4.

## M0-M4 capabilities

### Included

- public Gamma API market discovery and metadata normalization;
- original question, description, resolution source, deadlines, outcomes, and token IDs;
- a conservative market-contract compiler skeleton with explicit ambiguity and review state;
- public CLOB REST snapshots and public market WebSocket ingestion;
- canonical events for books, price changes, best bid/ask, trades, new markets, and resolutions where available;
- immutable local event storage with idempotent writes;
- capture manifests and source-health metrics;
- deterministic replay using the same domain handlers as live ingestion;
- baseline market probabilities and executable quote representation;
- resolution records and proper-scoring evaluation;
- CLI commands, tests, CI, documentation, and owner handoff.

### Excluded

- order placement, cancellation, wallet creation, signing, private keys, balances, positions, or execution;
- user WebSocket channel or any authenticated CLOB endpoint;
- portfolio construction, sizing, P&L optimization, market making, or arbitrage execution;
- external news ingestion and web scraping;
- LLM-based probability estimates;
- a production frontend;
- generalized multi-platform support beyond a clean adapter boundary;
- claims that ARGOS has predictive edge.

## Market scope for the first dataset

Use only markets that satisfy configurable, documented filters. The initial default filter should favor:

- active and not closed;
- binary YES/NO outcomes;
- non-empty condition ID and CLOB token IDs;
- non-empty description or resolution material;
- future end date;
- observable order book;
- sufficient liquidity for stable data collection.

Thresholds must be configuration, not hidden constants. Save the filter configuration in the capture manifest.

## Product boundary

ARGOS owns:

- normalization;
- semantic contract representation;
- event capture and replay;
- forecasting/evaluation contracts;
- research metrics;
- provenance and reproducibility.

ARGOS does not own:

- Polymarket resolution policy;
- external-source truth;
- custody or execution;
- user investment decisions.

## Owner review boundary

Claude Code may autonomously complete M0-M4. It must not proceed to external evidence, RESON, advanced forecasting, or execution before the owner gate in `OWNER_REVIEW_GATE.md` is approved.
