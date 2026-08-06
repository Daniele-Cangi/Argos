# Handoff format

At the M4 owner gate, produce `docs/HANDOFF_M4.md` with the following exact structure.

## 1. Executive state

- current commit;
- milestones completed;
- one-paragraph description of what works;
- explicit statement that execution/auth code is absent.

## 2. How to run

Commands from a clean checkout for:

- environment setup;
- tests and quality gates;
- market discovery;
- market audit;
- sample capture;
- replay;
- baseline evaluation.

## 3. Architecture map

- implemented packages;
- dependency direction;
- event flow;
- storage layout;
- configuration and manifests.

## 4. Contract inventory

Table of each persistent schema, version, implementation path, and test path.

## 5. Milestone evidence

For each M0-M4 exit criterion, link the code/test/report that proves it.

## 6. Sample run

- selected market identifiers without secrets;
- capture manifest;
- counts and health warnings;
- replay hash;
- evaluation summary;
- known limitations.

## 7. Scientific status

- what has and has not been evaluated;
- baseline definitions;
- no edge claims unless statistically earned;
- data gaps and potential leakage.

## 8. Security status

- secret scan result;
- authenticated API absence;
- dependency review;
- remaining risks.

## 9. Open decisions

List each owner decision needed before M5, including options and recommended default.

## 10. Recommended next work

A prioritized plan for M5-M7, without starting implementation.

## 11. Git status

- branches;
- uncommitted changes;
- open TODOs;
- local-only generated artifacts.
