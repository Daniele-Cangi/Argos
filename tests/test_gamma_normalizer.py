"""Normalization of Gamma market payloads.

Cases are built from the recorded fixture wherever possible, and hand-built only
for failures the live API does not conveniently produce.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from argos.domain.market import MarketDefinitionV1, QuarantinedMarketV1
from argos.errors import ContractViolationError, IngestionError, RejectionReason
from argos.ingestion import NORMALIZER_VERSION, normalize_market, normalize_markets

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gamma"
NORMALIZED_AT = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
DIGEST = "a" * 64


def _page() -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = json.loads(
        (FIXTURES / "markets_list.raw.json").read_text(encoding="utf-8")
    )
    return payload


def _market(**overrides: Any) -> dict[str, Any]:
    payload = deepcopy(_page()[0])
    payload.update(overrides)
    return payload


def _normalize(payload: dict[str, Any]) -> MarketDefinitionV1:
    return normalize_market(payload, raw_payload_sha256=DIGEST, normalized_at=NORMALIZED_AT)


# --- the happy path, against a real recorded payload --------------------------------


def test_a_recorded_market_normalizes() -> None:
    market = _normalize(_market())
    assert market.market_id
    assert market.condition_id.startswith("0x")
    assert market.question
    assert market.outcomes
    assert tuple(market.outcome_token_map) == market.outcomes
    assert market.raw_payload_sha256 == DIGEST
    assert market.normalizer_version == NORMALIZER_VERSION


def test_gamma_encodes_outcomes_as_a_json_string_and_we_decode_it() -> None:
    """The live payload really does deliver `outcomes` as `'["Yes", "No"]'`."""
    raw = _market()
    assert isinstance(raw["outcomes"], str)
    assert isinstance(raw["clobTokenIds"], str)
    assert _normalize(raw).outcomes == tuple(json.loads(raw["outcomes"]))


def test_an_already_decoded_list_is_accepted_too() -> None:
    """The same normalizer must survive Gamma dropping the string encoding."""
    raw = _market()
    decoded = _market(
        outcomes=json.loads(raw["outcomes"]),
        clobTokenIds=json.loads(raw["clobTokenIds"]),
    )
    assert _normalize(decoded) == _normalize(raw)


def test_normalization_is_deterministic() -> None:
    assert _normalize(_market()).to_record() == _normalize(_market()).to_record()


def test_the_whole_recorded_page_normalizes_without_quarantine() -> None:
    report = normalize_markets(_page(), raw_payload_sha256=DIGEST, normalized_at=NORMALIZED_AT)
    assert report.quarantined == ()
    assert len(report.accepted) == len(_page())
    assert report.total == len(_page())


def test_money_fields_become_decimals_without_float_noise() -> None:
    market = _normalize(_market(liquidity="16085.17368", orderPriceMinTickSize=0.001))
    assert market.liquidity == Decimal("16085.17368")
    assert market.tick_size == Decimal("0.001")


def test_timestamps_are_anchored_to_utc() -> None:
    market = _normalize(_market(endDate="2026-06-01T00:00:00Z"))
    assert market.end_time == datetime(2026, 6, 1, tzinfo=UTC)
    assert market.end_time.tzinfo is UTC


def test_the_original_rule_material_is_preserved_verbatim() -> None:
    """Core invariant 3: the question title is not the resolution contract."""
    raw = _market()
    market = _normalize(raw)
    assert market.question == raw["question"]
    assert market.description == raw["description"]


def test_a_record_round_trips_through_storage() -> None:
    market = _normalize(_market())
    assert MarketDefinitionV1.from_record(market.to_record()) == market


def test_canonical_key_sorting_does_not_change_outcome_mapping_semantics() -> None:
    market = _normalize(_market())
    record = market.to_record()
    record["outcome_token_map"] = dict(sorted(record["outcome_token_map"].items()))

    restored = MarketDefinitionV1.from_record(record)

    assert restored == market
    assert tuple(restored.outcome_token_map) == restored.outcomes


# --- token mapping failures ---------------------------------------------------------


def test_more_outcomes_than_tokens_is_quarantined() -> None:
    with pytest.raises(IngestionError) as caught:
        _normalize(_market(outcomes='["Yes", "No", "Maybe"]'))
    assert caught.value.reason is RejectionReason.QUARANTINED_MAPPING


def test_more_tokens_than_outcomes_is_quarantined() -> None:
    with pytest.raises(IngestionError) as caught:
        _normalize(_market(clobTokenIds='["1", "2", "3"]'))
    assert caught.value.reason is RejectionReason.QUARANTINED_MAPPING


def test_an_empty_outcome_list_is_quarantined() -> None:
    with pytest.raises(IngestionError) as caught:
        _normalize(_market(outcomes="[]", clobTokenIds="[]"))
    assert caught.value.reason is RejectionReason.QUARANTINED_MAPPING


def test_duplicate_token_ids_are_quarantined() -> None:
    """Two outcomes sharing a token would price both sides identically."""
    with pytest.raises(IngestionError) as caught:
        _normalize(_market(clobTokenIds='["777", "777"]'))
    assert caught.value.reason is RejectionReason.QUARANTINED_MAPPING


def test_a_condition_id_where_a_token_id_belongs_is_quarantined() -> None:
    """`.claude` note: condition ids address markets, token ids address sides."""
    condition = _market()["conditionId"]
    with pytest.raises(IngestionError):
        _normalize(_market(clobTokenIds=json.dumps([condition, "777"])))


def test_a_token_id_that_is_not_a_decimal_string_is_quarantined() -> None:
    with pytest.raises(IngestionError):
        _normalize(_market(clobTokenIds='["0xdeadbeef", "777"]'))


def test_an_unparseable_outcomes_string_is_quarantined() -> None:
    with pytest.raises(IngestionError) as caught:
        _normalize(_market(outcomes="Yes and No"))
    assert caught.value.reason is RejectionReason.MALFORMED_PAYLOAD


def test_missing_token_ids_are_quarantined() -> None:
    payload = _market()
    del payload["clobTokenIds"]
    with pytest.raises(IngestionError) as caught:
        _normalize(payload)
    assert caught.value.reason is RejectionReason.QUARANTINED_MAPPING


# --- other malformed input ----------------------------------------------------------


def test_a_missing_condition_id_is_rejected() -> None:
    payload = _market()
    del payload["conditionId"]
    with pytest.raises(IngestionError) as caught:
        _normalize(payload)
    assert caught.value.reason is RejectionReason.MALFORMED_PAYLOAD


def test_a_truncated_condition_id_is_rejected() -> None:
    with pytest.raises(IngestionError):
        _normalize(_market(conditionId="0xdead"))


def test_a_missing_required_flag_is_rejected() -> None:
    payload = _market()
    del payload["closed"]
    with pytest.raises(IngestionError) as caught:
        _normalize(payload)
    assert caught.value.reason is RejectionReason.MALFORMED_PAYLOAD


@pytest.mark.parametrize("bad", ["not-a-date", "2026-06-01T00:00:00", 1730000000])
def test_an_unusable_timestamp_is_rejected_rather_than_replaced_with_now(bad: object) -> None:
    """data-integrity rule: never substitute the current time for a bad source time."""
    with pytest.raises(IngestionError) as caught:
        _normalize(_market(endDate=bad))
    assert caught.value.reason is RejectionReason.INVALID_TIMESTAMP


def test_a_non_numeric_money_field_is_rejected() -> None:
    with pytest.raises(IngestionError) as caught:
        _normalize(_market(liquidity="a lot"))
    assert caught.value.reason is RejectionReason.MALFORMED_PAYLOAD


def test_a_non_object_payload_is_rejected() -> None:
    with pytest.raises(IngestionError) as caught:
        normalize_market(
            ["not", "a", "market"], raw_payload_sha256=DIGEST, normalized_at=NORMALIZED_AT
        )
    assert caught.value.reason is RejectionReason.MALFORMED_PAYLOAD


# --- quarantine accounting ----------------------------------------------------------


def test_a_bad_market_does_not_stop_the_page() -> None:
    good = _market()
    bad = _market(id="999", slug="broken", clobTokenIds='["1", "2", "3"]')
    report = normalize_markets([good, bad], raw_payload_sha256=DIGEST, normalized_at=NORMALIZED_AT)
    assert len(report.accepted) == 1
    assert len(report.quarantined) == 1
    assert report.total == 2


def test_a_quarantine_record_keeps_the_reason_and_the_evidence() -> None:
    bad = _market(id="999", slug="broken", clobTokenIds='["1", "2", "3"]')
    report = normalize_markets([bad], raw_payload_sha256=DIGEST, normalized_at=NORMALIZED_AT)
    (record,) = report.quarantined
    assert record.market_id == "999"
    assert record.slug == "broken"
    assert record.reason is RejectionReason.QUARANTINED_MAPPING
    assert record.detail
    assert record.raw_payload_sha256 == DIGEST
    assert QuarantinedMarketV1.from_record(record.to_record()) == record


def test_quarantine_reasons_are_counted() -> None:
    payloads = [
        _market(id="1", clobTokenIds='["1", "2", "3"]'),
        _market(id="2", clobTokenIds='["4", "5", "6"]'),
        _market(id="3", endDate="not-a-date"),
    ]
    report = normalize_markets(payloads, raw_payload_sha256=DIGEST, normalized_at=NORMALIZED_AT)
    assert report.counts_by_reason() == {"quarantined_mapping": 2, "invalid_timestamp": 1}


def test_nothing_is_ever_silently_dropped() -> None:
    payloads = [_market(), "garbage", {"id": "x"}, 42]
    report = normalize_markets(payloads, raw_payload_sha256=DIGEST, normalized_at=NORMALIZED_AT)
    assert report.total == len(payloads)


# --- the record itself --------------------------------------------------------------


def test_the_token_map_cannot_be_edited_after_normalization() -> None:
    market = _normalize(_market())
    with pytest.raises(TypeError):
        market.outcome_token_map["Yes"] = "0"  # type: ignore[index]


def test_asking_for_an_unknown_outcome_is_a_contract_violation() -> None:
    market = _normalize(_market())
    with pytest.raises(ContractViolationError):
        market.token_id_for("Perhaps")
    assert market.token_id_for(market.outcomes[0]) == market.outcome_token_map[market.outcomes[0]]


def test_binary_markets_are_identified_but_not_required() -> None:
    binary = _normalize(_market(outcomes='["Yes", "No"]', clobTokenIds='["11", "22"]'))
    assert binary.is_binary
    three = _normalize(_market(outcomes='["A", "B", "C"]', clobTokenIds='["11", "22", "33"]'))
    assert not three.is_binary


def test_a_normalized_at_in_the_future_is_still_the_callers_choice() -> None:
    """The normalizer never reads a clock; determinism is the caller's guarantee."""
    later = NORMALIZED_AT + timedelta(days=365)
    market = normalize_market(_market(), raw_payload_sha256=DIGEST, normalized_at=later)
    assert market.normalized_at == later


