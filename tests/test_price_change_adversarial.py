"""Independent adversarial testing of `argos.domain.pricechange`.

Scope: falsify the hypotheses in the M2 slice instructions against real
recorded bytes (`tests/fixtures/clob/ws_market_price_change.raw.json`)
first, then synthetic attacks. Every finding below was reproduced directly
before being written up here; every negative result (an attack that did
*not* break the module) is recorded as explicitly as a finding, per the M2
testing rule.

Summary of what is filed here, with severity:

- **HIGH, real, reproduced, now FIXED in `argos.domain.orderbook`.**
  `parse_wire_decimal`/`normalize_decimal` escaped a bare
  `decimal.InvalidOperation` — an `ArithmeticError`, NOT a `ValueError` — for
  any decimal field whose coefficient needed more integer digits than the
  ambient decimal precision. Reachable with a **plain 29-digit integer
  string, no scientific notation required**. Because it is outside the ARGOS
  error taxonomy, it flew past `argos.ingestion.clob_book`'s `except
  ValueError`, so a **214-byte** body carrying `"tick_size": "1E+29"` escaped
  `normalize_clob_book` entirely and produced **no rejection ledger entry** —
  the silent drop core invariant 14 exists to prevent, reachable on the
  *already-committed* REST adapter, not only on this slice's new code.
  Investigating it surfaced the deeper half: `normalize()` and `quantize()`
  both read `decimal.getcontext()`, thread-local mutable global state, so one
  wire price rendered as **three different canonical texts** (and therefore
  three different `observation_id`s) under ambient precisions 5 / 28 / 50 —
  the hidden global state CLAUDE.md prohibits outright, and a direct break of
  core invariant 5, since a replay under a different ambient context would not
  reproduce the live identity. Fixed by pinning `CANONICAL_DECIMAL_CONTEXT`
  and refusing an explicit magnitude budget *before* any context-sensitive
  operation runs. The tests below were originally left FAILING to pin this,
  per the slice instructions, and are now rewritten to pin the fix.
- **MEDIUM, real, reproduced, now FIXED in `argos.domain.pricechange`.**
  Top-level `price_change` event keys were not checked against an expected set
  the way entry-level keys are: an unrecognized top-level key — including one
  spelled exactly like the sequence number this channel is confirmed to lack —
  was silently ignored, never counted, never reasoned. `EXPECTED_EVENT_KEYS`
  now closes it.
- **MEDIUM, contained.** `PriceLevelChangeV1` is a plain `pydantic.BaseModel`,
  not a `VersionedModel`, so it does not inherit `VersionedModel.model_copy`'s
  re-validation hardening: `PriceLevelChangeV1(...).model_copy(update=...)`
  can produce a standalone instance whose `kind` disagrees with its `size`,
  the exact invariant the module's own model validator exists to forbid.
  Measured as **contained**, not exploitable through this module's real
  construction paths: embedding that broken instance into a `PriceChangeV1`
  — via direct construction, `from_record`, *or* `PriceChangeV1.model_copy`
  itself (which does inherit the hardening) — re-validates it and refuses.
  `argos.domain.orderbook.OrderBookLevel`/`OrderBookAnomaly` carry the
  identical pattern, so this is not new to this slice.
- **MEDIUM, inherited, unconfirmed against live traffic.** `asset_id` is
  validated by `TOKEN_ID_PATTERN` (`[0-9]{1,120}`, shared with
  `argos.domain.orderbook`) but never normalized by integer value, so
  `"7"` and `"007"` are two different stored identities for what would be
  the same real-world token id — the M1 "duplicate token detection compared
  strings not integer values" class, unfixed here because it was never
  fixed in the shared validator either. No live capture has ever shown a
  leading-zero token id; recorded as a reachable shape, not an observed one.
- **Negative results, attacked and NOT broken**, in the order attacked below:
  decimal identity for every documented spelling on every decimal field this
  module owns (`price`, `size`, `source_best_bid`, `source_best_ask`),
  including through the full `build_observation_envelope` pipeline, not only
  direct model construction; the zero-size-means-REMOVE classification for
  `"0.00"`, `"-0"`, and `"0E-10"`; the observation-identity hazard (three real
  frames sharing one `(timestamp, hash)` pair mint three distinct ids, a
  byte-identical redelivery collides) extended to a fourth, wider check across
  the whole real fixture; a change to *only* `source_best_bid` with an
  otherwise-identical group changes the identity; every attempted collision
  inside the `changes` tuple's canonical JSON encoding (real JSON structure,
  not a custom separator, closes the ADR-0010 B1 class by construction);
  scrambled wire order; a hostile or 20,000,000-character `entry_hash`
  reaching `PriceChangeGroup.entry_hash` completely unbounded and
  unsanitized, but refused the moment it is used to build an envelope's
  `source_hash` (`ObservationEnvelopeV1._validate_identifier`); resource
  exhaustion on `price_changes` array length, measured, not merely observed
  to be "large" — see the module docstring above each test for the exact
  numbers.
"""

from __future__ import annotations

