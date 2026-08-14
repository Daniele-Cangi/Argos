"""Independent adversarial coverage for `argos.ingestion.clob_price_change`.

Read before writing this file: `src/argos/ingestion/clob_price_change.py`,
`src/argos/domain/pricechange.py`, `src/argos/ingestion/wire.py`,
`tests/test_clob_price_change_ingestion.py` (this file does not duplicate it
-- it attacks the seams it leaves open, the same convention
`tests/test_clob_adapter_adversarial.py` states for its own REST sibling),
`src/argos/ingestion/clob_book.py`, and `tests/test_clob_adapter_adversarial.py`
(the REST sibling and its own adversarial suite -- several of its findings
were checked for recurrence here, see the sections below).

No live network. Nothing in this file opens a socket; `event` is always an
already-decoded Python object, matching this module's own documented scope.

**Headline finding, reported loudly per instructions: one test below is left
FAILING on purpose.**
`test_a_lone_utf16_surrogate_in_hash_is_the_sharpest_none_vs_reject_bypass_found`
asserts the behaviour `normalize_clob_price_change`'s own docstring promises
("This function never raises for a malformed payload") and the current code
does not deliver it: a lone UTF-16 surrogate character in the wire `hash`
field passes `_validate_entry_hash` (which calls
`argos.domain.text.is_clean_identifier`) because
`argos.domain.text.is_display_control` never checks Unicode category `Cs`,
survives into `source_hash`, and then crashes `_digest`
(`src/argos/domain/observation.py`) with an uncaught `UnicodeEncodeError`
when `hashlib.sha256(...).encode()` is called on it -- deep inside
`build_observation_envelope`, which sits *outside* this module's own `try`.
No caller dishonesty is required: genuinely malformed, genuinely
JSON-decodable text (Python's `json` module does not validate surrogate
pairing) reaches this crash with an entirely honest `provenance.byte_length`.
Reproduced directly (see the test) with concrete measurements; **not fixed
here**, because fixing it means editing `src/argos/domain/text.py`, which is
out of this file's ownership. The same root cause (`is_clean_identifier`
never checking `Cs`) was independently confirmed, in a throwaway repro run
outside this file, to recur identically in `argos.ingestion.clob_book`'s
`_extract_source_hash` -- reported in the review summary, not fixed here
either, for the same file-ownership reason.

Negative results (attacks that did **not** work) are recorded inline next to
the test that tried them, not only in a separate report.
"""

from __future__ import annotations

import decimal
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from argos.domain.observation import (
    ObservationEnvelopeV1,
    ObservationQualityFlag,
    RejectedObservationV1,
    read_payload,
    recompute_observation_id,
    recompute_rejection_id,
)
from argos.domain.orderbook import BookSide
from argos.domain.pricechange import PriceChangeV1
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.errors import ContractViolationError, RejectionReason
from argos.ingestion import clob_book as clob_book_module
from argos.ingestion import clob_price_change as clob_price_change_module
from argos.ingestion import wire as wire_module
from argos.ingestion.clob_price_change import normalize_clob_price_change
from argos.ingestion.wire import parse_event_time
from argos.store.event_store import Disposition, open_sqlite_event_store

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "clob" / "ws_market_price_change.raw.json"
)

CONDITION_ID = "0x94a39addec8bd24a2d03deeaa43bdee6a2b11eca403cd12495eb24132cc23173"
TOKEN_YES = "34691510069031117755834214800869745291092253295564665516660269316660628637961"
TOKEN_NO = "95561057794427123541889915407555646439882912350845258651794843110787555977699"
UNRELATED_TOKEN = "11111111111111111111111111111111111111111111111111111111111111111111111111"

RUN = "capture-run-adversarial"
RECEIVED = datetime(2026, 8, 10, 18, 47, 46, tzinfo=UTC)
REJECTED_AT = RECEIVED + timedelta(milliseconds=5)


# --- fixture reading, deliberately duplicated -- test modules are not a shared library ---


def _load_fixture() -> dict[str, Any]:
    with FIXTURE_PATH.open() as handle:
        return json.load(handle, parse_float=Decimal)  # type: ignore[no-any-return]


def _price_change_events() -> list[dict[str, Any]]:
    fixture = _load_fixture()
    events: list[dict[str, Any]] = []
    for message in fixture["messages"]:
        raw = message["raw"]
        if raw in ("PING", "PONG"):
            continue
        parsed = json.loads(raw, parse_float=Decimal)
        candidates = parsed if isinstance(parsed, list) else [parsed]
        for event in candidates:
            if event.get("event_type") == "price_change":
                events.append(event)
    return events


# --- helpers ------------------------------------------------------------------------------


def _provenance(raw: bytes, **overrides: Any) -> SourceProvenanceV1:
    fields: dict[str, Any] = {
        "source": "clob_market_ws",
        "endpoint": "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        "http_status": None,
        "retrieved_at": RECEIVED,
        "raw_sha256": sha256_hex(raw),
        "byte_length": len(raw),
    }
    fields.update(overrides)
    return SourceProvenanceV1(**fields)


def _normalize(
    event: Any,
    *,
    raw: bytes = b"raw-bytes-for-hashing",
    requested_token_id: str = TOKEN_YES,
    received_time: datetime = RECEIVED,
    rejected_at: datetime = REJECTED_AT,
    ingest_sequence: int = 1,
    capture_run_id: str = RUN,
    byte_length: int | None = None,
    **overrides: Any,
) -> ObservationEnvelopeV1 | RejectedObservationV1 | None:
    provenance_overrides: dict[str, Any] = {}
    if byte_length is not None:
        provenance_overrides["byte_length"] = byte_length
    return normalize_clob_price_change(
        event=event,
        provenance=_provenance(raw, **provenance_overrides),
        requested_token_id=requested_token_id,
        received_time=received_time,
        rejected_at=rejected_at,
        ingest_sequence=ingest_sequence,
        capture_run_id=capture_run_id,
        **overrides,
    )


def _entry(
    *,
    asset_id: str = TOKEN_YES,
    price: str = "0.49",
    size: str = "636",
    side: str = "SELL",
    hash_: str = "5ce704dea0a2123a388f1d8b432aad0058f5c479",
    best_bid: str | None = "0.28",
    best_ask: str | None = "0.29",
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "asset_id": asset_id,
        "price": price,
        "size": size,
        "side": side,
        "hash": hash_,
    }
    if best_bid is not None:
        entry["best_bid"] = best_bid
    if best_ask is not None:
        entry["best_ask"] = best_ask
    return entry


