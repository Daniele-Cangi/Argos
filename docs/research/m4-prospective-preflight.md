# M4 prospective public-source preflight — 2026-08-19

This is a bounded readiness probe, not an included experiment run. All source
access was public and read-only; no user channel, credential, wallet, order or
authenticated trading endpoint was used.

## Discovery and lifecycle source

An official Gamma `/markets` query bounded by end time and minimum liquidity
returned 100 near-term candidates. Its exact response was archived locally at
`.data/preflight/gamma-near-term-20260819T1945Z.raw.json`:

- bytes: `831703`;
- SHA-256: `d0b4bacb7c24340c964251ab876464c5d35dac15b236cbf5e907f4bce7eab1d0`.

The sample contained liquid, CLOB-enabled binary sports/esports markets with
both token ids and useful near-term end times. A separate closed-market schema
probe was archived at
`.data/preflight/gamma-recent-closed-20260819T1950Z.raw.json` (538803 bytes,
SHA-256 `49d7d23ecd3fcb4cd0e5b679d4c820f98ad7f0dfe5976b648582ff8327ea0de8`).
It confirmed that source update time, closed state, exact outcome prices and UMA
status are distinct facts. A generic `updatedAt` is therefore not promoted to
a terminal cutoff.

The pilot uses `retrieved_at` of the first immutably persisted lifecycle payload
that normalizes to final settlement. Each poll must retain its raw source bytes,
endpoint, retrieval time and predecessor link.

## WebSocket capture probe

A 45-second public market-channel capture subscribed to both tokens of two
near-term targets, capped at 500 frames. It produced:

- 37 source frames;
- 40 stored events;
- 76 accepted dispatches;
- 84 not-applicable dispatches;
- zero rejections, unknown event types or duplicate deliveries;
- 74 archived raw payload files totaling 44,981 bytes.

The capture manifest is
`.data/preflight/m4-preflight-ws-20260819T1955Z.manifest.json` (SHA-256
`7421e7662919c2663b423df77f27615d6d247e610161fb3c7c732781c8f05b21`),
and the SQLite probe is SHA-256
`fbc16bc88dbe3320e00721734f372fdd71fe2d01636216a2676539ffdba7a514`.
The manifest binds clean revision
`ae649009667c06c6fc15ee088d515d60146ba5ca`, configuration fingerprint
`d0ee4294e61f2e69d7a843a71dcee9d46fc331125ffcb1b7abd3d5d307037be5`,
the four subscribed token ids and the bounded stopping parameters.

## Standalone `last_trade_price`

No standalone event of that type appeared. Subscribe-time snapshots did carry
`last_trade_price`, which the existing snapshot schema already preserves as
auxiliary evidence. Because no real standalone payload was observed, no fixture
or schema was invented from documentation.

The frozen pilot policy is target-level exclusion if a standalone occurrence
is unhandled. Each target is captured separately so an occurrence cannot hide
which target was affected or automatically invalidate unrelated evidence.

### Later prospective observation

The statement above is scoped only to this 45-second readiness probe. The later
V2 pilot captured real standalone events on both selected targets: the earliest
rejections were received at `2026-08-19T22:20:44.059730Z` and
`2026-08-19T22:24:48.145796Z`. Their exact raw payloads are content-addressed by
SHA-256 `9a6d43ed15d780707b4e47694337cd7ef4e4c4f4deb9e0d52a562810f9666601`
and `70877c8d3b91c31cc9ed310888c7e84e06bedec19ae1c31e1ef1c5ce7751794f`.
That evidence falsifies any universal reading of “no standalone event” while
leaving the bounded preflight observation true. The frozen exclusion policy was
applied to both targets; subsequent schema modeling cannot reinterpret those
captures.

## Readiness conclusion

The bounded path demonstrated discovery, contract inputs, public capture, raw
archival and a pollable lifecycle source. It did not itself prove prospective
admissibility because no final protocol/contract/target receipt preceded these
probe frames. Only a later run created after the frozen protocol commit may be
included.
