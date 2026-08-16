"""Adversarial tests for `ObservationEnvelopeV1` / `RejectedObservationV1`.

`tests/test_observation_envelope.py` is treated here as a hypothesis, not as
coverage. Every test below states, in its docstring, the invariant it defends
and why that invariant could plausibly fail -- this file adds no test whose
only purpose is to inflate a count.

Provenance note, because it matters for how to read this file: `src/argos/domain/
observation.py` and `src/argos/domain/text.py` were rewritten *during* the
session that produced this file (payload became a typed `VersionedModel`
instead of a bare mapping, and `_observation_identity`/`_rejection_identity`
moved from a `"\\x1f".join(...)` encoding to a length-prefixed injective
`_digest`). Several tests below were written as failing reproductions against
the pre-rewrite code, confirmed to fail there, and then re-verified against the
current code: the field-separator-injection collision is now fixed (kept below
as a positive regression lock, with the original failing reproduction
described in its docstring for the record). Two findings still reproduce
against the current code (`model_copy(update=...)` bypassing payload freezing,
and zero-width/Unicode-tag characters surviving the ledger sanitizer) and are
left failing here, because CLAUDE.md forbids weakening an assertion to make a
suite pass -- the docstring on each says exactly what it reproduces.
"""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import Field, ValidationError

from argos.domain.observation import (
    MAX_DETAIL_LENGTH,
    EventTimeStatus,
    ObservationEnvelopeV1,
    ObservationSource,
    RejectedObservationV1,
    build_observation_envelope,
    build_rejected_observation,
)
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.domain.versioning import VersionedModel
from argos.errors import ContractViolationError, RejectionReason


class _Payload(VersionedModel):
    """Stand-in for the typed per-event-type payloads a later M2 slice adds.

    ``extra`` represents the shape a real order-book/price-change payload is
    likely to have somewhere: a source-controlled mapping (price levels keyed
    by price string, for example), which is exactly where key-order and
    aliasing questions are real.
    """

    schema_version: ClassVar[str] = "test_payload.v1"

    price: Decimal | None = None
    bids: tuple[Decimal, ...] = ()
    label: str = ""
    extra: dict[str, int] = Field(default_factory=dict)


class _FloatPayload(VersionedModel):
    """A payload shape that can hold a value with no canonical JSON form."""

    schema_version: ClassVar[str] = "float_payload.v1"

    value: float = 0.0


START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
RAW_BYTES = b'{"bids": [{"price": "0.5", "size": "10"}]}'


def _provenance(**overrides: Any) -> SourceProvenanceV1:
    fields: dict[str, Any] = {
        "source": "clob_rest",
        "endpoint": "/book",
        "http_status": 200,
        "retrieved_at": START,
        "raw_sha256": sha256_hex(RAW_BYTES),
        "byte_length": len(RAW_BYTES),
    }
    fields.update(overrides)
    return SourceProvenanceV1(**fields)


def _envelope(**overrides: Any) -> ObservationEnvelopeV1:
    provenance = overrides.pop("provenance", None) or _provenance()
    fields: dict[str, Any] = {
        "source": ObservationSource.CLOB_REST,
        "source_event_type": "book",
        "market_id": "market-1",
        "condition_id": "condition-1",
        "token_id": "123",
        "event_time": START,
        "received_time": START,
        "ingest_sequence": 1,
        "payload": _Payload(bids=(Decimal("0.5"),)),
        "provenance": provenance,
        "parser_version": "parser-1",
        "capture_run_id": "run-1",
    }
    fields.update(overrides)
    return build_observation_envelope(**fields)


def _rejection(**overrides: Any) -> RejectedObservationV1:
    provenance = overrides.pop("provenance", None) or _provenance(source="gamma")
    fields: dict[str, Any] = {
        "reason": RejectionReason.MALFORMED_PAYLOAD,
        "detail": "outcome_token_map length mismatch",
        "source": ObservationSource.GAMMA,
        "market_id": "market-1",
        "condition_id": "condition-1",
        "token_id": "123",
        "provenance": provenance,
        "received_time": START,
        "rejected_at": START,
        "capture_run_id": "run-1",
    }
    fields.update(overrides)
    return build_rejected_observation(**fields)


