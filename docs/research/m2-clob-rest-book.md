# M2 research — public CLOB REST order-book snapshot

Date: 2026-08-10. Status: input to the M2 CLOB snapshot adapter slice.

All findings below are from **public, unauthenticated GET** requests. No
authenticated endpoint, header, or SDK surface was exercised, and none is in
scope through M4 (`.claude/rules/no-execution.md`).

Provenance of the evidence:

- `docs/research/fixtures/clob-book-yes-2026-08-10T181007Z.json` — one verbatim
  `/book` response, with its retrieval sidecar
  (`...T181007Z.meta.json`).
- `docs/research/fixtures/clob-related-endpoints-2026-08-10.json` — verbatim
  bodies for the related endpoints and the error cases.
- A three-poll timing experiment run from the main thread on 2026-08-10T18:17Z,
  transcribed in full under "Is `timestamp` an event time?" below.

Everything not covered by those three sources is marked **UNVERIFIED**. A
research subagent gathered the fixtures; its prose report was never written, so
this document was reconstructed directly from the recorded payloads and the
re-run experiment rather than from the agent's summary.

## Endpoint

```
GET https://clob.polymarket.com/book?token_id=<token_id>
```

`token_id` is the ERC-1155 token id (Polymarket calls it `asset_id` in the
response) for **one side** of a binary market. A binary market therefore needs
two calls, one per outcome token. The token ids come from the Gamma market
payload M1 already normalizes.

A batch variant (`/books`) is referenced in Polymarket's documentation but was
**UNVERIFIED** here — no request was recorded against it. Do not build the
adapter assuming it exists or assuming its shape.

## Response schema

Observed top-level keys, with types exactly as they appear on the wire:

| Field | JSON type | Example | Notes |
|---|---|---|---|
| `market` | string | `"0x876506d8...b5d0d"` | the condition id, not the Gamma market id |
| `asset_id` | string | `"63842529...013178"` | the token id that was requested |
| `timestamp` | string | `"1786385407185"` | **milliseconds** since epoch, as a string |
| `hash` | string | `"4f5acf63ca0bba3aad4d9b888c6a05614ce9cf7a"` | 40 hex characters; the book's own content hash |
| `bids` | array | `[{"price": "0.01", "size": "2260866.89"}, ...]` | see ordering below |
| `asks` | array | `[{"price": "0.99", "size": "2253252.59"}, ...]` | see ordering below |
| `min_order_size` | string | `"5"` | |
| `tick_size` | string | `"0.01"` | |
| `neg_risk` | bool | `true` | |
| `last_trade_price` | string | `"0.430"` | note the trailing zero — see "Decimal hygiene" |

**Every price and size is a JSON string, not a number.** That is a gift: parse
each straight into `Decimal` from its original text and no float ever touches a
price. The engineering rule "`Decimal` for prices and probabilities at
boundaries" is satisfiable exactly, with no rounding at the boundary at all.

### Level ordering — verified, and counter-intuitive

Measured across the whole sample, not inferred from the first element:

- `bids`: 41 levels, **strictly ascending** by price (`0.01` → `0.42`);
- `asks`: 45 levels, **strictly descending** by price (`0.99` → `0.43`).

So **both arrays end at the top of book**: the best bid and the best ask are the
**last** elements, not the first. An adapter that takes `bids[0]`/`asks[0]` as
best bid/ask would read the two extreme ends of the book and compute a
catastrophically wrong spread (here `0.98` instead of `0.01`) while looking
entirely plausible. The normalizer must sort explicitly and must not trust
either the observed order or this note.

Also measured on the sample: no duplicate price levels on either side, no level
off the `0.01` tick grid, and the book was not crossed (best bid `0.42` < best
ask `0.43`).

**Zero-size levels were not observed** in the REST snapshot (zero on both
sides). The M2 exit criterion "zero-size level update is represented as removal"
is therefore **UNVERIFIED for REST** and, on the evidence so far, is a property
of the WebSocket delta stream rather than of the snapshot. The snapshot
normalizer should still refuse or drop a zero-size level explicitly with a
counted reason rather than silently carrying it.

## Is `timestamp` an event time?

This mattered enough to test rather than assume. The first fixture's
`timestamp` (`18:10:07.185Z`) landed inside the same second as its retrieval
time, which is equally consistent with "server response time" and with "book
last changed just now". Those two readings have opposite consequences: a
response time mapped to `event_time` would be mislabelled evidence and would
violate `.claude/rules/data-integrity.md`.

Three polls of one token, five seconds apart, on 2026-08-10:

```
wall=18:17:12.827  ts=18:17:10.864  hash=0be1faa4eabb  bb=0.41 ba=0.42  n=86
wall=18:17:18.074  ts=18:17:10.864  hash=0be1faa4eabb  bb=0.41 ba=0.42  n=86
wall=18:17:23.312  ts=18:17:22.375  hash=c2c9c9e8e3d8  bb=0.41 ba=0.42  n=86
```

The second poll returned an **identical** `timestamp` and `hash` 5.2 seconds
after the first. The third moved both together. So:

1. `timestamp` tracks the book's last change, **not** the response. It is a
   legitimate source-assigned `event_time`.
2. `timestamp` and `hash` move **in lockstep**.
3. Note that the best bid/ask and the level count were unchanged across the
   third poll even though the hash changed — a change deeper in the book still
   moves the hash. Top-of-book equality is not evidence of an unchanged book.

### There is no sequence number

