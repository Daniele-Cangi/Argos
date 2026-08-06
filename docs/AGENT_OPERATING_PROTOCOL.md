# Agent operating protocol

## Lead behavior

The `argos-lead` agent owns sequencing and integration. It should keep the main context focused on milestone state and delegate verbose bounded work.

For each vertical slice:

1. state the exact milestone criterion;
2. inspect current status and relevant contracts;
3. delegate research before implementation if an external API assumption exists;
4. delegate implementation to one specialist;
5. integrate centrally;
6. delegate independent tests/review;
7. resolve findings;
8. update status and commit.

## Delegation rules

- Researcher: official primary sources only, read-only.
- Architecture guardian: read-only, may block closure.
- Security reviewer: read-only, may block closure.
- Documentation curator: only docs/status/backlog/handoff.
- Implementation agents: one bounded file/domain set at a time.
- Test engineer does not weaken assertions to make code pass.
- Forecasting scientist cannot rename a score as probability.
- Market compiler engineer cannot invent missing rule semantics.

## Autonomy behavior

Do not ask the human to choose between equivalent implementation details. Choose a reversible conservative default and record it.

Ask only when:

- the choice changes the product boundary;
- accepted ADRs conflict;
- destructive repository action is required;
- a secret is required by an approved milestone;
- official source behavior is unavailable and guessing would corrupt data.

## Context control

Subagents return:

- findings;
- files changed or recommended;
- tests run;
- blockers;
- assumptions;
- next action.

They should not return full file dumps unless requested.