# --- the shape of a page ------------------------------------------------------------


def test_a_generator_is_accepted_because_the_parameter_is_an_iterable() -> None:
    """`normalize_markets` is typed `Iterable`, so a stream must not need a list."""
    report = normalize_markets(
        (payload for payload in _page()), raw_payload_sha256=DIGEST, normalized_at=NORMALIZED_AT
    )
    assert len(report.accepted) == len(_page())
    assert report.total == len(_page())


def test_an_empty_page_is_an_empty_report_not_a_failure() -> None:
    """Gamma legitimately answers `[]` past the last page of results."""
    report = normalize_markets([], raw_payload_sha256=DIGEST, normalized_at=NORMALIZED_AT)
    assert report.accepted == ()
    assert report.quarantined == ()
    assert report.total == 0
    assert report.counts_by_reason() == {}


def test_a_paginated_envelope_never_yields_a_partial_sample() -> None:
    """If Gamma wraps the page in an object, no market may be accepted from it.

    A silently empty-but-successful discovery run is the dangerous outcome here:
    the sample would look valid and contain nothing.
    """
    with pytest.raises(IngestionError) as caught:
        normalize_markets(
            {"data": _page(), "next_cursor": "abc"},
            raw_payload_sha256=DIGEST,
            normalized_at=NORMALIZED_AT,
        )
    # Refused whole, not quarantined key by key: an envelope is a page-level
    # structural failure, and per-key records would read as a sample of markets.
    assert caught.value.reason is RejectionReason.MALFORMED_PAYLOAD


