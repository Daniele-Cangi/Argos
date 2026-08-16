# M2 research — public CLOB market WebSocket channel

Date: 2026-08-10. Status: in progress, written incrementally per instruction —
this file is created before any finding is fully verified, and each section
below is appended only after the claim it makes has been checked against a
primary source (official documentation or directly observed live traffic).

Scope constraint, repeated from the task and from `.claude/rules/no-execution.md`:
**public, unauthenticated market channel only.** No user channel, no API key,
no L1/L2 auth header, no wallet, no order/position surface. If a capability
requires authentication, this document notes that it exists and stops there.

Companion document: `docs/research/m2-clob-rest-book.md` (REST `/book`
snapshot). Its three open questions are this document's priority 1-3 and are
answered inline below, not just at the end.

## Provenance log (updated as evidence is gathered)

- Environment has outbound internet access as of 2026-08-10 (verified: `curl`
  to `https://clob.polymarket.com/` returned HTTP 200).
- `python3 -m pip` has no `websockets` package preinstalled; `wscat` (Node) is
  present on PATH. Live capture will use `wscat` and/or a raw `curl
  --http1.1 --include` upgrade probe, recorded below as each is run.

---

(Sections below are appended as findings are verified. Nothing past this line
is final until its own verification note says so.)

## Documentation findings (verified against docs.polymarket.com, retrieved 2026-08-10)

Source: `https://docs.polymarket.com/market-data/realtime-data.md` (Mintlify's
markdown mirror of `https://docs.polymarket.com/market-data/realtime-data`),
"API" tab, retrieved via `curl` on 2026-08-10. This is the official public
docs page for the "Market Stream", i.e. the public unauthenticated market
channel. There is a companion `Tabs` block on the same page for TypeScript and
Python SDK usage of the same channel — those are wrapper types over the same
wire protocol and are recorded here only where they clarify field semantics.

### Connect URL (from documentation)

```
wss://ws-subscriptions-clob.polymarket.com/ws/market
```

### Subscribe message shape (from documentation)

```json
{ "assets_ids": ["<token_id>"], "type": "market" }
```

- Subscription is keyed by **token id** (`assets_ids`, plural key, array
  value), not condition id. This matches the REST `/book?token_id=` unit from
  the companion REST research.
- Multiple token ids can be listed in one subscribe frame.
- Additional token ids can be added or removed on an already-open connection
  with `{"assets_ids": [...], "operation": "subscribe"}` or `"operation":
  "unsubscribe"`, without reopening the socket. Documentation does not state a
  maximum token-id count per connection or per subscribe frame — **UNVERIFIED**.
- An opt-in flag, `"custom_feature_enabled": true`, added to the subscribe
  frame, enables three additional event types (`best_bid_ask`, `new_market`,
  `market_resolved`) beyond the four "standard" ones. Documentation does not
  explain the default state's rationale or whether it costs anything
  server-side — recorded as-is.

### Heartbeat (from documentation)

> The market WebSocket uses an application-level heartbeat. Send the text
> frame `PING` every 10 seconds; the server replies with `PONG`.

This is a client-initiated heartbeat (unlike the sports channel, which is
server-initiated with a 5s ping / 10s pong-timeout, and unlike RTDS which is
also client-initiated at 5s). Documentation does not state what happens if the
client stops sending `PING` — idle timeout duration is **UNVERIFIED**.

### Message types (from documentation, four "standard" + three "custom_feature_enabled")

All standard-tier examples below are transcribed verbatim from the docs page,
API tab. Every price/size field is a JSON string, matching the REST `/book`
finding. `market` is the condition id (0x-prefixed hex); `asset_id` is the
token id — same naming convention as REST `/book`.

