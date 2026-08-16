"""Independent adversarial coverage for the CLOB REST adapter and its normalizer.

This file does not duplicate `tests/test_clob_client.py`, `tests/test_clob_book_ingestion.py`,
or `tests/test_orderbook_snapshot.py` (all read before writing this one). It attacks
the seams those suites leave open: the full pipeline (`ClobClient` -> orjson decoding
-> `normalize_clob_book` -> `OrderBookSnapshotV1` -> `SQLiteEventStore`) exercised
together with hostile or edge-case input, rather than any one layer in isolation.

No live network call. respx intercepts every HTTP request; `tests/conftest.py`
additionally blocks outbound sockets at the OS level for the whole suite.

Findings are documented inline, next to the test that reproduces them, rather than
only in a separate report -- a test that currently fails is the reproduction, not
an opinion about one.
"""

from __future__ import annotations

import json
import random
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import anyio
import httpx
import orjson
import pytest
import respx
from anyio import CancelScope

from argos.clock import ReplayClock
from argos.clock.base import _require_duration
from argos.config import Settings
from argos.domain.observation import (
    DEFAULT_CLOCK_SKEW_TOLERANCE,
    EventTimeStatus,
    ObservationEnvelopeV1,
    ObservationQualityFlag,
    RejectedObservationV1,
    read_payload,
    recompute_observation_id,
)
from argos.domain.orderbook import OrderBookAnomalyKind, OrderBookSnapshotV1
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.errors import RejectionReason
from argos.ingestion.clob_book import normalize_clob_book
from argos.sources import ClobClient
from argos.sources.clob import ClobBookBadRequestError, ClobBookNotFoundError
from argos.store.event_store import Disposition, open_sqlite_event_store

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "clob" / "book_yes.raw.json"
BASE = "https://clob.polymarket.com"
TOKEN_ID = "63842529068710005716169325380315470359047749786610778647370693404952498013178"
CONDITION_ID = "0x876506d8b2bd7a0d3fa4fe18c024eee6e1dd81ee24c26795dadd6cfe4a7b5d0d"
START = datetime(2026, 8, 10, 18, 10, 7, tzinfo=UTC)
RUN = "capture-run-adversarial"


# --- shared fixtures / helpers, deliberately duplicated rather than imported ------------
# Matching tests/test_clob_client.py's own stated convention: "test modules are not a
# shared library in this repository (no tests/__init__.py)".


class RecordingPacer:
    """Test fake pacer. Copied from tests/test_clob_client.py's own copy, which is
    itself copied from tests/test_gamma_client.py, for the same reason both give."""

    def __init__(self) -> None:
        self.waits: list[float] = []
        self._scope: CancelScope | None = None
        self._budget = 0.0
        self._elapsed = 0.0

    async def wait(self, seconds: float) -> None:
        _require_duration(seconds)
        self.waits.append(seconds)
        if self._scope is not None:
            self._elapsed += seconds
            if self._elapsed >= self._budget:
                self._scope.cancel()
        await anyio.lowlevel.checkpoint()

    def move_on_after(self, seconds: float) -> Iterator[CancelScope]:
        _require_duration(seconds)
        return self._scoped(seconds)

    @contextmanager
    def _scoped(self, seconds: float) -> Iterator[CancelScope]:
        scope = CancelScope()
        previous = (self._scope, self._budget, self._elapsed)
        self._scope, self._budget, self._elapsed = scope, seconds, 0.0
        try:
            with scope:
                yield scope
        finally:
            self._scope, self._budget, self._elapsed = previous


@pytest.fixture(name="raw_book")
def raw_book_fixture() -> bytes:
    return FIXTURE.read_bytes()


@pytest.fixture(name="clock")
def clock_fixture() -> ReplayClock:
    return ReplayClock(START)


@pytest.fixture(name="pacer")
def pacer_fixture() -> RecordingPacer:
    return RecordingPacer()


@pytest.fixture(name="client")
async def client_fixture(clock: ReplayClock, pacer: RecordingPacer) -> ClobClient:
    settings = Settings(http_max_attempts=3, http_timeout_seconds=5.0)
    return ClobClient(settings, clock, pacer=pacer)


def _load_payload(**overrides: Any) -> dict[str, Any]:
    with FIXTURE.open() as handle:
        payload: dict[str, Any] = json.load(handle, parse_float=Decimal)
    payload.update(overrides)
    return payload


def _provenance(
    raw: bytes, *, received_time: datetime = START, **overrides: Any
) -> SourceProvenanceV1:
    fields: dict[str, Any] = {
        "source": "clob_rest",
        "endpoint": f"{BASE}/book?token_id={TOKEN_ID}",
        "http_status": 200,
        "retrieved_at": received_time,
        "raw_sha256": sha256_hex(raw),
        "byte_length": len(raw),
    }
    fields.update(overrides)
    return SourceProvenanceV1(**fields)