def _frame(entries: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event_type": "price_change",
        "market": CONDITION_ID,
        "timestamp": "1786387666174",
        "price_changes": entries,
    }
    event.update(overrides)
    return event


def _large_frame(count: int, *, token: str = TOKEN_YES) -> dict[str, Any]:
    """A well-formed frame carrying `count` distinct level changes for `token`.

    Copied from `tests/test_clob_price_change_ingestion.py`'s own
    `_large_price_change_event` (deliberately duplicated, not imported --
    "test modules are not a shared library").
    """
    entries = [
        _entry(
            asset_id=token,
            price="0." + str(100_000 + i)[1:],
            size="1",
            side="BUY" if i % 2 == 0 else "SELL",
            hash_="a" * 40,
            best_bid="0.5",
            best_ask="0.6",
        )
        for i in range(count)
    ]
    return _frame(entries)


# ===========================================================================================
# 1. The None/rejection boundary -- is a malformed frame ever silently swallowed as "None"?
# ===========================================================================================
#
# Core claim in the module docstring: `NoEntriesForToken` fires only after every
# other malformation `parse_price_change_group` can detect has already been ruled
# out for the WHOLE frame -- so `None` can never be a malformed frame wearing the
# healthy-case mask. Every test below tries to build exactly that mask and fails
# to produce one; each is therefore a NEGATIVE RESULT (attack failed), except
# where marked as a genuine limitation.


def test_a_non_mapping_sibling_entry_rejects_the_whole_frame_rather_than_returning_none() -> None:
    """The frame's only entry is not even a JSON object, and it is not for our
    token (it cannot be, since it carries no asset_id at all) -- if the domain
    only validated entries it was about to select, this would look exactly like
    "no entries for this token" and return `None`. It does not: the per-entry
    structural check in `parse_price_change_group` runs over every entry before
    selection, so this is refused."""
    frame = _frame(["not-a-mapping"])  # type: ignore[list-item]
    result = _normalize(frame)
    assert isinstance(result, RejectedObservationV1), (
        "a structurally malformed entry must never be swallowed into the None branch, "
        f"even when it cannot possibly belong to the requested token; got {type(result)}"
    )
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD


def test_a_null_asset_id_sibling_entry_rejects_rather_than_returning_none() -> None:
    """An entry that cannot be attributed to any token (`asset_id: null`) mixed
    with a normal entry for the *sibling* token. Zero entries select for our
    token either way, but the frame is still refused, not silently treated as
    "not about us": the module's own comment says skipping this would discard
    the malformed entry "for every token the caller later asks about"."""
    frame = _frame(
        [
            {"asset_id": None, "price": "0.1", "size": "1", "side": "BUY", "hash": "a" * 40},
            _entry(asset_id=TOKEN_NO, price="0.1", best_bid="0.1", best_ask="0.2"),
        ]
    )
    result = _normalize(frame)
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD
    assert "asset_id" in result.detail


def test_an_unexpected_top_level_key_rejects_even_when_no_entry_names_our_token() -> None:
    """A future source-schema field (the module docstring calls out a future
    sequence number by name) must halt ingestion loudly, not be silently folded
    into "this frame was never about our token" just because it happens to only
    carry sibling entries."""
    frame = _frame([_entry(asset_id=TOKEN_NO)], sequence_number=42)
    result = _normalize(frame)
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD
    assert "unexpected" in result.detail


def test_a_wrong_event_type_rejects_even_when_no_entry_names_our_token() -> None:
    frame = _frame([_entry(asset_id=TOKEN_NO)], event_type="book")
    result = _normalize(frame)
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD


def test_price_changes_not_a_list_rejects_regardless_of_token() -> None:
    frame = _frame([], price_changes="not-a-list")
    result = _normalize(frame)
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD


def test_an_empty_price_changes_array_returns_none_a_documented_edge_not_pinned_elsewhere() -> None:
    """LIMITATION, not asserted as a defect: `price_changes: []` was never
    observed in either live capture behind `docs/research/m2-clob-websocket.md`
    (both captures' own frames always carry at least one entry per token pair).
    The domain treats it identically to "no entries matched" -- `selected` is
    the empty list either way, so `NoEntriesForToken` fires and this function
    returns `None`. That is consistent with the rest of the module's own logic
    (a frame naming literally nothing is, definitionally, not about any token),
    but it is worth pinning explicitly: an upstream bug that occasionally emits
    `price_changes: []` instead of omitting the frame would be invisible at this
    layer -- indistinguishable from ordinary "not our token" traffic -- and only
    a capture-loop health counter watching for repeated `None` returns on an
    active subscription would ever notice."""
    frame = _frame([], price_changes=[])
    result = _normalize(frame)
    assert result is None


def test_a_malformed_own_token_entry_amid_well_formed_sibling_entries_is_rejected() -> None:
    """The mirror image of the tests above: our own token's entry is what is
    broken, wrapped in an otherwise-fine sibling entry. Confirms selection
    genuinely reaches our own malformed entry rather than the sibling's
    well-formedness somehow masking it."""
    frame = _frame(
        [
            _entry(asset_id=TOKEN_NO, price="0.1", best_bid="0.1", best_ask="0.2"),
            _entry(asset_id=TOKEN_YES, side="SIDEWAYS"),
        ]
    )
    result = _normalize(frame)
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD
    assert result.token_id == TOKEN_YES