@pytest.mark.parametrize("payload", [None, 5, "maintenance in progress"])
def test_a_top_level_payload_that_is_not_a_list_of_markets_is_refused(payload: object) -> None:
    with pytest.raises(IngestionError):
        normalize_markets(payload, raw_payload_sha256=DIGEST, normalized_at=NORMALIZED_AT)  # type: ignore[arg-type]


def test_duplicate_market_ids_in_one_page_are_both_kept_and_counted() -> None:
    """M1 does not deduplicate; it must at least not lose or merge a record silently."""
    report = normalize_markets(
        [_market(), _market()], raw_payload_sha256=DIGEST, normalized_at=NORMALIZED_AT
    )
    assert report.total == 2
    assert [m.market_id for m in report.accepted] == [_market()["id"]] * 2
    assert report.accepted[0].to_record() == report.accepted[1].to_record()


# --- source schema drift ------------------------------------------------------------


def test_an_unknown_field_is_ignored_rather_than_breaking_the_parse() -> None:
    """Gamma adds fields without warning; an addition must be a non-event."""
    baseline = _normalize(_market())
    drifted = _normalize(
        _market(brandNewField={"nested": [1, 2, 3]}, anotherOne="whatever", umaResolutionStatus=7)
    )
    assert drifted.to_record() == baseline.to_record()