def _normalize(
    payload: Any,
    *,
    raw: bytes = b"raw-bytes-for-hashing",
    requested_token_id: str = TOKEN_ID,
    received_time: datetime = START,
    rejected_at: datetime = START,
    ingest_sequence: int = 1,
    capture_run_id: str = RUN,
    **overrides: Any,
) -> ObservationEnvelopeV1 | RejectedObservationV1:
    return normalize_clob_book(
        payload=payload,
        provenance=_provenance(raw, received_time=received_time),
        requested_token_id=requested_token_id,
        received_time=received_time,
        rejected_at=rejected_at,
        ingest_sequence=ingest_sequence,
        capture_run_id=capture_run_id,
        **overrides,
    )


# =========================================================================================
# 1. The wire-ordering trap
# =========================================================================================


def test_a_fully_scrambled_wire_order_is_still_re_derived_correctly_through_the_full_pipeline() -> (
    None
):
    """Neither `parse_order_book_snapshot` nor `normalize_clob_book` ever trusts wire
    order, even when it is scrambled arbitrarily (not merely reversed): the naive `[0]`
    trap in the module docstrings is not merely dodged for the one wire shape the real
    fixture happens to use."""
    payload = _load_payload()
    bids = list(payload["bids"])
    asks = list(payload["asks"])
    # Neither ascending nor descending: an arbitrary permutation, seeded so the
    # failure (if any) is reproducible rather than flaky.
    rng = random.Random(20260810)
    rng.shuffle(bids)
    rng.shuffle(asks)
    payload["bids"] = bids
    payload["asks"] = asks

    result = _normalize(payload)
    assert isinstance(result, ObservationEnvelopeV1)
    snapshot = read_payload(result, OrderBookSnapshotV1)

    assert snapshot.best_bid == Decimal("0.42")
    assert snapshot.best_ask == Decimal("0.43")
    prices_desc = [level.price for level in snapshot.bids]
    assert prices_desc == sorted(prices_desc, reverse=True)
    prices_asc = [level.price for level in snapshot.asks]
    assert prices_asc == sorted(prices_asc)


def test_a_single_level_book_on_each_side_has_a_correct_and_unambiguous_top_of_book() -> None:
    payload = _load_payload(
        bids=[{"price": "0.30", "size": "10"}],
        asks=[{"price": "0.35", "size": "10"}],
    )
    result = _normalize(payload)
    assert isinstance(result, ObservationEnvelopeV1)
    snapshot = read_payload(result, OrderBookSnapshotV1)
    assert snapshot.best_bid == Decimal("0.30")
    assert snapshot.best_ask == Decimal("0.35")


def test_a_one_sided_book_with_no_bids_reports_no_best_bid_and_no_spread() -> None:
    payload = _load_payload(bids=[])
    result = _normalize(payload)
    assert isinstance(result, ObservationEnvelopeV1)
    snapshot = read_payload(result, OrderBookSnapshotV1)
    assert snapshot.best_bid is None
    assert snapshot.best_ask is not None
    assert snapshot.spread is None
    assert snapshot.derived_midpoint is None


def test_an_empty_book_on_both_sides_is_accepted_with_no_top_of_book_and_no_crossed_flag() -> None:
    """An empty book is not a malformed book -- it is an honest observation that
    nothing rests on either side right now, and must not be refused or must not
    spuriously claim a crossed/locked anomaly it cannot possibly exhibit."""
    payload = _load_payload(bids=[], asks=[])
    result = _normalize(payload)
    assert isinstance(result, ObservationEnvelopeV1)
    snapshot = read_payload(result, OrderBookSnapshotV1)
    assert snapshot.bids == ()
    assert snapshot.asks == ()
    assert snapshot.best_bid is None
    assert snapshot.best_ask is None
    structural_kinds = {
        a.kind
        for a in snapshot.anomalies
        if a.kind in (OrderBookAnomalyKind.CROSSED_BOOK, OrderBookAnomalyKind.LOCKED_BOOK)
    }
    assert structural_kinds == set()


def test_bid_and_ask_at_the_identical_price_is_reported_as_locked_not_crossed() -> None:
    payload = _load_payload(
        bids=[{"price": "0.50", "size": "10"}],
        asks=[{"price": "0.50", "size": "10"}],
    )
    result = _normalize(payload)
    assert isinstance(result, ObservationEnvelopeV1)
    snapshot = read_payload(result, OrderBookSnapshotV1)
    kinds = {a.kind for a in snapshot.anomalies}
    assert OrderBookAnomalyKind.LOCKED_BOOK in kinds
    assert OrderBookAnomalyKind.CROSSED_BOOK not in kinds


# =========================================================================================
# 2. Decimal and identity
# =========================================================================================


def test_exponent_notation_collapses_to_the_same_identity_as_plain_decimal_text() -> None:
    """`Decimal("4.3E-1")` and `Decimal("0.43")` describe the same price. ADR-0010
    requires this to mint one identity, not two."""
    exponent_form = _normalize(_load_payload(last_trade_price="4.3E-1"))
    plain_form = _normalize(_load_payload(last_trade_price="0.43"))
    assert isinstance(exponent_form, ObservationEnvelopeV1)
    assert isinstance(plain_form, ObservationEnvelopeV1)
    assert exponent_form.observation_id == plain_form.observation_id