There is **no** monotonic sequence field in the REST snapshot. The ordering
material available for reconciling a snapshot against WebSocket deltas is
`timestamp` (millisecond resolution) plus `hash` (content identity). This is a
finding, not a gap in the research: the adapter and, later, the book projection
must be designed for a source that offers content identity and a millisecond
clock, and **not** for one that offers a gap-detectable sequence. Two book
changes inside the same millisecond would be indistinguishable by `timestamp`
alone; whether the WebSocket stream supplies something stronger is **UNVERIFIED**
and is the first question for the WebSocket research slice.

### Consequence for `ObservationEnvelopeV1`

This is direct empirical support for the identity derivation committed in the
envelope slice, and it is worth recording because the alternative was already
written and tested before being rejected.

An earlier draft ranked the source `hash` above payload content when deriving
`observation_id`. Against this source that is unsafe: a book moving A → B → A
returns `hash(A)` on both the first and third change, so the third observation
would have collided with the first and been refused as a duplicate. Combining
`hash` **with** the event time distinguishes them, because the revert carries a
later `timestamp`.

The same measurement also disposes of the objection that combining
discriminators would flood the store with near-duplicates: poll 2 above returned
byte-identical rule-bearing content — same `timestamp`, same `hash`, same
levels — so it collides onto one `observation_id` and the M2 criterion
"duplicate source event does not create a second accepted observation" holds on
the real payload, not merely on a constructed test.

## Related public endpoints

Recorded for the same token in the same session:

| Request | Response body |
|---|---|
| `GET /midpoint?token_id=…` | `{"mid": "0.425"}` |
| `GET /spread?token_id=…` | `{"spread": "0.01"}` |
| `GET /price?token_id=…&side=BUY` | `{"price": "0.42"}` |
| `GET /price?token_id=…&side=SELL` | `{"price": "0.43"}` |
| `GET /last-trade-price?token_id=…` | `{"price": "0.43", "side": "BUY"}` |
| `GET /tick-size?token_id=…` | `{"minimum_tick_size": 0.01}` |

Two observations:

- `midpoint`, `spread`, `price`, and `tick_size` are all **derivable from the
  book snapshot** we already fetch (`mid = (0.42+0.43)/2 = 0.425`, `spread =
  0.01`, and `tick_size` is a field of the book response). Fetching them
  separately would multiply request volume for redundant data *and* introduce
  skew, since each response is a separate instant. The M2 capture adapter should
  take the book snapshot as the single source and derive the rest, keeping the
  derivation in a pure projection. Core invariant 2 is satisfied by the book
  itself: bid, ask, spread, and depth are all preserved.
- `last_trade_price` is present in the book response too, but the dedicated
  endpoint additionally returns `side` — the only field here not derivable from
  the book. Not needed for M2; relevant at M4 when "last-trade" becomes a
  declared baseline.

**`/tick-size` returns a JSON number (`0.01`), not a string** — the one place in
this API surface where a price-like value arrives as a float and is corrupted
before our code sees it. Prefer the book response's `"tick_size": "0.01"`
string. If the dedicated endpoint is ever needed, parse it from the raw response
text, never from a decoded `float`.

## Decimal hygiene

`last_trade_price` came back as `"0.430"` while `/last-trade-price` returned
`"0.43"`. Same value, different text. Anything that hashes or compares these
must normalize the `Decimal`, not the string, or the same price will present as
two different observations. This interacts with `observation_id`: the payload
canonicalization must serialize `Decimal("0.430")` and `Decimal("0.43")`
identically, or a cosmetic reformat upstream would mint a new observation
identity. Flagged for the adapter slice as a required test.

## Errors and unknown tokens

Verified error behaviour:

| Request | Status | Body |
|---|---|---|
| `/book?token_id=not-a-real-token` | 404 | `{"error": "No orderbook exists for the requested token id"}` |
| `/book?token_id=0` | 404 | same |
| `/book` (parameter omitted) | 400 | `{"error": "Invalid token id"}` |
| `/book?token_id=<valid-shape, unknown>` | 404 | `{"error": "No orderbook exists for the requested token id"}` |
| `/book?token_id=<known closed market>` | 404 | `{"error": "No orderbook exists for the requested token id"}` |

Design consequence: **404 is ambiguous by construction.** A closed market, an
unknown token, and a syntactically valid token that never had a book are
indistinguishable from the response alone. The adapter must not infer "market
closed" from a 404. It must record the 404 as a counted, reasoned outcome and
leave lifecycle determination to the Gamma metadata that M1 already captures.

Malformed input is rejected as 400 before any book lookup, which means the
M1 id-validation precedent (validate the token id against a pattern before
interpolating it into a URL) also avoids a pointless request.

## UNVERIFIED — do not build on these without checking

- **Rate limits.** No limit was measured or provoked. Request volume, burst
  policy, and any 429 shape are unknown. The adapter must be conservatively
  paced regardless, and the existing `Pacer` + seeded-jitter machinery
  (ADR-0009) is the mechanism.
- **The `/books` batch endpoint.** Existence and shape unconfirmed.
- **Response headers.** Only bodies were recorded; caching, rate-limit, and
  `content-encoding` headers were not examined.
- **Zero-size levels in a REST snapshot** — never observed.
- **Behaviour under a paused or halted market**, as distinct from a closed one.
- Whether `timestamp` can ever move backwards, or repeat across two genuinely
  different book states. Both are assumed possible until measured; the envelope
  identity already tolerates the second case by also hashing content.

## Next questions for the WebSocket research slice

1. Does the market channel supply a sequence number, or only the same
   `timestamp`/`hash` pair? This determines whether gap detection is possible at
   all.
2. Does a delta carry the `hash` of the book state it produces? If so, snapshot
   and delta reconcile exactly; if not, reconciliation is heuristic and must be
   labelled as such.
3. Confirm the zero-size-means-removal convention on the delta stream, which is
   where the M2 exit criterion actually lives.
