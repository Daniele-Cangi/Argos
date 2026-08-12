"""Tests for `PriceChangeV1` and `parse_price_change_group`.

Built from the real recorded fixture
`docs/research/fixtures/clob-ws-market-2026-08-10T184742Z.json`, not only
synthetic data, per the M2 slice instructions. Two facts about that fixture
are verified here, not merely asserted from the research doc:

- Every `price_change` frame in the capture carries exactly one entry for
  each of the two observed tokens (the subscribed YES token and its
  unsubscribed binary sibling — `docs/research/m2-clob-websocket.md`,
  "Priority question 4"), so `parse_price_change_group` succeeds for both
  tokens on all 34 `price_change` frames (68 successful parses total,
  `test_every_price_change_frame_parses_for_both_observed_tokens`).
- Across the whole capture there are 58 distinct `(timestamp, hash)` pairs
  and none of them spans more than one `asset_id`
  (`test_no_timestamp_hash_pair_in_the_capture_spans_more_than_one_asset_id`)
  — this is *why* grouping by `asset_id` inside one frame yields a
  well-defined per-token post-state hash, and it is checked here rather than
  only asserted from the research note.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from argos.domain.observation import ObservationSource, build_observation_envelope
from argos.domain.orderbook import BookSide, normalize_decimal
from argos.domain.pricechange import (
    PriceChangeGroup,
    PriceChangeV1,
    PriceLevelChangeKind,
    PriceLevelChangeV1,
    parse_price_change_group,
)
from argos.domain.provenance import SourceProvenanceV1, sha256_hex

# A byte-identical copy of docs/research/fixtures/clob-ws-market-2026-08-10T184742Z.json,
# carrying a provenance sidecar. Read from here rather than from docs/research/
# so `tests/test_fixtures.py`'s drift guard actually covers it: a test whose
# ground truth lives only under docs/ can have its meaning silently changed by
# an edit no hash check ever sees. This is the same convention the CLOB REST
# adapter slice established for `tests/fixtures/clob/book_yes.raw.json`.
FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "clob" / "ws_market_price_change.raw.json"
)

TOKEN_YES = "34691510069031117755834214800869745291092253295564665516660269316660628637961"
TOKEN_NO = "95561057794427123541889915407555646439882912350845258651794843110787555977699"
CONDITION_ID = "0x94a39addec8bd24a2d03deeaa43bdee6a2b11eca403cd12495eb24132cc23173"

START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _load_fixture() -> dict[str, Any]:
    with FIXTURE_PATH.open() as handle:
        # parse_float=Decimal mirrors the discipline the adapter slice must
        # apply when it decodes the real WebSocket frame text; this module
        # does not assume that was done (see the float-rejection tests below).
        return json.load(handle, parse_float=Decimal)  # type: ignore[no-any-return]


def _price_change_events() -> list[tuple[int, dict[str, Any]]]:
    """Every real `price_change` event in the fixture, tagged with its message index.

    Each `messages[i]["raw"]` is either `"PING"`/`"PONG"` (skipped), a JSON
    array (only the very first message: one initial `book` snapshot), or a
    single JSON object (every subsequent message in this capture) — verified
    directly against the fixture, not assumed from the research note's prose.
    """
    fixture = _load_fixture()
    events: list[tuple[int, dict[str, Any]]] = []
    for index, message in enumerate(fixture["messages"]):
        raw = message["raw"]
        if raw in ("PING", "PONG"):
            continue
        parsed = json.loads(raw, parse_float=Decimal)
        candidates = parsed if isinstance(parsed, list) else [parsed]
        for event in candidates:
            if event.get("event_type") == "price_change":
                events.append((index, event))
    return events


def _event_at(message_index: int) -> dict[str, Any]:
    for index, event in _price_change_events():
        if index == message_index:
            return event
    raise AssertionError(f"no price_change event at message index {message_index}")


def _minimal_event(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_type": "price_change",
        "market": CONDITION_ID,
        "timestamp": "1786387666174",
        "price_changes": [
            {
                "asset_id": TOKEN_YES,
                "price": "0.49",
                "size": "636",
                "side": "SELL",
                "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                "best_bid": "0.28",
                "best_ask": "0.29",
            }
        ],
    }
    payload.update(overrides)
    return payload


def _provenance(raw: bytes) -> SourceProvenanceV1:
    return SourceProvenanceV1(
        source="clob_market_ws",
        endpoint="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        http_status=None,
        retrieved_at=START,
        raw_sha256=sha256_hex(raw),
        byte_length=len(raw),
    )


def _envelope_for(group: PriceChangeGroup, *, event_ms: str, ingest_sequence: int) -> Any:
    raw_bytes = json.dumps(group.payload.to_record()).encode()
    return build_observation_envelope(
        source=ObservationSource.CLOB_MARKET_WS,
        source_event_type="price_change",
        condition_id=group.payload.condition_id,
        token_id=group.payload.asset_id,
        event_time=datetime.fromtimestamp(int(event_ms) / 1000, tz=UTC),
        received_time=START,
        ingest_sequence=ingest_sequence,
        source_hash=group.entry_hash,
        payload=group.payload,
        provenance=_provenance(raw_bytes),
        parser_version="test-parser-1",
        capture_run_id="run-1",
    )


# --- real fixture: every price_change frame parses for both observed tokens ---------


def test_every_price_change_frame_parses_for_both_observed_tokens() -> None:
    events = _price_change_events()
    assert len(events) == 34

    parsed_for_yes = 0
    parsed_for_no = 0
    for _index, event in events:
        yes_group = parse_price_change_group(event, asset_id=TOKEN_YES)
        no_group = parse_price_change_group(event, asset_id=TOKEN_NO)
        assert yes_group.payload.asset_id == TOKEN_YES
        assert no_group.payload.asset_id == TOKEN_NO
        assert yes_group.payload.condition_id == CONDITION_ID.lower()
        parsed_for_yes += 1
        parsed_for_no += 1

    assert parsed_for_yes == 34
    assert parsed_for_no == 34


def test_no_timestamp_hash_pair_in_the_capture_spans_more_than_one_asset_id() -> None:
    """Verified directly against the fixture, not only asserted from the research note.

    This is *why* grouping by `asset_id` inside one frame is well-defined: if
    some pair were shared across the two tokens, a caller filtering only by
    `asset_id` could still end up mixing two tokens' entries under one hash.
    """
    pair_to_assets: dict[tuple[str, str], set[str]] = {}
    for _index, event in _price_change_events():
        timestamp = event["timestamp"]
        for entry in event["price_changes"]:
            key = (timestamp, entry["hash"])
            pair_to_assets.setdefault(key, set()).add(entry["asset_id"])

    assert len(pair_to_assets) == 58
    spanning = {key: assets for key, assets in pair_to_assets.items() if len(assets) > 1}
    assert spanning == {}


# --- zero-size means removal, observed directly on real traffic ---------------------


def test_the_real_fixtures_two_zero_size_entries_are_represented_as_removal() -> None:
    """The exact two real zero-size entries in this capture.

    `docs/research/m2-clob-websocket.md`, "Priority question 3": a
    `price_change` entry with `size: "0"` represents removal, confirmed live.
    """
    removals: list[tuple[str, BookSide, Decimal]] = []
    for _index, event in _price_change_events():
        for token in (TOKEN_YES, TOKEN_NO):
            group = parse_price_change_group(event, asset_id=token)
            for change in group.payload.changes:
                if change.kind is PriceLevelChangeKind.REMOVE:
                    removals.append((token, change.side, change.price))

    assert removals == [
        (TOKEN_YES, BookSide.BID, Decimal("0.17")),
        (TOKEN_NO, BookSide.ASK, Decimal("0.83")),
    ]


def test_removals_were_actually_observed_in_the_real_fixture() -> None:
    """Mirrors the REST suite's own negative-result test, in the opposite direction.

    `OrderBookSnapshotV1`'s suite pins that zero-size levels were *never*
    observed on the REST snapshot. This pins the opposite: they *were*
    observed, directly, on this delta stream — the confirmation
    `docs/research/m2-clob-websocket.md` reports.
    """
    saw_removal = False
    for _index, event in _price_change_events():
        for token in (TOKEN_YES, TOKEN_NO):
            group = parse_price_change_group(event, asset_id=token)
            if any(c.kind is PriceLevelChangeKind.REMOVE for c in group.payload.changes):
                saw_removal = True
    assert saw_removal is True


# --- the identity hazard: one (timestamp, hash) pair spread across three frames -----


def test_the_same_timestamp_hash_pair_spans_three_frames_with_three_different_price_changes() -> (
    None
):
    """Verified directly against the fixture before pinning it as a hazard below."""
    matches = []
    for index, event in _price_change_events():
        for entry in event["price_changes"]:
            if (
                entry["asset_id"] == TOKEN_YES
                and event["timestamp"] == "1786387666174"
                and entry["hash"] == "5ce704dea0a2123a388f1d8b432aad0058f5c479"
            ):
                matches.append((index, entry["price"], entry["size"], entry["side"]))

    assert matches == [
        (1, "0.49", "636", "SELL"),
        (2, "0.65", "142.85", "SELL"),
        (3, "0.48", "17", "SELL"),
    ]


def test_the_shared_timestamp_hash_pair_mints_three_distinct_observation_ids() -> None:
    """Pins the identity hazard measured in `docs/research/m2-clob-websocket.md`.

    For token `34691...8637961`, `(timestamp=1786387666174,
    hash=5ce704dea0a2123a388f1d8b432aad0058f5c479)` appears across three
    separate real WebSocket frames (`messages[1]`, `[2]`, `[3]`), each
    carrying one *different* price level change: `(0.49, 636, SELL)`, then
    `(0.65, 142.85, SELL)`, then `(0.48, 17, SELL)`.

    ADR-0010 identity (`_observation_identity`) hashes the *canonical
    payload* alongside `source_hash` and the event-time marker — both of
    which are identical across all three frames here, since they share one
    `(timestamp, hash)` pair. If `PriceChangeV1` did not carry the level
    change itself as payload content, all three frames would collapse onto
    one `observation_id` and two real, distinct level changes would be
    silently lost — exactly the failure this test exists to catch. Building
    real `ObservationEnvelopeV1` records (not just comparing canonical JSON
    by hand) exercises the actual mechanism ADR-0010 specifies.
    """
    frames = [_event_at(1), _event_at(2), _event_at(3)]
    envelopes = [
        _envelope_for(
            parse_price_change_group(event, asset_id=TOKEN_YES),
            event_ms=event["timestamp"],
            ingest_sequence=sequence,
        )
        for sequence, event in enumerate(frames, start=1)
    ]

    observation_ids = {envelope.observation_id for envelope in envelopes}
    assert len(observation_ids) == 3

    # And each envelope's payload really does carry a distinct level change,
    # not merely a distinct observation_id for some other reason.
    prices = [envelope.payload["changes"][0]["price"] for envelope in envelopes]
    assert prices == ["0.49", "0.65", "0.48"]


def test_a_byte_identical_redelivery_of_one_frame_reproduces_its_own_observation_id() -> None:
    """The other half of the hazard test: redelivery must still collide (M2 exit criterion)."""
    event = _event_at(1)
    group_first = parse_price_change_group(event, asset_id=TOKEN_YES)
    group_redelivered = parse_price_change_group(event, asset_id=TOKEN_YES)

    envelope_first = _envelope_for(group_first, event_ms=event["timestamp"], ingest_sequence=1)
    envelope_redelivered = _envelope_for(
        group_redelivered, event_ms=event["timestamp"], ingest_sequence=2
    )

    assert envelope_first.observation_id == envelope_redelivered.observation_id
    assert json.dumps(group_first.payload.to_record(), sort_keys=True) == json.dumps(
        group_redelivered.payload.to_record(), sort_keys=True
    )


# --- reuse of the shared decimal normalization: the third guard against this class ---


def test_reuses_shared_negative_zero_normalization_from_orderbook() -> None:
    """`docs/STATUS.md`'s sharpest recorded blind spot: this class has recurred
    twice independently (the observation envelope, then `OrderBookSnapshotV1`'s
    own negative-zero finding). This is the third guard, not a fourth instance:
    `PriceLevelChangeV1` must reuse `parse_wire_decimal`/`normalize_decimal`
    rather than reimplement decimal parsing.
    """
    for spelling in ("-0", "-0.0", "-0E+5", "-0.00000"):
        change = PriceLevelChangeV1(
            side=BookSide.BID,
            price=Decimal(spelling),
            size=Decimal(spelling),
            kind=PriceLevelChangeKind.REMOVE,
        )
        assert str(change.price) == "0"
        assert str(change.size) == "0"
        assert json.dumps(change.model_dump(mode="json")) == json.dumps(
            {"side": "bid", "price": "0", "size": "0", "kind": "remove"}
        )


def test_reuses_shared_trailing_zero_normalization_from_orderbook() -> None:
    padded = parse_price_change_group(
        _minimal_event(
            price_changes=[
                {
                    "asset_id": TOKEN_YES,
                    "price": "0.430",
                    "size": "10",
                    "side": "SELL",
                    "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                    "best_bid": "0.28",
                    "best_ask": "0.29",
                }
            ]
        ),
        asset_id=TOKEN_YES,
    )
    minimal = parse_price_change_group(
        _minimal_event(
            price_changes=[
                {
                    "asset_id": TOKEN_YES,
                    "price": "0.43",
                    "size": "10",
                    "side": "SELL",
                    "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                    "best_bid": "0.28",
                    "best_ask": "0.29",
                }
            ]
        ),
        asset_id=TOKEN_YES,
    )
    assert padded.payload.changes[0].price == minimal.payload.changes[0].price
    assert json.dumps(padded.payload.to_record(), sort_keys=True) == json.dumps(
        minimal.payload.to_record(), sort_keys=True
    )


def test_normalize_decimal_and_parse_wire_decimal_are_the_same_functions_orderbook_uses() -> None:
    """A direct identity check, not just behavioural agreement, that this module
    imports rather than reimplements the shared helpers."""
    import argos.domain.orderbook as orderbook_module
    import argos.domain.pricechange as pricechange_module

    assert pricechange_module.parse_wire_decimal is orderbook_module.parse_wire_decimal
    assert normalize_decimal is orderbook_module.normalize_decimal


# --- no float, no bool, ever ----------------------------------------------------------


def test_a_float_price_is_refused() -> None:
    with pytest.raises(ValueError, match="float"):
        parse_price_change_group(
            _minimal_event(
                price_changes=[
                    {
                        "asset_id": TOKEN_YES,
                        "price": 0.49,
                        "size": "636",
                        "side": "SELL",
                        "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                        "best_bid": "0.28",
                        "best_ask": "0.29",
                    }
                ]
            ),
            asset_id=TOKEN_YES,
        )


def test_a_float_size_is_refused() -> None:
    with pytest.raises(ValueError, match="float"):
        parse_price_change_group(
            _minimal_event(
                price_changes=[
                    {
                        "asset_id": TOKEN_YES,
                        "price": "0.49",
                        "size": 636.0,
                        "side": "SELL",
                        "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                        "best_bid": "0.28",
                        "best_ask": "0.29",
                    }
                ]
            ),
            asset_id=TOKEN_YES,
        )


def test_a_bool_price_is_refused() -> None:
    with pytest.raises(ValueError, match="boolean"):
        parse_price_change_group(
            _minimal_event(
                price_changes=[
                    {
                        "asset_id": TOKEN_YES,
                        "price": True,
                        "size": "636",
                        "side": "SELL",
                        "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                        "best_bid": "0.28",
                        "best_ask": "0.29",
                    }
                ]
            ),
            asset_id=TOKEN_YES,
        )


def test_a_float_best_bid_is_refused() -> None:
    with pytest.raises(ValueError, match="float"):
        parse_price_change_group(
            _minimal_event(
                price_changes=[
                    {
                        "asset_id": TOKEN_YES,
                        "price": "0.49",
                        "size": "636",
                        "side": "SELL",
                        "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                        "best_bid": 0.28,
                        "best_ask": "0.29",
                    }
                ]
            ),
            asset_id=TOKEN_YES,
        )


# --- side mapping: BUY -> BID, SELL -> ASK, nothing else silently coerced ------------


def test_buy_maps_to_bid_and_sell_maps_to_ask() -> None:
    group = parse_price_change_group(
        _minimal_event(
            price_changes=[
                {
                    "asset_id": TOKEN_YES,
                    "price": "0.49",
                    "size": "636",
                    "side": "BUY",
                    "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                    "best_bid": "0.28",
                    "best_ask": "0.29",
                }
            ]
        ),
        asset_id=TOKEN_YES,
    )
    assert group.payload.changes[0].side is BookSide.BID


def test_an_unrecognized_side_is_refused_not_defaulted() -> None:
    with pytest.raises(ValueError, match=r"BUY.*SELL"):
        parse_price_change_group(
            _minimal_event(
                price_changes=[
                    {
                        "asset_id": TOKEN_YES,
                        "price": "0.49",
                        "size": "636",
                        "side": "bid",
                        "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                        "best_bid": "0.28",
                        "best_ask": "0.29",
                    }
                ]
            ),
            asset_id=TOKEN_YES,
        )


# --- kind must never disagree with size: recomputable, never drifting ---------------


def test_a_zero_size_change_claiming_kind_set_is_refused() -> None:
    with pytest.raises(ValidationError, match="REMOVE"):
        PriceLevelChangeV1(
            side=BookSide.BID, price=Decimal("0.5"), size=Decimal(0), kind=PriceLevelChangeKind.SET
        )


def test_a_nonzero_size_change_claiming_kind_remove_is_refused() -> None:
    with pytest.raises(ValidationError, match="SET"):
        PriceLevelChangeV1(
            side=BookSide.BID,
            price=Decimal("0.5"),
            size=Decimal("10"),
            kind=PriceLevelChangeKind.REMOVE,
        )


# --- negative size refuses the whole group, matching orderbook.py -------------------


def test_a_negative_size_refuses_the_whole_group() -> None:
    with pytest.raises(ValueError, match="negative"):
        parse_price_change_group(
            _minimal_event(
                price_changes=[
                    {
                        "asset_id": TOKEN_YES,
                        "price": "0.49",
                        "size": "-1",
                        "side": "SELL",
                        "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                        "best_bid": "0.28",
                        "best_ask": "0.29",
                    }
                ]
            ),
            asset_id=TOKEN_YES,
        )


# --- duplicate (side, price) has no non-arbitrary resolution ------------------------


def test_a_duplicate_side_price_in_one_group_is_refused() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        PriceChangeV1(
            condition_id=CONDITION_ID,
            asset_id=TOKEN_YES,
            changes=(
                PriceLevelChangeV1(
                    side=BookSide.ASK,
                    price=Decimal("0.5"),
                    size=Decimal("10"),
                    kind=PriceLevelChangeKind.SET,
                ),
                PriceLevelChangeV1(
                    side=BookSide.ASK,
                    price=Decimal("0.5"),
                    size=Decimal("20"),
                    kind=PriceLevelChangeKind.SET,
                ),
            ),
        )


def test_changes_out_of_canonical_order_are_refused_not_silently_resorted() -> None:
    with pytest.raises(ValidationError, match="sorted"):
        PriceChangeV1(
            condition_id=CONDITION_ID,
            asset_id=TOKEN_YES,
            changes=(
                PriceLevelChangeV1(
                    side=BookSide.ASK,
                    price=Decimal("0.6"),
                    size=Decimal("10"),
                    kind=PriceLevelChangeKind.SET,
                ),
                PriceLevelChangeV1(
                    side=BookSide.ASK,
                    price=Decimal("0.5"),
                    size=Decimal("10"),
                    kind=PriceLevelChangeKind.SET,
                ),
            ),
        )


# --- disagreement on source top-of-book has no non-arbitrary resolution -------------


def test_entries_disagreeing_on_best_bid_are_refused() -> None:
    with pytest.raises(ValueError, match="best_bid"):
        parse_price_change_group(
            {
                "event_type": "price_change",
                "market": CONDITION_ID,
                "timestamp": "1786387666174",
                "price_changes": [
                    {
                        "asset_id": TOKEN_YES,
                        "price": "0.49",
                        "size": "636",
                        "side": "SELL",
                        "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                        "best_bid": "0.28",
                        "best_ask": "0.29",
                    },
                    {
                        "asset_id": TOKEN_YES,
                        "price": "0.48",
                        "size": "17",
                        "side": "SELL",
                        "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                        "best_bid": "0.27",
                        "best_ask": "0.29",
                    },
                ],
            },
            asset_id=TOKEN_YES,
        )


def test_entries_agreeing_after_normalization_are_not_treated_as_disagreeing() -> None:
    """ "0.28" and "0.280" are the same normalized price; must not spuriously refuse."""
    group = parse_price_change_group(
        {
            "event_type": "price_change",
            "market": CONDITION_ID,
            "timestamp": "1786387666174",
            "price_changes": [
                {
                    "asset_id": TOKEN_YES,
                    "price": "0.49",
                    "size": "636",
                    "side": "SELL",
                    "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                    "best_bid": "0.28",
                    "best_ask": "0.29",
                },
                {
                    "asset_id": TOKEN_YES,
                    "price": "0.48",
                    "size": "17",
                    "side": "SELL",
                    "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                    "best_bid": "0.280",
                    "best_ask": "0.29",
                },
            ],
        },
        asset_id=TOKEN_YES,
    )
    assert group.payload.source_best_bid == Decimal("0.28")
    assert len(group.payload.changes) == 2


# --- an unobserved shape: more than one hash for one token in one frame -------------


def test_more_than_one_hash_for_one_token_in_one_frame_is_refused() -> None:
    """Never observed in either live capture behind `docs/research/m2-clob-websocket.md`.

    Refusing this unobserved shape, rather than inventing a grouping policy
    for it, is the deliberate design decision documented in
    `parse_price_change_group`'s own docstring.
    """
    with pytest.raises(ValueError, match="more than one distinct hash"):
        parse_price_change_group(
            {
                "event_type": "price_change",
                "market": CONDITION_ID,
                "timestamp": "1786387666174",
                "price_changes": [
                    {
                        "asset_id": TOKEN_YES,
                        "price": "0.49",
                        "size": "636",
                        "side": "SELL",
                        "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                        "best_bid": "0.28",
                        "best_ask": "0.29",
                    },
                    {
                        "asset_id": TOKEN_YES,
                        "price": "0.48",
                        "size": "17",
                        "side": "SELL",
                        "hash": "eb66a126a58e5a4d3c94abb48b71594d990b89c0",
                        "best_bid": "0.28",
                        "best_ask": "0.29",
                    },
                ],
            },
            asset_id=TOKEN_YES,
        )


# --- structural refusals -------------------------------------------------------------


def test_a_non_object_event_is_refused() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        parse_price_change_group(["not", "an", "object"], asset_id=TOKEN_YES)  # type: ignore[arg-type]


def test_wrong_event_type_is_refused() -> None:
    with pytest.raises(ValueError, match="event_type"):
        parse_price_change_group(_minimal_event(event_type="book"), asset_id=TOKEN_YES)


def test_price_changes_must_be_a_list() -> None:
    with pytest.raises(ValueError, match="array"):
        parse_price_change_group(_minimal_event(price_changes={}), asset_id=TOKEN_YES)


def test_no_matching_asset_id_is_refused() -> None:
    with pytest.raises(ValueError, match="no price_changes entries"):
        parse_price_change_group(_minimal_event(), asset_id="999999999999")


def test_an_unexpected_field_on_an_entry_is_refused() -> None:
    with pytest.raises(ValueError, match="unexpected fields"):
        parse_price_change_group(
            _minimal_event(
                price_changes=[
                    {
                        "asset_id": TOKEN_YES,
                        "price": "0.49",
                        "size": "636",
                        "side": "SELL",
                        "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                        "best_bid": "0.28",
                        "best_ask": "0.29",
                        "extra_field": "unexpected",
                    }
                ]
            ),
            asset_id=TOKEN_YES,
        )


def test_a_missing_required_key_on_an_entry_is_refused() -> None:
    with pytest.raises(ValueError, match="missing"):
        parse_price_change_group(
            _minimal_event(
                price_changes=[
                    {
                        "asset_id": TOKEN_YES,
                        "size": "636",
                        "side": "SELL",
                        "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                        "best_bid": "0.28",
                        "best_ask": "0.29",
                    }
                ]
            ),
            asset_id=TOKEN_YES,
        )


# --- condition_id / asset_id validation, matching orderbook.py exactly --------------


def test_condition_id_is_lowercased() -> None:
    group = parse_price_change_group(
        _minimal_event(market=CONDITION_ID.upper()), asset_id=TOKEN_YES
    )
    assert group.payload.condition_id == CONDITION_ID.lower()


def test_a_malformed_condition_id_is_refused() -> None:
    with pytest.raises(ValidationError, match="condition_id"):
        parse_price_change_group(_minimal_event(market="not-a-condition-id"), asset_id=TOKEN_YES)


def test_a_malformed_asset_id_is_refused() -> None:
    with pytest.raises(ValidationError, match="asset_id"):
        parse_price_change_group(
            _minimal_event(
                price_changes=[
                    {
                        "asset_id": "not-a-token-id",
                        "price": "0.49",
                        "size": "636",
                        "side": "SELL",
                        "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                        "best_bid": "0.28",
                        "best_ask": "0.29",
                    }
                ]
            ),
            asset_id="not-a-token-id",
        )


# --- price range and empty-changes refusal -------------------------------------------


def test_a_price_outside_zero_one_is_refused() -> None:
    with pytest.raises(ValueError, match="outside the valid"):
        parse_price_change_group(
            _minimal_event(
                price_changes=[
                    {
                        "asset_id": TOKEN_YES,
                        "price": "1.01",
                        "size": "636",
                        "side": "SELL",
                        "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                        "best_bid": "0.28",
                        "best_ask": "0.29",
                    }
                ]
            ),
            asset_id=TOKEN_YES,
        )


def test_an_empty_changes_tuple_is_refused() -> None:
    with pytest.raises(ValidationError):
        PriceChangeV1(condition_id=CONDITION_ID, asset_id=TOKEN_YES, changes=())


# --- round trip through to_record()/from_record() -----------------------------------


def test_round_trips_through_to_record_and_from_record() -> None:
    group = parse_price_change_group(_event_at(1), asset_id=TOKEN_YES)
    record = group.payload.to_record()
    restored = PriceChangeV1.from_record(record)
    assert restored == group.payload
    assert restored.to_record() == record


# --- malformed sibling entries are refused, never silently skipped -------------------
#
# Found during integration review of this slice, not by the agent that wrote
# the module: the first implementation filtered `price_changes` with
# `isinstance(entry, Mapping) and entry.get("asset_id") == asset_id`, so an
# entry that was not an object, or carried no `asset_id`, was skipped in
# silence. Because the caller invokes this function once per token, such an
# entry would have been discarded for *every* token — never counted, never
# reasoned, which is the silent drop core invariant 14 forbids. It is now
# refused, matching `argos.domain.orderbook._build_level`.


def test_a_non_object_entry_is_refused_rather_than_skipped() -> None:
    good_entry = {
        "asset_id": TOKEN_YES,
        "price": "0.49",
        "size": "636",
        "side": "SELL",
        "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
        "best_bid": "0.28",
        "best_ask": "0.29",
    }
    with pytest.raises(ValueError, match="must be a JSON object"):
        parse_price_change_group(
            _minimal_event(price_changes=[good_entry, "not-an-object"]),
            asset_id=TOKEN_YES,
        )


def test_an_entry_without_an_asset_id_is_refused_rather_than_skipped() -> None:
    """The requested token's own entry is valid; the frame is still refused.

    This is the point of the finding: the parse would otherwise have succeeded
    and reported nothing wrong, while the frame carried an entry ARGOS could
    not attribute to any token at all.
    """
    good_entry = {
        "asset_id": TOKEN_YES,
        "price": "0.49",
        "size": "636",
        "side": "SELL",
        "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
        "best_bid": "0.28",
        "best_ask": "0.29",
    }
    orphan_entry = {
        "price": "0.51",
        "size": "636",
        "side": "BUY",
        "hash": "9b697d845171b76940bdbd2ed1e561922558210e",
        "best_bid": "0.71",
        "best_ask": "0.72",
    }
    with pytest.raises(ValueError, match="no usable 'asset_id'"):
        parse_price_change_group(
            _minimal_event(price_changes=[good_entry, orphan_entry]),
            asset_id=TOKEN_YES,
        )


def test_a_valid_sibling_entry_is_still_ignored_not_refused() -> None:
    """The guard above must not become "refuse every frame with a sibling".

    Cross-token frames are normal on this channel
    (`docs/research/m2-clob-websocket.md`, "Priority question 4"): every real
    frame in the capture carries the unsubscribed sibling. Only entries that
    cannot be attributed to *any* token are refused.
    """
    group = parse_price_change_group(_event_at(1), asset_id=TOKEN_YES)
    assert group.payload.asset_id == TOKEN_YES
    assert all(change.side is BookSide.ASK for change in group.payload.changes)
