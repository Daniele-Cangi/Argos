# Core invariants

These rules define ARGOS. They are more important than implementation convenience.

1. **Observation is not evidence; evidence is not a forecast; a forecast is not edge; edge is not a decision; a decision is not execution.** Keep these objects separate.
2. **The market is a benchmark and an evidence source, not merely an opponent.** Preserve midpoint, bid, ask, depth, spread, and lifecycle state.
3. **Resolution rules are the ground-truth contract.** The question title is not sufficient. Store the original rules and source before compiling them.
4. **Every forecast is time-indexed and reproducible.** It must be possible to reconstruct what ARGOS knew at the forecast timestamp.
5. **Live and replay use the same domain handlers.** Only the clock and event source may differ.
6. **Arrival order and event time are distinct.** Replay original arrival sequence; use event time and explicit watermarks for temporal reasoning.
7. **Raw data is immutable.** Normalization creates a new versioned representation; it never replaces the source payload.
8. **Probabilities must be calibrated or explicitly labeled as scores.** No heuristic may masquerade as probability.
9. **Dependency is explicit.** Two engines that share inputs or consume one another are not independent votes.
10. **Abstention is a first-class result.** High ambiguity, low liquidity, stale data, correlated evidence, or forecast disagreement may block a decision.
11. **No execution in the research core.** Authenticated trading and wallet operations are outside M0-M4 and require a separate owner decision.
12. **Claims are earned by evaluation.** Do not claim edge, market outperformance, information lead, or predictive power from anecdotes.
13. **Configuration is part of the experiment.** Every run records configuration, code revision, schema versions, and data provenance.
14. **Errors are data.** Late, duplicated, malformed, rejected, or missing events are counted and explained rather than silently discarded.
15. **Human review remains mandatory for semantic ambiguity.** A market compiler can surface ambiguity; it cannot silently redefine resolution.