1. `book` — full snapshot-shaped payload, presumably sent on subscribe and on
   material book changes:
   ```json
   {
     "event_type": "book",
     "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
     "asset_id": "1075058827677314893...",
     "timestamp": "1782753357257",
     "hash": "0xabc123…",
     "bids": [{ "price": "0.08", "size": "33343.4" }, ...],
     "asks": [{ "price": "0.99", "size": "218442.27" }, ...]
   }
   ```
   Note the doc's own example `hash` (`"0xabc123…"`) is a placeholder/elided
   value, not a real 40-hex-char hash — do not treat its shape (`0x`-prefixed)
   as authoritative; the REST book's real observed `hash` had **no** `0x`
   prefix (`"4f5acf63ca0bba3aad4d9b888c6a05614ce9cf7a"`, 40 hex chars). This
   discrepancy is flagged for live verification below.

2. `price_change` — one or more deltas in one message:
   ```json
   {
     "event_type": "price_change",
     "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
     "price_changes": [
       {
         "asset_id": "1075058827677314893...",
         "price": "0.08",
         "size": "33343.4",
         "side": "BUY",
         "hash": "56621a121a47ed9333273e21c83b660cff37ae50",
         "best_bid": "0.08",
         "best_ask": "0.09"
       }
     ],
     "timestamp": "1782753357257"
   }
   ```
   **This directly answers priority question 2 from documentation**: each
   entry in `price_changes` carries its own `hash`, in the same 40-hex-char
   format as the REST book's `hash` (this example has no `0x` prefix, unlike
   the `book` event's placeholder above). The documentation does not say in
   words whether this hash is "the hash of the resulting book state" — that
   is an inference from field naming and format identity with the REST
   snapshot's `hash`, not a documented guarantee. **Must be verified against a
   live capture**: reconcile a `price_change.hash` against a same-moment
   REST `/book` `hash` for the same token, if a live capture is possible in
   this session.
   - This message is also the natural home for the M2 exit criterion about
     zero-size levels: a `price_change` entry with `"size": "0"` most likely
     represents removal of that price level. **Not yet confirmed by
     documentation text or observed message** — see below.

3. `last_trade_price` — a single executed trade:
   ```json
   {
     "event_type": "last_trade_price",
     "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
     "asset_id": "1075058827677314893...",
     "price": "0.08",
     "size": "219.217767",
     "fee_rate_bps": "0",
     "side": "SELL",
     "timestamp": "1782753357257",
     "transaction_hash": "0xeeefff…"
   }
   ```
   `transaction_hash` is new information not present in the REST `/book` or
   `/last-trade-price` endpoints — an on-chain settlement reference. Not
   needed for M2 per the REST document's note that `last_trade_price` becomes
   relevant at M4.

4. `tick_size_change`:
   ```json
   {
     "event_type": "tick_size_change",
     "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
     "asset_id": "1075058827677314893...",
     "old_tick_size": "0.01",
     "new_tick_size": "0.001",
     "timestamp": "1782753357257"
   }
   ```

5-7. Behind `custom_feature_enabled: true`: `best_bid_ask` (derivable from
   `book`, same relationship the REST document found for `/midpoint` and
   `/spread`), `new_market` (a lifecycle announcement carrying Gamma-shaped
   metadata — `question`, `slug`, `outcomes`, `tags`, etc. — over the CLOB
   channel, which is a cross-source convenience but means the market channel
   is not purely CLOB-shaped data), and `market_resolved` (carries
   `winning_asset_id`/`winning_outcome` — this is a resolution *signal*
   arriving over an unauthenticated market-data channel, not the resolution
   *contract* itself; core invariant 3 still requires the Gamma/UMA
   resolution rules as ground truth, not this message).

### No documented sequence number

None of the seven message shapes above include any field resembling a
monotonic sequence number. The only ordering/identity material documented is
the same pair the REST book has: `timestamp` (millisecond string) and `hash`
(on `book`, and per-entry on `price_change`). **This answers priority question
1 from documentation: there is no sequence number on the public market
channel.** Gap detection cannot rely on a sequence field; it can at best
notice a `hash` that does not match the hash implied by applying a delta to
the last known book state, which is exactly the heuristic-reconciliation
concern in priority question 2. This is a documentation-level finding pending
live confirmation that no undocumented sequence field is actually present on
the wire.

### Zero-size levels — not settled by documentation text