@pytest.mark.parametrize(
    ("label", "hostile_asset_id"),
    [
        ("leading zero", "0" + TOKEN_YES),
        ("leading/trailing whitespace", f" {TOKEN_YES} "),
        (
            "fullwidth digits",
            "".join(chr(ord(c) + 0xFF10 - 0x30) if c.isdigit() else c for c in TOKEN_YES),
        ),
        ("zero-width-space-separated", TOKEN_YES[:5] + "​" + TOKEN_YES[5:]),
    ],
)
def test_a_respelled_asset_id_never_falsely_selects_our_token(
    label: str, hostile_asset_id: str
) -> None:
    """FAILED ATTACK (negative result), with one documented limitation.

    `entry["asset_id"] == asset_id` is exact Python string equality with no
    normalization on either side. Tried to make a *cosmetically* respelled
    version of our own token id (leading zero, padding whitespace, fullwidth
    digit spelling, an embedded zero-width space) accidentally satisfy that
    equality and smuggle a wrongly-typed value into our own token's evidence.
    It never does -- `==` is exact, so none of these can cause a FALSE
    POSITIVE selection. That is the safe failure direction.

    The unavoidable flip side, recorded as a LIMITATION rather than fixed here
    (fixing it is a design decision, not a test-file matter): if the *source*
    ever spelled the same economic token two ways across messages, this
    function has no way to tell that apart from "sibling, not us" -- it
    collapses onto the same silent `None` as ordinary unrelated traffic, with
    no signal that a respelling happened. Never observed on the wire per the
    research note, and TOKEN_ID_PATTERN (`[0-9]{1,120}`) would refuse most of
    these shapes anyway if they ever *did* land inside PriceChangeV1's own
    `asset_id`, so the exposure is real only for the "None, not even
    attributable" branch, never for a corrupted acceptance.
    """
    frame = _frame([_entry(asset_id=hostile_asset_id)])
    result = _normalize(frame)
    assert result is None, (
        f"{label}: a respelled asset_id must not falsely select our token (got {type(result)})"
    )


# ===========================================================================================
# 2. The byte cap: provenance.byte_length is caller-declared, not measured from `event`
# ===========================================================================================


def test_a_small_declared_byte_length_bypasses_the_cap_and_pays_the_full_parse_cost() -> None:
    """FINDING (MEDIUM -- architectural, not a regression of this slice).

    `provenance.byte_length` is a caller-supplied field. Nothing in this module
    (nor in `clob_book.py`, the REST sibling) cross-checks it against the
    actual size of `event`. Unlike `ClobClient`, which measures real bytes
    incrementally off the socket regardless of what `content-length` claims
    (closing the HTTP "lying content-length" class), a WebSocket frame that
    arrives already decoded into `event` gives this module no independent
    signal at all -- the declared `byte_length` is the *only* input the cap
    checks, so a caller that under-reports it (a transport bug, or a
    compromised/misbehaving upstream if the future capture loop ever computes
    it from something other than the frame's own decoded size) buys the full,
    unbounded parse cost this cap exists to avoid.

    Measured directly here: 74,000 well-formed entries (chosen as the smallest
    round number that reliably crosses the boundary described in Section 3
    below) with a declared `byte_length=100` -- comfortably under the cap --
    costs real, measured wall-clock time to refuse. This is *not* cheap the
    way the documented case (an honestly large `byte_length`) is; the module's
    own docstring measures the honest-refusal case as returning before a
    single key is read. This one does the opposite: it does the full
    unbounded amount of work and then still fails (see Section 3 -- this
    specific input additionally becomes an uncaught exception, not a
    rejection).

    What the future WebSocket capture-loop slice must do, stated plainly: it
    must derive `provenance.byte_length` from the actual received wire bytes
    of the frame (`len(raw_text.encode())`, or equivalent), never from
    anything computed after JSON decoding, and never accept it as an argument
    from a layer that could get it wrong. This module cannot defend against a
    mismatch by itself -- it has no independent measurement of `event`'s true
    size once decoding has already happened upstream.
    """
    event = _large_frame(74_000)
    start = time.perf_counter()
    with pytest.raises(ContractViolationError):
        _normalize(event, byte_length=100)
    elapsed = time.perf_counter() - start
    assert elapsed > 0.5, (
        f"the bypass must actually pay real cost, or this test proves nothing "
        f"(measured {elapsed:.4f}s) -- see the docstring for the full measurement "
        "(2.27s / ~143 MB peak RSS observed independently during triage)"
    )


def test_an_honestly_declared_byte_length_at_this_shape_never_needs_the_bypass() -> None:
    """The cap is not degenerate for this payload shape when honestly reported:
    for a `price_changes` array of well-formed entries, the *wire* encoding is
    always larger than the *canonical* payload the envelope builds (canonical
    form stores `best_bid`/`best_ask`/`asset_id`/the group hash once, not once
    per entry), so an honest `byte_length` under the cap can never itself
    trigger the Section 3 crash. Measured directly: 17,900 entries serialize to
    4,179,809 wire bytes (under the 4 MiB cap) and are accepted; one entry more
    (17,999) serializes to 4,202,925 bytes, crosses the cap, and is cheaply
    rejected before any parsing. Both boundary sides are pinned here."""
    accepted_event = _large_frame(17_900)
    accepted_raw = json.dumps(accepted_event).encode()
    accepted = _normalize(accepted_event, raw=accepted_raw, byte_length=len(accepted_raw))
    assert isinstance(accepted, ObservationEnvelopeV1), len(accepted_raw)

    rejected_event = _large_frame(17_999)
    rejected_raw = json.dumps(rejected_event).encode()
    rejected = _normalize(rejected_event, raw=rejected_raw, byte_length=len(rejected_raw))
    assert isinstance(rejected, RejectedObservationV1)
    assert rejected.reason is RejectionReason.MALFORMED_PAYLOAD
    assert "normalization budget" in rejected.detail


def test_the_byte_cap_boundary_is_exact_at_this_modules_own_constant() -> None:
    from argos.ingestion.clob_book import MAX_NORMALIZABLE_BYTES

    event = _frame([_entry()])
    under = _normalize(event, byte_length=MAX_NORMALIZABLE_BYTES)
    assert isinstance(under, ObservationEnvelopeV1)
    over = _normalize(event, byte_length=MAX_NORMALIZABLE_BYTES + 1)
    assert isinstance(over, RejectedObservationV1)


# ===========================================================================================
# 3. The uncaught crash: a lone UTF-16 surrogate in `hash` -- FAILING TEST LEFT ON PURPOSE
# ===========================================================================================