def test_a_leading_plus_sign_collapses_to_the_same_identity_as_unsigned_text() -> None:
    plus_form = _normalize(_load_payload(last_trade_price="+0.43"))
    plain_form = _normalize(_load_payload(last_trade_price="0.43"))
    assert isinstance(plus_form, ObservationEnvelopeV1)
    assert isinstance(plain_form, ObservationEnvelopeV1)
    assert plus_form.observation_id == plain_form.observation_id


def test_a_leading_zero_collapses_to_the_same_identity_as_the_minimal_spelling() -> None:
    leading_zero = _normalize(_load_payload(last_trade_price="00.43"))
    plain_form = _normalize(_load_payload(last_trade_price="0.43"))
    assert isinstance(leading_zero, ObservationEnvelopeV1)
    assert isinstance(plain_form, ObservationEnvelopeV1)
    assert leading_zero.observation_id == plain_form.observation_id


@pytest.mark.parametrize("hostile", ["Infinity", "-Infinity", "inf", "NaN", "-NaN"])
def test_infinite_and_nan_spellings_are_rejected_not_silently_admitted(hostile: str) -> None:
    result = _normalize(_load_payload(last_trade_price=hostile))
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD


def test_very_high_precision_beyond_the_canonical_budget_is_refused_not_silently_rounded() -> None:
    """Rewritten: this test used to make a claim it never checked.

    Its previous name ended in ``and_is_preserved`` and its only assertion was
    ``str(...).startswith("0.4444")`` — which a *truncated* value satisfies
    just as well as a preserved one. Measured directly: the 61-significant-digit
    wire value ``"0." + "4"*60 + "3"`` was silently rounded to 28 digits by
    ``Decimal.normalize()`` under the ambient decimal precision, and the stored
    value compared **unequal** to the source value. So ARGOS durably stored a
    price the source never sent, called it preserved, and passed its own
    adversarial test — the "claim outrunning its assertion" pattern
    ``docs/STATUS.md`` names as recurring in this repository.

    Worse, the rounding was a function of ``decimal.getcontext()``, thread-local
    mutable global state, so the stored value and therefore the
    ``observation_id`` depended on ambient configuration rather than on the wire
    bytes alone.

    ``normalize_decimal`` now refuses anything past ``MAX_SIGNIFICANT_DIGITS``
    before any context-sensitive operation runs. A silently mutated price
    becomes a counted rejection with a reason, per
    ``.claude/rules/data-integrity.md``.
    """
    result = _normalize(_load_payload(last_trade_price="0." + "4" * 60 + "3"))
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD
    assert "significant digits" in result.detail


def test_a_precision_within_the_canonical_budget_is_still_preserved_exactly() -> None:
    """The guard above must not be degenerate: real precision still survives.

    Asserts equality against the source ``Decimal``, which is what the previous
    test's name promised and its assertion did not deliver.
    """
    source_text = "0." + "4" * 32 + "3"
    result = _normalize(_load_payload(last_trade_price=source_text))
    assert isinstance(result, ObservationEnvelopeV1)
    snapshot = read_payload(result, OrderBookSnapshotV1)
    assert snapshot.last_trade_price == Decimal(source_text)


def test_negative_zero_mints_a_different_identity_than_positive_zero_for_the_same_price() -> None:
    """FINDING (decimal / identity, priority 2). `_normalize_decimal`
    (`src/argos/domain/orderbook.py`) only re-quantizes when `Decimal.normalize()`
    produced a *positive* exponent (its own docstring: "restores plain-integer text").
    It never special-cases sign: `Decimal("-0").normalize()` stays `Decimal('-0')`
    (exponent 0, so the `exponent > 0` guard never fires), while `Decimal("0")`
    normalizes to `Decimal('0')`. The two are numerically equal (`Decimal('-0') ==
    Decimal('0')` is `True`) but `str()` of them differs ('-0' vs '0'), and
    `_canonical_json` hashes the `str`-rendered canonical payload, not the Decimal
    value. `last_trade_price` and a book level's own `price` both allow a
    boundary value of exactly 0 (`MIN_PRICE = Decimal(0)`, `ge=MIN_PRICE`), so
    "-0" is not merely legal Decimal syntax, it satisfies the domain's own price
    range validator (`Decimal('-0') >= Decimal('0')` is `True`) and reaches
    storage as `-0` in the canonical JSON, splitting one economic price into two
    observation_ids depending purely on the sign byte the source happened to
    spell. This violates the module's own stated contract ("identical prices
    regardless of how the source spelled them", `src/argos/domain/orderbook.py`
    docstring point 1) exactly as much as the trailing-zero case the module
    explicitly closes.

    **Fixed.** `_normalize_decimal` now collapses any zero onto positive zero
    before scale handling, so this test asserts the corrected behaviour. The
    collapse is general rather than a patch for the literal spelling `"-0"`:
    `-0`, `-0.0`, `-0E+5`, `-0.00000` and `0E+3` all render as `"0"`, because
    the branch keys on `Decimal.is_zero()` before sign or exponent can reach
    canonical text.

    `tick_size` and `min_order_size` are not exposed to this because both are
    `Field(gt=0)`, which `Decimal('-0') > Decimal('0')` fails (they compare
    equal, so `gt` is false) -- only the two fields whose valid range includes
    the boundary 0 itself (`last_trade_price`, and a bid/ask level's `price`)
    are reachable.
    """
    positive_zero = _normalize(_load_payload(last_trade_price="0"))
    negative_zero = _normalize(_load_payload(last_trade_price="-0"))
    assert isinstance(positive_zero, ObservationEnvelopeV1)
    assert isinstance(negative_zero, ObservationEnvelopeV1)

    positive_snapshot = read_payload(positive_zero, OrderBookSnapshotV1)
    negative_snapshot = read_payload(negative_zero, OrderBookSnapshotV1)
    assert positive_snapshot.last_trade_price == negative_snapshot.last_trade_price, (
        "the two are the same economic price by Decimal equality"
    )

    assert positive_zero.observation_id == negative_zero.observation_id, (
        "the same economic price, spelled with two different zero signs, minted two "
        f"different observation_ids: {positive_zero.observation_id!r} vs "
        f"{negative_zero.observation_id!r} -- see the docstring above"
    )