@pytest.mark.parametrize("field", ["id", "conditionId", "slug", "question", "outcomes"])
def test_a_renamed_field_is_refused_rather_than_silently_defaulted(field: str) -> None:
    """A rename must quarantine the market, never produce a half-filled record."""
    payload = _market()
    payload[f"{field}_v2"] = payload.pop(field)
    with pytest.raises(IngestionError):
        _normalize(payload)


@pytest.mark.parametrize(
    "outcomes",
    ['{"0": "Yes", "1": "No"}', "42", '"Yes"', "true", "null"],
    ids=["object", "number", "nested-string", "bool", "null"],
)
def test_outcomes_retyped_to_something_other_than_a_list_is_refused(outcomes: str) -> None:
    with pytest.raises(IngestionError) as caught:
        _normalize(_market(outcomes=outcomes))
    assert caught.value.reason in {
        RejectionReason.MALFORMED_PAYLOAD,
        RejectionReason.QUARANTINED_MAPPING,
    }


def test_outcomes_delivered_as_a_real_json_object_is_refused() -> None:
    with pytest.raises(IngestionError) as caught:
        _normalize(_market(outcomes={"0": "Yes", "1": "No"}))
    assert caught.value.reason is RejectionReason.MALFORMED_PAYLOAD


def test_a_retyped_boolean_flag_is_refused_rather_than_coerced() -> None:
    """`"false"` is truthy in Python; coercing it would invert the market's state."""
    for value in ("false", 0, 1, None):
        with pytest.raises(IngestionError) as caught:
            _normalize(_market(closed=value))
        assert caught.value.reason is RejectionReason.MALFORMED_PAYLOAD


def test_a_numeric_id_is_read_as_text_because_gamma_has_used_both() -> None:
    assert _normalize(_market(id=2063134)).market_id == "2063134"


# --- hostile and extreme text -------------------------------------------------------


HOSTILE_TEXT = "Will “X” resolve? ‮ rtl bell \t tab \U0001f9ea 中文 ‮ combining é"


def test_unicode_and_control_characters_are_preserved_verbatim_and_round_trip() -> None:
    """Rule material is evidence: it is stored exactly, not sanitized into something else."""
    market = _normalize(_market(question=HOSTILE_TEXT, description=HOSTILE_TEXT * 3))
    assert market.question == HOSTILE_TEXT
    assert market.description == HOSTILE_TEXT * 3
    assert MarketDefinitionV1.from_record(market.to_record()) == market