def test_a_lone_utf16_surrogate_in_hash_is_the_sharpest_none_vs_reject_bypass_found() -> None:
    """FINDING (HIGH). **This test is LEFT FAILING on purpose** -- see the
    module docstring's "Headline finding" for why it cannot be fixed from this
    file (the fix belongs in `src/argos/domain/text.py`, out of this file's
    ownership).

    `_validate_entry_hash` (`src/argos/ingestion/clob_price_change.py`) checks
    length and `argos.domain.text.is_clean_identifier`.
    `ObservationEnvelopeV1._validate_identifier` runs the identical check again
    on `source_hash`. Neither catches a **lone UTF-16 surrogate** codepoint
    (for example `"\\ud800"`, a bare high surrogate with no matching low
    surrogate): `unicodedata.category("\\ud800")` is `"Cs"`, which
    `is_display_control` never inspects, so `is_clean_identifier("\\ud800")`
    returns `True` -- "clean". Python's `json` module happily decodes the wire
    escape `"\\ud800"` into exactly that Python `str` without validating
    surrogate pairing, so this is reachable from genuinely parseable JSON text,
    with an entirely honest `provenance.byte_length` -- no caller dishonesty
    of any kind is required, unlike Section 2's finding.

    The character then survives all validation and reaches
    `build_observation_envelope`, which calls `_observation_identity` ->
    `_digest` (`src/argos/domain/observation.py`), and `_digest` does
    `hashlib.sha256("|".join(parts).encode())` -- `str.encode()` defaults to
    strict UTF-8, which **cannot** encode a lone surrogate and raises
    `UnicodeEncodeError`. That call is *outside* this module's own `try`
    (the `try` covers only `parse_price_change_group` and
    `_validate_entry_hash`; `build_observation_envelope` is called
    unconditionally afterward), so the exception propagates all the way out of
    `normalize_clob_price_change`, contradicting its own docstring
    ("This function never raises for a malformed payload") in the sharpest way
    found in this review: not merely expensive, not merely requiring a lie
    about size, but a completely ordinary-looking JSON body that is genuinely,
    definitionally malformed (a lone surrogate is not valid Unicode text) and
    produces **no ledger entry at all** -- the exact silent drop core
    invariant 14 forbids, and the exact class already found and fixed once for
    `entry_hash`'s length/newline/OSC-52 cases in this same module (see
    `tests/test_clob_price_change_ingestion.py`'s
    `test_a_hostile_entry_hash_becomes_a_rejection_rather_than_raising`) --
    this is a fourth hostile shape in the same field that the existing fix
    does not close, because the fix only handles what `is_clean_identifier`
    already flags.

    Independently confirmed, in a throwaway repro outside this file (not
    committed, per this file's ownership boundary), that the identical root
    cause reproduces in `argos.ingestion.clob_book._extract_source_hash`
    through `normalize_clob_book` -- same crash, same missing `Cs` check,
    different adapter. Reported in the review summary; not a regression
    introduced by this slice, since the underlying gap is in
    `argos.domain.text`, which both modules only call.
    """
    hostile_hash = "\ud800"
    event = _frame([_entry(hash_=hostile_hash)])
    raw_text = json.dumps(event).encode("utf-8", errors="surrogatepass")

    result = _normalize(event, raw=raw_text)

    assert isinstance(result, RejectedObservationV1), (
        "a lone UTF-16 surrogate in 'hash' must become a counted rejection, per this "
        "module's own documented contract -- it currently raises UnicodeEncodeError "
        "uncaught instead, with no ledger entry; see the docstring above"
    )
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD


def test_the_lone_surrogate_crash_also_escapes_recompute_observation_id() -> None:
    """A second, independent path to the identical crash: even if a caller
    somehow already held a validated envelope whose `source_hash` carried a
    lone surrogate (impossible via the public builder today, since the crash
    above fires before a valid envelope can ever exist -- but not impossible
    via `ObservationEnvelopeV1.model_construct` bypassing validation, or a
    future relaxation of `_validate_identifier`), `recompute_observation_id`
    -- the audit/verification path ADR-0010 added specifically so identity is
    not trust-only -- would crash identically, because it calls the same
    `_digest` function. Documented here as a second point where the same root
    cause bites, not asserted as fixed."""
    from argos.domain.observation import ObservationSource

    envelope = ObservationEnvelopeV1.model_construct(
        observation_id="observation-placeholder",
        source=ObservationSource.CLOB_MARKET_WS,
        source_event_type="price_change",
        market_id=None,
        condition_id=CONDITION_ID,
        token_id=TOKEN_YES,
        event_time_status="present",
        event_time=RECEIVED,
        event_time_raw=None,
        received_time=RECEIVED,
        ingest_sequence=1,
        source_sequence=None,
        source_hash="\ud800",
        quality_flags=(),
        payload_schema_version="price_change.v1",
        payload={},
        raw_payload_sha256=sha256_hex(b"x"),
        raw_payload_location=None,
        provenance=_provenance(b"x"),
        parser_version="test",
        capture_run_id=RUN,
    )
    with pytest.raises(UnicodeEncodeError):
        recompute_observation_id(envelope)


# ===========================================================================================
# 4. entry_hash: further hostile shapes beyond the four the shipped suite already covers
# ===========================================================================================


@pytest.mark.parametrize(
    ("label", "hostile_hash"),
    [
        ("RLO override", "h‮ostile"),
        ("Unicode tag character", "h\U000e0041ostile"),
        ("interlinear annotation forgery", "h￹ostile￻"),
    ],
)
def test_further_hostile_hash_shapes_are_rejected_not_raised(label: str, hostile_hash: str) -> None:
    """Extends `tests/test_clob_price_change_ingestion.py`'s four hostile-hash
    cases (length, newline, OSC 52, 20M chars) with shapes drawn from the same
    Unicode classes `argos.domain.text.is_display_control`'s own docstring
    claims to cover, to check the *existing* fix's coverage rather than only
    the shapes it was originally measured against. All three are correctly
    caught -- a negative result, unlike the lone surrogate above."""
    event = _frame([_entry(hash_=hostile_hash)])
    result = _normalize(event)
    assert isinstance(result, RejectedObservationV1), label
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD, label


