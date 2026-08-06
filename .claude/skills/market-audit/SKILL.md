---
name: market-audit
description: Audit one Polymarket market end-to-end: metadata, condition/token mapping, rules, ambiguity, quotes, and capture readiness without making a probability claim.
allowed-tools: Read, Glob, Grep, Bash, Agent
---

Audit the requested market using public read-only data.

Required output:

- identifiers: event, market, condition, slug, outcome token mapping;
- original question and rule material hashes/locations;
- resolution source and time boundaries;
- outcome/token consistency;
- active/closed/restricted/neg-risk state;
- best bid/ask, midpoint, spread, and timestamp for both outcomes where available;
- compiler output and ambiguity flags;
- human-review status;
- capture readiness and rejection reasons;
- official source references and retrieval time.

Do not estimate P(YES), claim edge, or access authenticated endpoints. Store the report under `reports/market-audits/` only when M1 has established the report contract; otherwise return a design-only result.
