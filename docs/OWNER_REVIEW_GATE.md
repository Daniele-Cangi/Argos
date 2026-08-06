# Owner Review Gate A — after M4

Claude must stop implementation when this gate is reached.

## Required repository state

- [ ] Clean working tree or clearly documented local-only artifacts.
- [ ] M0-M4 marked complete with evidence.
- [ ] All CI/local quality gates pass.
- [ ] No execution, wallet, private key, authenticated channel, or order code.
- [ ] Public-source endpoints and schemas re-verified.
- [ ] Sample discovery, capture, replay, and evaluation commands work from clean checkout.
- [ ] Golden replay hash is stable.
- [ ] At least one completed baseline evaluation exists, without edge claims.
- [ ] Architecture, security, and test reviews have no critical blocker.
- [ ] `HANDOFF_M4.md` is complete.

## Questions for Daniele and Nexus

1. Which initial market categories should M5 prioritize?
2. Which external evidence sources are acceptable and affordable?
3. Should the market compiler use LLM assistance, and under what human-review policy?
4. Which RESON channels should be independent in the first experiment?
5. What minimum resolved sample is required before calibration/fusion claims?
6. Should the next interface remain CLI/report-first or introduce a research UI?
7. Should execution remain a separate repository permanently?

## Prohibited automatic continuation

Do not start M5-M8 merely because M4 passes. Produce recommendations only.