def test_negative_zero_on_a_book_level_price_is_also_reachable_not_only_last_trade_price() -> None:
    """Same defect as above, reached through a bid level's `price` field instead
    of `last_trade_price`, proving it was not particular to one field.

    Both are now closed by `_normalize_decimal` collapsing any zero onto
    positive zero. A '-0' price level is still *accepted* -- `OrderBookLevel`'s
    range validator is `ge=MIN_PRICE` and `Decimal('-0') >= Decimal('0')` is
    True -- but it can no longer render as a second canonical text for the same
    economic price, which is what split one book into two identities."""
    payload = _load_payload(bids=[{"price": "-0", "size": "10"}], asks=[])
    result = _normalize(payload)
    assert isinstance(result, ObservationEnvelopeV1)
    snapshot = read_payload(result, OrderBookSnapshotV1)
    assert str(snapshot.bids[0].price) == "0"

    positive = _normalize(_load_payload(bids=[{"price": "0", "size": "10"}], asks=[]))
    assert isinstance(positive, ObservationEnvelopeV1)
    assert result.observation_id == positive.observation_id, (
        "the same economic book spelled with two zero signs must be one observation"
    )


# =========================================================================================
# 3. Timestamp handling
# =========================================================================================


def test_a_timestamp_string_with_leading_zeros_still_parses_to_the_right_instant() -> None:
    result = _normalize(_load_payload(timestamp="0001000"))
    assert isinstance(result, ObservationEnvelopeV1)
    assert result.event_time_status is EventTimeStatus.PRESENT
    assert result.event_time == datetime(1970, 1, 1, 0, 0, 1, tzinfo=UTC)


def test_zero_timestamp_parses_to_the_epoch_not_to_the_current_time() -> None:
    result = _normalize(_load_payload(timestamp="0"))
    assert isinstance(result, ObservationEnvelopeV1)
    assert result.event_time_status is EventTimeStatus.PRESENT
    assert result.event_time == datetime(1970, 1, 1, tzinfo=UTC)


def test_a_negative_timestamp_parses_to_a_pre_epoch_instant_not_a_crash() -> None:
    result = _normalize(_load_payload(timestamp="-100"))
    assert isinstance(result, ObservationEnvelopeV1)
    assert result.event_time_status is EventTimeStatus.PRESENT
    assert result.event_time == datetime(1969, 12, 31, 23, 59, 59, 900_000, tzinfo=UTC)


def test_a_float_shaped_timestamp_string_is_unparseable_not_truncated() -> None:
    """`1786385407185.5` is not `-?[0-9]+`; silently truncating the fractional part
    would be a small, undocumented act of interpretation this module's own docstring
    says it refuses to do."""
    result = _normalize(_load_payload(timestamp="1786385407185.5"))
    assert isinstance(result, ObservationEnvelopeV1)
    assert result.event_time_status is EventTimeStatus.UNPARSEABLE
    assert result.event_time is None
    assert result.event_time_raw == "1786385407185.5"


def test_an_enormous_timestamp_overflowing_datetime_range_is_unparseable_not_a_crash() -> None:
    huge = "9" * 30
    result = _normalize(_load_payload(timestamp=huge))
    assert isinstance(result, ObservationEnvelopeV1)
    assert result.event_time_status is EventTimeStatus.UNPARSEABLE
    assert result.event_time is None
    assert result.event_time_raw == huge


def test_an_enormous_negative_timestamp_overflowing_datetime_range_is_unparseable() -> None:
    huge_negative = "-" + "9" * 30
    result = _normalize(_load_payload(timestamp=huge_negative))
    assert isinstance(result, ObservationEnvelopeV1)
    assert result.event_time_status is EventTimeStatus.UNPARSEABLE
    assert result.event_time is None
    assert result.event_time_raw == huge_negative


