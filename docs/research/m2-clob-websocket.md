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