The docs page never states in prose that a zero-size level means removal. It
is a reasonable inference from the `price_change` schema (a level update
without a separate `"removed"` boolean, so a size of `"0"` is the only way to
express deletion), but it is an **inference, not a documented guarantee**, and
the REST document already flagged this exact gap. This is priority question 3
and needs a real observed zero-size message, not a documentation reading.


## Live capture — verified against real traffic, 2026-08-10

A live capture was possible in this environment (outbound internet access
confirmed). No `websockets` package was preinstalled in the project's Python
environment; one was installed in an **isolated venv under the session
scratchpad** (`/tmp/claude-1000/.../scratchpad/wsenv`, not part of this
repository, not `uv`-managed, not a project dependency change) purely to run
the capture script. Two captures were made against the live public market
channel for one real, actively-trading token:

- Token: `34691510069031117755834214800869745291092253295564665516660269316660628637961`
  — the YES side of "National Bank Open: Diana Shnaider vs Iga Swiatek"
  (`conditionId 0x94a39addec8bd24a2d03deeaa43bdee6a2b11eca403cd12495eb24132cc23173`),
  selected via `GET https://gamma-api.polymarket.com/markets?limit=5&active=true&closed=false&order=volume24hr&ascending=false`
  for high 24h volume, i.e. likely to produce real-time book activity within a
  short capture window. This is public market discovery, the same surface M1
  already uses — no auth.
- Capture 1: `docs/research/fixtures/clob-ws-market-2026-08-10T184742Z.json` +
  `.meta.json`. 45 seconds, one token subscribed, connect/subscribe timestamps
  and every raw frame (including `PING`/`PONG`) recorded with client-side UTC
  wall time.
- Capture 2: `docs/research/fixtures/clob-ws-hash-reconciliation-2026-08-10T185418Z.json`
  + `.meta.json`. 40 seconds, same WebSocket subscription running concurrently
  with a `GET /book?token_id=...` poll every 3 seconds against the same token,
  specifically to test priority question 2 against REST, not just against the
  WebSocket's own `book` events.

### Priority question 1 — sequence number: confirmed absent, by observation

Every parsed message across both captures was inspected for any field beyond
the documented set. **No sequence number, offset, or monotonic counter of any
kind was present on any message.** The only ordering material on the wire is
exactly what the documentation states: `timestamp` (millisecond string) and
`hash`. This upgrades the documentation-based finding above from "not
documented" to "confirmed absent in over 70 live messages across two
sessions." Gap detection on this channel cannot rely on a sequence field and
must be designed around timestamp/hash reconciliation only, exactly like the
REST book.

In-session `timestamp` values were monotonically non-decreasing across both
captures (`hash_compare2.json`: 29 consecutive WS records, strictly
non-decreasing). This is a two-capture, few-dozen-message observation, not a
guaranteed property — the REST document already flagged that `timestamp`
reordering is unverified in general, and nothing here proves the server can
never deliver out of order under load, reconnect, or multi-node fan-out.
Still **UNVERIFIED** as a hard guarantee; treated as "observed, not proven"
per the REST document's own standard.

### Priority question 2 — delta hash reconciles exactly with REST snapshot hash: confirmed, both ways

This is the strongest finding in this document, and it is answered twice,
independently:

1. **Within the WebSocket stream itself** (capture 1 and capture 2): every
   time a `book` (full snapshot) event followed a `price_change` event
   closely, the `book` event's `hash` **exactly equaled** the immediately
   preceding `price_change` entry's `hash`, at the same `timestamp`. Example
   from `hash_compare2.json`:
   ```
   {"source": "WS_price_change", "hash": "11ff2096d0d3d48b94f1664dd89ed1dd6781e763", "timestamp": "1786387805565", ...}
   {"source": "WS_book",         "hash": "11ff2096d0d3d48b94f1664dd89ed1dd6781e763", "timestamp": "1786387805565"}
   ```
