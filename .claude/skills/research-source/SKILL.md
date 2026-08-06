---
name: research-source
description: Research a current external technical assumption using primary official sources, record provenance and uncertainty, and translate it into an implementation brief.
allowed-tools: Read, Glob, Grep, Bash, Agent
---

Delegate to polymarket-researcher when the topic concerns Polymarket.

Create or update a dated note under `docs/research/` containing:

- exact question;
- official sources and retrieval date;
- documented behavior;
- observed fixture behavior if available;
- discrepancies;
- implementation contract;
- failure/edge cases;
- assumptions still requiring live verification;
- whether an ADR or schema update is required.

Do not cite blogs when an official primary source exists.
