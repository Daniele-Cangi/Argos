# ARGOS

**Probability intelligence for Polymarket, built as a replayable and self-evaluating research system.**

ARGOS observes prediction-market metadata, rules, order books, trades, lifecycle events, and later external evidence. It produces explicit probability estimates, compares them with market-implied and executable prices, records why a forecast was made, and evaluates every forecast after resolution.

The initial repository is intentionally **read-only** with respect to trading. It uses public Polymarket data only and does not contain wallet, order-placement, or execution code.

## First autonomous build target

The repository is considered ready for owner review when it can:

- discover and normalize active binary markets;
- preserve original market rules and compile a reviewable contract representation;
- capture public CLOB market events into an immutable event store;
- replay captured events deterministically through the same core used by live ingestion;
- ingest resolution outcomes;
- evaluate market baselines with Brier score, log loss, and calibration summaries;
- produce a complete reproducibility manifest and technical handoff.

See `README_FIRST.md`, `CLAUDE.md`, and `docs/07_MILESTONES.md` before starting development.
