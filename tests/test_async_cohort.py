"""Adversarial offline guards for the unlaunched asynchronous M4 cohort."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from runpy import run_path
from typing import Any
from urllib.parse import urlencode

import orjson
import pytest
from pydantic import ValidationError
from test_prospective_evidence import DECLARED_AT, WINDOW_START, _protocol

from argos.domain.market import MarketDefinitionV1
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.errors import SchemaVersionError
from argos.evaluation.async_cohort import (
    AsynchronousCohortProtocolV1,
    BlockSelectionStatus,
    CandidateExclusionReason,
    CohortBlockV1,
    GammaSelectionV1,
    OfflineBlockSelectionV1,
    validate_block_admission,
)
from argos.evaluation.async_cohort import (
    select_block_candidates as _select_block_candidates,
)
from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    load_persisted_record,
    persist_evidence_record,
)


def _candidate(**updates: object) -> AsynchronousCohortProtocolV1:
    blocks = tuple(
        CohortBlockV1(
            ordinal=index,
            start=WINDOW_START + timedelta(hours=index - 1),
            end=WINDOW_START + timedelta(hours=index),
            intended_targets=4,
        )
        for index in range(1, 5)
    )
    selection = GammaSelectionV1(
        base_url="https://gamma-api.polymarket.com",
        query=(("active", "true"), ("limit", "100")),
        minimum_liquidity=Decimal("1000"),
        minimum_outcome_price=Decimal("0.05"),
        maximum_outcome_price=Decimal("0.95"),
        target_end_min=WINDOW_START + timedelta(days=1),
        target_end_max=WINDOW_START + timedelta(days=30),
    )
    fields = {
        **_protocol().model_dump(mode="python"),
        "experiment_id": "synthetic-m4-cohort",
        "declared_at": DECLARED_AT,
        "observation_window_start": blocks[0].start,
        "observation_window_end": blocks[-1].end,
        "minimum_intended_resolved_target_count": 16,
        "market_selection_mechanism": (
            "four independent UTC blocks, liquidity then numeric market id"
        ),
        "stopping_rule": "no replacement; retain unresolved targets after operational review",
        "blocks": blocks,
        "selection": selection,
        "capture_max_seconds_per_target": 120,
        "capture_max_frames_per_target": 500,
        "capture_separate_database_per_target": True,
        "capture_subscribe_both_tokens": True,
        "capture_raw_archive": True,
        "distinct_event_identity": True,
        "replacement_allowed": False,
        "reject_short_block_before_forecast": True,
        "pending_retained_in_denominator": True,
        "operational_review_at": WINDOW_START + timedelta(days=14),
        "initial_poll_interval_seconds": 300,
        "post_review_poll_interval_seconds": 3600,
    }
    fields.update(updates)
    return AsynchronousCohortProtocolV1.model_validate(fields)


def test_nested_protocol_round_trip_preserves_versions_and_exact_plan() -> None:
    protocol = _candidate()
    record = protocol.to_record()
    assert record["blocks"][0]["schema_version"] == CohortBlockV1.schema_version
    assert record["selection"]["schema_version"] == GammaSelectionV1.schema_version
    assert AsynchronousCohortProtocolV1.from_record(record) == protocol
    record["blocks"][0]["schema_version"] = "m4_cohort_block.v0"
    with pytest.raises(SchemaVersionError, match="not supported"):
        AsynchronousCohortProtocolV1.from_record(record)


def test_protocol_receipt_reloads_the_same_versioned_blocks(tmp_path: Path) -> None:
    protocol = _candidate()
    receipt = persist_evidence_record(
        tmp_path,
        record=protocol,
        experiment_id=protocol.experiment_id,
        artifact_kind=EvidenceArtifactKind.EXPERIMENT_PROTOCOL,
        artifact_id=protocol.experiment_id,
        persisted_at=protocol.declared_at,
    )
    assert receipt.artifact_schema_version == protocol.schema_version
    assert load_persisted_record(tmp_path, receipt, AsynchronousCohortProtocolV1) == protocol


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"blocks": _candidate().blocks[:3]}, "four ordered blocks"),
        ({"blocks": tuple(reversed(_candidate().blocks))}, "four ordered blocks"),
        ({"minimum_intended_resolved_target_count": 15}, "16 intended"),
        ({"scientific_minimum_resolved_target_count": 16}, "calibration sufficiency"),
        ({"capture_max_seconds_per_target": 121}, "less than or equal to 120"),
        ({"capture_max_frames_per_target": 501}, "less than or equal to 500"),
        ({"replacement_allowed": True}, "no-replacement"),
        ({"distinct_event_identity": False}, "independence"),
        ({"capture_raw_archive": False}, "capture"),
        ({"pending_retained_in_denominator": False}, "cohort capture"),
        ({"minimum_yes_outcomes_for_calibration": 1}, "cannot weaken calibration"),
        ({"operational_review_at": _candidate().observation_window_end}, "review"),
        ({"post_review_poll_interval_seconds": 60}, "post-review cadence"),
        (
            {
                "selection": _candidate().selection.model_copy(
                    update={
                        "target_end_min": WINDOW_START - timedelta(days=3),
                        "target_end_max": WINDOW_START - timedelta(days=1),
                    }
                )
            },
            "target end-time lower bound",
        ),
    ],
)
def test_protocol_rejects_invalid_cohort_plan(update: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _candidate(**update)


def test_blocks_cannot_overlap_or_be_too_short() -> None:
    original = _candidate()
    overlapping = original.blocks[1].model_copy(
        update={"start": original.blocks[0].end - timedelta(seconds=1)}
    )
    with pytest.raises(ValidationError, match="must not overlap"):
        _candidate(blocks=(original.blocks[0], overlapping, *original.blocks[2:]))
    short = original.blocks[1].model_copy(
        update={"end": original.blocks[1].start + timedelta(seconds=1)}
    )
    with pytest.raises(ValidationError, match="cannot fit"):
        _candidate(blocks=(original.blocks[0], short, *original.blocks[2:]))


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"base_url": "http://gamma-api.polymarket.com"}, "public HTTPS"),
        ({"base_url": "https://gamma-api.polymarket.com:444"}, "public HTTPS"),
        ({"base_url": "https://user:pass@gamma-api.polymarket.com"}, "public HTTPS"),
        ({"query": (("limit", "100"), ("active", "true"))}, "sorted"),
        ({"query": (("limit", "100"), ("limit", "200"))}, "unique"),
        ({"minimum_outcome_price": Decimal("0.95")}, "increasing"),
    ],
)
def test_selection_is_exact_public_and_deterministic(
    update: dict[str, object], message: str
) -> None:
    selection = _candidate().selection
    with pytest.raises(ValidationError, match=message):
        GammaSelectionV1.model_validate({**selection.model_dump(mode="python"), **update})


def test_block_admission_is_atomic_and_never_reuses_event() -> None:
    protocol = _candidate()
    valid = tuple(
        _market(index).model_copy(update={"event_id": event_id})
        for index, event_id in enumerate(("event-a", "event-b", "event-c", "event-d"), start=1)
    )
    at = protocol.blocks[1].start + timedelta(seconds=1)
    prior, prior_source = _prior_selection(protocol)
    validate_block_admission(
        protocol,
        block_ordinal=2,
        selected_at=at,
        selected_markets=valid,
        prior_block_selections=(prior,),
        prior_block_sources=(prior_source,),
    )
    cases = [
        (at, valid[:3], (prior,), "short"),
        (
            at,
            (valid[0], valid[1].model_copy(update={"event_id": "event-a"}), *valid[2:]),
            (prior,),
            "more than once",
        ),
        (
            at,
            valid,
            (
                prior.model_copy(
                    update={
                        "selected_markets": (
                            prior.selected_markets[0].model_copy(update={"event_id": "event-c"}),
                            *prior.selected_markets[1:],
                        )
                    }
                ),
            ),
            "disagrees with its archived discovery page",
        ),
        (protocol.blocks[1].end, valid, (prior,), "outside frozen block"),
        (at, valid, (), "missing predecessors"),
        (
            protocol.blocks[1].end - timedelta(seconds=479),
            valid,
            (prior,),
            "insufficient time remains",
        ),
    ]
    for selected_at, markets, predecessors, message in cases:
        with pytest.raises(ValueError, match=message):
            validate_block_admission(
                protocol,
                block_ordinal=2,
                selected_at=selected_at,
                selected_markets=markets,
                prior_block_selections=predecessors,
                prior_block_sources=(prior_source,) if predecessors else (),
            )


def _market(market_id: int, *, event_id: str | None = None) -> MarketDefinitionV1:
    return MarketDefinitionV1(
        market_id=str(market_id),
        condition_id="0x" + f"{market_id:064x}",
        slug=f"synthetic-{market_id}",
        event_id=event_id or f"event-{market_id}",
        question=f"Will synthetic event {market_id} resolve Yes?",
        description="Synthetic test rules.",
        resolution_source="Synthetic test source.",
        start_time=WINDOW_START - timedelta(days=1),
        end_time=WINDOW_START + timedelta(days=2),
        active=True,
        closed=False,
        archived=False,
        liquidity=Decimal(10000 - market_id),
        outcomes=("Yes", "No"),
        outcome_token_map={"Yes": str(market_id * 2), "No": str(market_id * 2 + 1)},
        raw_payload_sha256="a" * 64,
        normalized_at=WINDOW_START,
        normalizer_version="synthetic-test/1",
    )


def _prior_selection(
    protocol: AsynchronousCohortProtocolV1,
) -> tuple[OfflineBlockSelectionV1, bytes]:
    markets = tuple(
        _market(index).model_copy(update={"event_id": f"earlier-{index}"})
        for index in range(100, 104)
    )
    source = _source(markets)
    selection = select_block_candidates(
        protocol,
        block_ordinal=1,
        selected_at=protocol.blocks[0].start,
        source_payload_bytes=source,
        source_retrieved_at=protocol.blocks[0].start,
        prior_block_selections=(),
    )
    return selection, source


def _raw_market(market: MarketDefinitionV1, **updates: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": market.market_id,
        "conditionId": market.condition_id,
        "slug": market.slug,
        "events": [{"id": market.event_id}] if market.event_id is not None else [],
        "question": market.question,
        "description": market.description,
        "resolutionSource": market.resolution_source,
        "startDate": market.start_time.isoformat() if market.start_time is not None else None,
        "endDate": market.end_time.isoformat() if market.end_time is not None else None,
        "active": market.active,
        "closed": market.closed,
        "archived": market.archived,
        "liquidity": str(market.liquidity) if market.liquidity is not None else None,
        "outcomes": list(market.outcomes),
        "clobTokenIds": [market.outcome_token_map[outcome] for outcome in market.outcomes],
        "enableOrderBook": True,
        "acceptingOrders": True,
        "outcomePrices": '["0.4", "0.6"]',
    }
    entry.update(updates)
    return entry


def _source(
    markets: tuple[MarketDefinitionV1, ...],
    *,
    updates: dict[str, dict[str, object]] | None = None,
    extra: tuple[object, ...] = (),
) -> bytes:
    return orjson.dumps(
        [_raw_market(market, **(updates or {}).get(market.market_id, {})) for market in markets]
        + list(extra)
    )


def _endpoint(protocol: AsynchronousCohortProtocolV1) -> str:
    return f"{protocol.selection.base_url}/markets?{urlencode(protocol.selection.query)}"


def select_block_candidates(
    protocol: AsynchronousCohortProtocolV1,
    *,
    source_payload_bytes: bytes,
    source_retrieved_at: datetime,
    source_endpoint: str | None = None,
    **kwargs: Any,
) -> OfflineBlockSelectionV1:
    kwargs.setdefault("prior_block_sources", ())
    provenance = SourceProvenanceV1(
        source="gamma",
        endpoint=source_endpoint or _endpoint(protocol),
        http_status=200,
        retrieved_at=source_retrieved_at,
        raw_sha256=sha256_hex(source_payload_bytes),
        byte_length=len(source_payload_bytes),
    )
    return _select_block_candidates(
        protocol,
        source_payload_bytes=source_payload_bytes,
        source_provenance=provenance,
        **kwargs,
    )


def test_offline_selector_is_deterministic_atomic_and_independent() -> None:
    protocol = _candidate()
    markets = (
        _market(7),
        _market(4),
        _market(2, event_id="event-1"),
        _market(3),
        _market(1),
        _market(5),
    )
    selected = select_block_candidates(
        protocol,
        block_ordinal=1,
        selected_at=protocol.blocks[0].start + timedelta(seconds=1),
        source_payload_bytes=_source(markets),
        source_retrieved_at=WINDOW_START,
        prior_block_selections=(),
    )
    assert selected.status is BlockSelectionStatus.ADMITTED
    assert tuple(market.market_id for market in selected.selected_markets) == ("1", "3", "4", "5")
    assert len({market.event_id for market in selected.selected_markets}) == 4
    assert selected.source_candidate_count == 6
    assert {item.reason for item in selected.exclusions} == {
        CandidateExclusionReason.EVENT_ID_REUSED,
        CandidateExclusionReason.RANK_BELOW_CUTOFF,
    }
    assert OfflineBlockSelectionV1.from_record(selected.to_record()) == selected
    incomplete = selected.to_record()
    incomplete["source_candidate_count"] -= 1
    with pytest.raises(ValidationError, match="account for every discovery entry"):
        OfflineBlockSelectionV1.from_record(incomplete)
    overlapping = selected.to_record()
    overlapping["exclusions"][0]["market_id"] = selected.selected_markets[0].market_id
    with pytest.raises(ValidationError, match="must be disjoint"):
        OfflineBlockSelectionV1.from_record(overlapping)
    repeated_exclusion = selected.to_record()
    repeated_exclusion["exclusions"][1]["market_id"] = (
        "0" + repeated_exclusion["exclusions"][0]["market_id"]
    )
    with pytest.raises(ValidationError, match="must be disjoint"):
        OfflineBlockSelectionV1.from_record(repeated_exclusion)
    foreign_exclusion = selected.to_record()
    foreign_exclusion["exclusions"][0]["schema_version"] = "m4_candidate_exclusion.v0"
    with pytest.raises(SchemaVersionError, match="not supported"):
        OfflineBlockSelectionV1.from_record(foreign_exclusion)
    duplicate_market = selected.to_record()
    duplicate_market["selected_markets"][1]["market_id"] = duplicate_market["selected_markets"][0][
        "market_id"
    ]
    with pytest.raises(ValidationError, match="distinct canonical numeric IDs"):
        OfflineBlockSelectionV1.from_record(duplicate_market)
    duplicate_event = selected.to_record()
    duplicate_event["selected_markets"][1]["event_id"] = duplicate_event["selected_markets"][0][
        "event_id"
    ]
    with pytest.raises(ValidationError, match="distinct explicit event identities"):
        OfflineBlockSelectionV1.from_record(duplicate_event)
    aliased_event = selected.to_record()
    aliased_event["selected_markets"][0]["event_id"] = "411239"
    aliased_event["selected_markets"][1]["event_id"] = "0411239"
    with pytest.raises(ValidationError, match="distinct explicit event identities"):
        OfflineBlockSelectionV1.from_record(aliased_event)
    duplicate_condition = selected.to_record()
    duplicate_condition["selected_markets"][1]["condition_id"] = duplicate_condition[
        "selected_markets"
    ][0]["condition_id"]
    with pytest.raises(ValidationError, match="distinct condition and CLOB token identities"):
        OfflineBlockSelectionV1.from_record(duplicate_condition)
    duplicate_token = selected.to_record()
    duplicate_token["selected_markets"][1]["outcome_token_map"]["Yes"] = "02"
    with pytest.raises(ValidationError, match="distinct condition and CLOB token identities"):
        OfflineBlockSelectionV1.from_record(duplicate_token)
    foreign_source = selected.to_record()
    foreign_source["selected_markets"][0]["raw_payload_sha256"] = "b" * 64
    with pytest.raises(ValidationError, match="recorded discovery source digest"):
        OfflineBlockSelectionV1.from_record(foreign_source)
    reordered = select_block_candidates(
        protocol,
        block_ordinal=1,
        selected_at=protocol.blocks[0].start + timedelta(seconds=1),
        source_payload_bytes=_source(tuple(reversed(markets))),
        source_retrieved_at=WINDOW_START,
        prior_block_selections=(),
    )
    assert tuple(market.market_id for market in reordered.selected_markets) == tuple(
        market.market_id for market in selected.selected_markets
    )
    rejected = select_block_candidates(
        protocol,
        block_ordinal=1,
        selected_at=protocol.blocks[0].start + timedelta(seconds=1),
        source_payload_bytes=_source(
            markets,
            updates={
                "3": {"acceptingOrders": False},
                "4": {"outcomePrices": '["NaN", "0.6"]'},
            },
        ),
        source_retrieved_at=WINDOW_START,
        prior_block_selections=(),
    )
    assert rejected.status is BlockSelectionStatus.REJECTED_SHORT_BLOCK
    assert not rejected.selected_markets
    assert len(rejected.exclusions) == rejected.source_candidate_count
    assert {item.reason for item in rejected.exclusions} >= {
        CandidateExclusionReason.ORDER_BOOK_UNAVAILABLE,
        CandidateExclusionReason.MALFORMED_PRICES,
        CandidateExclusionReason.BLOCK_SHORTFALL,
    }


def test_duplicate_market_ids_fail_closed_independent_of_page_order() -> None:
    protocol = _candidate()
    markets = (_market(1), _market(1, event_id="different-event"), _market(2))
    for page in (markets, tuple(reversed(markets))):
        with pytest.raises(ValueError, match=r"duplicate market IDs.*'1'"):
            select_block_candidates(
                protocol,
                block_ordinal=1,
                selected_at=protocol.blocks[0].start + timedelta(seconds=1),
                source_payload_bytes=_source(page),
                source_retrieved_at=WINDOW_START,
                prior_block_selections=(),
            )
    alias = markets[1].model_copy(update={"market_id": "01"})
    with pytest.raises(ValueError, match="duplicate market IDs"):
        select_block_candidates(
            protocol,
            block_ordinal=1,
            selected_at=WINDOW_START + timedelta(seconds=1),
            source_payload_bytes=_source((markets[0], alias)),
            source_retrieved_at=WINDOW_START,
            prior_block_selections=(),
        )
    with pytest.raises(ValueError, match="duplicate market IDs"):
        select_block_candidates(
            protocol,
            block_ordinal=1,
            selected_at=WINDOW_START + timedelta(seconds=1),
            source_payload_bytes=orjson.dumps([{"id": "invalid"}, {"id": "invalid"}]),
            source_retrieved_at=WINDOW_START,
            prior_block_selections=(),
        )
    with pytest.raises(ValueError, match="duplicate market IDs"):
        select_block_candidates(
            protocol,
            block_ordinal=1,
            selected_at=WINDOW_START + timedelta(seconds=1),
            source_payload_bytes=orjson.dumps([_raw_market(markets[0]), {"id": 1}]),
            source_retrieved_at=WINDOW_START,
            prior_block_selections=(),
        )


def test_later_blocks_derive_independence_from_hash_linked_prior_records() -> None:
    protocol = _candidate()
    first_source = _source(
        (_market(100, event_id="411239"), *(_market(index) for index in range(101, 104)))
    )
    first = select_block_candidates(
        protocol,
        block_ordinal=1,
        selected_at=protocol.blocks[0].start,
        source_payload_bytes=first_source,
        source_retrieved_at=protocol.blocks[0].start,
        prior_block_selections=(),
    )
    second_source = _source(
        (_market(1, event_id="0411239"), *(_market(index) for index in range(2, 8))),
        updates={
            "2": {"conditionId": first.selected_markets[0].condition_id},
            "3": {"clobTokenIds": ["0200", "7"]},
        },
    )
    second = select_block_candidates(
        protocol,
        block_ordinal=2,
        selected_at=protocol.blocks[1].start,
        source_payload_bytes=second_source,
        source_retrieved_at=protocol.blocks[1].start,
        prior_block_selections=(first,),
        prior_block_sources=(first_source,),
    )
    assert second.status is BlockSelectionStatus.ADMITTED
    assert CandidateExclusionReason.EVENT_ID_REUSED in {item.reason for item in second.exclusions}
    assert [item.reason for item in second.exclusions].count(
        CandidateExclusionReason.MARKET_IDENTITY_REUSED
    ) == 2
    assert second.predecessor_selection_sha256 == sha256_hex(
        orjson.dumps(first.to_record(), option=orjson.OPT_SORT_KEYS)
    )
    third_args = {
        "block_ordinal": 3,
        "selected_at": protocol.blocks[2].start,
        "source_payload_bytes": _source(tuple(_market(index) for index in range(10, 14))),
        "source_retrieved_at": protocol.blocks[2].start,
        "prior_block_selections": (first, second),
        "prior_block_sources": (first_source, second_source),
    }
    third = select_block_candidates(protocol, **third_args)
    assert third.status is BlockSelectionStatus.ADMITTED
    assert third.predecessor_selection_sha256 == sha256_hex(
        orjson.dumps(second.to_record(), option=orjson.OPT_SORT_KEYS)
    )
    forged = second.model_copy(update={"predecessor_selection_sha256": "b" * 64})
    with pytest.raises(ValueError, match="hash-linked chain"):
        select_block_candidates(
            protocol, **{**third_args, "prior_block_selections": (first, forged)}
        )
    with pytest.raises(ValueError, match="missing predecessors"):
        select_block_candidates(protocol, **{**third_args, "prior_block_selections": (first,)})
    closed_first = first.model_copy(
        update={
            "selected_markets": (
                first.selected_markets[0].model_copy(update={"closed": True}),
                *first.selected_markets[1:],
            )
        }
    )
    with pytest.raises(ValueError, match="disagrees with its archived discovery page"):
        select_block_candidates(
            protocol,
            **{**third_args, "prior_block_selections": (closed_first, second)},
        )
    late_first = first.model_copy(
        update={"selected_at": protocol.blocks[0].end - timedelta(seconds=1)}
    )
    with pytest.raises(ValueError, match="hash-linked chain"):
        select_block_candidates(
            protocol,
            **{**third_args, "prior_block_selections": (late_first, second)},
        )


def test_numeric_event_aliases_share_one_identity_at_selection() -> None:
    protocol = _candidate()
    markets = (
        _market(1, event_id="411239"),
        _market(2, event_id="0411239"),
        *(_market(index) for index in range(3, 6)),
    )
    result = select_block_candidates(
        protocol,
        block_ordinal=1,
        selected_at=WINDOW_START,
        source_payload_bytes=_source(markets),
        source_retrieved_at=WINDOW_START,
        prior_block_selections=(),
    )
    assert result.status is BlockSelectionStatus.ADMITTED
    assert tuple(market.market_id for market in result.selected_markets) == ("1", "3", "4", "5")
    assert CandidateExclusionReason.EVENT_ID_REUSED in {item.reason for item in result.exclusions}


def test_condition_and_token_aliases_are_excluded_before_admission() -> None:
    protocol = _candidate()
    markets = tuple(_market(index) for index in range(1, 7))
    result = select_block_candidates(
        protocol,
        block_ordinal=1,
        selected_at=WINDOW_START,
        source_payload_bytes=_source(
            markets,
            updates={
                "2": {"conditionId": markets[0].condition_id},
                "3": {"clobTokenIds": ["02", "7"]},
            },
        ),
        source_retrieved_at=WINDOW_START,
        prior_block_selections=(),
    )
    assert result.status is BlockSelectionStatus.ADMITTED
    assert tuple(market.market_id for market in result.selected_markets) == ("1", "4", "5", "6")
    assert [item.reason for item in result.exclusions].count(
        CandidateExclusionReason.MARKET_IDENTITY_REUSED
    ) == 2


def test_quarantined_candidates_keep_structured_reasons() -> None:
    protocol = _candidate()
    markets = tuple(_market(index) for index in range(1, 4))
    result = select_block_candidates(
        protocol,
        block_ordinal=1,
        selected_at=protocol.blocks[0].start + timedelta(seconds=1),
        source_payload_bytes=_source(markets, extra=({"id": "invalid"},)),
        source_retrieved_at=WINDOW_START,
        prior_block_selections=(),
    )
    assert result.status is BlockSelectionStatus.REJECTED_SHORT_BLOCK
    assert len(result.exclusions) == result.source_candidate_count == 4
    assert {item.reason for item in result.exclusions} >= {
        CandidateExclusionReason.NORMALIZATION_REJECTED,
        CandidateExclusionReason.BLOCK_SHORTFALL,
    }


def test_missing_liquidity_is_not_observed_zero() -> None:
    selection = _candidate().selection.model_copy(update={"minimum_liquidity": Decimal(0)})
    protocol = _candidate(selection=selection)
    market = _market(1).model_copy(update={"liquidity": None})
    result = select_block_candidates(
        protocol,
        block_ordinal=1,
        selected_at=WINDOW_START,
        source_payload_bytes=_source((market,)),
        source_retrieved_at=WINDOW_START,
        prior_block_selections=(),
    )
    assert result.status is BlockSelectionStatus.REJECTED_SHORT_BLOCK
    assert result.exclusions[0].reason is CandidateExclusionReason.LIQUIDITY_MISSING


def test_selection_rejects_stale_or_malformed_source_evidence() -> None:
    protocol = _candidate()
    markets = tuple(_market(index) for index in range(1, 5))
    args = {
        "block_ordinal": 1,
        "selected_at": WINDOW_START + timedelta(seconds=1),
        "source_payload_bytes": _source(markets),
        "source_retrieved_at": WINDOW_START,
        "prior_block_selections": (),
    }
    with pytest.raises(ValueError, match="retrieved after"):
        select_block_candidates(
            protocol, **{**args, "source_retrieved_at": WINDOW_START + timedelta(seconds=2)}
        )
    with pytest.raises(ValueError, match="retrieved inside the selected block"):
        select_block_candidates(
            protocol, **{**args, "source_retrieved_at": WINDOW_START - timedelta(seconds=1)}
        )
    with pytest.raises(ValueError, match="not valid JSON"):
        select_block_candidates(protocol, **{**args, "source_payload_bytes": b"not json"})
    with pytest.raises(ValueError, match="market page"):
        select_block_candidates(protocol, **{**args, "source_payload_bytes": b"{}"})
    with pytest.raises(ValueError, match="frozen Gamma query"):
        select_block_candidates(
            protocol,
            **{
                **args,
                "source_endpoint": f"{protocol.selection.base_url}/markets?limit=1",
            },
        )
    with pytest.raises(ValueError, match="insufficient time remains"):
        select_block_candidates(
            protocol,
            **{
                **args,
                "selected_at": protocol.blocks[0].end - timedelta(seconds=479),
            },
        )
    with pytest.raises(ValueError, match="outside frozen block"):
        select_block_candidates(
            protocol,
            **{**args, "selected_at": protocol.blocks[0].end},
        )
    selected = select_block_candidates(protocol, **args)
    altered = select_block_candidates(
        protocol,
        **{
            **args,
            "source_payload_bytes": _source(markets, updates={"1": {"acceptingOrders": False}}),
        },
    )
    assert selected.source_payload_sha256 != altered.source_payload_sha256
    assert altered.status is BlockSelectionStatus.REJECTED_SHORT_BLOCK
    assert CandidateExclusionReason.ORDER_BOOK_UNAVAILABLE in {
        item.reason for item in altered.exclusions
    }


@pytest.mark.parametrize(
    ("market_update", "raw_update", "reason"),
    [
        ({"event_id": None}, {}, CandidateExclusionReason.EVENT_ID_MISSING),
        ({"market_id": "01"}, {}, CandidateExclusionReason.MARKET_ID_INVALID),
        ({"market_id": "1" * 4301}, {}, CandidateExclusionReason.MARKET_ID_INVALID),
        ({"active": False}, {}, CandidateExclusionReason.NOT_ACTIVE),
        ({"closed": True}, {}, CandidateExclusionReason.CLOSED),
        ({"archived": True}, {}, CandidateExclusionReason.ARCHIVED),
        ({"outcomes": ("No", "Yes")}, {}, CandidateExclusionReason.NOT_BINARY_YES_NO),
        ({"end_time": None}, {}, CandidateExclusionReason.END_TIME_OUT_OF_RANGE),
        ({}, {"liquidity": "NaN"}, CandidateExclusionReason.NORMALIZATION_REJECTED),
        ({"liquidity": Decimal(1)}, {}, CandidateExclusionReason.LIQUIDITY_BELOW_MINIMUM),
        ({}, {"enableOrderBook": False}, CandidateExclusionReason.ORDER_BOOK_UNAVAILABLE),
        ({}, {"outcomePrices": "not json"}, CandidateExclusionReason.MALFORMED_PRICES),
        (
            {},
            {"outcomePrices": '["invalid", "0.5"]'},
            CandidateExclusionReason.MALFORMED_PRICES,
        ),
        ({}, {"outcomePrices": ["0.01", "0.99"]}, CandidateExclusionReason.PRICE_OUT_OF_RANGE),
        (
            {},
            {"description": "", "resolutionSource": ""},
            CandidateExclusionReason.CONTRACT_UNVERIFIABLE,
        ),
    ],
)
def test_candidate_exclusion_is_recorded_for_each_guard(
    market_update: dict[str, object],
    raw_update: dict[str, object],
    reason: CandidateExclusionReason,
) -> None:
    protocol = _candidate()
    market = _market(1).model_copy(update=market_update)
    result = select_block_candidates(
        protocol,
        block_ordinal=1,
        selected_at=WINDOW_START + timedelta(seconds=1),
        source_payload_bytes=_source((market,), updates={market.market_id: raw_update}),
        source_retrieved_at=WINDOW_START,
        prior_block_selections=(),
    )
    assert result.status is BlockSelectionStatus.REJECTED_SHORT_BLOCK
    assert result.exclusions[0].reason is reason


_SCRIPT = run_path(
    str(Path(__file__).resolve().parents[1] / "scripts" / "m4_async_cohort_preflight.py")
)
preflight_protocol = _SCRIPT["preflight_protocol"]


def test_preflight_cannot_masquerade_as_freeze() -> None:
    protocol = _candidate()
    report = preflight_protocol(
        protocol.to_record(),
        revision=protocol.code_revision,
        checked_at=protocol.declared_at + timedelta(minutes=1),
    )
    assert report["status"] == "PREFLIGHT_ONLY_NOT_FROZEN"
    assert report["intended_targets"] == 16
    with pytest.raises(ValueError, match="revision disagrees"):
        preflight_protocol(
            protocol.to_record(), revision="different", checked_at=protocol.declared_at
        )
    with pytest.raises(ValueError, match="already begun"):
        preflight_protocol(
            protocol.to_record(),
            revision=protocol.code_revision,
            checked_at=protocol.blocks[0].start,
        )