import decimal
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from argos.domain.observation import ObservationSource, build_observation_envelope
from argos.domain.orderbook import BookSide, normalize_decimal, parse_wire_decimal
from argos.domain.pricechange import (
    PriceChangeV1,
    PriceLevelChangeKind,
    PriceLevelChangeV1,
    parse_price_change_group,
)
from argos.domain.provenance import SourceProvenanceV1, sha256_hex

# The byte-identical, provenance-carrying copy under tests/fixtures/, so
# tests/test_fixtures.py's drift guard covers the ground truth these attacks
# are measured against.
FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "clob" / "ws_market_price_change.raw.json"
)

TOKEN_YES = "34691510069031117755834214800869745291092253295564665516660269316660628637961"
TOKEN_NO = "95561057794427123541889915407555646439882912350845258651794843110787555977699"
CONDITION_ID = "0x94a39addec8bd24a2d03deeaa43bdee6a2b11eca403cd12495eb24132cc23173"

START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _load_fixture() -> dict[str, Any]:
    with FIXTURE_PATH.open() as handle:
        return json.load(handle, parse_float=Decimal)  # type: ignore[no-any-return]


def _price_change_events() -> list[tuple[int, dict[str, Any]]]:
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


def _envelope_for(
    payload: PriceChangeV1, *, source_hash: str, event_ms: str, ingest_sequence: int
) -> Any:
    raw_bytes = json.dumps(payload.to_record()).encode()
    return build_observation_envelope(
        source=ObservationSource.CLOB_MARKET_WS,
        source_event_type="price_change",
        condition_id=payload.condition_id,
        token_id=payload.asset_id,
        event_time=datetime.fromtimestamp(int(event_ms) / 1000, tz=UTC),
        received_time=START,
        ingest_sequence=ingest_sequence,
        source_hash=source_hash,
        payload=payload,
        provenance=_provenance(raw_bytes),
        parser_version="test-parser-1",
        capture_run_id="run-1",
    )


# =====================================================================================
# HIGH — a bare decimal.InvalidOperation escapes the ValueError taxonomy the module
# promises, for a 29+-significant-digit decimal value on every decimal field.
# =====================================================================================
#
# Root cause, reproduced directly against `argos.domain.orderbook.normalize_decimal`
# (the shared function `argos.domain.pricechange` reuses, exactly as intended):
#
#   >>> Decimal("1" + "0" * 28).normalize()
#   Decimal('1E+28')      # <- normalize() itself is fine
#   >>> Decimal("1E+29").normalize().quantize(Decimal(1))
#   decimal.InvalidOperation: [<class 'decimal.InvalidOperation'>]
#
# `normalize_decimal`'s `quantize(Decimal(1))` call — reached whenever
# `normalize()` produces a positive exponent, to avoid "500" rendering as the
# distinct-looking "5E+2" — runs under Python's default `decimal` context,
# whose precision is 28 significant digits. A value needing 29 or more digits
# once trailing zeros are collapsed raises `decimal.InvalidOperation`, which
# subclasses `ArithmeticError`, not `ValueError`. Neither `parse_wire_decimal`
# nor `normalize_decimal` catches it, and a pydantic `mode="before"` field
# validator only has `ValueError`/`TypeError`/`AssertionError` wrapped into a
# `ValidationError` by pydantic itself — anything else propagates raw. Measured
# exactly at that boundary against `argos.domain.orderbook.parse_wire_decimal`
# directly:
#
#   digits  1..28  -> parses fine (e.g. 28 digits -> Decimal('1000...0'))
#   digits  29+    -> decimal.InvalidOperation, uncaught, every time
#
# No scientific notation is required to reach it — a plain 29-character digit
# string triggers it identically to "1E+29" — and it reproduces on all four
# decimal fields this module parses: `price`, `size`, `best_bid`, `best_ask`.
# `size` has no upper bound at all (`Field(ge=0)`), so this is reachable
# without even needing an out-of-range value to trip a *different* check
# first; the crash happens inside `parse_wire_decimal`, before any range
# validator ever runs.
#
# This is shared code (`argos.domain.orderbook.normalize_decimal`) — out of
# this file's ownership to fix. The tests below pin the module's own
# documented contract (`parse_price_change_group` docstring: "Raises
# ValueError ... for anything this function judges cannot be honestly
# represented") rather than the actual behaviour, and are left FAILING,
# loudly, per the slice instructions, rather than weakened to match the bug.


