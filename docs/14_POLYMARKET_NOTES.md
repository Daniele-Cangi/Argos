# Polymarket implementation notes

Last verified against official documentation: **2026-08-06**. Re-check official docs before implementation because API schemas and endpoints can change.

## API topology

- Gamma API: market/event discovery and metadata.
- Data API: public trades, activity, holders, open interest, and related analytics.
- CLOB API: public order books, prices, midpoints, spreads, price history; authenticated operations are out of scope.
- Market WebSocket: public order-book, price, trade, and lifecycle events.

Official introduction:
`https://docs.polymarket.com/api-reference/introduction`

## Public market WebSocket

Endpoint documented as:

`wss://ws-subscriptions-clob.polymarket.com/ws/market`

Subscribe by outcome token IDs. The documented message families include:

- `book`
- `price_change`
- `last_trade_price`
- `tick_size_change`
- `best_bid_ask` when custom features are enabled
- `new_market` when custom features are enabled
- `market_resolved` when custom features are enabled

Official references:

- `https://docs.polymarket.com/market-data/websocket/overview`
- `https://docs.polymarket.com/market-data/websocket/market-channel`

## Market metadata

Gamma market metadata can include question, description, condition ID, resolution source, end date, outcomes, prices, volume, active/closed state, and CLOB token IDs. Validate actual payloads; some fields may be encoded strings containing arrays.

Official reference:
`https://docs.polymarket.com/api-reference/markets/get-market-by-id`

## Prices

Polymarket documents prices as implied probabilities. The displayed price is generally bid/ask midpoint, with last trade used when spread is wider than the platform threshold. A user cannot necessarily transact at displayed price.

ARGOS must separately store:

- display method;
- midpoint;
- best bid;
- best ask;
- last trade;
- spread;
- depth/size assumption.

Official references:

- `https://docs.polymarket.com/concepts/prices-orderbook`
- `https://docs.polymarket.com/trading/orderbook`

## Resolution

The market title is not the final semantic authority. Resolution rules specify source, end date, and edge cases. Polymarket documents UMA Optimistic Oracle resolution, disputes, and possible clarification.

ARGOS must preserve the exact rule material available at forecast time.

Official reference:
`https://docs.polymarket.com/concepts/resolution`

## Authentication boundary

Gamma, Data API, and public CLOB reads require no authentication according to current docs. Trading and user-channel functionality require credentials and are prohibited through M4.

Official references:

- `https://docs.polymarket.com/api-reference/authentication`
- `https://docs.polymarket.com/market-data/websocket/user-channel`

## Rate limits

Implement configurable rate limiting and backoff based on current official limits rather than hardcoding assumptions from this note.

Official reference:
`https://docs.polymarket.com/api-reference/rate-limits`