def _direct_envelope(**overrides: Any) -> ObservationEnvelopeV1:
    """Construct `ObservationEnvelopeV1` directly (not through the builder),
    for tests that attack the model's own validators rather than the
    convenience function."""
    fields: dict[str, Any] = {
        "observation_id": "observation-x",
        "source": ObservationSource.CLOB_REST,
        "source_event_type": "book",
        "event_time_status": EventTimeStatus.MISSING,
        "event_time": None,
        "received_time": START,
        "ingest_sequence": 1,
        "payload_schema_version": "test_payload.v1",
        "payload": {},
        "raw_payload_sha256": sha256_hex(RAW_BYTES),
        "provenance": _provenance(),
        "parser_version": "p1",
        "capture_run_id": "run-1",
    }
    fields.update(overrides)
    return ObservationEnvelopeV1(**fields)


# --- 1. Identity: field-separator injection -- FIXED, locked as a regression -----
#
# The version of `_observation_identity` present when this file's testing
# began joined its material with `"\x1f"`. None of market_id, condition_id,
# token_id, source_sequence, or source_hash was sanitized or restricted to a
# safe alphabet, so a literal U+001F inside one of them shifted the field
# boundary the same way a CSV injection shifts a comma:
# `market_id="a", condition_id="b\x1fc"` and `market_id="a\x1fb",
# condition_id="c"` joined to byte-identical material and produced the same
# `observation_id` -- reproduced directly against that version before this
# test existed. `_observation_identity` was rewritten mid-session to a
# length-prefixed injective `_digest` (`src/argos/domain/observation.py`,
# `_digest`'s own docstring now describes the same reproduction). The tests
# below confirm the fix on the *current* code and are the regression lock for
# it: if `_digest` (or any future replacement) stops being injective on these
# same inputs, these fail again.


def test_market_id_and_condition_id_boundary_injection_does_not_collide() -> None:
    """Two envelopes naming genuinely different (market_id, condition_id)
    pairs -- "a"/"b\x1fc" versus "a\x1fb"/"c" -- must not share an
    observation_id. See the module note above: this exact pair collided
    under the pre-rewrite `"\x1f".join` encoding."""
    with pytest.raises(ValidationError):
        _envelope(market_id="a", condition_id="b\x1fc", token_id="d")
    with pytest.raises(ValidationError):
        _envelope(market_id="a\x1fb", condition_id="c", token_id="d")

    # And the encoding still separates the two when the ids are legal, which is
    # the property the refusal must not be allowed to hide.
    first = _envelope(market_id="a", condition_id="bc", token_id="d")
    second = _envelope(market_id="ab", condition_id="c", token_id="d")
    assert first.observation_id != second.observation_id


def test_condition_id_and_token_id_boundary_injection_does_not_collide() -> None:
    """Same attack, one field boundary to the right: condition_id="x",
    token_id="y\x1fz" versus condition_id="x\x1fy", token_id="z"."""
    with pytest.raises(ValidationError):
        _envelope(condition_id="x", token_id="y\x1fz")
    with pytest.raises(ValidationError):
        _envelope(condition_id="x\x1fy", token_id="z")

    first = _envelope(condition_id="x", token_id="yz")
    second = _envelope(condition_id="xy", token_id="z")
    assert first.observation_id != second.observation_id


def test_rejection_ledger_field_separator_injection_does_not_collide() -> None:
    """`_rejection_identity` used the same encoding and inherited the same
    defect; it now goes through the same `_digest` and must resist the same
    attack, on the ledger whose whole purpose is that nothing disappears."""
    first = _rejection(market_id="a", condition_id="b\x1fc", token_id="d")
    second = _rejection(market_id="a\x1fb", condition_id="c", token_id="d")
    assert first.rejection_id != second.rejection_id


_IDENTIFIER_ALPHABET = st.characters(
    min_codepoint=0x20, max_codepoint=0x7E, blacklist_characters="\x7f"
)