def test_a_numeric_json_timestamp_never_becomes_the_wall_clock() -> None:
    """`.claude/rules/data-integrity.md`: never substitute the current time for an
    unparseable source timestamp. Assert directly against a real injected
    ReplayClock instant far from the fixture's own timestamp, so a substitution
    bug would be caught even if it happened to coincide with `received_time`."""
    received = datetime(2030, 1, 1, tzinfo=UTC)
    result = _normalize(
        _load_payload(timestamp=1786385407185), received_time=received, rejected_at=received
    )
    assert isinstance(result, ObservationEnvelopeV1)
    assert result.event_time_status is EventTimeStatus.UNPARSEABLE
    assert result.event_time is None
    assert result.received_time == received
    assert result.event_time_raw == "1786385407185"


def test_event_time_ahead_of_receipt_flag_actually_fires_when_the_source_clock_leads() -> None:
    """docs/04_DATA_CONTRACTS.md requires this flag; it is untested by
    tests/test_clob_book_ingestion.py entirely (no test there references
    quality_flags or ObservationQualityFlag)."""
    event_instant_ms = 1_000_000  # 1970-01-01T00:16:40Z
    received = datetime(1970, 1, 1, 0, 0, 0, tzinfo=UTC)  # far earlier than the event time
    result = _normalize(
        _load_payload(timestamp=str(event_instant_ms)),
        received_time=received,
        rejected_at=received,
    )
    assert isinstance(result, ObservationEnvelopeV1)
    assert result.event_time_status is EventTimeStatus.PRESENT
    assert ObservationQualityFlag.EVENT_TIME_AHEAD_OF_RECEIPT in result.quality_flags


def test_event_time_ahead_of_receipt_flag_does_not_fire_within_tolerance() -> None:
    """The flag must not be a hair-trigger: an event time that leads receipt by
    less than the tolerance is unremarkable clock skew, not a defect to flag."""
    received = datetime(2026, 8, 10, 18, 10, 7, tzinfo=UTC)
    event_instant = received + (DEFAULT_CLOCK_SKEW_TOLERANCE / 2)
    ms = int(event_instant.timestamp() * 1000)
    result = _normalize(
        _load_payload(timestamp=str(ms)), received_time=received, rejected_at=received
    )
    assert isinstance(result, ObservationEnvelopeV1)
    assert ObservationQualityFlag.EVENT_TIME_AHEAD_OF_RECEIPT not in result.quality_flags


def test_event_time_behind_receipt_never_flags_regardless_of_how_far_behind() -> None:
    """A source clock running slow (or a genuinely stale book) is not the anomaly
    this flag exists to catch -- only *ahead* of receipt is."""
    received = datetime(2026, 8, 10, 18, 10, 7, tzinfo=UTC)
    event_instant = received - timedelta(days=365)
    ms = int(event_instant.timestamp() * 1000)
    result = _normalize(
        _load_payload(timestamp=str(ms)), received_time=received, rejected_at=received
    )
    assert isinstance(result, ObservationEnvelopeV1)
    assert ObservationQualityFlag.EVENT_TIME_AHEAD_OF_RECEIPT not in result.quality_flags


# =========================================================================================
# 4. Determinism
# =========================================================================================


@respx.mock
async def test_duplicate_json_keys_on_the_wire_normalize_deterministically_every_time(
    client: ClobClient,
) -> None:
    """orjson resolves a duplicate top-level key to the last-written value (verified
    directly: `orjson.loads(b'{"a": 1, "a": 2}') == {"a": 2}`). This asserts that
    property holds through the real client and that repeated decode+normalize of the
    identical bytes is stable, rather than assuming orjson's behavior is documented
    Python stdlib behavior (it is not the same library)."""
    base = _load_payload()
    # Two "market" keys with different values; the second must consistently win.
    text = orjson.dumps(base).decode()
    hostile_raw = text[:-1] + f',"market":"{base["market"]}"' + "}"
    respx.get(f"{BASE}/book").mock(return_value=httpx.Response(200, content=hostile_raw.encode()))
    async with client:
        first = await client.get_book(TOKEN_ID)
        second = await client.get_book(TOKEN_ID)

    result_a = normalize_clob_book(
        payload=first.payload,
        provenance=first.provenance,
        requested_token_id=TOKEN_ID,
        received_time=START,
        rejected_at=START,
        ingest_sequence=1,
        capture_run_id=RUN,
    )
    result_b = normalize_clob_book(
        payload=second.payload,
        provenance=second.provenance,
        requested_token_id=TOKEN_ID,
        received_time=START,
        rejected_at=START,
        ingest_sequence=1,
        capture_run_id=RUN,
    )
    assert isinstance(result_a, ObservationEnvelopeV1)
    assert isinstance(result_b, ObservationEnvelopeV1)
    assert result_a.observation_id == result_b.observation_id


def test_whitespace_and_key_order_differences_in_raw_bytes_do_not_change_identity() -> None:
    """Two byte strings that decode to the same JSON value, differing only in
    incidental formatting, must still collapse onto one observation_id -- exercised
    at the raw-bytes/orjson boundary, not only on a pre-built Python dict as
    tests/test_clob_book_ingestion.py already does."""
    payload = _load_payload()
    compact_raw = orjson.dumps(payload)
    spaced_raw = json.dumps(payload, indent=4, sort_keys=False).encode()

    compact_decoded = orjson.loads(compact_raw)
    spaced_decoded = json.loads(spaced_raw)

    first = _normalize(compact_decoded, raw=compact_raw)
    second = _normalize(spaced_decoded, raw=spaced_raw)
    assert isinstance(first, ObservationEnvelopeV1)
    assert isinstance(second, ObservationEnvelopeV1)
    assert first.observation_id == second.observation_id