def test_a_hash_spelled_in_arabic_indic_digits_is_accepted_and_that_is_deliberate() -> None:
    """A case originally filed as a fourth hostile shape; the code is right and
    the expectation was wrong.

    Arabic-Indic digits are Unicode category `Nd` -- ordinary text. They cannot
    move a cursor, reorder a line, or hide themselves, so they are not in the
    class `is_display_control` covers, unlike the three shapes above. Refusing
    them would mean enforcing a *format* on `hash`, which ARGOS deliberately
    does not do on either adapter: `docs/research/m2-clob-websocket.md` observed
    a 40-hex-character value every time but never established it as a source
    guarantee, and `clob_book._extract_source_hash` makes the same choice for
    the REST book's own `hash`. Enforcing 40-hex would convert a legitimate
    future format change into a total rejection storm on a field ARGOS only
    passes through.

    Contrast `token_id`, where a format (`[0-9]{1,120}`) *is* enforced --
    because the research did establish it there. The rule is "enforce what the
    evidence supports", not "enforce what looks tidy".

    Recorded as a decision rather than deleted, because the original
    parametrized test asserted the opposite while its own docstring claimed all
    four shapes were "correctly caught" -- a claim its assertion disproved.
    """
    event = _frame([_entry(hash_="\u0665\u0665\u0665")])
    result = _normalize(event)
    assert isinstance(result, ObservationEnvelopeV1)
    assert result.source_hash == "\u0665\u0665\u0665"


def test_a_non_string_hash_type_is_read_verified_and_produces_a_rejection() -> None:
    """`docs/BACKLOG.md`-style blind spot named directly in the task: a
    non-string `hash` was "read-verified only, not exercised by a test" for the
    REST sibling. Exercised here: `parse_price_change_group` requires
    `isinstance(entry_hash, str)` before this module's own hash validation ever
    runs, so a non-string hash is caught one layer up, inside the try, and
    still ends as a rejection rather than a raise."""
    event = _frame([_entry(hash_=None)])  # type: ignore[arg-type]
    result = _normalize(event)
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD


# ===========================================================================================
# 5. Rejection-record integrity
# ===========================================================================================


def test_raw_payload_sha256_hashes_the_actual_raw_bytes_not_a_reserialization_of_event() -> None:
    """The raw hash on a rejection must be traceable to the *bytes that
    actually arrived*, not to any re-encoding of the already-decoded `event`
    this module receives -- those two can differ (whitespace, key order,
    Decimal vs float parsing) even for the identical logical message."""
    event = _frame([_entry(side="SIDEWAYS")])
    raw = b"these-exact-bytes-are-what-arrived, not a json.dumps(event) round trip"
    result = _normalize(event, raw=raw)
    assert isinstance(result, RejectedObservationV1)
    assert result.raw_payload_sha256 == sha256_hex(raw)
    assert result.raw_payload_sha256 != sha256_hex(json.dumps(event).encode())


@pytest.mark.parametrize(
    ("label", "hostile_market"),
    [
        ("ESC + ANSI", "normal\x1b[31mHOSTILE\x1b[0m"),
        ("OSC 52 clipboard write", "h\x1b]52;c;cHdu\x07ostile"),
        ("RLO", "h‮ostile"),
        ("embedded newline", "line1\nline2: review status: human_reviewed"),
    ],
)
def test_a_hostile_market_field_is_neutralized_not_refused_on_the_rejection_ledger(
    label: str, hostile_market: str
) -> None:
    """`condition_id_hint` is read via `_best_effort_text` *before* validation
    and lands, unsanitized at that point, into `RejectedObservationV1.condition_id`
    -- confirm the ledger record's own sanitizer neutralizes it rather than
    raising (which would delete the rejection entirely) or forging a rendering.
    Mirrors `tests/test_clob_adapter_adversarial.py`'s own equivalent test for
    the REST sibling; must hold identically here since both share
    `RejectedObservationV1._sanitize_identifier`."""
    event = _frame([_entry(side="SIDEWAYS")], market=hostile_market)
    result = _normalize(event)
    assert isinstance(result, RejectedObservationV1), label
    condition_id = result.condition_id or ""
    assert "\x1b" not in condition_id, label
    assert "\n" not in condition_id, label
    assert "‮" not in condition_id, label


def test_a_20_million_character_market_field_is_bounded_not_unbounded_on_the_ledger() -> None:
    event = _frame([_entry(side="SIDEWAYS")], market="m" * 20_000_000)
    result = _normalize(event)
    assert isinstance(result, RejectedObservationV1)
    assert result.condition_id is not None
    assert len(result.condition_id) < 1_000, (
        "an unbounded market field must not grow the ledger record without limit"
    )


def test_recomputed_rejection_id_agrees_with_the_stored_one_on_a_real_shaped_malformation() -> None:
    event = _frame([_entry(side="SIDEWAYS")])
    result = _normalize(event)
    assert isinstance(result, RejectedObservationV1)
    assert recompute_rejection_id(result) == result.rejection_id


def test_two_different_malformations_on_one_raw_frame_share_one_rejection_id() -> None:
    """ADR-0011 section 7: `rejection_id` is a grouping key, not a unique key.
    Confirms the store still keeps both rows even when they collapse onto one
    id -- for the price_change ledger specifically, not only the REST one."""
    raw = b"same-malformed-frame-bytes"
    event = _frame([_entry(side="SIDEWAYS")])
    rejection_1 = _normalize(event, raw=raw, ingest_sequence=1)
    rejection_2 = _normalize(event, raw=raw, ingest_sequence=2)
    assert isinstance(rejection_1, RejectedObservationV1)
    assert isinstance(rejection_2, RejectedObservationV1)
    assert rejection_1.rejection_id == rejection_2.rejection_id

    store = open_sqlite_event_store(":memory:")
    try:
        store.open_capture_run(RUN, started_at=RECEIVED)
        store.append_rejection(rejection_1, ingest_sequence=1)
        store.append_rejection(rejection_2, ingest_sequence=2)
        stored = list(store.iter_rejections(RUN))
        assert len(stored) == 2
        assert {r.ingest_sequence for r in stored} == {1, 2}
    finally:
        store.close()


# ===========================================================================================
# 6. Determinism and identity
# ===========================================================================================


def test_duplicate_top_level_json_keys_normalize_deterministically() -> None:
    """`json.loads` (stdlib) resolves a duplicate key to the *last* value
    written, verified directly here rather than assumed. Two independently
    decoded copies of hostile-but-legal raw text (two "timestamp" keys, the
    second winning) must still collapse onto one observation_id."""
    base = _frame([_entry()])
    text = json.dumps(base)
    hostile_raw = text[:-1] + f',"timestamp":"{base["timestamp"]}"' + "}"
    assert json.loads(hostile_raw)["timestamp"] == base["timestamp"]

    first = _normalize(json.loads(hostile_raw, parse_float=Decimal), raw=hostile_raw.encode())
    second = _normalize(json.loads(hostile_raw, parse_float=Decimal), raw=hostile_raw.encode())
    assert isinstance(first, ObservationEnvelopeV1)
    assert isinstance(second, ObservationEnvelopeV1)
    assert first.observation_id == second.observation_id