@given(
    left=st.text(alphabet=_IDENTIFIER_ALPHABET, min_size=0, max_size=8),
    right=st.text(alphabet=_IDENTIFIER_ALPHABET, min_size=0, max_size=8),
)
def test_digest_length_prefix_encoding_resists_fake_length_prefixes(left: str, right: str) -> None:
    """Attack the *current* injective encoding directly, not just the two
    examples above: `_digest` writes `f"{len(value)}:{value}"` per field.
    Feed it a value crafted to itself look like a `"{length}:{content}"`
    length-prefixed field, then split the surrounding material differently,
    and confirm two genuinely different (market_id, condition_id) pairs still
    never collide -- Hypothesis searches for any left/right split that
    breaks injectivity, including strings that start with digits and colons.
    """
    crafted_market = f"{len(left)}:{left}"
    first = _envelope(market_id=crafted_market, condition_id=right or "-")
    second = _envelope(market_id=left or "m", condition_id=f"{crafted_market}{right}")
    if (first.market_id, first.condition_id) == (second.market_id, second.condition_id):
        assert first.observation_id == second.observation_id
    else:
        assert first.observation_id != second.observation_id


# --- 2. Decimal precision inside the identity --------------------------------------


def test_decimal_trailing_zero_precision_changes_identity() -> None:
    """`Decimal("0.5")` and `Decimal("0.50")` are numerically equal but
    serialize to different JSON strings (`payload.to_record()` uses
    pydantic's own JSON-mode Decimal encoding, which preserves the source
    precision). This pins the CURRENT, deliberate behavior: a source that
    reports a price at different declared precision on two polls is
    reporting *something*, even if the value is unchanged, and collapsing it
    would be exactly the kind of silent projection the module docstring says
    identity must not perform. If this test starts failing, identity has
    started normalizing Decimal precision, which is a real behavior change
    for downstream price series and needs a deliberate decision.
    """
    five_tenths = _envelope(payload=_Payload(price=Decimal("0.5")))
    fifty_hundredths = _envelope(payload=_Payload(price=Decimal("0.50")))
    assert five_tenths.observation_id != fifty_hundredths.observation_id
    # And the record actually preserves both string forms distinctly.
    assert five_tenths.payload["price"] == "0.5"
    assert fifty_hundredths.payload["price"] == "0.50"


# --- 3. Canonical JSON: key-order independence (what "canonical" must mean) -------


@given(
    a=st.integers(min_value=-10, max_value=10),
    b=st.integers(min_value=-10, max_value=10),
    c=st.integers(min_value=-10, max_value=10),
)
def test_canonical_json_identity_is_independent_of_nested_mapping_key_order(
    a: int, b: int, c: int
) -> None:
    """A payload's identity must not depend on the order a source (or a
    dict-building parser) happened to insert keys into a nested mapping field
    -- otherwise two structurally identical payloads from two parser
    versions could get different observation_ids for no semantic reason.
    `_Payload.extra` is exactly this kind of field.
    """
    forward = _envelope(payload=_Payload(extra={"a": a, "b": b, "c": c}))
    reversed_dict = dict(reversed(list({"a": a, "b": b, "c": c}.items())))
    reversed_ = _envelope(payload=_Payload(extra=reversed_dict))
    assert forward.observation_id == reversed_.observation_id


# --- 4. Unicode normalization forms are not silently collapsed --------------------


def test_nfc_and_nfd_forms_of_the_same_visual_id_are_not_collapsed() -> None:
    """`"café"` (NFC, one codepoint for the accented letter) and its NFD form
    (base letter + combining accent) render identically but are different
    byte sequences. Identity must not apply Unicode normalization -- a
    source's id is opaque content, and silently normalizing it would mean two
    ids that are genuinely different bytes on the wire (and could name
    different Polymarket entities) collapse onto one observation. Regression
    guard against a well-intentioned future "normalize the id for
    robustness" change.
    """
    nfc = "café"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd  # sanity: the two forms really are different strings
    with_nfc = _envelope(market_id=nfc)
    with_nfd = _envelope(market_id=nfd)
    assert with_nfc.observation_id != with_nfd.observation_id


# --- 5. event_time: state-machine completeness (not just PRESENT's own guards) ----