def test_a_whitespace_only_question_is_not_a_question() -> None:
    for blank in ("", "   ", "\n\t "):
        with pytest.raises(IngestionError) as caught:
            _normalize(_market(question=blank))
        assert caught.value.reason is RejectionReason.MALFORMED_PAYLOAD


def test_an_enormous_question_and_description_are_kept_whole() -> None:
    long_question = "Will " + "very " * 20_000 + "long?"
    market = _normalize(_market(question=long_question, description="x" * 500_000))
    assert market.question == long_question
    assert len(market.description) == 500_000


# --- outcome labels and token ids ---------------------------------------------------


def test_duplicate_outcome_labels_are_quarantined() -> None:
    """Two identical labels cannot address two different tokens."""
    with pytest.raises(IngestionError) as caught:
        _normalize(_market(outcomes='["Yes", "Yes"]', clobTokenIds='["111", "222"]'))
    assert caught.value.reason is RejectionReason.QUARANTINED_MAPPING


@pytest.mark.parametrize("label", ["   ", "\t", "\n", ""])
def test_a_whitespace_only_outcome_label_is_refused(label: str) -> None:
    with pytest.raises(IngestionError) as caught:
        _normalize(_market(outcomes=json.dumps(["Yes", label]), clobTokenIds='["111", "222"]'))
    assert caught.value.reason is RejectionReason.MALFORMED_PAYLOAD


def test_an_outcome_label_keeps_its_surrounding_whitespace_verbatim() -> None:
    """Trimming would quietly turn `" Yes"` into the standard label it is not."""
    market = _normalize(_market(outcomes='[" Yes", "No"]', clobTokenIds='["111", "222"]'))
    assert market.outcomes == (" Yes", "No")
    assert market.token_id_for(" Yes") == "111"


def test_zero_padded_token_ids_are_still_the_same_token() -> None:
    with pytest.raises(IngestionError):
        _normalize(_market(clobTokenIds='["007", "7"]'))


# --- numbers and timestamps ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1e5", Decimal("1E+5")),
        ("1E+21", Decimal("1E+21")),
        ("0.000000000000000000001", Decimal("1E-21")),
        (1e21, Decimal("1E+21")),
        ("74637265.46443298", Decimal("74637265.46443298")),
        (0.1, Decimal("0.1")),
    ],
    ids=["sci-string", "sci-caps", "tiny", "sci-float", "long-decimal", "float-tenth"],
)
def test_money_values_keep_their_exact_value_in_any_notation(
    raw: object, expected: Decimal
) -> None:
    """Going through `float` would turn 0.1 into 0.1000000000000000055."""
    assert _normalize(_market(liquidity=raw)).liquidity == expected


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_a_non_finite_money_value_is_refused(raw: str) -> None:
    """A NaN liquidity would defeat every `<` comparison in the selection policy."""
    with pytest.raises(IngestionError):
        _normalize(_market(liquidity=raw))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-08-06T22:38:20.060949Z", datetime(2026, 8, 6, 22, 38, 20, 60949, tzinfo=UTC)),
        ("2026-01-01T00:00:00+05:30", datetime(2025, 12, 31, 18, 30, tzinfo=UTC)),
        ("2025-12-31T23:59:59.999999Z", datetime(2025, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)),
        ("2026-01-01T00:00:00-00:00", datetime(2026, 1, 1, tzinfo=UTC)),
        ("2026-12-31T23:59:59Z", datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)),
    ],
    ids=["subsecond", "offset", "year-boundary", "negative-zero-offset", "year-end"],
)
def test_timestamps_with_precision_and_offsets_land_on_the_same_instant(
    raw: str, expected: datetime
) -> None:
    market = _normalize(_market(endDate=raw))
    assert market.end_time == expected
    assert market.end_time is not None
    assert market.end_time.tzinfo is UTC


def test_sub_microsecond_precision_is_truncated_not_rejected() -> None:
    """Python's parser keeps microseconds; the loss must be truncation, not a crash."""
    market = _normalize(_market(endDate="2026-08-06T22:38:20.0609491Z"))
    assert market.end_time == datetime(2026, 8, 6, 22, 38, 20, 60949, tzinfo=UTC)