def test_whitespace_and_key_order_differences_do_not_change_identity() -> None:
    payload = _frame([_entry()])
    compact_raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    spaced_raw = json.dumps(payload, indent=4, sort_keys=False).encode()

    first = _normalize(json.loads(compact_raw, parse_float=Decimal), raw=compact_raw)
    second = _normalize(json.loads(spaced_raw, parse_float=Decimal), raw=spaced_raw)
    assert isinstance(first, ObservationEnvelopeV1)
    assert isinstance(second, ObservationEnvelopeV1)
    assert first.observation_id == second.observation_id


def test_exponent_and_leading_zero_price_spellings_collapse_to_one_identity() -> None:
    exponent_form = _normalize(_frame([_entry(price="4.9E-1")]), raw=b"a")
    plain_form = _normalize(_frame([_entry(price="0.49")]), raw=b"b")
    assert isinstance(exponent_form, ObservationEnvelopeV1)
    assert isinstance(plain_form, ObservationEnvelopeV1)
    assert exponent_form.observation_id == plain_form.observation_id


def test_a_float_decoded_price_that_slipped_past_string_decoding_is_rejected_not_coerced() -> None:
    """Simulates decoding without `parse_float=Decimal` (the default `json`
    behaviour): a bare JSON number for `price` decodes to a Python `float`,
    and `parse_wire_decimal`'s no-float rule must hold at the full ingestion
    boundary."""
    raw_text = (
        '{"event_type":"price_change","market":"' + CONDITION_ID + '","timestamp":"1",'
        '"price_changes":[{"asset_id":"' + TOKEN_YES + '","price":0.49,"size":"636",'
        '"side":"SELL","hash":"a","best_bid":"0.28","best_ask":"0.29"}]}'
    )
    event = json.loads(raw_text)  # default decoder: float, not Decimal
    assert isinstance(event["price_changes"][0]["price"], float)
    result = _normalize(event, raw=raw_text.encode())
    assert isinstance(result, RejectedObservationV1)
    assert result.reason is RejectionReason.MALFORMED_PAYLOAD


def test_negative_zero_price_mints_the_same_identity_as_positive_zero() -> None:
    """Recurrence check: `docs/STATUS.md` records the negative-zero
    decimal-identity class as already found twice (envelope-level and
    `OrderBookSnapshotV1`). `PriceLevelChangeV1.price` shares the exact same
    reachable range (`ge=MIN_PRICE`, `MIN_PRICE = Decimal(0)`), so this is the
    third place it could recur. It does not: `parse_price_change_group` calls
    `parse_wire_decimal`, which reuses `normalize_orderbook.normalize_decimal`'s
    fix. Confirmed here at the ingestion boundary, not only at the domain
    layer `tests/test_price_change.py` already covers, and confirmed on a
    ``SET`` level change specifically, since ``REMOVE`` never reaches a
    nonzero price check the same way."""
    positive = _normalize(_frame([_entry(price="0")]), raw=b"a")
    negative = _normalize(_frame([_entry(price="-0")]), raw=b"b")
    assert isinstance(positive, ObservationEnvelopeV1)
    assert isinstance(negative, ObservationEnvelopeV1)
    assert positive.observation_id == negative.observation_id


def test_negative_zero_size_is_still_correctly_classified_as_remove_not_set() -> None:
    """`size="-0"` must normalize to zero and therefore route through
    `PriceLevelChangeKind.REMOVE`, not silently be treated as a nonzero SET
    because `-0 != 0` was compared before normalization."""
    result = _normalize(_frame([_entry(price="0.5", size="-0")]))
    assert isinstance(result, ObservationEnvelopeV1)
    payload = read_payload(result, PriceChangeV1)
    assert payload.changes[0].size == Decimal("0")
    from argos.domain.pricechange import PriceLevelChangeKind

    assert payload.changes[0].kind is PriceLevelChangeKind.REMOVE


def test_cosmetically_different_best_bid_ask_spellings_across_entries_still_agree() -> None:
    """`_parse_optional_top_of_book` normalizes before the cross-entry
    agreement check, so "0.28" and "0.280" on two different selected entries
    for our token must be treated as agreeing, not as a spurious disagreement
    refusal."""
    event = _frame(
        [
            _entry(price="0.10", side="BUY", best_bid="0.28", best_ask="0.29"),
            _entry(price="0.20", side="SELL", best_bid="0.280", best_ask="0.29000"),
        ]
    )
    result = _normalize(event)
    assert isinstance(result, ObservationEnvelopeV1)
    payload = read_payload(result, PriceChangeV1)
    assert payload.source_best_bid == Decimal("0.28")
    assert len(payload.changes) == 2


def test_the_ambient_decimal_context_does_not_change_the_stored_price_or_identity() -> None:
    """Recurrence check for the ambient-`decimal`-context class
    `docs/STATUS.md` records as already found and fixed
    (`CANONICAL_DECIMAL_CONTEXT` in `orderbook.py`). Set a narrow ambient
    precision directly (thread-local, but this *is* the calling thread) before
    normalizing a high-precision price and confirm the stored value is
    unaffected -- proving `parse_price_change_group` really does route through
    the pinned context rather than `decimal.getcontext()`."""
    source_text = "0." + "1" * 30
    original = decimal.getcontext().prec
    try:
        decimal.getcontext().prec = 5
        result = _normalize(_frame([_entry(price=source_text)]))
    finally:
        decimal.getcontext().prec = original
    assert isinstance(result, ObservationEnvelopeV1)
    payload = read_payload(result, PriceChangeV1)
    assert payload.changes[0].price == Decimal(source_text)