def test_a_genuinely_different_book_produces_a_genuinely_different_identity() -> None:
    """The determinism guard is not degenerate: it must be possible for two
    envelopes to differ."""
    payload = _load_payload()
    mutated = _load_payload()
    mutated["bids"][-1] = {"price": "0.42", "size": "999999.99"}  # size changed only

    first = _normalize(payload)
    second = _normalize(mutated)
    assert isinstance(first, ObservationEnvelopeV1)
    assert isinstance(second, ObservationEnvelopeV1)
    assert first.observation_id != second.observation_id


def test_recomputed_identity_agrees_with_the_stored_identity_on_the_real_fixture() -> None:
    result = _normalize(_load_payload())
    assert isinstance(result, ObservationEnvelopeV1)
    assert recompute_observation_id(result) == result.observation_id


# =========================================================================================
# 5. Rejection rather than exception
# =========================================================================================


@pytest.mark.parametrize("hostile_body", [None, "a string", 42, True, 3.14, []])
def test_every_wrong_top_level_type_is_rejected_not_raised(hostile_body: Any) -> None:
    result = _normalize(hostile_body)
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD


def test_an_empty_object_body_is_rejected_not_raised() -> None:
    result = _normalize({})
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD


def test_null_values_for_required_fields_are_rejected_not_raised() -> None:
    for field in ("bids", "asks", "tick_size", "min_order_size", "neg_risk", "last_trade_price"):
        result = _normalize(_load_payload(**{field: None}))
        assert isinstance(result, RejectedObservationV1), field
        assert result.reason is RejectionReason.MALFORMED_PAYLOAD, field


def test_a_level_that_is_a_scalar_not_an_object_is_rejected_not_raised() -> None:
    payload = _load_payload(bids=["0.42"])
    result = _normalize(payload)
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD


def test_a_level_that_is_a_list_not_an_object_is_rejected_not_raised() -> None:
    payload = _load_payload(bids=[["0.42", "10"]])
    result = _normalize(payload)
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD


def test_a_level_missing_price_is_rejected_not_raised() -> None:
    payload = _load_payload(bids=[{"size": "10"}])
    result = _normalize(payload)
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD


def test_a_level_with_an_extra_unexpected_field_is_rejected_not_silently_ignored() -> None:
    payload = _load_payload(bids=[{"price": "0.42", "size": "10", "order_id": "abc123"}])
    result = _normalize(payload)
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD


def test_a_crossed_book_is_accepted_and_flagged_through_the_full_normalizer() -> None:
    """Not the same as tests/test_orderbook_snapshot.py's own crossed-book test:
    this exercises the crossed case through normalize_clob_book end to end, where
    it has never been checked before, confirming an anomaly is not accidentally
    reason for rejection at this layer."""
    payload = _load_payload(
        bids=[{"price": "0.60", "size": "10"}],
        asks=[{"price": "0.50", "size": "10"}],
    )
    result = _normalize(payload)
    assert isinstance(result, ObservationEnvelopeV1)
    snapshot = read_payload(result, OrderBookSnapshotV1)
    kinds = {a.kind for a in snapshot.anomalies}
    assert OrderBookAnomalyKind.CROSSED_BOOK in kinds


def test_a_duplicate_price_level_is_rejected_whole_through_the_full_normalizer_not_dropped() -> (
    None
):
    """Unlike a zero-size level (dropped + recorded as an anomaly), a duplicate
    price level has no non-arbitrary resolution and the whole snapshot is refused
    -- verified here through normalize_clob_book, not only OrderBookSnapshotV1
    directly."""
    payload = _load_payload(bids=[{"price": "0.30", "size": "10"}, {"price": "0.30", "size": "20"}])
    result = _normalize(payload)
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD


def test_a_zero_size_level_was_never_observed_but_is_handled_end_to_end_when_synthesized() -> None:
    """The research note's own suspicion (never observed on REST) does not mean
    untested; this proves the drop-and-flag convention survives all the way
    through the normalizer, not only the domain constructor."""
    # 0.21 is absent from the fixture's own bid ladder (it jumps 0.20 -> 0.22),
    # so this cannot coincide with a real, non-zero level at the same price.
    payload = _load_payload(bids=[{"price": "0.21", "size": "0"}, *_load_payload()["bids"]])
    result = _normalize(payload)
    assert isinstance(result, ObservationEnvelopeV1)
    snapshot = read_payload(result, OrderBookSnapshotV1)
    dropped = [
        a for a in snapshot.anomalies if a.kind == OrderBookAnomalyKind.ZERO_SIZE_LEVEL_DROPPED
    ]
    assert len(dropped) == 1
    assert dropped[0].price == Decimal("0.21")
    assert all(level.price != Decimal("0.21") for level in snapshot.bids)