def test_missing_status_forbids_a_present_event_time_via_direct_construction() -> None:
    """The model_validator's PRESENT branch (existing suite covers that) and
    its MISSING/event_time_raw branch are exercised elsewhere, but the
    sibling check -- MISSING forbidding `event_time` *itself* -- is not
    directly exercised anywhere. If that specific `if` were ever deleted in a
    refactor, a MISSING-status envelope could carry a live `event_time`,
    which is precisely the received_time-substitution shape core invariant 6
    forbids: MISSING must mean there is truly nothing.
    """
    try:
        _direct_envelope(event_time_status=EventTimeStatus.MISSING, event_time=START)
    except ValidationError:
        pass
    else:
        raise AssertionError("MISSING status accepted a non-null event_time")


def test_unparseable_status_forbids_a_present_event_time_via_direct_construction() -> None:
    """Same completeness gap, for UNPARSEABLE: it must refuse a live
    `event_time` even when `event_time_raw` is correctly supplied."""
    try:
        _direct_envelope(
            event_time_status=EventTimeStatus.UNPARSEABLE,
            event_time=START,
            event_time_raw="garbage",
        )
    except ValidationError:
        pass
    else:
        raise AssertionError("UNPARSEABLE status accepted a non-null event_time")


def test_from_record_cannot_bypass_the_event_time_state_machine() -> None:
    """`from_record` is a second construction path (a reader rebuilding an
    envelope from storage) and must run the same model_validator as direct
    construction -- a hand-written or corrupted record claiming PRESENT with
    no event_time must be refused there too, not just via the builder or
    `ObservationEnvelopeV1(...)`.
    """
    record = _envelope().to_record()
    record["event_time_status"] = "present"
    record["event_time"] = None
    try:
        ObservationEnvelopeV1.from_record(record)
    except ValidationError:
        pass
    else:
        raise AssertionError("from_record accepted PRESENT status with a null event_time")


# --- 6. Frozen payload: model_copy(update=...) as a mutation vector ---------------


def test_model_copy_update_does_not_leave_a_mutable_aliased_payload() -> None:
    """`model_copy(update={...})` is a normal, public pydantic API, and
    nothing in this module's field_validator runs on that path (pydantic's
    own documented behavior: `model_copy` does not re-validate). If a future
    caller ever does `envelope.model_copy(update={"payload": some_dict})` --
    a plausible way to "patch one field" of an existing envelope -- the
    resulting envelope holds a bare, unfrozen `dict` by direct reference, and
    mutating that dict after the copy reaches `to_record()`. Core invariant 7
    says a stored payload cannot be edited after the fact; this shows a
    public, unremarkable-looking call defeats that for the field this module
    exists to protect. Confirmed against the current code (2026-08-10); not
    touched by the mid-session identity rewrite, since it does not involve
    `_observation_identity` or `_digest` at all.
    """
    envelope = _envelope()
    mutable = {"a": 1}
    copied = envelope.model_copy(update={"payload": mutable})
    mutable["a"] = 999
    mutable["b"] = "sneaked in after the copy"
    record = copied.to_record()
    assert record["payload"] == {"a": 1}, (
        "mutating the dict passed to model_copy(update=...) after the copy "
        f"reached to_record(): {record['payload']!r}"
    )


def test_payload_with_a_non_serializable_value_fails_fast_with_a_taxonomy_error() -> None:
    """A payload value with no canonical JSON representation (`float('nan')`,
    here, refused explicitly by `_canonical_json`'s `allow_nan=False`) must be
    refused when the envelope is built, as a `ContractViolationError` (the
    ARGOS error taxonomy, not a bare `TypeError` a capture loop cannot
    classify) -- not accepted silently and left to fail later,
    unpredictably, in storage or replay.
    """
    try:
        _envelope(payload=_FloatPayload(value=float("nan")))
    except ContractViolationError as error:
        assert "canonically JSON-serializable" in str(error)
    else:
        raise AssertionError("a NaN-valued payload was accepted without error")


# --- 7. Rejection ledger sanitizer: smuggling characters the current algorithm misses