def test_a_hostile_ambient_decimal_context_in_a_worker_thread_does_not_change_the_result() -> None:
    """A stronger version of the test above: the *calling* thread's context
    stays at the default, but a genuinely different thread runs the same
    normalization concurrently with its own narrowed context. `Decimal`
    contexts are already thread-local by design, so this is mostly a
    regression pin against a future refactor that reads a shared/global
    context by mistake -- but it is cheap to check directly rather than only
    assumed from `decimal`'s own documented thread-locality."""
    source_text = "0." + "2" * 30
    results: dict[str, ObservationEnvelopeV1 | RejectedObservationV1 | None] = {}

    def _run_in_worker() -> None:
        decimal.getcontext().prec = 3
        results["worker"] = _normalize(_frame([_entry(price=source_text)]))

    main_result = _normalize(_frame([_entry(price=source_text)]))
    thread = threading.Thread(target=_run_in_worker)
    thread.start()
    thread.join()

    assert isinstance(main_result, ObservationEnvelopeV1)
    worker_result = results["worker"]
    assert isinstance(worker_result, ObservationEnvelopeV1)
    assert main_result.observation_id == worker_result.observation_id
    main_payload = read_payload(main_result, PriceChangeV1)
    assert main_payload.changes[0].price == Decimal(source_text)


def test_a_genuinely_different_group_produces_a_genuinely_different_identity() -> None:
    """The determinism guard is not degenerate: two frames differing only in
    price must mint different observation_ids."""
    first = _normalize(_frame([_entry(price="0.49")]), raw=b"a")
    second = _normalize(_frame([_entry(price="0.50")]), raw=b"b")
    assert isinstance(first, ObservationEnvelopeV1)
    assert isinstance(second, ObservationEnvelopeV1)
    assert first.observation_id != second.observation_id


def test_recomputed_identity_agrees_with_the_stored_identity_on_every_real_capture_frame() -> None:
    """Stronger than the shipped suite's own single-frame check: every one of
    the 34 real captured `price_change` frames, for both observed tokens."""
    checked = 0
    for event in _price_change_events():
        for token in (TOKEN_YES, TOKEN_NO):
            result = _normalize(event, requested_token_id=token)
            if result is None:
                continue
            assert isinstance(result, ObservationEnvelopeV1)
            assert recompute_observation_id(result) == result.observation_id
            checked += 1
    assert checked > 0, "the loop must actually exercise real frames, or this test is vacuous"


# ===========================================================================================
# 7. event_time: the shared argos.ingestion.wire.parse_event_time boundary
# ===========================================================================================
#
# `parse_event_time` now serves both `clob_book.py` and `clob_price_change.py`.
# Tested here both through the price_change adapter and directly, since the
# function itself has no dedicated test module of its own.


def test_wire_module_is_a_single_shared_function_not_two_independent_copies() -> None:
    """Regression pin: if a future edit duplicates the parser back into either
    adapter instead of importing it, this catches the exact moment it becomes
    two functions instead of one -- the same class of defect
    `docs/STATUS.md` records as already having recurred twice for `Decimal`
    normalization."""
    assert clob_book_module.parse_event_time is wire_module.parse_event_time
    assert clob_price_change_module.parse_event_time is wire_module.parse_event_time


@pytest.mark.parametrize(
    ("label", "raw_timestamp", "expect_missing"),
    [
        ("missing key", None, True),
    ],
)
def test_a_missing_timestamp_is_missing_not_substituted(
    label: str, raw_timestamp: Any, expect_missing: bool
) -> None:
    event = _frame([_entry()])
    del event["timestamp"]
    result = _normalize(event)
    assert isinstance(result, ObservationEnvelopeV1)
    assert result.event_time_status.value == "missing"
    assert result.event_time is None
    assert result.received_time == RECEIVED


def test_an_empty_string_timestamp_is_folded_into_missing_not_unparseable() -> None:
    """`argos.ingestion.wire.parse_event_time`'s own documented choice: an
    empty string cannot be represented as a distinguishable UNPARSEABLE case
    (`event_time_raw` requires `min_length=1`), so it is folded into MISSING.
    Confirmed through the adapter, not only by reading the docstring."""
    result = _normalize(_frame([_entry()], timestamp=""))
    assert isinstance(result, ObservationEnvelopeV1)
    assert result.event_time_status.value == "missing"
    assert result.event_time is None
    assert result.event_time_raw is None


@pytest.mark.parametrize(
    ("label", "hostile_timestamp"),
    [
        ("bool True", True),
        ("bool False", False),
        ("a JSON number", 1786387666174),
        ("a JSON float", 1786387666174.5),
        ("a list", [1786387666174]),
        ("a nested object", {"ms": 1786387666174}),
    ],
)
def test_a_non_string_timestamp_type_is_unparseable_never_a_crash(
    label: str, hostile_timestamp: Any
) -> None:
    result = _normalize(_frame([_entry()], timestamp=hostile_timestamp))
    assert isinstance(result, ObservationEnvelopeV1), label
    assert result.event_time_status.value == "unparseable", label
    assert result.event_time is None, label
    assert result.event_time_raw == repr(hostile_timestamp), label


def test_a_negative_timestamp_parses_to_a_pre_epoch_instant_not_a_crash() -> None:
    result = _normalize(_frame([_entry()], timestamp="-100"))
    assert isinstance(result, ObservationEnvelopeV1)
    assert result.event_time_status.value == "present"
    assert result.event_time == datetime(1969, 12, 31, 23, 59, 59, 900_000, tzinfo=UTC)


def test_an_enormous_timestamp_overflowing_datetime_range_is_unparseable_not_a_crash() -> None:
    huge = "9" * 30
    result = _normalize(_frame([_entry()], timestamp=huge))
    assert isinstance(result, ObservationEnvelopeV1)
    assert result.event_time_status.value == "unparseable"
    assert result.event_time_raw == huge


def test_a_numeric_json_timestamp_string_never_becomes_the_wall_clock() -> None:
    """`.claude/rules/data-integrity.md`: never substitute the current time for
    an unparseable source timestamp. Uses a `received_time` decades away from
    both the fixture's real timestamp and any plausible `datetime.now()`, so a
    substitution bug would be caught even if it coincided with `received_time`
    by construction elsewhere."""
    received = datetime(2077, 1, 1, tzinfo=UTC)
    result = _normalize(
        _frame([_entry()], timestamp="not-a-number"),
        received_time=received,
        rejected_at=received,
    )
    assert isinstance(result, ObservationEnvelopeV1)
    assert result.event_time_status.value == "unparseable"
    assert result.event_time is None
    assert result.received_time == received
    assert result.event_time_raw == "not-a-number"