def test_an_empty_string_hash_is_rejected_not_treated_as_absent() -> None:
    """The module docstring distinguishes 'absent' (honestly represented as
    source_hash=None) from 'present but wrong', and an empty string is present."""
    result = _normalize(_load_payload(hash=""))
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD


def test_a_float_shaped_price_that_slipped_past_string_decoding_is_rejected_not_coerced() -> None:
    """Simulates a wire response that (unlike the real fixture) sent a bare JSON
    number for a price -- orjson would decode this to a Python float, and the no-
    float rule must hold at the full ingestion boundary, not only when a caller
    hand-builds an OrderBookLevel directly."""
    payload = _load_payload(bids=[{"price": 0.42, "size": "10"}])
    result = _normalize(payload)
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD


def test_a_hostile_market_field_on_an_otherwise_malformed_body_does_not_crash_the_rejection() -> (
    None
):
    """condition_id_hint is read via _best_effort_text before validation and lands
    unsanitized-at-that-point into RejectedObservationV1.condition_id -- confirm the
    ledger record's own sanitizer (RejectedObservationV1._sanitize_identifier)
    still neutralizes it rather than raising or forging a rendering."""
    payload = _load_payload(bids="not-a-list", market="normal\x1b[31mHOSTILE\x1b[0m")
    result = _normalize(payload)
    assert isinstance(result, RejectedObservationV1)
    assert "\x1b" not in (result.condition_id or "")


# =========================================================================================
# 6. 404 / 400 stay distinct and never imply "closed"
# =========================================================================================


@respx.mock
async def test_a_400_response_body_never_leaks_a_closed_market_inference_either(
    client: ClobClient,
) -> None:
    """tests/test_clob_client.py checks this for 404; it never checks the 400
    exception's own message for the same false inference."""
    respx.get(f"{BASE}/book").mock(return_value=httpx.Response(400, json={"error": "bad"}))
    async with client:
        with pytest.raises(ClobBookBadRequestError) as caught:
            await client.get_book(TOKEN_ID)
    assert "closed" not in str(caught.value).lower()


@respx.mock
async def test_a_404_never_reaches_the_normalizer_at_all_structurally(client: ClobClient) -> None:
    """A 404 is raised by ClobClient before any payload exists to normalize; this
    pins that a caller cannot accidentally route a 404's absence-of-body into
    normalize_clob_book and have it silently treated as an empty, valid book."""
    respx.get(f"{BASE}/book").mock(return_value=httpx.Response(404))
    async with client:
        with pytest.raises(ClobBookNotFoundError):
            response = await client.get_book(TOKEN_ID)
            # unreachable if the contract holds; documents the intended shape
            normalize_clob_book(
                payload=response.payload,
                provenance=response.provenance,
                requested_token_id=TOKEN_ID,
                received_time=START,
                rejected_at=START,
                ingest_sequence=1,
                capture_run_id=RUN,
            )


# =========================================================================================
# 7. Store integration: duplicate collapse and the harder revert case
# =========================================================================================


@respx.mock
async def test_a_refetched_unchanged_book_collapses_through_the_real_http_client_not_just_a_dict(
    client: ClobClient, raw_book: bytes
) -> None:
    """tests/test_clob_book_ingestion.py's own duplicate-collapse test builds the
    envelope from a hand-constructed provenance and a payload dict directly. This
    exercises the same property through the real ClobClient response (real
    provenance, real orjson decode, real raw_sha256), which is never combined with
    the store anywhere else in the suite."""
    respx.get(f"{BASE}/book").mock(return_value=httpx.Response(200, content=raw_book))
    async with client:
        first_response = await client.get_book(TOKEN_ID)
        second_response = await client.get_book(TOKEN_ID)

    first = normalize_clob_book(
        payload=first_response.payload,
        provenance=first_response.provenance,
        requested_token_id=TOKEN_ID,
        received_time=START,
        rejected_at=START,
        ingest_sequence=1,
        capture_run_id=RUN,
    )
    second = normalize_clob_book(
        payload=second_response.payload,
        provenance=second_response.provenance,
        requested_token_id=TOKEN_ID,
        received_time=START,
        rejected_at=START,
        ingest_sequence=2,
        capture_run_id=RUN,
    )
    assert isinstance(first, ObservationEnvelopeV1)
    assert isinstance(second, ObservationEnvelopeV1)

    store = open_sqlite_event_store(":memory:")
    try:
        store.open_capture_run(RUN, started_at=START)
        first_delivery = store.append_observation(first)
        second_delivery = store.append_observation(second)
        assert first_delivery.disposition is Disposition.ACCEPTED_NEW
        assert second_delivery.disposition is Disposition.DUPLICATE
        counts = store.counts_for_capture_run(RUN)
        assert counts.accepted == 1
        assert counts.duplicate == 1
    finally:
        store.close()