2. **Against the independent REST `/book` endpoint**, polled concurrently
   with a browser-like `User-Agent` (see the 403 note below): every REST poll
   in the capture returned a `hash` identical to the most recent preceding
   `price_change.hash` for the same token, at the same `timestamp`. Example
   from `clob-ws-hash-reconciliation-2026-08-10T185418Z.json`:
   ```
   {"source": "WS_price_change", "hash": "1c2e9dd2560b5625ecf9cd29a9a83d90d8f09902", "timestamp": "1786388061629", "price": "0.31", "size": "57153.04"}
   {"source": "REST",            "hash": "1c2e9dd2560b5625ecf9cd29a9a83d90d8f09902", "timestamp": "1786388061629"}
   ```
   Four independent instances of exact REST/WS hash agreement occur in this
   one 40-second capture (see the fixture for the full list).

**Conclusion: yes, a `price_change` delta's `hash` is the hash of the
resulting book state, and it reconciles exactly — byte for byte — with what
REST `/book` returns for the same state, using the same hash algorithm as the
REST snapshot (same 40-hex-char format, no `0x` prefix, confirmed identical
across sources).** This is not heuristic reconciliation; it is exact content
identity. A capture/replay design can trust `hash` as a content-addressed book
identity that is comparable across the WebSocket and REST sources. This
directly informs `ObservationEnvelopeV1`: hash-plus-timestamp discriminates
observations the same way on both sources, and a WebSocket-derived book state
can be cross-checked against a REST snapshot by hash equality alone, with no
tolerance/fuzzy matching needed.

One important nuance observed, not documented anywhere: **multiple
`price_change` entries — and even multiple separate WebSocket text frames —
can share the same `(timestamp, hash)` pair.** In `hash_compare2.json`, six
separate `price_changes` array entries inside one message all carried hash
`fd5a85bfbc86adcb6fc4c7a923b197739f2288e5` at `timestamp
1786388094692` (one underlying book-changing event moved six price levels at
once). Separately, in the same fixture, two **distinct top-level WebSocket
messages** arrived 196 microseconds apart, each containing one
`price_changes` entry, both stamped with the identical hash
`c4b01299dccf9a942a4e33200f2de7147a773661` and identical `timestamp
1786388069688`. So `(timestamp, hash)` identifies a **post-state**, not a
single wire message — an adapter must be prepared to receive the same
resulting book identity spread across more than one frame and must not assume
a 1:1 mapping between "one WebSocket message" and "one book transition." This
is a genuine subtlety for exactly-once/idempotent ingestion design.

**REST 403 caveat, recorded because it nearly produced a false negative**: the
first attempt at this comparison used Python's default `urllib` User-Agent
(`Python-urllib/3.13`) and every REST poll returned `HTTP 403 Forbidden`
(visible in the discarded `hash_compare.json` intermediate — not committed as
a fixture, since it demonstrates a client misconfiguration, not a server
property). `curl`'s default User-Agent and a `Mozilla/5.0`-style header both
succeeded with `200`. This suggests the REST endpoint (or a CDN/WAF in front
of it) applies User-Agent-based filtering. **UNVERIFIED as a general rule**
(only two User-Agent strings were tried, not a systematic study), but
practically important: the M2 capture adapter must set an explicit,
reasonable `User-Agent` and must not assume "no auth header" means "no
client-identification requirement." This is a new finding beyond what the
REST companion document recorded, since that document's requests were all
made with `curl`'s default UA and never hit this path.

### Priority question 3 — zero-size levels: confirmed as removal, observed directly