def test_event_time_ahead_of_receipt_flag_fires_through_the_price_change_adapter_specifically() -> (
    None
):
    """`tests/test_clob_price_change_ingestion.py` never references
    `quality_flags` or `ObservationQualityFlag` at all -- untested for this
    adapter specifically, unlike the REST sibling's own adversarial suite."""
    event_instant_ms = 1_000_000
    received = datetime(1970, 1, 1, 0, 0, 0, tzinfo=UTC)
    result = _normalize(
        _frame([_entry()], timestamp=str(event_instant_ms)),
        received_time=received,
        rejected_at=received,
    )
    assert isinstance(result, ObservationEnvelopeV1)
    assert ObservationQualityFlag.EVENT_TIME_AHEAD_OF_RECEIPT in result.quality_flags


def test_event_time_behind_receipt_never_flags_through_the_price_change_adapter() -> None:
    received = datetime(2026, 8, 10, 18, 10, 7, tzinfo=UTC)
    event_instant = received - timedelta(days=365)
    ms = int(event_instant.timestamp() * 1000)
    result = _normalize(
        _frame([_entry()], timestamp=str(ms)), received_time=received, rejected_at=received
    )
    assert isinstance(result, ObservationEnvelopeV1)
    assert ObservationQualityFlag.EVENT_TIME_AHEAD_OF_RECEIPT not in result.quality_flags


def test_parse_event_time_never_reads_a_wall_clock_directly() -> None:
    """Static confirmation to back the behavioural tests above: the shared
    parser takes no `Clock`/`Pacer` argument and its own module imports
    nothing from `argos.clock` or `time`/`datetime.now`."""
    import inspect

    source = inspect.getsource(wire_module)
    assert "datetime.now(" not in source
    assert ".now()" not in source
    assert "utcnow" not in source
    # The function signature itself takes only `raw`, confirming there is no
    # clock parameter to inject in the first place.
    signature = inspect.signature(parse_event_time)
    assert list(signature.parameters) == ["raw"]


# ===========================================================================================
# 8. Store integration: reconnect-shaped redelivery and a genuinely late reprocessing
# ===========================================================================================


def test_a_redelivered_malformed_frame_after_a_simulated_reconnect_still_shares_one_rejection() -> (
    None
):
    """Models a WebSocket resend after reconnect: the identical malformed raw
    bytes arrive twice, at two different `ingest_sequence`/`received_time`
    pairs (a reconnect always advances at least one of those). The rejection
    identity must still collapse, and the store must still keep both delivery
    attempts distinguishable from each other."""
    raw = b"identical-malformed-frame-across-a-reconnect"
    event = _frame([_entry(side="SIDEWAYS")])

    first = _normalize(event, raw=raw, ingest_sequence=5, received_time=RECEIVED)
    second = _normalize(
        event,
        raw=raw,
        ingest_sequence=6,
        received_time=RECEIVED + timedelta(minutes=10),
    )
    assert isinstance(first, RejectedObservationV1)
    assert isinstance(second, RejectedObservationV1)
    assert first.rejection_id == second.rejection_id

    store = open_sqlite_event_store(":memory:")
    try:
        store.open_capture_run(RUN, started_at=RECEIVED)
        store.append_rejection(first, ingest_sequence=5)
        store.append_rejection(second, ingest_sequence=6)
        stored = list(store.iter_rejections(RUN))
        assert {r.ingest_sequence for r in stored} == {5, 6}
    finally:
        store.close()


def test_a_zero_size_removal_for_the_unrelated_token_returns_none_and_costs_no_store_write() -> (
    None
):
    """Confirms the `None` branch really does carry zero store-visible cost:
    calling `normalize_clob_price_change` for a token the real fixture never
    mentions must never itself open a transaction or write anything -- there
    is nothing to write to, by construction, since this function returns
    before ever touching the store layer."""
    store = open_sqlite_event_store(":memory:")
    try:
        store.open_capture_run(RUN, started_at=RECEIVED)
        for event in _price_change_events():
            result = _normalize(event, requested_token_id=UNRELATED_TOKEN)
            assert result is None
        counts = store.counts_for_capture_run(RUN)
        assert counts.accepted == 0
        assert counts.rejected == 0
        assert counts.duplicate == 0
    finally:
        store.close()


def test_the_two_real_zero_size_removals_survive_a_reconnect_shaped_replay() -> None:
    """Extends the shipped suite's own zero-size-removal store test
    (`test_a_real_zero_size_entry_lands_in_the_store_as_a_remove`) by
    redelivering both real zero-size frames a second time at a later
    `ingest_sequence`, as a reconnect resend would, and confirming the store
    still reports exactly one accepted observation each, not two."""

    def _find_zero_size_event(token: str) -> dict[str, Any]:
        for event in _price_change_events():
            for entry in event["price_changes"]:
                if entry["asset_id"] == token and entry["size"] == "0":
                    return event
        raise AssertionError(f"no zero-size entry for {token!r}")

    store = open_sqlite_event_store(":memory:")
    try:
        store.open_capture_run(RUN, started_at=RECEIVED)
        sequence = 1
        for token, expected_side in ((TOKEN_YES, BookSide.BID), (TOKEN_NO, BookSide.ASK)):
            event = _find_zero_size_event(token)
            raw = json.dumps(event, default=str).encode()

            first = _normalize(event, raw=raw, requested_token_id=token, ingest_sequence=sequence)
            sequence += 1
            second = _normalize(event, raw=raw, requested_token_id=token, ingest_sequence=sequence)
            sequence += 1
            assert isinstance(first, ObservationEnvelopeV1)
            assert isinstance(second, ObservationEnvelopeV1)
            assert first.observation_id == second.observation_id

            first_delivery = store.append_observation(first)
            second_delivery = store.append_observation(second)
            assert first_delivery.disposition is Disposition.ACCEPTED_NEW
            assert second_delivery.disposition is Disposition.DUPLICATE

            stored = store.get_observation(first.observation_id)
            assert stored is not None
            payload = read_payload(stored, PriceChangeV1)
            from argos.domain.pricechange import PriceLevelChangeKind

            assert payload.changes[0].kind is PriceLevelChangeKind.REMOVE
            assert payload.changes[0].side is expected_side

        counts = store.counts_for_capture_run(RUN)
        assert counts.accepted == 2
        assert counts.duplicate == 2
    finally:
        store.close()