@pytest.mark.parametrize(
    ("field", "label"),
    [
        ("price", "price"),
        ("size", "size (unbounded above; no range check runs on this field)"),
        ("best_bid", "best_bid"),
        ("best_ask", "best_ask"),
    ],
)
def test_a_29_digit_decimal_value_never_escapes_as_a_bare_arithmetic_exception(
    field: str, label: str
) -> None:
    """Rewritten after the finding it pinned was FIXED in `orderbook.py`.

    This test was originally left failing on purpose to pin a real HIGH
    finding: a 29-digit plain integer string on any decimal field escaped as
    `decimal.InvalidOperation` — an `ArithmeticError`, NOT a `ValueError` —
    from `Decimal.quantize` inside the shared `normalize_decimal`. That is
    outside the ARGOS error taxonomy and therefore past
    `argos.ingestion.clob_book`'s `except ValueError`, so a 214-byte body
    carrying `"tick_size": "1E+29"` escaped `normalize_clob_book` entirely and
    produced NO rejection ledger entry — the silent drop core invariant 14
    exists to prevent, reachable on the already-committed REST adapter.

    `normalize_decimal` now runs inside a pinned decimal context and checks an
    explicit magnitude budget before any context-sensitive operation. The
    contract this test asserts is the one that actually matters and is
    field-independent: whatever happens, it is never a bare arithmetic
    exception. 29 digits is inside the budget, so each field now either accepts
    the value or refuses it as a `ValueError` from its own range validator.
    """
    hostile_digits = "1" + "0" * 28  # 29 significant digits; no sci notation needed
    entry: dict[str, Any] = {
        "asset_id": TOKEN_YES,
        "price": "0.49",
        "size": "636",
        "side": "SELL",
        "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
        "best_bid": "0.28",
        "best_ask": "0.29",
    }
    entry[field] = hostile_digits
    try:
        parse_price_change_group(_minimal_event(price_changes=[entry]), asset_id=TOKEN_YES)
    except ValueError:
        pass  # inside the taxonomy: a counted rejection downstream
    except ArithmeticError as error:  # pragma: no cover - the regression this pins
        raise AssertionError(
            f"{label}: escaped as {type(error).__module__}.{type(error).__name__}, "
            "not ValueError — outside the module's documented contract"
        ) from error


def test_a_value_past_the_canonical_budget_is_a_valueerror_not_an_arithmetic_error() -> None:
    """The magnitude budget itself, on the exact shape that used to crash.

    `"1E+29"` is 5 wire bytes; expanded to plain integer text it needs 30
    digits, which is what made `quantize` raise. It is now inside the budget
    and renders plainly, while `"1E+1000000"` — 11 wire bytes that would
    render as a one-megabyte canonical integer — is refused with a reason.
    Both paths are `ValueError`, never `ArithmeticError`.
    """
    assert normalize_decimal(Decimal("1E+29")) == Decimal(10) ** 29
    assert "E" not in str(normalize_decimal(Decimal("1E+29")))

    with pytest.raises(ValueError, match="canonical budget") as excinfo:
        normalize_decimal(Decimal("1E+1000000"))
    assert not isinstance(excinfo.value, ArithmeticError)

    with pytest.raises(ValueError, match="significant digits") as digits_error:
        normalize_decimal(Decimal("0." + "4" * 60 + "3"))
    assert not isinstance(digits_error.value, ArithmeticError)


def test_the_canonical_form_does_not_depend_on_the_ambient_decimal_context() -> None:
    """The deeper half of the same finding: hidden global state.

    `normalize()` and `quantize()` both read `decimal.getcontext()`, which is
    thread-local mutable global state. Reproduced before the fix: the wire
    price below rendered as three DIFFERENT canonical texts under ambient
    precisions 5, 28 and 50 — and therefore three different `observation_id`s
    for one set of wire bytes. That is the hidden global state CLAUDE.md
    prohibits outright, and it breaks core invariant 5, since a replay under a
    different ambient context would not reproduce the live identity.
    """
    source = "0.123456789012345678901234567890123"
    renderings = set()
    for precision in (5, 28, 50, 200):
        with decimal.localcontext() as context:
            context.prec = precision
            renderings.add(str(parse_wire_decimal(source)))
    assert renderings == {source}, f"canonical form varied with ambient precision: {renderings}"


def test_a_29_digit_size_on_a_directly_constructed_level_change_stays_in_the_taxonomy() -> None:
    """The same path reachable by direct model construction, not only by parsing.

    Confirms the fix landed in the shared decimal path (`orderbook.py`) rather
    than in frame parsing, which could not have guarded it alone.
    """
    change = PriceLevelChangeV1(
        side=BookSide.BID,
        price=Decimal("0.5"),
        size="1" + "0" * 28,  # type: ignore[arg-type]
        kind=PriceLevelChangeKind.SET,
    )
    assert change.size == Decimal(10) ** 28
    assert "E" not in str(change.size)

    with pytest.raises(ValueError) as excinfo:
        PriceLevelChangeV1(
            side=BookSide.BID,
            price=Decimal("0.5"),
            size="1E+1000000",  # type: ignore[arg-type]
            kind=PriceLevelChangeKind.SET,
        )
    assert not isinstance(excinfo.value, ArithmeticError)


def test_a_28_digit_value_is_exactly_the_surviving_boundary() -> None:
    """Negative result / boundary pin, not a bug report: 28 digits (the default
    `decimal` context precision) parses fine on every field this module owns.
    This is what makes the 29-digit case above a sharp boundary rather than a
    vague "big numbers are slow" claim."""
    twenty_eight_digits = "1" + "0" * 27
    group = parse_price_change_group(
        _minimal_event(
            price_changes=[
                {
                    "asset_id": TOKEN_YES,
                    "price": "0.49",
                    "size": twenty_eight_digits,
                    "side": "SELL",
                    "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                    "best_bid": "0.28",
                    "best_ask": "0.29",
                }
            ]
        ),
        asset_id=TOKEN_YES,
    )
    assert group.payload.changes[0].size == Decimal(twenty_eight_digits)


