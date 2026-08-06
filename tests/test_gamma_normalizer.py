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
