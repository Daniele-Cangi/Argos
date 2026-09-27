"""Synthetic, no-network proof of partial admission and archived replay."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import orjson
import pytest
from pydantic import ValidationError
from test_cohort_protocol_v2 import START, _protocol

from argos.compiler import compile_market_contract
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.errors import ImmutabilityViolationError
from argos.evaluation.cohort_review_v2 import HumanSemanticReviewV1, SemanticReviewDecision
from argos.evaluation.cohort_selection_v2 import (
    BlockAdmissionStatusV2,
    BookAttemptStatus,
    BookAttemptV1,
    OfflineBlockSelectionV2,
    PageDecisionV1,
    V2ExclusionReason,
    select_block_candidates_v2,
    verify_block_selection_v2_archives,
)
from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    EvidencePersistenceReceiptV1,
    build_persistence_receipt_id,
    persist_evidence_record,
)
from argos.ingestion.gamma_markets import normalize_market
from argos.store.raw_archive import write_raw_payload


def _entry(market_id: int, *, group: int | None = None) -> dict[str, object]:
    return {
        "id": str(market_id),
        "conditionId": "0x" + f"{market_id:064x}",
        "slug": f"synthetic-{market_id}",
        "events": [{"id": str(group or market_id)}],
        "question": f"Will synthetic event {market_id} occur?",
        "description": "A synthetic event that resolves Yes if the event occurs.",
        "resolutionSource": "Synthetic authority.",
        "startDate": (START - timedelta(days=1)).isoformat(),
        "endDate": (START + timedelta(hours=2)).isoformat(),
        "active": True,
        "closed": False,
        "archived": False,
        "liquidity": "5000",
        "outcomes": ["Yes", "No"],
        "clobTokenIds": [str(market_id * 2), str(market_id * 2 + 1)],
        "enableOrderBook": True,
        "acceptingOrders": True,
        "outcomePrices": '["0.4","0.6"]',
    }


def _provenance(source: str, endpoint: str, raw: bytes, at: datetime) -> SourceProvenanceV1:
    return SourceProvenanceV1(
        source=source,
        endpoint=endpoint,
        http_status=200,
        retrieved_at=at,
        raw_sha256=sha256_hex(raw),
        byte_length=len(raw),
    )


def _prepare(
    archive: Path,
    entries: list[object],
    *,
    block_ordinal: int = 1,
    selected_offset: int = 30,
    approved_ids: tuple[str, ...] = (),
    failed_book_ids: tuple[str, ...] = (),
    group_overrides: dict[str, str] | None = None,
) -> tuple[
    bytes,
    SourceProvenanceV1,
    tuple[HumanSemanticReviewV1, ...],
    tuple[EvidencePersistenceReceiptV1, ...],
    tuple[BookAttemptV1, ...],
    tuple[EvidencePersistenceReceiptV1, ...],
    dict[str, bytes],
]:
    protocol = _protocol()
    block_start = protocol.blocks[block_ordinal - 1].start
    raw = orjson.dumps(entries)
    page_sha = sha256_hex(raw)
    provenance = _provenance(
        "gamma",
        f"{protocol.selection.base_url}/markets?{urlencode(protocol.selection.query)}",
        raw,
        block_start,
    )
    write_raw_payload(archive, raw=raw, provenance=provenance)
    reviews = []
    review_receipts = []
    attempts = []
    attempt_receipts = []
    books: dict[str, bytes] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or str(entry.get("id")) not in approved_ids:
            continue
        market = normalize_market(
            entry,
            raw_payload_sha256=page_sha,
            normalized_at=block_start,
        )
        reviewed_at = block_start + timedelta(seconds=3)
        contract = compile_market_contract(
            market, compiled_at=block_start + timedelta(seconds=selected_offset)
        )
        review = HumanSemanticReviewV1(
            experiment_id=protocol.experiment_id,
            reviewer_id="synthetic-reviewer",
            reviewed_at=reviewed_at,
            source_payload_sha256=page_sha,
            entry_index=index,
            entry_sha256=sha256_hex(orjson.dumps(entry, option=orjson.OPT_SORT_KEYS)),
            market_id=market.market_id,
            condition_id=market.condition_id,
            contract_id=contract.contract_id,
            compiler_version=contract.compiler_version,
            decision=SemanticReviewDecision.APPROVED,
            notes="Synthetic Yes/No semantics reviewed for replay only.",
            yes_condition="The named event occurs.",
            no_condition="The named event does not occur.",
            resolution_authority="Synthetic authority",
            category="reviewed-category",
            event_group_id=(group_overrides or {}).get(market.market_id, market.event_id),
            earliest_outcome_knowable_at=START + timedelta(hours=2),
        )
        reviews.append(review)
        review_receipts.append(
            persist_evidence_record(
                archive,
                record=review,
                experiment_id=protocol.experiment_id,
                artifact_kind=EvidenceArtifactKind.SEMANTIC_REVIEW,
                artifact_id=review.review_id,
                persisted_at=reviewed_at + timedelta(seconds=1),
            )
        )
        requested_at = block_start + timedelta(seconds=5)
        if market.market_id in failed_book_ids:
            attempt = BookAttemptV1(
                experiment_id=protocol.experiment_id,
                market_id=market.market_id,
                requested_token_id=market.token_id_for("Yes"),
                attempted_at=requested_at,
                status=BookAttemptStatus.FAILED,
                failure_code="synthetic_timeout",
            )
        else:
            book = orjson.dumps(
                {
                    "market": market.condition_id,
                    "asset_id": market.token_id_for("Yes"),
                    "bids": [{"price": "0.40", "size": "10"}],
                    "asks": [{"price": "0.45", "size": "10"}],
                    "tick_size": "0.01",
                    "min_order_size": "1",
                    "neg_risk": False,
                    "last_trade_price": "0.42",
                }
            )
            books[market.market_id] = book
            book_provenance = _provenance(
                "clob_rest",
                f"https://clob.polymarket.com/book?token_id={market.token_id_for('Yes')}",
                book,
                block_start + timedelta(seconds=6),
            )
            write_raw_payload(archive, raw=book, provenance=book_provenance)
            attempt = BookAttemptV1(
                experiment_id=protocol.experiment_id,
                market_id=market.market_id,
                requested_token_id=market.token_id_for("Yes"),
                attempted_at=requested_at,
                status=BookAttemptStatus.RESPONSE,
                source_provenance=book_provenance,
            )
        attempts.append(attempt)
        attempt_receipts.append(
            persist_evidence_record(
                archive,
                record=attempt,
                experiment_id=protocol.experiment_id,
                artifact_kind=EvidenceArtifactKind.COHORT_BOOK_ATTEMPT,
                artifact_id=attempt.attempt_id,
                persisted_at=requested_at + timedelta(seconds=7),
            )
        )
    return (
        raw,
        provenance,
        tuple(reviews),
        tuple(review_receipts),
        tuple(attempts),
        tuple(attempt_receipts),
        books,
    )


def _persist_selection(
    archive: Path, selection: OfflineBlockSelectionV2
) -> EvidencePersistenceReceiptV1:
    return persist_evidence_record(
        archive,
        record=selection,
        experiment_id=selection.experiment_id,
        artifact_kind=EvidenceArtifactKind.COHORT_BLOCK_SELECTION,
        artifact_id=f"block-{selection.block_ordinal}",
        persisted_at=selection.selected_at + timedelta(seconds=1),
    )


def test_partial_block_retains_one_target_and_archives_replay(tmp_path: Path) -> None:
    protocol = _protocol()
    raw, source, reviews, receipts, attempts, attempt_receipts, books = _prepare(
        tmp_path,
        [_entry(101), {"id": "anonymous-bad"}, _entry(102)],
        approved_ids=("101",),
    )
    decision = select_block_candidates_v2(
        protocol,
        block_ordinal=1,
        selected_at=START + timedelta(seconds=30),
        source_payload_bytes=raw,
        source_provenance=source,
        reviews=reviews,
        review_receipts=receipts,
        book_attempts=attempts,
        book_attempt_receipts=attempt_receipts,
        book_payload_bytes=books,
    )
    assert decision.status is BlockAdmissionStatusV2.PARTIAL
    assert (decision.admitted_count, decision.unfilled_slots) == (1, 1)
    assert decision.page_decisions[0].observed_spread == Decimal("0.05")
    assert decision.page_decisions[1].exclusion_reason is V2ExclusionReason.NORMALIZATION_REJECTED
    assert decision.page_decisions[2].exclusion_reason is V2ExclusionReason.UNREVIEWED
    assert OfflineBlockSelectionV2.from_record(decision.to_record()) == decision
    assert (
        verify_block_selection_v2_archives(
            protocol,
            decision,
            archive_dir=tmp_path,
            selection_receipt=_persist_selection(tmp_path, decision),
        )
        == decision
    )


def test_book_failure_yields_empty_accounted_block(tmp_path: Path) -> None:
    protocol = _protocol()
    raw, source, reviews, receipts, attempts, attempt_receipts, books = _prepare(
        tmp_path, [_entry(103)], approved_ids=("103",), failed_book_ids=("103",)
    )
    decision = select_block_candidates_v2(
        protocol,
        block_ordinal=1,
        selected_at=START + timedelta(seconds=30),
        source_payload_bytes=raw,
        source_provenance=source,
        reviews=reviews,
        review_receipts=receipts,
        book_attempts=attempts,
        book_attempt_receipts=attempt_receipts,
        book_payload_bytes=books,
    )
    assert decision.status is BlockAdmissionStatusV2.EMPTY
    assert decision.page_decisions[0].exclusion_reason is V2ExclusionReason.BOOK_UNAVAILABLE
    verify_block_selection_v2_archives(
        protocol,
        decision,
        archive_dir=tmp_path,
        selection_receipt=_persist_selection(tmp_path, decision),
    )


def test_tampered_archive_and_foreign_review_fail_closed(tmp_path: Path) -> None:
    protocol = _protocol()
    raw, source, reviews, receipts, attempts, attempt_receipts, books = _prepare(
        tmp_path, [_entry(104)], approved_ids=("104",)
    )
    with pytest.raises(ValueError, match="semantic review disagrees"):
        select_block_candidates_v2(
            protocol,
            block_ordinal=1,
            selected_at=START + timedelta(seconds=30),
            source_payload_bytes=raw,
            source_provenance=source,
            reviews=(reviews[0].model_copy(update={"entry_sha256": "f" * 64}),),
            review_receipts=receipts,
            book_attempts=attempts,
            book_attempt_receipts=attempt_receipts,
            book_payload_bytes=books,
        )
    decision = select_block_candidates_v2(
        protocol,
        block_ordinal=1,
        selected_at=START + timedelta(seconds=30),
        source_payload_bytes=raw,
        source_provenance=source,
        reviews=reviews,
        review_receipts=receipts,
        book_attempts=attempts,
        book_attempt_receipts=attempt_receipts,
        book_payload_bytes=books,
    )
    book_source = attempts[0].source_provenance
    assert book_source is not None
    path = tmp_path / "clob_rest" / f"{book_source.raw_sha256}.raw.json"
    receipt = _persist_selection(tmp_path, decision)
    path.write_bytes(b"tampered")
    with pytest.raises(ImmutabilityViolationError):
        verify_block_selection_v2_archives(
            protocol, decision, archive_dir=tmp_path, selection_receipt=receipt
        )


def test_duplicate_page_identity_is_not_ranked(tmp_path: Path) -> None:
    protocol = _protocol()
    raw, source, *_ = _prepare(tmp_path, [_entry(105), {**_entry(106), "id": "0105"}])
    with pytest.raises(ValueError, match="duplicate discovery identities"):
        select_block_candidates_v2(
            protocol,
            block_ordinal=1,
            selected_at=START + timedelta(seconds=30),
            source_payload_bytes=raw,
            source_provenance=source,
            reviews=(),
            review_receipts=(),
            book_attempts=(),
            book_attempt_receipts=(),
            book_payload_bytes={},
        )


def test_second_block_replays_predecessor_and_rejects_reused_group(tmp_path: Path) -> None:
    protocol = _protocol()
    (
        first_raw,
        first_source,
        first_reviews,
        first_receipts,
        first_attempts,
        first_attempt_receipts,
        first_books,
    ) = _prepare(tmp_path, [_entry(201)], approved_ids=("201",))
    first = select_block_candidates_v2(
        protocol,
        block_ordinal=1,
        selected_at=START + timedelta(seconds=30),
        source_payload_bytes=first_raw,
        source_provenance=first_source,
        reviews=first_reviews,
        review_receipts=first_receipts,
        book_attempts=first_attempts,
        book_attempt_receipts=first_attempt_receipts,
        book_payload_bytes=first_books,
    )
    second_raw, second_source, reviews, receipts, attempts, attempt_receipts, books = _prepare(
        tmp_path,
        [_entry(202), _entry(203)],
        block_ordinal=2,
        approved_ids=("202", "203"),
        group_overrides={"202": "201"},
    )
    second = select_block_candidates_v2(
        protocol,
        block_ordinal=2,
        selected_at=START + timedelta(hours=1, seconds=30),
        source_payload_bytes=second_raw,
        source_provenance=second_source,
        reviews=reviews,
        review_receipts=receipts,
        book_attempts=attempts,
        book_attempt_receipts=attempt_receipts,
        book_payload_bytes=books,
        prior_selections=(first,),
        prior_source_bytes=(first_raw,),
        prior_book_bytes=(first_books,),
    )
    assert second.status is BlockAdmissionStatusV2.PARTIAL
    assert second.page_decisions[0].exclusion_reason is V2ExclusionReason.EVENT_GROUP_REUSED
    assert second.page_decisions[1].market_id == "203"
    first_receipt = _persist_selection(tmp_path, first)
    second_receipt = _persist_selection(tmp_path, second)
    verify_block_selection_v2_archives(
        protocol,
        second,
        archive_dir=tmp_path,
        selection_receipt=second_receipt,
        prior_selections=(first,),
        prior_selection_receipts=(first_receipt,),
    )
    with pytest.raises(ValueError, match="every verified predecessor"):
        verify_block_selection_v2_archives(
            protocol,
            second,
            archive_dir=tmp_path,
            selection_receipt=second_receipt,
        )


def test_time_limited_block_accounts_unfilled_slot(tmp_path: Path) -> None:
    protocol = _protocol()
    raw, source, reviews, receipts, attempts, attempt_receipts, books = _prepare(
        tmp_path,
        [_entry(301), _entry(302)],
        selected_offset=450,
        approved_ids=("301", "302"),
    )
    decision = select_block_candidates_v2(
        protocol,
        block_ordinal=1,
        selected_at=START + timedelta(seconds=450),
        source_payload_bytes=raw,
        source_provenance=source,
        reviews=reviews,
        review_receipts=receipts,
        book_attempts=attempts,
        book_attempt_receipts=attempt_receipts,
        book_payload_bytes=books,
    )
    assert decision.admitted_count == 1
    assert decision.page_decisions[1].exclusion_reason is V2ExclusionReason.BLOCK_CAPACITY
    verify_block_selection_v2_archives(
        protocol,
        decision,
        archive_dir=tmp_path,
        selection_receipt=_persist_selection(tmp_path, decision),
    )


def test_non_object_page_entry_is_quarantined_without_market_id(tmp_path: Path) -> None:
    protocol = _protocol()
    raw, source, *_ = _prepare(tmp_path, [None, "bad", 4])
    decision = select_block_candidates_v2(
        protocol,
        block_ordinal=1,
        selected_at=START + timedelta(seconds=30),
        source_payload_bytes=raw,
        source_provenance=source,
        reviews=(),
        review_receipts=(),
        book_attempts=(),
        book_attempt_receipts=(),
        book_payload_bytes={},
    )
    assert decision.status is BlockAdmissionStatusV2.EMPTY
    assert all(
        item.exclusion_reason is V2ExclusionReason.NORMALIZATION_REJECTED and item.market_id is None
        for item in decision.page_decisions
    )
    verify_block_selection_v2_archives(
        protocol,
        decision,
        archive_dir=tmp_path,
        selection_receipt=_persist_selection(tmp_path, decision),
    )


def test_three_of_four_declared_slots_are_retained(tmp_path: Path) -> None:
    baseline = _protocol()
    wider_block = baseline.blocks[0].model_copy(
        update={"end": START + timedelta(minutes=12), "intended_targets": 4}
    )
    wider_stratum = baseline.strata[0].model_copy(update={"maximum_targets": 4})
    protocol = _protocol(
        blocks=(wider_block,),
        strata=(wider_stratum,),
        target_budget=4,
    )
    raw, source, reviews, receipts, attempts, attempt_receipts, books = _prepare(
        tmp_path,
        [_entry(401), _entry(402), _entry(403)],
        approved_ids=("401", "402", "403"),
    )
    decision = select_block_candidates_v2(
        protocol,
        block_ordinal=1,
        selected_at=START + timedelta(seconds=30),
        source_payload_bytes=raw,
        source_provenance=source,
        reviews=reviews,
        review_receipts=receipts,
        book_attempts=attempts,
        book_attempt_receipts=attempt_receipts,
        book_payload_bytes=books,
    )
    assert decision.status is BlockAdmissionStatusV2.PARTIAL
    assert (decision.admitted_count, decision.unfilled_slots) == (3, 1)
    verify_block_selection_v2_archives(
        protocol,
        decision,
        archive_dir=tmp_path,
        selection_receipt=_persist_selection(tmp_path, decision),
    )


def test_empty_first_block_does_not_block_valid_second_block(tmp_path: Path) -> None:
    protocol = _protocol()
    first_raw, first_source, reviews, receipts, attempts, attempt_receipts, books = _prepare(
        tmp_path, [_entry(501)], approved_ids=("501",), failed_book_ids=("501",)
    )
    first = select_block_candidates_v2(
        protocol,
        block_ordinal=1,
        selected_at=START + timedelta(seconds=30),
        source_payload_bytes=first_raw,
        source_provenance=first_source,
        reviews=reviews,
        review_receipts=receipts,
        book_attempts=attempts,
        book_attempt_receipts=attempt_receipts,
        book_payload_bytes=books,
    )
    second_raw, second_source, reviews, receipts, attempts, attempt_receipts, books = _prepare(
        tmp_path, [_entry(502)], block_ordinal=2, approved_ids=("502",)
    )
    second = select_block_candidates_v2(
        protocol,
        block_ordinal=2,
        selected_at=START + timedelta(hours=1, seconds=30),
        source_payload_bytes=second_raw,
        source_provenance=second_source,
        reviews=reviews,
        review_receipts=receipts,
        book_attempts=attempts,
        book_attempt_receipts=attempt_receipts,
        book_payload_bytes=books,
        prior_selections=(first,),
        prior_source_bytes=(first_raw,),
        prior_book_bytes=({},),
    )
    assert first.status is BlockAdmissionStatusV2.EMPTY
    assert second.status is BlockAdmissionStatusV2.PARTIAL
    assert second.admitted_count == 1
    verify_block_selection_v2_archives(
        protocol,
        second,
        archive_dir=tmp_path,
        selection_receipt=_persist_selection(tmp_path, second),
        prior_selections=(first,),
        prior_selection_receipts=(_persist_selection(tmp_path, first),),
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"reviewer_id": " "}, "reviewer"),
        ({"notes": " "}, "substantive notes"),
        ({"yes_condition": None}, "both outcomes"),
        ({"yes_condition": "The named event does not occur."}, "must differ"),
        ({"earliest_outcome_knowable_at": None}, "future outcome"),
        ({"earliest_outcome_knowable_at": START}, "future outcome"),
        ({"rejection_reason": "none"}, "cannot carry"),
        ({"decision": "rejected"}, "needs a reason"),
    ],
)
def test_semantic_review_requires_substantive_attestation(
    tmp_path: Path, change: dict[str, object], message: str
) -> None:
    _, _, reviews, _, _, _, _ = _prepare(tmp_path, [_entry(601)], approved_ids=("601",))
    record = reviews[0].to_record()
    record.update(change)
    with pytest.raises(ValidationError, match=message):
        HumanSemanticReviewV1.from_record(record)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"source_provenance": None}, "needs provenance"),
        ({"failure_code": "unexpected"}, "needs provenance"),
        ({"attempted_at": START + timedelta(seconds=8)}, "predates"),
        ({"status": "failed", "failure_code": None}, "needs a code"),
    ],
)
def test_book_attempt_requires_consistent_response_or_failure(
    tmp_path: Path, change: dict[str, object], message: str
) -> None:
    _, _, _, _, attempts, _, _ = _prepare(tmp_path, [_entry(602)], approved_ids=("602",))
    record = attempts[0].to_record()
    record.update(change)
    with pytest.raises(ValidationError, match=message):
        BookAttemptV1.from_record(record)


def test_explicit_rejected_review_roundtrips(tmp_path: Path) -> None:
    _, _, reviews, _, _, _, _ = _prepare(tmp_path, [_entry(603)], approved_ids=("603",))
    record = reviews[0].to_record()
    record.update({"decision": "rejected", "rejection_reason": "Ambiguous synthetic rules"})
    rejected = HumanSemanticReviewV1.from_record(record)
    assert rejected.decision is SemanticReviewDecision.REJECTED
    assert HumanSemanticReviewV1.from_record(rejected.to_record()) == rejected


def test_review_is_bound_to_compiler_version(tmp_path: Path) -> None:
    _, kwargs = _valid_selection(tmp_path)
    review = kwargs["reviews"][0]
    kwargs["reviews"] = (review.model_copy(update={"compiler_version": "foreign"}),)
    with pytest.raises(ValueError, match="semantic review disagrees"):
        select_block_candidates_v2(_protocol(), **kwargs)


def _valid_selection(tmp_path: Path) -> tuple[OfflineBlockSelectionV2, dict[str, Any]]:
    protocol = _protocol()
    raw, source, reviews, receipts, attempts, attempt_receipts, books = _prepare(
        tmp_path, [_entry(701), _entry(702)], approved_ids=("701",)
    )
    kwargs: dict[str, Any] = {
        "block_ordinal": 1,
        "selected_at": START + timedelta(seconds=30),
        "source_payload_bytes": raw,
        "source_provenance": source,
        "reviews": reviews,
        "review_receipts": receipts,
        "book_attempts": attempts,
        "book_attempt_receipts": attempt_receipts,
        "book_payload_bytes": books,
    }
    return select_block_candidates_v2(protocol, **kwargs), kwargs


def test_decision_models_refuse_partition_and_disposition_forgery(tmp_path: Path) -> None:
    selection, _ = _valid_selection(tmp_path)
    selected = selection.page_decisions[0].to_record()
    excluded = selection.page_decisions[1].to_record()
    selected["stratum_id"] = None
    with pytest.raises(ValidationError, match="selected entry needs"):
        PageDecisionV1.from_record(selected)
    selected_market = selection.page_decisions[0].market
    assert selected_market is not None
    excluded["market"] = selected_market.to_record()
    with pytest.raises(ValidationError, match="excluded entry cannot"):
        PageDecisionV1.from_record(excluded)

    base = selection.to_record()
    mutations: tuple[tuple[str, object, str], ...] = (
        ("source_candidate_count", 3, "partition every page"),
        ("admitted_count", 0, "admitted count"),
        ("unfilled_slots", 0, "unfilled slot count"),
        ("status", "full", "status disagrees"),
        ("predecessor_selection_sha256", "a" * 64, "predecessor digest"),
        ("review_receipts", [], "persistence receipts"),
    )
    for field, value, message in mutations:
        record = deepcopy(base)
        record[field] = value
        with pytest.raises(ValidationError, match=message):
            OfflineBlockSelectionV2.from_record(record)
    record = deepcopy(base)
    record["page_decisions"][1]["entry_index"] = 0
    with pytest.raises(ValidationError, match="partition every page"):
        OfflineBlockSelectionV2.from_record(record)
    record = deepcopy(base)
    record["page_decisions"][0]["market"]["raw_payload_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="exact discovery page"):
        OfflineBlockSelectionV2.from_record(record)


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("wrong_source", "discovery disagrees"),
        ("wrong_query", "discovery disagrees"),
        ("wrong_status", "discovery disagrees"),
        ("changed_bytes", "discovery disagrees"),
        ("unknown_block", "unknown cohort block"),
        ("late_selection", "inside the declared block"),
        ("invalid_json", "not valid JSON"),
        ("not_page", "must be a market page"),
        ("missing_review_receipt", "receipts must be complete"),
        ("duplicate_review", "one semantic review per page entry"),
        ("duplicate_book_attempt", "one book attempt per reviewed market"),
        ("missing_book_attempt", "missing an accounted book attempt"),
        ("extra_book_bytes", "must exactly match recorded response attempts"),
        ("extra_review", "unaccounted review or book attempt"),
        ("extra_book_attempt", "unaccounted review or book attempt"),
        ("late_review_receipt", "no valid pre-selection receipt"),
        ("foreign_book_attempt", "no valid pre-selection receipt"),
    ],
)
def test_selector_refuses_invalid_input_and_unaccounted_evidence(
    tmp_path: Path, fault: str, message: str
) -> None:
    _, kwargs = _valid_selection(tmp_path)
    protocol = _protocol()
    source = kwargs["source_provenance"]
    if fault == "wrong_source":
        kwargs["source_provenance"] = source.model_copy(update={"source": "elsewhere"})
    elif fault == "wrong_query":
        kwargs["source_provenance"] = source.model_copy(update={"endpoint": "https://bad"})
    elif fault == "wrong_status":
        kwargs["source_provenance"] = source.model_copy(update={"http_status": 500})
    elif fault == "changed_bytes":
        kwargs["source_payload_bytes"] = b"[]"
    elif fault == "unknown_block":
        kwargs["block_ordinal"] = 3
    elif fault == "late_selection":
        kwargs["selected_at"] = START + timedelta(minutes=11)
    elif fault in {"invalid_json", "not_page"}:
        raw = b"bad json" if fault == "invalid_json" else b"{}"
        kwargs["source_payload_bytes"] = raw
        kwargs["source_provenance"] = _provenance("gamma", source.endpoint, raw, START)
    elif fault == "missing_review_receipt":
        kwargs["review_receipts"] = ()
    elif fault == "duplicate_review":
        kwargs["reviews"] = kwargs["reviews"] * 2
        kwargs["review_receipts"] = kwargs["review_receipts"] * 2
    elif fault == "duplicate_book_attempt":
        kwargs["book_attempts"] = kwargs["book_attempts"] * 2
        kwargs["book_attempt_receipts"] = kwargs["book_attempt_receipts"] * 2
    elif fault == "missing_book_attempt":
        kwargs["book_attempts"] = ()
        kwargs["book_attempt_receipts"] = ()
    elif fault == "extra_book_bytes":
        kwargs["book_payload_bytes"] = {**kwargs["book_payload_bytes"], "other": b"{}"}
    elif fault == "extra_review":
        extra = kwargs["reviews"][0].model_copy(update={"entry_index": 3})
        kwargs["reviews"] = (*kwargs["reviews"], extra)
        kwargs["review_receipts"] = (*kwargs["review_receipts"], kwargs["review_receipts"][0])
    elif fault == "extra_book_attempt":
        extra = kwargs["book_attempts"][0].model_copy(update={"market_id": "999"})
        kwargs["book_attempts"] = (*kwargs["book_attempts"], extra)
        kwargs["book_attempt_receipts"] = (
            *kwargs["book_attempt_receipts"],
            kwargs["book_attempt_receipts"][0],
        )
    elif fault == "late_review_receipt":
        receipt = kwargs["review_receipts"][0]
        late_at = START + timedelta(minutes=1)
        kwargs["review_receipts"] = (
            receipt.model_copy(
                update={
                    "persisted_at": late_at,
                    "receipt_id": build_persistence_receipt_id(
                        experiment_id=receipt.experiment_id,
                        artifact_kind=receipt.artifact_kind,
                        artifact_id=receipt.artifact_id,
                        artifact_schema_version=receipt.artifact_schema_version,
                        artifact_sha256=receipt.artifact_sha256,
                        artifact_byte_length=receipt.artifact_byte_length,
                        persisted_at=late_at,
                        storage_backend=receipt.storage_backend,
                        storage_identity=receipt.storage_identity,
                    ),
                }
            ),
        )
    elif fault == "foreign_book_attempt":
        attempt = kwargs["book_attempts"][0]
        kwargs["book_attempts"] = (
            attempt.model_copy(update={"experiment_id": "foreign-experiment"}),
        )
    with pytest.raises(ValueError, match=message):
        select_block_candidates_v2(protocol, **kwargs)


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ("source_ineligible", V2ExclusionReason.SOURCE_INELIGIBLE),
        ("contract_invalid", V2ExclusionReason.CONTRACT_INVALID),
        ("review_rejected", V2ExclusionReason.REVIEW_REJECTED),
        ("review_too_late", V2ExclusionReason.REVIEW_TOO_LATE),
        ("no_stratum", V2ExclusionReason.NO_DECLARED_STRATUM),
    ],
)
def test_source_and_human_review_exclusions_are_partitioned(
    tmp_path: Path, fault: str, expected: V2ExclusionReason
) -> None:
    protocol = _protocol()
    if fault in {"source_ineligible", "contract_invalid"}:
        entry = _entry(801)
        if fault == "source_ineligible":
            entry["liquidity"] = "10"
        else:
            entry["description"] = ""
            entry["resolutionSource"] = ""
        raw = orjson.dumps([entry])
        source = _provenance(
            "gamma",
            f"{protocol.selection.base_url}/markets?{urlencode(protocol.selection.query)}",
            raw,
            START,
        )
        kwargs: dict[str, Any] = {
            "block_ordinal": 1,
            "selected_at": START + timedelta(seconds=30),
            "source_payload_bytes": raw,
            "source_provenance": source,
            "reviews": (),
            "review_receipts": (),
            "book_attempts": (),
            "book_attempt_receipts": (),
            "book_payload_bytes": {},
        }
    else:
        _, kwargs = _valid_selection(tmp_path)
        review = kwargs["reviews"][0]
        record = review.to_record()
        if fault == "review_rejected":
            record.update({"decision": "rejected", "rejection_reason": "Ambiguous event"})
        elif fault == "review_too_late":
            record["earliest_outcome_knowable_at"] = (START + timedelta(minutes=3)).isoformat()
        else:
            record["category"] = "other-category"
        changed_review = HumanSemanticReviewV1.from_record(record)
        changed_receipt = persist_evidence_record(
            tmp_path,
            record=changed_review,
            experiment_id=protocol.experiment_id,
            artifact_kind=EvidenceArtifactKind.SEMANTIC_REVIEW,
            artifact_id=changed_review.review_id,
            persisted_at=changed_review.reviewed_at + timedelta(seconds=1),
        )
        kwargs["reviews"] = (changed_review,)
        kwargs["review_receipts"] = (changed_receipt,)
        if fault != "no_stratum":
            kwargs["book_attempts"] = ()
            kwargs["book_attempt_receipts"] = ()
            kwargs["book_payload_bytes"] = {}
    selection = select_block_candidates_v2(protocol, **kwargs)
    assert selection.page_decisions[0].exclusion_reason is expected