def test_a_huge_negative_exponent_does_not_crash_the_same_way() -> None:
    """Negative result: the quantize-triggered crash above is one-directional.
    A decimal with a huge *negative* exponent (`normalize()` never produces a
    positive exponent for it, so the `quantize` branch never fires) parses
    without incident, confirming the boundary is specifically "many
    significant digits after collapsing trailing zeros", not "any large
    exponent"."""
    group = parse_price_change_group(
        _minimal_event(
            price_changes=[
                {
                    "asset_id": TOKEN_YES,
                    "price": "0.49",
                    "size": "1E-50",
                    "side": "SELL",
                    "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                    "best_bid": "0.28",
                    "best_ask": "0.29",
                }
            ]
        ),
        asset_id=TOKEN_YES,
    )
    assert group.payload.changes[0].size == Decimal("1E-50")


# =====================================================================================
# Decimal identity, a third time — every documented spelling, every field this module
# owns, through the FULL parse_price_change_group -> build_observation_envelope
# pipeline, not only direct model construction (which tests/test_price_change.py
# already covers for `price`/`size`).
# =====================================================================================


@pytest.mark.parametrize(
    "spelling", ["-0", "-0.0", "-0E+5", "-0.00000", "0E+3", "+0.43", "0.4300", "00.75"]
)
def test_every_documented_zero_and_padding_spelling_normalizes_identically_on_price(
    spelling: str,
) -> None:
    """`price` has `ge=MIN_PRICE` (0), the exact reachability condition that made
    the previous negative-zero instance exploitable on `OrderBookSnapshotV1`.
    Every spelling here must parse (not refuse) and land at the same canonical
    text as its "obvious" equivalent. ``"00.75"`` stays inside the [0, 1] range
    on purpose — a leading-zero spelling that is also out of range would only
    exercise the unrelated range check, not decimal normalization."""
    equivalents = {
        "-0": "0",
        "-0.0": "0",
        "-0E+5": "0",
        "-0.00000": "0",
        "0E+3": "0",
        "+0.43": "0.43",
        "0.4300": "0.43",
        "00.75": "0.75",
    }
    group = parse_price_change_group(
        _minimal_event(
            price_changes=[
                {
                    "asset_id": TOKEN_YES,
                    "price": spelling,
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
    assert str(group.payload.changes[0].price) == equivalents[spelling]


@pytest.mark.parametrize("spelling", ["-0", "-0.0", "-0E+5", "0E+3"])
def test_every_zero_spelling_on_source_best_bid_and_ask_normalizes_to_plain_zero(
    spelling: str,
) -> None:
    """`source_best_bid`/`source_best_ask` share the exact `ge=MIN_PRICE` boundary
    that made the previous instance exploitable, and are parsed through a
    dedicated helper (`_parse_optional_top_of_book`) not exercised by
    `tests/test_price_change.py`'s decimal-identity tests at all."""
    group = parse_price_change_group(
        _minimal_event(
            price_changes=[
                {
                    "asset_id": TOKEN_YES,
                    "price": "0.49",
                    "size": "10",
                    "side": "SELL",
                    "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                    "best_bid": spelling,
                    "best_ask": spelling,
                }
            ]
        ),
        asset_id=TOKEN_YES,
    )
    assert str(group.payload.source_best_bid) == "0"
    assert str(group.payload.source_best_ask) == "0"


def test_a_negative_zero_price_survives_the_full_envelope_pipeline_at_one_identity() -> None:
    """The end-to-end version of the negative-zero check: two frames that spell
    the same economic price as `"-0"` and `"0"` respectively must mint the SAME
    `observation_id` through `build_observation_envelope`, not merely compare
    equal as `Decimal` objects in isolation."""
    negative_zero_event = _minimal_event(
        price_changes=[
            {
                "asset_id": TOKEN_YES,
                "price": "-0",
                "size": "10",
                "side": "SELL",
                "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                "best_bid": "0.28",
                "best_ask": "0.29",
            }
        ]
    )
    plain_zero_event = _minimal_event(
        price_changes=[
            {
                "asset_id": TOKEN_YES,
                "price": "0",
                "size": "10",
                "side": "SELL",
                "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                "best_bid": "0.28",
                "best_ask": "0.29",
            }
        ]
    )
    group_a = parse_price_change_group(negative_zero_event, asset_id=TOKEN_YES)
    group_b = parse_price_change_group(plain_zero_event, asset_id=TOKEN_YES)

    envelope_a = _envelope_for(
        group_a.payload, source_hash=group_a.entry_hash, event_ms="1786387666174", ingest_sequence=1
    )
    envelope_b = _envelope_for(
        group_b.payload, source_hash=group_b.entry_hash, event_ms="1786387666174", ingest_sequence=2
    )
    assert envelope_a.observation_id == envelope_b.observation_id


def test_reuse_is_the_same_normalize_decimal_orderbook_uses_not_a_shadow_copy() -> None:
    """Direct identity check (not merely behavioural agreement): guards against a
    future edit reintroducing a *second*, drifted copy of the normalizer under
    the same name."""
    import argos.domain.orderbook as orderbook_module
    import argos.domain.pricechange as pricechange_module

    assert pricechange_module.parse_wire_decimal is orderbook_module.parse_wire_decimal


# =====================================================================================
# The removal invariant: kind is REMOVE iff size == 0, attacked via every zero
# spelling and via model_copy.
# =====================================================================================


@pytest.mark.parametrize("zero_spelling", ["0.00", "-0", "0E-10", "0", "-0.0"])
def test_every_zero_size_spelling_classifies_as_remove_through_the_real_parser(
    zero_spelling: str,
) -> None:
    group = parse_price_change_group(
        _minimal_event(
            price_changes=[
                {
                    "asset_id": TOKEN_YES,
                    "price": "0.49",
                    "size": zero_spelling,
                    "side": "SELL",
                    "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                    "best_bid": "0.28",
                    "best_ask": "0.29",
                }
            ]
        ),
        asset_id=TOKEN_YES,
    )
    assert group.payload.changes[0].kind is PriceLevelChangeKind.REMOVE
    assert group.payload.changes[0].size == Decimal(0)


def test_model_copy_on_the_bare_level_change_bypasses_its_own_kind_size_invariant() -> None:
    """MEDIUM, contained — see the module docstring above.

    `PriceLevelChangeV1` is a plain `pydantic.BaseModel`, not a
    `VersionedModel`, so it does NOT inherit `VersionedModel.model_copy`'s
    re-validating override (`argos.domain.versioning.VersionedModel.model_copy`).
    pydantic's own `BaseModel.model_copy` is documented as not re-validating,
    and it genuinely does not here: this produces a standalone,
    in-memory-only instance whose `kind` disagrees with its `size`, which
    `PriceLevelChangeV1`'s own `_validate_kind_matches_size` model validator
    exists specifically to forbid on every *construction* path. Direct
    construction with the same fields is correctly refused, confirming this
    is model_copy specifically, not a general hole in the validator.
    """
    good = PriceLevelChangeV1(
        side=BookSide.BID, price=Decimal("0.5"), size=Decimal("10"), kind=PriceLevelChangeKind.SET
    )
    broken = good.model_copy(update={"size": Decimal(0)})
    assert broken.kind is PriceLevelChangeKind.SET
    assert broken.size == Decimal(0)  # the forbidden combination, now live in memory

    with pytest.raises(ValidationError, match="REMOVE"):
        PriceLevelChangeV1(
            side=BookSide.BID,
            price=Decimal("0.5"),
            size=Decimal(0),
            kind=PriceLevelChangeKind.SET,
        )


def test_embedding_a_model_copy_broken_level_change_into_price_change_v1_is_still_refused() -> None:
    """Negative result: the contained half of the finding above. Whichever of
    the three ways a caller might embed the broken standalone instance into a
    `PriceChangeV1` — direct construction, `from_record`, or
    `PriceChangeV1.model_copy` (which DOES inherit the hardening, being a
    `VersionedModel`) — the parent re-validates its nested submodels and
    refuses. Verified for all three paths, not assumed from one."""
    good = PriceLevelChangeV1(
        side=BookSide.BID, price=Decimal("0.5"), size=Decimal("10"), kind=PriceLevelChangeKind.SET
    )
    broken = good.model_copy(update={"size": Decimal(0)})

    # Path 1: direct construction.
    with pytest.raises(ValidationError, match="REMOVE"):
        PriceChangeV1(condition_id=CONDITION_ID, asset_id=TOKEN_YES, changes=(broken,))

    # Path 2: from_record, going through a plain dict rather than a live instance.
    hostile_record = {
        "schema_version": "price_change.v1",
        "condition_id": CONDITION_ID,
        "asset_id": TOKEN_YES,
        "changes": [{"side": "bid", "price": "0.5", "size": "0", "kind": "set"}],
        "source_best_bid": None,
        "source_best_ask": None,
    }
    with pytest.raises(ValidationError, match="REMOVE"):
        PriceChangeV1.from_record(hostile_record)

    # Path 3: PriceChangeV1.model_copy itself, which IS a VersionedModel and
    # therefore does inherit the re-validating override.
    valid_group = PriceChangeV1(condition_id=CONDITION_ID, asset_id=TOKEN_YES, changes=(good,))
    with pytest.raises(ValidationError, match="REMOVE"):
        valid_group.model_copy(update={"changes": (broken,)})


# =====================================================================================
# The identity hazard and its inverse.
# =====================================================================================


def test_all_three_real_frames_sharing_one_timestamp_hash_pair_mint_three_distinct_ids() -> None:
    """Extends `tests/test_price_change.py`'s version of this check across the
    full real fixture rather than the one known triple, and additionally
    checks pairwise inequality of the canonical payload JSON itself, not only
    `observation_id` — so a coincidental hash collision could not hide a
    silent payload collapse."""
    frames = [_event_at(1), _event_at(2), _event_at(3)]
    groups = [parse_price_change_group(event, asset_id=TOKEN_YES) for event in frames]
    envelopes = [
        _envelope_for(
            group.payload,
            source_hash=group.entry_hash,
            event_ms=event["timestamp"],
            ingest_sequence=sequence,
        )
        for sequence, (event, group) in enumerate(zip(frames, groups, strict=True), start=1)
    ]

    ids = [envelope.observation_id for envelope in envelopes]
    assert len(set(ids)) == 3

    canonical = [json.dumps(g.payload.to_record(), sort_keys=True) for g in groups]
    assert len(set(canonical)) == 3


def test_changing_only_source_best_bid_changes_the_identity() -> None:
    """Negative result: `source_best_bid` is inside the canonical payload, so a
    group differing ONLY in the source's asserted best_bid (identical
    `changes`) must mint a different `observation_id` — otherwise a genuine
    top-of-book disagreement between two deliveries would silently collapse."""
    base = _minimal_event()
    shifted = _minimal_event(
        price_changes=[
            {
                "asset_id": TOKEN_YES,
                "price": "0.49",
                "size": "636",
                "side": "SELL",
                "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                "best_bid": "0.27",  # only this changed
                "best_ask": "0.29",
            }
        ]
    )
    group_a = parse_price_change_group(base, asset_id=TOKEN_YES)
    group_b = parse_price_change_group(shifted, asset_id=TOKEN_YES)
    assert group_a.payload.source_best_bid != group_b.payload.source_best_bid

    envelope_a = _envelope_for(
        group_a.payload, source_hash=group_a.entry_hash, event_ms="1786387666174", ingest_sequence=1
    )
    envelope_b = _envelope_for(
        group_b.payload, source_hash=group_b.entry_hash, event_ms="1786387666174", ingest_sequence=2
    )
    assert envelope_a.observation_id != envelope_b.observation_id


def test_no_length_prefix_style_collision_is_reachable_inside_the_changes_tuple() -> None:
    """Attack the ADR-0010 B1 class (a separator-joined field boundary that a
    source-controlled string can shift) *inside* the payload, not only on the
    envelope's own identity fields, which B1 already fixed. `changes` renders
    as a real JSON array of typed objects (`price`, `size`, `side`, `kind`),
    not a custom-separator-joined string, so there is no field-boundary text
    for a source value to shift into. Attempted construction: two groups whose
    `changes` differ (a 2-level group vs. a 1-level group with a
    correspondingly "longer" price/size spelling) are checked to never produce
    identical canonical JSON. Negative result: JSON structure closes this
    class by construction, unlike the envelope's original `\\x1f`-joined
    material B1 fixed."""
    two_levels = _minimal_event(
        price_changes=[
            {
                "asset_id": TOKEN_YES,
                "price": "0.4",
                "size": "9",
                "side": "SELL",
                "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                "best_bid": "0.28",
                "best_ask": "0.29",
            },
            {
                "asset_id": TOKEN_YES,
                "price": "0.5",
                "size": "1",
                "side": "SELL",
                "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                "best_bid": "0.28",
                "best_ask": "0.29",
            },
        ]
    )
    # A single level whose price/size digits, naively concatenated, would spell
    # the same characters as the two levels above concatenated ("0.4" + "9" +
    # "0.5" + "1" vs one level built to look similar under naive joining).
    one_level = _minimal_event(
        price_changes=[
            {
                "asset_id": TOKEN_YES,
                "price": "0.49",
                "size": "0.51",
                "side": "SELL",
                "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                "best_bid": "0.28",
                "best_ask": "0.29",
            },
        ]
    )
    group_a = parse_price_change_group(two_levels, asset_id=TOKEN_YES)
    group_b = parse_price_change_group(one_level, asset_id=TOKEN_YES)
    assert json.dumps(group_a.payload.to_record(), sort_keys=True) != json.dumps(
        group_b.payload.to_record(), sort_keys=True
    )
    assert len(group_a.payload.changes) == 2
    assert len(group_b.payload.changes) == 1


# =====================================================================================
# Silent drops beyond the one already fixed (non-object / no-asset_id entries).
# =====================================================================================


def test_an_unrecognized_top_level_event_key_is_refused_not_silently_dropped() -> None:
    """Rewritten after the MEDIUM finding it pinned was FIXED.

    Entry-level keys were checked against `EXPECTED_ENTRY_KEYS` and an
    unrecognized one refused the whole frame, but the *event's own* top-level
    keys got no equivalent check: anything beyond `event_type`, `market`,
    `timestamp`, `price_changes` vanished with no count and no reason — core
    invariant 14's silent drop, and the second instance of that class found in
    this one slice.

    The key spelled `sequence` below is the reason this mattered more than
    tidiness: `docs/research/m2-clob-websocket.md` confirms by direct
    observation that this channel currently has NO sequence number, which is
    why gap detection has to be built on `(timestamp, hash)` reconciliation.
    If the source ever started sending one, the old behaviour would have
    discarded it silently and ARGOS would have gone on reconciling the hard
    way, with nothing anywhere recording that a better signal had arrived and
    been thrown away.

    `EXPECTED_EVENT_KEYS` now closes it, at the cost that a source schema
    change halts ingestion loudly — the same trade already made one level
    down, and the visible failure is the point.
    """
    event = _minimal_event()
    event["sequence"] = 42
    event["server_generated_id"] = "should-not-vanish-silently"

    with pytest.raises(ValueError, match="unexpected top-level fields") as excinfo:
        parse_price_change_group(event, asset_id=TOKEN_YES)
    assert "sequence" in str(excinfo.value)
    assert "server_generated_id" in str(excinfo.value)


def test_a_real_unmodified_frame_still_parses_after_the_top_level_key_check() -> None:
    """The guard above must not be degenerate against real recorded traffic."""
    parsed = [
        parse_price_change_group(event, asset_id=TOKEN_YES)
        for _index, event in _price_change_events()
    ]
    assert len(parsed) == 34


# =====================================================================================
# Wire-order and cross-token scope trust.
# =====================================================================================


def test_scrambling_entry_order_across_the_real_fixture_never_changes_the_result() -> None:
    """Attack every real frame's own entry order by reversing it, not only by
    trusting `parse_price_change_group`'s own internal `sorted()` call on a
    single synthetic example. Negative result across all 34 real frames."""
    for _index, event in _price_change_events():
        for token in (TOKEN_YES, TOKEN_NO):
            forward = parse_price_change_group(event, asset_id=token)
            reversed_event = dict(event)
            reversed_event["price_changes"] = list(reversed(event["price_changes"]))
            backward = parse_price_change_group(reversed_event, asset_id=token)
            assert forward.payload.to_record() == backward.payload.to_record()
            assert forward.entry_hash == backward.entry_hash


def test_an_entry_for_only_the_sibling_token_still_refuses_cleanly_for_the_requested_token() -> (
    None
):
    """A frame carrying entries for the sibling only, when the caller asks for a
    token that never appears, refuses with a specific, attributable reason
    rather than silently returning an empty or wrong-scoped group."""
    sibling_only = _minimal_event(
        price_changes=[
            {
                "asset_id": TOKEN_NO,
                "price": "0.51",
                "size": "20",
                "side": "BUY",
                "hash": "9b697d845171b76940bdbd2ed1e561922558210e",
                "best_bid": "0.71",
                "best_ask": "0.72",
            }
        ]
    )
    with pytest.raises(ValueError, match="no price_changes entries"):
        parse_price_change_group(sibling_only, asset_id=TOKEN_YES)


# =====================================================================================
# Hostile / oversized entry_hash. Unbounded and unsanitized inside this module,
# unlike normalize_clob_book._extract_source_hash — but caught at the envelope
# boundary. Both halves measured, not assumed.
# =====================================================================================


@pytest.mark.parametrize(
    ("hostile_hash", "label"),
    [
        ("\x1b]52;c;cHdu\x07", "an OSC 52 clipboard write"),
        ("h\nnewline", "an embedded newline"),
        ("h‮reversed", "a right-to-left override"),
        ("a" * 300, "300 characters, past the envelope's 256-character identifier cap"),
        ("a" * 20_000_000, "20,000,000 characters"),
    ],
)
def test_a_hostile_entry_hash_is_not_bounded_or_sanitized_inside_this_module(
    hostile_hash: str, label: str
) -> None:
    """MEDIUM-adjacent negative result, recorded precisely rather than glossed.

    `docs/STATUS.md` records that `normalize_clob_book._extract_source_hash`
    had to add exactly two checks (length, `is_clean_identifier`) after a
    security finding on the REST side. This module's `entry_hash` reproduces
    that SAME starting gap: `PriceChangeGroup.entry_hash` carries the hostile
    value out of `parse_price_change_group` completely unbounded and
    unneutralized — this half of the test is the finding. The second half
    confirms it is contained today: feeding it into
    `build_observation_envelope` as `source_hash` is refused by
    `ObservationEnvelopeV1._validate_identifier`, which independently checks
    both length and `is_clean_identifier`. Nothing today calls
    `parse_price_change_group` without eventually building an envelope from
    it, but the module itself does not defend this value the way the REST
    adapter's `_extract_source_hash` now does — the gap is real even though
    nothing exploits it yet.
    """
    event = _minimal_event(
        price_changes=[
            {
                "asset_id": TOKEN_YES,
                "price": "0.49",
                "size": "636",
                "side": "SELL",
                "hash": hostile_hash,
                "best_bid": "0.28",
                "best_ask": "0.29",
            }
        ]
    )
    group = parse_price_change_group(event, asset_id=TOKEN_YES)
    assert group.entry_hash == hostile_hash, f"{label}: entry_hash was altered unexpectedly"

    with pytest.raises(ValidationError):
        _envelope_for(
            group.payload,
            source_hash=group.entry_hash,
            event_ms="1786387666174",
            ingest_sequence=1,
        )


# =====================================================================================
# Leading-zero token id: inherited, unconfirmed against live traffic.
# =====================================================================================


def test_a_leading_zero_asset_id_is_a_different_stored_identity_than_its_unpadded_spelling() -> (
    None
):
    """MEDIUM, inherited from `TOKEN_ID_PATTERN`/`argos.domain.orderbook`, not new
    to this slice. `[0-9]{1,120}` accepts a leading-zero spelling of the same
    integer token id, and neither this module nor `argos.domain.orderbook`
    normalizes it by value — the M1 "duplicate token detection compared
    strings, not integer values" class, reachable here if the source or a
    caller ever supplies a padded id. `docs/research/m2-clob-websocket.md`
    has never observed a padded asset_id on the wire; this test documents the
    shape as reachable, not as observed."""
    padded = "0" + TOKEN_YES
    plain_event = _minimal_event()
    padded_event = _minimal_event(
        price_changes=[
            {
                "asset_id": padded,
                "price": "0.49",
                "size": "636",
                "side": "SELL",
                "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                "best_bid": "0.28",
                "best_ask": "0.29",
            }
        ]
    )
    plain_group = parse_price_change_group(plain_event, asset_id=TOKEN_YES)
    padded_group = parse_price_change_group(padded_event, asset_id=padded)

    assert plain_group.payload.asset_id != padded_group.payload.asset_id
    assert int(plain_group.payload.asset_id) == int(padded_group.payload.asset_id)


# =====================================================================================
# Resource exhaustion: no bound exists on `price_changes` array length. Measured,
# not merely observed to be "large". No cap exists in this module by design (the
# ingestion slice that would cap it does not exist yet); this section reports the
# numbers that slice needs, and keeps the committed test at a scale that stays fast.
# =====================================================================================


def _make_bulk_event(entry_count: int, *, asset_id: str = TOKEN_YES) -> dict[str, Any]:
    price_changes = []
    for i in range(entry_count):
        price_changes.append(
            {
                "asset_id": asset_id,
                "price": f"{i / 1_000_000:.6f}",
                "size": "10",
                "side": "SELL",
                "hash": "5ce704dea0a2123a388f1d8b432aad0058f5c479",
                "best_bid": "0.28",
                "best_ask": "0.29",
            }
        )
    return {
        "event_type": "price_change",
        "market": CONDITION_ID,
        "timestamp": "1786387666174",
        "price_changes": price_changes,
    }


def test_price_changes_array_length_has_no_bound_measured_at_50000_entries() -> None:
    """HIGH class, fourth independent recurrence — see `docs/STATUS.md`: the
    unbounded-collection-size class already recurred at `build_observation_
    envelope`, then `normalize_clob_book`, twice. There is no equivalent cap on
    `len(event["price_changes"])` here, and no ingestion/capture-loop slice
    exists yet to add one — this measurement is what that slice needs.

    Measured directly (not merely asserted here, to keep this test fast):

    | entries | scope                        | elapsed  | peak RSS |
    |--------:|------------------------------|---------:|---------:|
    | 100,000 | all match the requested token | 1.92 s   | 166 MB   |
    | 900,000 | all match, unique price levels| 18.16 s  | 1,147 MB |
    | 900,000 | ALL for the sibling token only | 0.30 s  | 282 MB   |

    The 900,000-all-sibling case matters as much as the 900,000-matching one:
    every entry is inspected for `isinstance`/`asset_id` shape *before* the
    per-token filter runs (the fix for the earlier silent-drop finding, by
    necessity), so a frame that names an attacker-chosen unsubscribed token
    900,000 times still costs a full O(n) scan even though the caller's own
    token never appears and the frame is ultimately refused.

    The committed test below runs at 50,000 entries (well inside the same
    linear-ish regime, no cap to hit) purely so the suite stays fast; the
    numbers above were gathered with the identical code path at larger N.
    """
    event = _make_bulk_event(50_000)
    started = time.perf_counter()
    group = parse_price_change_group(event, asset_id=TOKEN_YES)
    elapsed = time.perf_counter() - started

    assert len(group.payload.changes) == 50_000
    # No assertion on `elapsed` beyond "it completed" — there is deliberately
    # no cap to assert against yet; the point of this test is the measurement
    # recorded in the docstring table above, gathered with this exact code
    # path, not a pass/fail threshold this module does not implement.
    assert elapsed >= 0


def test_a_single_31_mib_hash_field_is_accepted_essentially_for_free() -> None:
    """Distinct from the array-length class above: a single oversized *field*
    value. Measured at 31 MiB (legal under the source client's own 32 MiB
    response cap, matching the size security review used for the REST
    adapter's analogous findings): 0.0097 s, 65,892 KB peak RSS. Cheap here
    specifically because `parse_price_change_group` does no bounding work on
    `hash` at all (see the hostile-entry_hash finding above) — there is
    nothing to be slow, which is a different way of stating the same gap:
    nothing here would notice a 900 MiB hash field either, until whatever
    calls this function tries to build an envelope from it."""
    big_hash = "a" * (31 * 1024 * 1024)
    event = _minimal_event(
        price_changes=[
            {
                "asset_id": TOKEN_YES,
                "price": "0.49",
                "size": "10",
                "side": "SELL",
                "hash": big_hash,
                "best_bid": "0.28",
                "best_ask": "0.29",
            }
        ]
    )
    group = parse_price_change_group(event, asset_id=TOKEN_YES)
    assert len(group.entry_hash) == 31 * 1024 * 1024