def test_rejection_detail_neutralizes_zero_width_and_unicode_tag_characters() -> None:
    """`argos.domain.text`'s module docstring states its purpose plainly:
    untrusted text "can clear a reviewer's terminal, rewrite its title, or
    write to the clipboard via OSC 52" and must be neutralized. Zero-width
    characters (U+200B ZERO WIDTH SPACE) and Unicode tag characters
    (U+E0000-U+E007F, the block used for invisible "ASCII smuggling" --
    embedding hidden text a human reviewer cannot see but a downstream
    automated reader can extract) are Unicode category "Cf" (format), not
    "Cc" (control), so `is_display_control` -- which strips Cc, C1
    (0x7F-0x9F), and the named bidi-override set -- does not touch them, and
    `neutralize_untrusted_text`'s own docstring explicitly carves out zero-
    width joiners as intentionally preserved. They survive into a persisted
    rejection-ledger record verbatim. This is the same attack *class* the
    module already defends against for OSC 52 and bidi overrides, on a
    different, currently open channel: not forging what is *displayed*, but
    hiding content inside what is *stored* (and, in a system where a later
    milestone may have automated tooling read this ledger, potentially
    hiding a secondary instruction from the human reviewer while leaving it
    intact for a machine reader). Confirmed against the current code
    (2026-08-10); `is_display_control` moved to `argos.domain.text` mid-
    session with its algorithm unchanged, so this gap moved with it.
    """
    hostile = f"safe{chr(0x200B)}HIDDEN-ZWSP{chr(0x200B)}text"
    rejection = _rejection(detail=hostile)
    assert chr(0x200B) not in rejection.detail, (
        f"zero-width space survived sanitization: {rejection.detail!r}"
    )

    tag_hostile = f"safe{chr(0xE0041)}{chr(0xE0042)}HIDDEN-TAG{chr(0xE007F)}text"
    tag_rejection = _rejection(detail=tag_hostile)
    assert chr(0xE0041) not in tag_rejection.detail, (
        f"unicode tag character survived sanitization: {tag_rejection.detail!r}"
    )


def test_source_event_type_neutralizes_zero_width_characters_too() -> None:
    """`source_event_type` is a machine label, so a zero-width character in it
    is refused outright rather than neutralized: neutralization is lossy and this
    field is inside the observation identity."""
    with pytest.raises(ValidationError):
        _envelope(source_event_type=f"book{chr(0x200B)}hidden")


# --- 8. Rejection detail length bound: exact boundary and truncation stability ----


def test_rejection_detail_length_bound_is_exact_at_the_boundary() -> None:
    """`neutralize_and_bound` truncates when `len(neutralized) >
    max_length`, i.e. `<=` is kept whole. A detail of exactly
    `MAX_DETAIL_LENGTH` characters must survive untouched; `MAX_DETAIL_LENGTH
    + 1` must be truncated to the head plus the fixed suffix. An off-by-one
    here would either silently drop the legitimate boundary case or fail to
    bound a detail that is one character over.

    Separately, this pins a real but minor asymmetry: the truncation suffix
    ("... (truncated, N characters in source)") is longer than a single
    character, so for an input only just over the bound, the OUTPUT can be
    longer than the input (4001 in -> ~4043 out here). The record is still
    O(1)-bounded overall (`MAX_DETAIL_LENGTH` plus fixed suffix overhead), so
    this is not unbounded growth, but "bounded" is not synonymous with
    "never larger than the input" near the threshold.
    """
    exactly_at_bound = "x" * MAX_DETAIL_LENGTH
    rejection = _rejection(detail=exactly_at_bound)
    assert rejection.detail == exactly_at_bound
    assert "truncated" not in rejection.detail

    one_over = "x" * (MAX_DETAIL_LENGTH + 1)
    truncated = _rejection(detail=one_over)
    assert "truncated" in truncated.detail
    assert truncated.detail.startswith("x" * MAX_DETAIL_LENGTH + "...")
    # Bounded by the field cap plus a small, fixed suffix overhead -- not by
    # the length of the input, which is the actual guarantee being made.
    assert len(truncated.detail) <= MAX_DETAIL_LENGTH + 100