def test_a_book_that_reverts_A_to_B_to_A_produces_three_distinct_observations_not_two() -> None:
    """The harder store-integration case the task calls out directly. The research
    note found the source's own `hash` field covers content only, so a genuine
    revert to a prior state repeats an earlier `hash` value; ADR-0010 rejected a
    hash-ranked identity design precisely because that would make the third,
    reverted observation collide with the first and silently vanish.

    This models a real revert honestly: content (and therefore the source's own
    `hash`) is identical between A and A', but the source's own `timestamp`
    advances between them, because a real last-change clock is stamped again when
    the book changes back -- it does not freeze. That is exactly the
    discriminator `_observation_identity` documents relying on for this scenario
    (`src/argos/domain/observation.py`, "State reversion").
    """
    payload_a = _load_payload(timestamp="1000000")
    payload_b = _load_payload(timestamp="2000000")
    payload_b["bids"][-1] = {"price": "0.42", "size": "1.00"}  # genuinely different content
    payload_a_reverted = _load_payload(timestamp="3000000")  # same content as A, later timestamp

    obs_a = _normalize(payload_a, raw=b"frame-a", ingest_sequence=1)
    obs_b = _normalize(payload_b, raw=b"frame-b", ingest_sequence=2)
    obs_a_reverted = _normalize(payload_a_reverted, raw=b"frame-a-again", ingest_sequence=3)
    assert isinstance(obs_a, ObservationEnvelopeV1)
    assert isinstance(obs_b, ObservationEnvelopeV1)
    assert isinstance(obs_a_reverted, ObservationEnvelopeV1)

    # Content really did revert: A and A' carry the same source hash and payload.
    assert obs_a.source_hash == obs_a_reverted.source_hash
    assert obs_a.payload == obs_a_reverted.payload

    ids = {obs_a.observation_id, obs_b.observation_id, obs_a_reverted.observation_id}
    assert len(ids) == 3, (
        "A, B, and reverted-A must mint three distinct observation_ids; a "
        "hash-ranked or content-only identity would collapse A and reverted-A "
        "onto one and silently erase the revert"
    )

    store = open_sqlite_event_store(":memory:")
    try:
        store.open_capture_run(RUN, started_at=START)
        delivery_a = store.append_observation(obs_a)
        delivery_b = store.append_observation(obs_b)
        delivery_a_reverted = store.append_observation(obs_a_reverted)

        assert delivery_a.disposition is Disposition.ACCEPTED_NEW
        assert delivery_b.disposition is Disposition.ACCEPTED_NEW
        assert delivery_a_reverted.disposition is Disposition.ACCEPTED_NEW, (
            "the reverted observation must be new evidence, not a silently "
            "collapsed duplicate of the first"
        )

        counts = store.counts_for_capture_run(RUN)
        assert counts.accepted == 3
        assert counts.duplicate == 0

        deliveries = list(store.iter_deliveries(RUN))
        assert [d.observation_id for d in deliveries] == [
            obs_a.observation_id,
            obs_b.observation_id,
            obs_a_reverted.observation_id,
        ]
    finally:
        store.close()


def test_rejections_sharing_one_rejection_id_in_one_frame_both_survive_in_the_ledger() -> None:
    """ADR-0011 section 7 (referenced in src/argos/store/event_store.py): rejection_id
    is a grouping key, not a unique key. Two distinct malformed deliveries refused for
    the identical reason against the identical raw frame hash must both still get a row."""
    raw = b"same-malformed-frame"
    payload = _load_payload(bids="not-a-list")
    rejection_1 = _normalize(payload, raw=raw, ingest_sequence=1)
    rejection_2 = _normalize(payload, raw=raw, ingest_sequence=2)
    assert isinstance(rejection_1, RejectedObservationV1)
    assert isinstance(rejection_2, RejectedObservationV1)
    assert rejection_1.rejection_id == rejection_2.rejection_id, (
        "identical reason + identical raw hash + identical identifiers is exactly "
        "the case ADR-0011 says shares one rejection_id"
    )

    store = open_sqlite_event_store(":memory:")
    try:
        store.open_capture_run(RUN, started_at=START)
        store.append_rejection(rejection_1, ingest_sequence=1)
        store.append_rejection(rejection_2, ingest_sequence=2)
        stored = list(store.iter_rejections(RUN))
        assert len(stored) == 2, "both rows must survive despite sharing one rejection_id"
        assert {r.ingest_sequence for r in stored} == {1, 2}
    finally:
        store.close()


# =========================================================================================
# Clock injection
# =========================================================================================


@respx.mock
async def test_retrieved_at_only_moves_when_the_injected_clock_is_told_to(
    client: ClobClient, clock: ReplayClock, raw_book: bytes
) -> None:
    """`retrieved_at` must come from the injected `Clock`, never from
    `datetime.now()`: two fetches with the clock frozen must carry the identical
    instant, and advancing the clock explicitly must be the only thing that
    changes it -- the same guarantee ADR-0009 requires everywhere else."""
    respx.get(f"{BASE}/book").mock(return_value=httpx.Response(200, content=raw_book))
    async with client:
        first = await client.get_book(TOKEN_ID)
        second = await client.get_book(TOKEN_ID)
        assert first.provenance.retrieved_at == second.provenance.retrieved_at == START

        later = START + timedelta(hours=1)
        clock.advance_to(later)
        third = await client.get_book(TOKEN_ID)
        assert third.provenance.retrieved_at == later
        assert third.provenance.retrieved_at != first.provenance.retrieved_at