Multiple real zero-size entries were captured on the live `price_change`
stream (never observed on REST, confirming the REST document's suspicion).
From capture 1:

```json
{"asset_id": "34691510069031117755834214800869745291092253295564665516660269316660628637961",
 "price": "0.17", "size": "0", "side": "BUY",
 "hash": "0c9b33fa62a60c1fd5a934dc751a536a31768904",
 "best_bid": "0.28", "best_ask": "0.29"}
```

From capture 2 (`clob-ws-hash-reconciliation-2026-08-10T185418Z.json`), three
zero-size entries inside one batched update:

```json
{"price": "0.61", "size": "0", ...}
{"price": "0.56", "size": "0", ...}
{"price": "0.42", "size": "0", ...}
```

In every case, `size: "0"` appeared for a price level that was present with
nonzero size in the immediately preceding book state at that price (verified
by cross-referencing the fixture's own book snapshots — e.g. `0.17` had size
`60.32` in the level list before capture began). **This confirms the M2 exit
criterion's convention directly, on real traffic: a `price_change` entry with
`"size": "0"` represents removal of that price level, not a level literally
resting at zero.** There is no separate boolean or enum for removal — size
`"0"` is the sole signal, exactly as the documentation-only inference
predicted, now with real evidence rather than inference alone.

### Priority question 4 — subscription protocol: documentation confirmed, plus one undocumented behavior

The documented connect URL, subscribe frame, and PING/PONG heartbeat all
worked exactly as specified:

- Connect: `wss://ws-subscriptions-clob.polymarket.com/ws/market` — succeeded
  immediately (`connected_at` in the fixture, ~60ms after socket open).
- Subscribe frame sent: `{"assets_ids": ["<token_id>"], "type": "market"}` —
  the server responded within ~60ms with an **array containing one `book`
  event** for the subscribed token (see "message types" below). So **yes, the
  server sends an initial snapshot on subscribe**, confirmed directly.
- Heartbeat: `PING` sent as a plain-text (non-JSON) frame every 10 seconds
  from the client; the server replied `PONG` (also plain text) each time — 4
  sends, 4 replies in the 45-second capture, matching the 10-second interval
  exactly. **Confirmed working as documented.**

**Undocumented behavior found by observation**: subscribing to a single token
id delivers `price_change` messages for **both** outcome tokens of the same
binary market — the subscribed YES token
(`34691...8637961`) and its unsubscribed NO sibling
(`95561...5977699`) both appeared as `asset_id` values inside
`price_changes` arrays in capture 1. This makes mechanical sense (a binary
market's two prices are complementary, so a price move on one side is a price
move on the other), but it is **not stated anywhere in the documentation
read for this report**. Critically, the **`book` (full snapshot) event type
never arrived for the unsubscribed sibling** — only `price_change` deltas did.
So an adapter that wants a correct, self-contained order book for a token
**must subscribe to that token explicitly**; incidental `price_change`
messages for an unsubscribed sibling are not sufficient to reconstruct its
book (no snapshot base, and unknown whether `tick_size_change` or other event
types would arrive for it either — **UNVERIFIED**, not observed in this
capture). Filtering incoming messages by `asset_id`/subscribed-token-set is
therefore a correctness requirement, not just a convenience, for any
multi-token connection.

Not tested in this session, both **UNVERIFIED**: the maximum number of token
ids per connection or per subscribe frame; the exact behavior of `operation:
"subscribe"`/`"unsubscribe"` frames on an already-open connection (the
documented shape was read but not exercised live, to keep this session's
footprint to one steady subscription plus heartbeat, per the research scope).

### Priority question 5 — message types observed live

Both captures together produced real examples of exactly two of the seven
documented types: `book` (4 total: one initial snapshot per capture, plus
periodic full-book refreshes interleaved with deltas — e.g. one appeared
mid-stream in capture 1 at `18:47:...`, not only at subscribe time) and
`price_change` (dozens total, batched and unbatched, as described above). No
`last_trade_price` or `tick_size_change` message arrived in the ~85 seconds of
combined capture — the token's book had heavy quote/level activity but the
observation window was not long enough to catch an actual trade or a tick
size change (tick size changes are rare, typically tied to a price crossing a
threshold). Both remain **documentation-only** for their exact field shapes;
the `market`/`asset_id` naming convention and string-typed price/size fields
are trusted by analogy with the confirmed `book` and `price_change` shapes and
with the REST book fixture, but this is an inference, flagged as such.

The three `custom_feature_enabled`-gated types (`best_bid_ask`, `new_market`,
`market_resolved`) were never requested in this session — the subscribe frame
used the default (`custom_feature_enabled` omitted), consistent with the
"public market channel only" scope; enabling it does not cross the
authentication boundary, so it would be safe to test in a future session, but
was left **UNVERIFIED** here since it was not needed to answer the priority
questions and the default-off `book`/`price_change` pair was sufficient
evidence for the exit criteria in question.

Observed field types in the live `book` event (capture 1, first message)
match the REST `/book` fixture exactly: `market` and `asset_id` as hex/decimal
strings, `timestamp` as a millisecond string, `hash` as a 40-hex-char string
with **no `0x` prefix** (contradicting the documentation's placeholder
`"0xabc123…"` for this exact field — see the documentation section above; this
is now confirmed by direct observation, not just inference from the REST
fixture's shape). `bids`/`asks` are arrays of `{price, size}` string pairs.
One structural difference from the REST snapshot: **the live `book` event
observed here omitted `min_order_size` and `neg_risk`**, which the REST
`/book` response and the documentation's own `book` event example both
include; it had `tick_size` and `last_trade_price` present. This is a single
observed instance, not a systematic check across many `book` events —
recorded as **UNVERIFIED whether this is consistent** (could be
value-dependent, e.g. omitted when null, or could be a genuine schema
difference between the WS and REST paths). A tolerant parser should treat
`min_order_size` and `neg_risk` as optional on the WebSocket `book` event even
though the REST equivalent always had them, rather than assuming schema
parity between the two paths.

### Priority question 6 — operational behaviour

- **Heartbeat**: confirmed client-initiated `PING`/`PONG` at 10s, see above.
  Idle timeout if the client stops pinging is **UNVERIFIED** — not tested, to
  avoid an avoidable disconnect/reconnect cycle against the live public
  service beyond what answering the priority questions required.
- **Disconnect behavior on close / reconnect replay**: **UNVERIFIED**. Both
  captures ended by the client closing the connection after its fixed
  duration; no reconnect was attempted, so whether the server resends
  anything on reconnect (e.g., a fresh `book` snapshot, which the subscribe
  behavior above suggests it would, since subscribing to a token always
  produced one) was not tested end-to-end as a reconnect scenario in this
  session. The subscribe-triggers-snapshot behavior confirmed above strongly
  suggests a reconnect-then-resubscribe recovers a consistent starting state,
  but this is inference from the subscribe behavior, not a direct reconnect
  test, and is recorded as **UNVERIFIED** rather than promoted to confirmed.
- **Rate limits**: **UNVERIFIED** for the WebSocket channel specifically (the
  REST document already flagged this for REST; the same gap applies here,
  and the observed 403 above is a User-Agent effect, not a rate-limit
  signal — no `429`-shaped response or WS close code tied to volume was
  observed).

### Priority question 7 — determinism hazards for replay

Directly relevant to core invariant 5/6 ("live and replay use the same domain
handlers"; "arrival order and event time are distinct") and to the M3 replay
work that will eventually consume this adapter's output:

1. **One logical book transition can arrive as more than one WebSocket
   frame**, confirmed above (`(timestamp, hash)` shared across two separate
   messages 196 microseconds apart). A domain handler that keys idempotency
   or "this is a new update" purely on receiving *a message* rather than on
   the `(timestamp, hash)` pair it carries would double-count or misorder.
   The event-time/arrival-order distinction in core invariant 6 must be
   built around `(timestamp, hash)`, not around "one WebSocket frame equals
   one event."
2. **`price_change` messages can carry multiple `price_changes` entries with
   different `asset_id` values in the same frame** (cross-token, as in
   priority question 4). A single WebSocket message is therefore not
   necessarily scoped to one token's stream; downstream fan-out by token must
   happen inside the adapter, not be assumed from the transport.
3. **No sequence number** (priority question 1) means gap detection is
   inherently heuristic on this channel too, exactly as it is on REST — a
   dropped WebSocket message is only detectable if a later `hash` fails to
   reconcile against locally-applied deltas, and even that requires trusting
   that the *content* of the deltas received so far was applied correctly.
   This is a strictly weaker guarantee than a sequence-numbered feed and
   should be documented as a known limitation of the adapter's gap-detection
   claims, not silently assumed away.
4. Message arrival order matched `timestamp` order in both captures (see
   priority question 1), so **no out-of-order delivery was observed** in this
   session — but the sample is small (two connections, under 90 combined
   seconds, one token, no reconnect, no network disruption). This is not
   sufficient to claim the property holds under load, multi-node fan-out, or
   reconnect, and should not be treated as a guarantee by the adapter design.
5. The undocumented cross-token behavior (question 4) is itself a determinism
   hazard if not handled explicitly: the exact set of `price_change` messages
   a connection receives depends on which *other* tokens happen to share a
   condition with the subscribed one, which is connection-state/topology
   dependent rather than purely a function of the subscribed token id. An
   adapter must filter by `asset_id` against its own intended token set,
   never assume "everything received on this connection belongs to what I
   subscribed to."

## Summary answers to the three questions carried over from the REST document

1. **Is there a sequence number?** No — confirmed absent by direct
   observation across two live captures, in addition to being undocumented.
   Gap detection must be built on `(timestamp, hash)` reconciliation, not a
   sequence field, on both REST and WebSocket.
2. **Does a delta carry the hash of the book state it produces?** Yes,
   confirmed twice independently: internally (WS `book` event's hash matches
   the preceding WS `price_change`'s hash) and externally (REST `/book`'s
   hash matches the preceding WS `price_change`'s hash, byte for byte, with a
   User-Agent caveat on the REST side). Snapshot and delta reconcile exactly,
   not heuristically — this is stronger than the REST document anticipated.
   One caveat for design: `(timestamp, hash)` can map to more than one wire
   frame, so reconciliation must be keyed on that pair, not on frame count.
3. **Zero-size levels represent removal.** Confirmed directly on live
   `price_change` messages (never observed on REST, consistent with the REST
   document's suspicion). No separate removal flag exists; `size: "0"` is the
   sole signal.

## UNVERIFIED — do not build on these without checking further

- Idle timeout duration if the client stops sending `PING`.
- Server behavior on reconnect: whether a fresh subscribe after a dropped
  connection reliably yields a consistent `book` snapshot with no missed
  interval, versus a genuine gap that must be detected via REST reconciliation.
- WebSocket-channel rate limits and any throttling/close-code behavior under
  high subscription counts or high message volume.
- Maximum token ids per connection or per subscribe frame.
- Whether `operation: "subscribe"`/`"unsubscribe"` frames behave exactly as
  documented (read, not exercised, in this session).
- Whether `tick_size_change`, `last_trade_price`, and the three
  `custom_feature_enabled` event types match their documented shapes on the
  wire — none arrived in the ~85 seconds of combined capture.
- Whether the live `book` event's omission of `min_order_size`/`neg_risk`
  (observed once) is systematic or incidental.
- The general rule behind the REST 403-without-User-Agent finding — only two
  User-Agent strings were tried.
- Whether `timestamp` ordering and `(timestamp, hash)` reconciliation hold
  under reconnect, multi-node fan-out, or sustained high load; only two short,
  single-connection, no-reconnect captures back the "no out-of-order delivery
  observed" claim.

## Provenance summary

- Documentation: `https://docs.polymarket.com/market-data/realtime-data.md`
  (also cross-referenced `https://docs.polymarket.com/market-data/prices-order-books.md`
  for the REST-side field/behavior parity check), retrieved via `curl` on
  2026-08-10.
- Live capture 1: `docs/research/fixtures/clob-ws-market-2026-08-10T184742Z.json`
  + `.meta.json`, 2026-08-10T18:47:42Z–18:48:27Z UTC.
- Live capture 2: `docs/research/fixtures/clob-ws-hash-reconciliation-2026-08-10T185418Z.json`
  + `.meta.json`, 2026-08-10T18:54:18Z–18:54:58Z UTC.
- Companion REST fixtures (pre-existing, not modified):
  `docs/research/fixtures/clob-book-yes-2026-08-10T181007Z.json` and
  `clob-related-endpoints-2026-08-10.json`, referenced for the hash-format and
  zero-size-level cross-checks above.
- Token/market discovery for capture selection:
  `GET https://gamma-api.polymarket.com/markets?limit=5&active=true&closed=false&order=volume24hr&ascending=false`,
  public and unauthenticated, 2026-08-10.
