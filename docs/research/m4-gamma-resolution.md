# M4 research: what a resolved Polymarket market actually looks like

- Date: 2026-08-18
- Scope: the public, unauthenticated Gamma `/markets` endpoint only.
- Method: measured against live traffic, not read off documentation. Four
  payloads are recorded verbatim in `tests/fixtures/gamma/` with provenance
  sidecars.

## Why this note exists

M4's exit criteria include **"unresolved markets are not scored as negatives"**.
Before this research the repository had no fixture of a resolved market at all —
all three recorded Gamma payloads are `closed: false` — so every field a
resolution normalizer would rely on was documentation-only. The obvious
implementation is "`closed == true` means resolved, read the winner out of
`outcomePrices`", and **it is wrong on the majority of closed markets.**

## The headline finding

`closed == true` does **not** mean a determined outcome, and `outcomePrices` is
not always a resolution. Measured over two samples of the same endpoint,
differing only in ordering:

| `outcomePrices` shape | Oldest-first (id order), n = 900 | Most recently ended, n = 500 |
|---|---|---|
| exactly `["1","0"]` or `["0","1"]` | **4 (0.4%)** | **500 (100%)** |
| fractional, summing to 1 | 839 (93.2%) | 0 |
| `["0","0"]` | 46 (5.1%) | 0 |
| not two outcomes | 11 (1.2%) | 0 |

The two samples are the same query with a different `order`, and they disagree
almost completely. A normalizer validated against either one alone would look
correct and be wrong about the other.

The fractional cases are plainly **last prices, not resolutions**. Market 40 is
*"Will Trump win the 2020 U.S. presidential election"*, closed, with
`outcomePrices` of `0.0000000436…` / `0.9999999…`. The real-world outcome of
that question is not in doubt; the field simply does not encode it. Market 12,
*"Will Joe Biden get Coronavirus before the election"*, is closed at
`["0","0"]` — closed with nothing determinable at all.

**Consequence for ARGOS.** Only an exact `{1, 0}` pair determines a winner.
Everything else is `unknown`, is counted, and is never scored. Had this been
inferred rather than measured, a naive evaluator run over an id-ordered sample
would have scored 93% of its markets against a number that is a price.

## `umaResolutionStatuses` is a sequence, not a status

Observed values across the 500 most recently ended closed markets:

| Value | Count |
|---|---|
| `["proposed"]` | 458 |
| `["proposed", "resolved"]` | 32 |
| `[]` | 8 |
| `["proposed", "disputed", "proposed", "resolved"]` | 2 |

Three things follow, none of which is guessable from the field's name:

1. **It is a history.** The current status is its *last* element, and the
   earlier elements are the trail that got there. A disputed market shows the
   dispute and the re-proposal rather than hiding them.
2. **`proposed` is the common case, not `resolved`.** 458 of 500 closed markets
   with an exact 1/0 outcome carry only a proposal. `docs/04_DATA_CONTRACTS.md`
   asks `ResolutionV1.resolution_status` to be
   `proposed | disputed | final | unknown`, and the observed vocabulary is
   `proposed | disputed | resolved` — so `resolved` maps to `final` and the
   mapping is recorded here rather than assumed by a reader.
3. **An empty list with an exact 1/0 outcome happens** (8 of 500). The outcome
   is determined and no UMA trail is recorded against it. That is a market with
   a winner and an unknown *status*, which is why the two are separate fields
   rather than one.

## Fields that do not mean what their names suggest

- **`active` is not "open".** All 500 recently-closed markets have
  `active: true`. Reading `active` as tradability would classify every resolved
  market as live.
- **`archived` is not a lifecycle end.** `false` on all 500.
- **`acceptingOrders` tracks tradability**, and even it is not perfectly aligned:
  499 of 500 closed markets are `false`, and **one is `true`**. A closed market
  still accepting orders is a real, observed state, not a hypothetical.

## Paging limit

`?closed=true&limit=100&offset=2250` returns **HTTP 422**, and lower offsets
succeed. The endpoint therefore has a paging ceiling somewhere below
offset 2250 for this query, reached without any warning in the response body of
the preceding page. Any future sampling code must treat a 422 as "the window
ended", not as a failure — and must not assume it can walk the whole history.

## Recorded fixtures

| Fixture | Why |
|---|---|
| `gamma/market_resolved.{raw,meta}.json` | The only shape ARGOS treats as determined: `closed`, exact `["0","1"]`, `umaResolutionStatuses` ending in `resolved` |
| `gamma/market_resolved_after_dispute.{raw,meta}.json` | `["proposed","disputed","proposed","resolved"]` — the history, and the reason the current status is the last element |
| `gamma/market_closed_without_outcome.{raw,meta}.json` | Closed at `["0","0"]`: nothing determinable |
| `gamma/market_closed_with_a_price_not_a_resolution.{raw,meta}.json` | Closed at a fractional price whose real-world outcome is known and unencoded |

## UNVERIFIED

Stated so nothing downstream assumes it:

- **Whether `outcomePrices` ever settles to exact 1/0 *before* a market closes.**
  0 of 200 sampled open markets showed one, which is consistent with "never" and
  does not establish it.
- **Whether the `resolved` status can be reached without `proposed` preceding
  it.** Not observed; the sample is 500 markets from one window.
- **What a market that resolved to "invalid"/void looks like**, and whether
  `["0","0"]` is that case or merely a legacy artifact of markets from 2020-21.
  Every `["0","0"]` market observed is old, which is suggestive and not
  evidence.
- **Whether the UMA vocabulary is closed.** Three values were observed;
  nothing establishes that a fourth does not exist. The normalizer therefore
  maps what it knows and refuses the rest rather than defaulting an unknown
  status to `final`.
- **The exact paging ceiling**, and whether it varies by query. Only that a 422
  occurs at offset 2250 and not at 2000.