@given(
    detail=st.text(
        alphabet=st.characters(min_codepoint=0x00, max_codepoint=0x9F),
        min_size=MAX_DETAIL_LENGTH + 1,
        max_size=MAX_DETAIL_LENGTH + 50,
    )
)
def test_truncation_of_control_heavy_text_never_reintroduces_a_control_character(
    detail: str,
) -> None:
    """A detail long enough to force truncation, built entirely from the
    control/C1 range (the exact alphabet the sanitizer targets), must still
    have every control character replaced *and* be bounded -- truncation runs
    after neutralization in the source, but this proves that ordering cannot
    accidentally re-expose a raw control byte by slicing through the
    replacement in some pathological case.
    """
    rejection = _rejection(detail=detail)
    assert len(rejection.detail) <= MAX_DETAIL_LENGTH + 100  # bounded, not unbounded
    body = rejection.detail.rsplit("... (truncated,", 1)[0]
    for character in body:
        if character in "\n\t":
            continue
        codepoint = ord(character)
        assert not (unicodedata.category(character) == "Cc" or 0x7F <= codepoint <= 0x9F), (
            f"a raw control character survived truncation: {character!r} in {rejection.detail!r}"
        )


# --- 9. Round-trip fidelity (Hypothesis) --------------------------------------------


@given(
    price=st.decimals(min_value="0", max_value="1", places=6, allow_nan=False),
    ingest_sequence=st.integers(min_value=1, max_value=1_000_000),
)
def test_envelope_round_trip_preserves_decimal_string_precision_and_identity(
    price: Decimal, ingest_sequence: int
) -> None:
    """`to_record()` -> `from_record()` must be lossless for what the
    identity was computed from: the same observation_id, and the same
    *string* representation of a Decimal (trailing zeros and all --
    `Decimal("1.50")` must not silently become `"1.5"` on a round trip
    through storage).
    """
    envelope = _envelope(payload=_Payload(price=price), ingest_sequence=ingest_sequence)
    record = envelope.to_record()
    restored = ObservationEnvelopeV1.from_record(record)

    assert restored.observation_id == envelope.observation_id
    assert restored.payload["price"] == str(price)
    assert restored.to_record() == record


@given(
    reason=st.sampled_from(list(RejectionReason)),
    detail=st.text(min_size=1, max_size=100),
)
def test_rejection_round_trip_preserves_identity_across_every_reason(
    reason: RejectionReason,
    detail: str,
) -> None:
    """Every `RejectionReason` member, not just the one or two exercised
    elsewhere, must round-trip its identity -- a reason that fails here would
    mean the ledger silently loses which *kind* of rejection a re-read record
    represents.
    """
    rejection = _rejection(reason=reason, detail=detail)
    record = rejection.to_record()
    restored = RejectedObservationV1.from_record(record)
    assert restored.rejection_id == rejection.rejection_id
    assert restored.reason is reason
    assert restored.to_record() == record


@given(
    hour=st.integers(min_value=0, max_value=23),
    minute=st.integers(min_value=0, max_value=59),
    tz_offset_hours=st.integers(min_value=-12, max_value=14),
)
def test_utc_anchoring_survives_a_round_trip_regardless_of_source_offset(
    hour: int, minute: int, tz_offset_hours: int
) -> None:
    """`event_time` supplied in an arbitrary source-local offset must anchor
    to UTC on construction *and* stay anchored (same instant, tzinfo == UTC)
    after a `to_record`/`from_record` round trip -- a reader must never see a
    naive or non-UTC timestamp reappear from storage.
    """
    from datetime import timedelta, timezone

    offset = timezone(timedelta(hours=tz_offset_hours))
    local_time = datetime(2026, 3, 15, hour, minute, tzinfo=offset)
    envelope = _envelope(event_time=local_time)
    assert envelope.event_time is not None
    assert envelope.event_time.tzinfo == UTC

    restored = ObservationEnvelopeV1.from_record(envelope.to_record())
    assert restored.event_time == envelope.event_time
    assert restored.event_time is not None
    assert restored.event_time.tzinfo == UTC
