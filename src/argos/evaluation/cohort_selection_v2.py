"""Offline V2 cohort admission from exact Gamma, CLOB and review evidence.

This module performs no network calls or writes. A live operator must archive
each raw response, persist the review/attempt records and receipts first, then
persist target declarations after this deterministic page decision.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qsl, urlsplit

import orjson
from pydantic import Field, field_validator, model_validator

from argos.clock import ensure_utc
from argos.compiler import compile_market_contract
from argos.domain.market import MarketDefinitionV1
from argos.domain.orderbook import parse_order_book_snapshot
from argos.domain.provenance import SHA256_LENGTH, SourceProvenanceV1, sha256_hex
from argos.domain.versioning import VersionedModel, ensure_supported_version
from argos.errors import IngestionError
from argos.evaluation.async_cohort import (
    _candidate_rejection,
    _event_identity,
    _market_identity,
    _source_query_matches,
    _token_identity,
)
from argos.evaluation.cohort_protocol_v2 import AsynchronousCohortProtocolV2, CohortStratumV1
from argos.evaluation.cohort_review_v2 import HumanSemanticReviewV1, SemanticReviewDecision
from argos.evaluation.numeric import evaluation_context
from argos.evaluation.prospective import (
    EvidenceArtifactKind,
    EvidencePersistenceReceiptV1,
    load_persisted_record,
    verify_receipt_for_record,
)
from argos.ingestion.gamma_markets import normalize_market
from argos.store.raw_archive import read_raw_payload


def _canonical_bytes(record: VersionedModel) -> bytes:
    return orjson.dumps(record.to_record(), option=orjson.OPT_SORT_KEYS)


def _entry_hash(entry: Any) -> str:
    return sha256_hex(orjson.dumps(entry, option=orjson.OPT_SORT_KEYS))


def _receipt_matches(
    receipt: EvidencePersistenceReceiptV1,
    record: VersionedModel,
    *,
    experiment_id: str,
    kind: EvidenceArtifactKind,
    artifact_id: str,
    earliest: datetime,
    latest: datetime,
) -> bool:
    verify_receipt_for_record(receipt, record)
    return (
        receipt.experiment_id == experiment_id
        and receipt.artifact_kind is kind
        and receipt.artifact_id == artifact_id
        and earliest <= receipt.persisted_at <= latest
    )


class BookAttemptStatus(StrEnum):
    RESPONSE = "response"
    FAILED = "failed"


class BookAttemptV1(VersionedModel):
    """One recorded public Yes-token book request before block selection."""

    schema_version: ClassVar[str] = "m4_cohort_book_attempt.v1"

    experiment_id: str = Field(min_length=1)
    market_id: str = Field(min_length=1)
    requested_token_id: str = Field(min_length=1)
    attempted_at: datetime
    status: BookAttemptStatus
    source_provenance: SourceProvenanceV1 | None = None
    failure_code: str | None = None

    @field_validator("attempted_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _status_has_evidence(self) -> BookAttemptV1:
        if self.status is BookAttemptStatus.RESPONSE:
            if self.source_provenance is None or self.failure_code is not None:
                raise ValueError("book response needs provenance and no failure code")
            if self.source_provenance.retrieved_at < self.attempted_at:
                raise ValueError("book response predates its request")
        elif self.source_provenance is not None or not self.failure_code:
            raise ValueError("failed book request needs a code and no response provenance")
        return self

    @property
    def attempt_id(self) -> str:
        return f"book-attempt-{sha256_hex(_canonical_bytes(self))}"

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        if self.source_provenance is not None:
            record["source_provenance"] = self.source_provenance.to_record()
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> BookAttemptV1:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        if payload.get("source_provenance") is not None:
            payload["source_provenance"] = SourceProvenanceV1.from_record(
                dict(payload["source_provenance"])
            )
        return cls.model_validate(payload)


class V2ExclusionReason(StrEnum):
    NORMALIZATION_REJECTED = "normalization_rejected"
    SOURCE_INELIGIBLE = "source_ineligible"
    CONTRACT_INVALID = "contract_invalid"
    UNREVIEWED = "unreviewed"
    REVIEW_REJECTED = "review_rejected"
    REVIEW_TOO_LATE = "review_too_late"
    BOOK_UNAVAILABLE = "book_unavailable"
    BOOK_INVALID = "book_invalid"
    NO_DECLARED_STRATUM = "no_declared_stratum"
    IDENTITY_REUSED = "identity_reused"
    EVENT_GROUP_REUSED = "event_group_reused"
    STRATUM_QUOTA = "stratum_quota"
    BLOCK_CAPACITY = "block_capacity"


class PageDecisionV1(VersionedModel):
    """Exactly one original page index, including anonymous quarantine."""

    schema_version: ClassVar[str] = "m4_cohort_page_decision.v1"

    entry_index: int = Field(ge=0, strict=True)
    entry_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    market_id: str | None = None
    exclusion_reason: V2ExclusionReason | None = None
    market: MarketDefinitionV1 | None = None
    stratum_id: str | None = None
    event_group_id: str | None = None
    review_receipt_id: str | None = None
    book_attempt_receipt_id: str | None = None
    observed_spread: Decimal | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _one_disposition(self) -> PageDecisionV1:
        if self.exclusion_reason is None:
            if (
                self.market is None
                or self.market_id != self.market.market_id
                or not all(
                    (
                        self.stratum_id,
                        self.event_group_id,
                        self.review_receipt_id,
                        self.book_attempt_receipt_id,
                    )
                )
                or self.observed_spread is None
            ):
                raise ValueError("selected entry needs a market, stratum, review and book")
        elif any(
            value is not None
            for value in (
                self.market,
                self.stratum_id,
                self.event_group_id,
                self.review_receipt_id,
                self.book_attempt_receipt_id,
                self.observed_spread,
            )
        ):
            raise ValueError("excluded entry cannot also contain a target")
        return self

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        if self.market is not None:
            record["market"] = self.market.to_record()
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> PageDecisionV1:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        if payload.get("market") is not None:
            payload["market"] = MarketDefinitionV1.from_record(dict(payload["market"]))
        return cls.model_validate(payload)


class BlockAdmissionStatusV2(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    EMPTY = "empty"


class OfflineBlockSelectionV2(VersionedModel):
    """Versioned partition of one exact page, with partial slot accounting."""

    schema_version: ClassVar[str] = "m4_offline_block_selection.v2"

    experiment_id: str = Field(min_length=1)
    block_ordinal: int = Field(ge=1, strict=True)
    selected_at: datetime
    protocol_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    predecessor_selection_sha256: str | None = None
    source_provenance: SourceProvenanceV1
    source_candidate_count: int = Field(ge=0, strict=True)
    slot_cap: int = Field(gt=0, strict=True)
    admitted_count: int = Field(ge=0, strict=True)
    unfilled_slots: int = Field(ge=0, strict=True)
    status: BlockAdmissionStatusV2
    page_decisions: tuple[PageDecisionV1, ...]
    reviews: tuple[HumanSemanticReviewV1, ...]
    review_receipts: tuple[EvidencePersistenceReceiptV1, ...]
    book_attempts: tuple[BookAttemptV1, ...]
    book_attempt_receipts: tuple[EvidencePersistenceReceiptV1, ...]

    @field_validator("selected_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _accounted_partition(self) -> OfflineBlockSelectionV2:
        if self.source_candidate_count != len(self.page_decisions) or tuple(
            entry.entry_index for entry in self.page_decisions
        ) != tuple(range(self.source_candidate_count)):
            raise ValueError("selection must partition every page entry in original order")
        selected = tuple(entry for entry in self.page_decisions if entry.exclusion_reason is None)
        if self.admitted_count != len(selected) or self.admitted_count > self.slot_cap:
            raise ValueError("admitted count disagrees with selected page entries or slot cap")
        if self.unfilled_slots != self.slot_cap - self.admitted_count:
            raise ValueError("unfilled slot count disagrees with the block cap")
        expected_status = (
            BlockAdmissionStatusV2.EMPTY
            if not selected
            else BlockAdmissionStatusV2.FULL
            if not self.unfilled_slots
            else BlockAdmissionStatusV2.PARTIAL
        )
        if self.status is not expected_status:
            raise ValueError("block status disagrees with admitted and unfilled counts")
        if (self.block_ordinal == 1) != (self.predecessor_selection_sha256 is None):
            raise ValueError("block predecessor digest disagrees with ordinal")
        if len(self.reviews) != len(self.review_receipts) or len(self.book_attempts) != len(
            self.book_attempt_receipts
        ):
            raise ValueError("reviews and book attempts each need persistence receipts")
        if len({entry.market_id for entry in selected}) != len(selected) or len(
            {entry.event_group_id for entry in selected}
        ) != len(selected):
            raise ValueError("admitted markets and reviewed event groups must be distinct")
        if any(
            entry.market is None
            or entry.market.raw_payload_sha256 != self.source_provenance.raw_sha256
            for entry in selected
        ):
            raise ValueError("admitted market must bind the exact discovery page")
        return self

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        record["source_provenance"] = self.source_provenance.to_record()
        record["page_decisions"] = [entry.to_record() for entry in self.page_decisions]
        record["reviews"] = [review.to_record() for review in self.reviews]
        record["review_receipts"] = [receipt.to_record() for receipt in self.review_receipts]
        record["book_attempts"] = [attempt.to_record() for attempt in self.book_attempts]
        record["book_attempt_receipts"] = [
            receipt.to_record() for receipt in self.book_attempt_receipts
        ]
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> OfflineBlockSelectionV2:
        payload = dict(record)
        ensure_supported_version(payload.pop("schema_version", None), (cls.schema_version,))
        payload["source_provenance"] = SourceProvenanceV1.from_record(
            dict(payload["source_provenance"])
        )
        payload["page_decisions"] = tuple(
            PageDecisionV1.from_record(dict(x)) for x in payload["page_decisions"]
        )
        payload["reviews"] = tuple(
            HumanSemanticReviewV1.from_record(dict(x)) for x in payload["reviews"]
        )
        payload["review_receipts"] = tuple(
            EvidencePersistenceReceiptV1.from_record(dict(x)) for x in payload["review_receipts"]
        )
        payload["book_attempts"] = tuple(
            BookAttemptV1.from_record(dict(x)) for x in payload["book_attempts"]
        )
        payload["book_attempt_receipts"] = tuple(
            EvidencePersistenceReceiptV1.from_record(dict(x))
            for x in payload["book_attempt_receipts"]
        )
        return cls.model_validate(payload)


@dataclass(frozen=True)
class _Eligible:
    index: int
    market: MarketDefinitionV1
    stratum: CohortStratumV1
    review: HumanSemanticReviewV1
    review_receipt: EvidencePersistenceReceiptV1
    book_attempt_receipt: EvidencePersistenceReceiptV1
    spread: Decimal


def _book_endpoint_matches(provenance: SourceProvenanceV1, token_id: str) -> bool:
    url = urlsplit(provenance.endpoint)
    return (
        url.scheme == "https"
        and url.hostname == "clob.polymarket.com"
        and url.port is None
        and not url.username
        and not url.password
        and url.path == "/book"
        and not url.fragment
        and parse_qsl(url.query, keep_blank_values=True) == [("token_id", token_id)]
    )


def _review_matches(
    review: HumanSemanticReviewV1,
    market: MarketDefinitionV1,
    *,
    index: int,
    entry_sha256: str,
    page_sha256: str,
    selected_at: datetime,
) -> bool:
    contract = compile_market_contract(market, compiled_at=selected_at)
    return (
        review.source_payload_sha256 == page_sha256
        and review.entry_index == index
        and review.entry_sha256 == entry_sha256
        and review.market_id == market.market_id
        and review.condition_id == market.condition_id
        and review.contract_id == contract.contract_id
        and review.compiler_version == contract.compiler_version
    )


def _spread_from_book(
    market: MarketDefinitionV1,
    attempt: BookAttemptV1,
    raw: bytes | None,
    *,
    selected_at: datetime,
    source_retrieved_at: datetime,
) -> Decimal | None:
    if attempt.requested_token_id != market.token_id_for("Yes"):
        raise ValueError("book attempt token disagrees with the reviewed Yes token")
    if not source_retrieved_at <= attempt.attempted_at <= selected_at:
        raise ValueError("book attempt is outside the discovery/selection interval")
    provenance = attempt.source_provenance
    if attempt.status is BookAttemptStatus.FAILED:
        if raw is not None:
            raise ValueError("failed book request cannot have a response body")
        return None
    if (
        provenance is None
        or raw is None
        or (
            provenance.source != "clob_rest"
            or provenance.reconstructed
            or not provenance.matches(raw)
            or not _book_endpoint_matches(provenance, attempt.requested_token_id)
            or provenance.retrieved_at > selected_at
        )
    ):
        raise ValueError("book response disagrees with public source, exact bytes or time")
    if provenance.http_status != 200:
        return None
    try:
        parsed = orjson.loads(raw)
        book = parse_order_book_snapshot(parsed)
    except (orjson.JSONDecodeError, ValueError, TypeError):
        return None
    if (
        book.condition_id != market.condition_id
        or _token_identity(book.asset_id) != _token_identity(attempt.requested_token_id)
        or book.anomalies
        or not book.bids
        or not book.asks
    ):
        return None
    with evaluation_context():
        spread = book.asks[0].price - book.bids[0].price
        if not Decimal(0) <= spread <= Decimal(1):
            return None
    return spread


def _matching_stratum(
    protocol: AsynchronousCohortProtocolV2,
    market: MarketDefinitionV1,
    review: HumanSemanticReviewV1,
    spread: Decimal,
    *,
    selected_at: datetime,
) -> CohortStratumV1 | None:
    if market.liquidity is None or review.earliest_outcome_knowable_at is None:
        return None
    return next(
        (
            stratum
            for stratum in sorted(protocol.strata, key=lambda x: x.stratum_id)
            if stratum.category == review.category
            and stratum.minimum_liquidity <= market.liquidity < stratum.maximum_liquidity
            and stratum.minimum_spread <= spread < stratum.maximum_spread
            and selected_at + timedelta(seconds=stratum.minimum_horizon_seconds)
            <= review.earliest_outcome_knowable_at
            < selected_at + timedelta(seconds=stratum.maximum_horizon_seconds)
        ),
        None,
    )


def _prior_state(
    protocol: AsynchronousCohortProtocolV2,
    *,
    block_ordinal: int,
    prior_selections: tuple[OfflineBlockSelectionV2, ...],
    prior_source_bytes: tuple[bytes, ...],
    prior_book_bytes: tuple[dict[str, bytes], ...],
) -> tuple[set[str], set[str], set[str], set[str], set[str], Counter[str], str | None]:
    if not 1 <= block_ordinal <= len(protocol.blocks):
        raise ValueError("unknown cohort block")
    if any(
        len(group) != block_ordinal - 1
        for group in (prior_selections, prior_source_bytes, prior_book_bytes)
    ):
        raise ValueError("cohort blocks need every verified predecessor")
    protocol_sha = sha256_hex(_canonical_bytes(protocol))
    market_ids: set[str] = set()
    event_ids: set[str] = set()
    conditions: set[str] = set()
    tokens: set[str] = set()
    groups: set[str] = set()
    quotas: Counter[str] = Counter()
    predecessor_sha: str | None = None
    for index, (prior, source, books) in enumerate(
        zip(prior_selections, prior_source_bytes, prior_book_bytes, strict=True), start=1
    ):
        if (
            prior.experiment_id != protocol.experiment_id
            or prior.protocol_sha256 != protocol_sha
            or prior.block_ordinal != index
            or prior.predecessor_selection_sha256 != predecessor_sha
        ):
            raise ValueError("predecessor chain or protocol identity disagrees")
        # The final predecessor recursively replays its entire prefix. Checking
        # every prefix again here would double the verification tree per block.
        if index == len(prior_selections):
            verify_block_selection_v2(
                protocol,
                prior,
                source_payload_bytes=source,
                book_payload_bytes=books,
                prior_selections=prior_selections[: index - 1],
                prior_source_bytes=prior_source_bytes[: index - 1],
                prior_book_bytes=prior_book_bytes[: index - 1],
            )
        for entry in prior.page_decisions:
            if entry.market is None:
                continue
            market = entry.market
            assert market.event_id is not None and entry.event_group_id is not None
            market_key = _market_identity(market.market_id)
            event_key = _event_identity(market.event_id)
            token_keys = {_token_identity(x) for x in market.outcome_token_map.values()}
            if (
                market_key in market_ids
                or event_key in event_ids
                or market.condition_id in conditions
                or token_keys & tokens
                or entry.event_group_id in groups
            ):
                raise ValueError("predecessors contain reused target identities")
            market_ids.add(market_key)
            event_ids.add(event_key)
            conditions.add(market.condition_id)
            tokens.update(token_keys)
            groups.add(entry.event_group_id)
            assert entry.stratum_id is not None
            quotas[entry.stratum_id] += 1
        predecessor_sha = sha256_hex(_canonical_bytes(prior))
    return market_ids, event_ids, conditions, tokens, groups, quotas, predecessor_sha


def select_block_candidates_v2(
    protocol: AsynchronousCohortProtocolV2,
    *,
    block_ordinal: int,
    selected_at: datetime,
    source_payload_bytes: bytes,
    source_provenance: SourceProvenanceV1,
    reviews: tuple[HumanSemanticReviewV1, ...],
    review_receipts: tuple[EvidencePersistenceReceiptV1, ...],
    book_attempts: tuple[BookAttemptV1, ...],
    book_attempt_receipts: tuple[EvidencePersistenceReceiptV1, ...],
    book_payload_bytes: dict[str, bytes],
    prior_selections: tuple[OfflineBlockSelectionV2, ...] = (),
    prior_source_bytes: tuple[bytes, ...] = (),
    prior_book_bytes: tuple[dict[str, bytes], ...] = (),
) -> OfflineBlockSelectionV2:
    """Retain valid targets from a short page and account for every original entry."""
    at = ensure_utc(selected_at)
    if (
        source_provenance.source != "gamma"
        or source_provenance.reconstructed
        or source_provenance.http_status != 200
        or not source_provenance.matches(source_payload_bytes)
        or not _source_query_matches(source_provenance, protocol.selection)
    ):
        raise ValueError("discovery disagrees with frozen Gamma query or raw bytes")
    earlier = _prior_state(
        protocol,
        block_ordinal=block_ordinal,
        prior_selections=prior_selections,
        prior_source_bytes=prior_source_bytes,
        prior_book_bytes=prior_book_bytes,
    )
    block = protocol.blocks[block_ordinal - 1]
    if not block.start <= source_provenance.retrieved_at <= at < block.end:
        raise ValueError("discovery and selection must occur inside the declared block")
    try:
        page = orjson.loads(source_payload_bytes)
    except orjson.JSONDecodeError as error:
        raise ValueError("discovery is not valid JSON") from error
    if not isinstance(page, list):
        raise ValueError("discovery must be a market page")
    raw_ids = [
        str(item["id"])
        for item in page
        if isinstance(item, dict)
        and isinstance(item.get("id"), str | int)
        and not isinstance(item["id"], bool)
        and str(item["id"])
    ]
    duplicate_ids = [
        identity
        for identity, count in Counter(_market_identity(raw_id) for raw_id in raw_ids).items()
        if count > 1
    ]
    if duplicate_ids:
        raise ValueError(f"ambiguous duplicate discovery identities: {sorted(duplicate_ids)}")
    if len(reviews) != len(review_receipts) or len(book_attempts) != len(book_attempt_receipts):
        raise ValueError("review and book attempt receipts must be complete")
    if len({review.entry_index for review in reviews}) != len(reviews):
        raise ValueError("one semantic review per page entry is permitted")
    if len({attempt.market_id for attempt in book_attempts}) != len(book_attempts):
        raise ValueError("one book attempt per reviewed market is permitted")
    review_by_index = {
        review.entry_index: (review, receipt)
        for review, receipt in zip(reviews, review_receipts, strict=True)
    }
    attempts_by_market = {
        attempt.market_id: (attempt, receipt)
        for attempt, receipt in zip(book_attempts, book_attempt_receipts, strict=True)
    }
    used_review_indices: set[int] = set()
    used_book_market_ids: set[str] = set()
    source_sha = source_provenance.raw_sha256
    decisions: dict[int, PageDecisionV1] = {}
    eligible: list[_Eligible] = []
    for index, raw_entry in enumerate(page):
        entry_sha = _entry_hash(raw_entry)
        raw_id = raw_entry.get("id") if isinstance(raw_entry, dict) else None
        market_id = (
            str(raw_id)
            if isinstance(raw_id, str | int) and not isinstance(raw_id, bool) and str(raw_id)
            else None
        )

        def exclude(
            reason: V2ExclusionReason,
            index: int = index,
            entry_sha: str = entry_sha,
            market_id: str | None = market_id,
        ) -> None:
            decisions[index] = PageDecisionV1(
                entry_index=index,
                entry_sha256=entry_sha,
                market_id=market_id,
                exclusion_reason=reason,
            )

        if not isinstance(raw_entry, dict):
            exclude(V2ExclusionReason.NORMALIZATION_REJECTED)
            continue
        try:
            market = normalize_market(
                raw_entry,
                raw_payload_sha256=source_sha,
                normalized_at=source_provenance.retrieved_at,
            )
        except IngestionError:
            exclude(V2ExclusionReason.NORMALIZATION_REJECTED)
            continue
        candidate_rejection = _candidate_rejection(
            market,
            raw_entry,
            protocol.selection,
            selected_at=at,
        )
        if candidate_rejection is not None:
            exclude(V2ExclusionReason.SOURCE_INELIGIBLE)
            continue
        try:
            contract = compile_market_contract(market, compiled_at=at)
        except ValueError:
            exclude(V2ExclusionReason.CONTRACT_INVALID)
            continue
        if not contract.resolution_source.strip() and not contract.source_rule_material.strip():
            exclude(V2ExclusionReason.CONTRACT_INVALID)
            continue
        matched_review = review_by_index.get(index)
        if matched_review is None:
            exclude(V2ExclusionReason.UNREVIEWED)
            continue
        review, review_receipt = matched_review
        used_review_indices.add(index)
        if (
            not _review_matches(
                review,
                market,
                index=index,
                entry_sha256=entry_sha,
                page_sha256=source_sha,
                selected_at=at,
            )
            or review.experiment_id != protocol.experiment_id
        ):
            raise ValueError("semantic review disagrees with exact page entry or contract")
        if (
            not _receipt_matches(
                review_receipt,
                review,
                experiment_id=protocol.experiment_id,
                kind=EvidenceArtifactKind.SEMANTIC_REVIEW,
                artifact_id=review.review_id,
                earliest=review.reviewed_at,
                latest=at,
            )
            or review.reviewed_at < source_provenance.retrieved_at
        ):
            raise ValueError("semantic review has no valid pre-selection receipt")
        if review.decision is SemanticReviewDecision.REJECTED:
            exclude(V2ExclusionReason.REVIEW_REJECTED)
            continue
        if (
            review.earliest_outcome_knowable_at is None
            or review.earliest_outcome_knowable_at
            < at
            + timedelta(
                seconds=protocol.capture_max_seconds_per_target
                + protocol.finalization_reserve_seconds
                + protocol.outcome_blind_margin_seconds
            )
        ):
            exclude(V2ExclusionReason.REVIEW_TOO_LATE)
            continue
        matched_attempt = attempts_by_market.get(market.market_id)
        if matched_attempt is None:
            raise ValueError("approved eligible candidate is missing an accounted book attempt")
        attempt, attempt_receipt = matched_attempt
        receipt_earliest = attempt.attempted_at
        if attempt.status is BookAttemptStatus.RESPONSE:
            assert attempt.source_provenance is not None
            receipt_earliest = attempt.source_provenance.retrieved_at
        used_book_market_ids.add(market.market_id)
        if attempt.experiment_id != protocol.experiment_id or not _receipt_matches(
            attempt_receipt,
            attempt,
            experiment_id=protocol.experiment_id,
            kind=EvidenceArtifactKind.COHORT_BOOK_ATTEMPT,
            artifact_id=attempt.attempt_id,
            earliest=receipt_earliest,
            latest=at,
        ):
            raise ValueError("book attempt has no valid pre-selection receipt")
        spread = _spread_from_book(
            market,
            attempt,
            book_payload_bytes.get(market.market_id),
            selected_at=at,
            source_retrieved_at=source_provenance.retrieved_at,
        )
        if spread is None:
            exclude(
                V2ExclusionReason.BOOK_UNAVAILABLE
                if attempt.status is BookAttemptStatus.FAILED
                else V2ExclusionReason.BOOK_INVALID
            )
            continue
        stratum = _matching_stratum(protocol, market, review, spread, selected_at=at)
        if stratum is None:
            exclude(V2ExclusionReason.NO_DECLARED_STRATUM)
            continue
        eligible.append(
            _Eligible(index, market, stratum, review, review_receipt, attempt_receipt, spread)
        )
    if used_review_indices != set(review_by_index) or used_book_market_ids != set(
        attempts_by_market
    ):
        raise ValueError("unaccounted review or book attempt supplied to selection")
    response_keys = {
        attempt.market_id
        for attempt in book_attempts
        if attempt.status is BookAttemptStatus.RESPONSE
    }
    if set(book_payload_bytes) != response_keys:
        raise ValueError("book response bytes must exactly match recorded response attempts")

    (
        earlier_markets,
        earlier_events,
        earlier_conditions,
        earlier_tokens,
        earlier_groups,
        quota_use,
        predecessor_sha,
    ) = earlier
    eligible.sort(
        key=lambda item: (
            item.stratum.stratum_id,
            (item.market.liquidity or Decimal(0)).copy_negate(),
            int(item.market.market_id),
        )
    )
    capacity = min(
        block.intended_targets,
        (block.end - at)
        // timedelta(
            seconds=protocol.capture_max_seconds_per_target + protocol.finalization_reserve_seconds
        ),
    )
    chosen = 0
    for item in eligible:
        reason: V2ExclusionReason
        market = item.market
        assert market.event_id is not None and item.review.event_group_id is not None
        market_key = _market_identity(market.market_id)
        event_key = _event_identity(market.event_id)
        token_keys = {_token_identity(token) for token in market.outcome_token_map.values()}
        if (
            market_key in earlier_markets
            or event_key in earlier_events
            or market.condition_id in earlier_conditions
            or token_keys & earlier_tokens
        ):
            reason = V2ExclusionReason.IDENTITY_REUSED
        elif item.review.event_group_id in earlier_groups:
            reason = V2ExclusionReason.EVENT_GROUP_REUSED
        elif quota_use[item.stratum.stratum_id] >= item.stratum.maximum_targets:
            reason = V2ExclusionReason.STRATUM_QUOTA
        elif chosen >= capacity:
            reason = V2ExclusionReason.BLOCK_CAPACITY
        else:
            decisions[item.index] = PageDecisionV1(
                entry_index=item.index,
                entry_sha256=_entry_hash(page[item.index]),
                market_id=market.market_id,
                market=market,
                stratum_id=item.stratum.stratum_id,
                event_group_id=item.review.event_group_id,
                review_receipt_id=item.review_receipt.receipt_id,
                book_attempt_receipt_id=item.book_attempt_receipt.receipt_id,
                observed_spread=item.spread,
            )
            chosen += 1
            earlier_markets.add(market_key)
            earlier_events.add(event_key)
            earlier_conditions.add(market.condition_id)
            earlier_tokens.update(token_keys)
            earlier_groups.add(item.review.event_group_id)
            quota_use[item.stratum.stratum_id] += 1
            continue
        decisions[item.index] = PageDecisionV1(
            entry_index=item.index,
            entry_sha256=_entry_hash(page[item.index]),
            market_id=market.market_id,
            exclusion_reason=reason,
        )
    return OfflineBlockSelectionV2(
        experiment_id=protocol.experiment_id,
        block_ordinal=block_ordinal,
        selected_at=at,
        protocol_sha256=sha256_hex(_canonical_bytes(protocol)),
        predecessor_selection_sha256=predecessor_sha,
        source_provenance=source_provenance,
        source_candidate_count=len(page),
        slot_cap=block.intended_targets,
        admitted_count=chosen,
        unfilled_slots=block.intended_targets - chosen,
        status=(
            BlockAdmissionStatusV2.EMPTY
            if not chosen
            else BlockAdmissionStatusV2.FULL
            if chosen == block.intended_targets
            else BlockAdmissionStatusV2.PARTIAL
        ),
        page_decisions=tuple(decisions[index] for index in range(len(page))),
        reviews=reviews,
        review_receipts=review_receipts,
        book_attempts=book_attempts,
        book_attempt_receipts=book_attempt_receipts,
    )


def verify_block_selection_v2(
    protocol: AsynchronousCohortProtocolV2,
    selection: OfflineBlockSelectionV2,
    *,
    source_payload_bytes: bytes,
    book_payload_bytes: dict[str, bytes],
    prior_selections: tuple[OfflineBlockSelectionV2, ...] = (),
    prior_source_bytes: tuple[bytes, ...] = (),
    prior_book_bytes: tuple[dict[str, bytes], ...] = (),
) -> OfflineBlockSelectionV2:
    """Recompute every entry, quota and predecessor from supplied archived bytes."""
    replayed = select_block_candidates_v2(
        protocol,
        block_ordinal=selection.block_ordinal,
        selected_at=selection.selected_at,
        source_payload_bytes=source_payload_bytes,
        source_provenance=selection.source_provenance,
        reviews=selection.reviews,
        review_receipts=selection.review_receipts,
        book_attempts=selection.book_attempts,
        book_attempt_receipts=selection.book_attempt_receipts,
        book_payload_bytes=book_payload_bytes,
        prior_selections=prior_selections,
        prior_source_bytes=prior_source_bytes,
        prior_book_bytes=prior_book_bytes,
    )
    if replayed != selection:
        raise ValueError("V2 block decision disagrees with archived source and review evidence")
    return selection


def verify_block_selection_v2_archives(
    protocol: AsynchronousCohortProtocolV2,
    selection: OfflineBlockSelectionV2,
    *,
    archive_dir: Path,
    selection_receipt: EvidencePersistenceReceiptV1,
    prior_selections: tuple[OfflineBlockSelectionV2, ...] = (),
    prior_selection_receipts: tuple[EvidencePersistenceReceiptV1, ...] = (),
) -> OfflineBlockSelectionV2:
    """Re-read each raw payload and persisted attestation before deterministic replay."""

    if len(prior_selections) != len(prior_selection_receipts):
        raise ValueError("every predecessor selection needs an archive receipt")

    def check_selection_receipt(
        record: OfflineBlockSelectionV2, receipt: EvidencePersistenceReceiptV1
    ) -> None:
        if (
            not _receipt_matches(
                receipt,
                record,
                experiment_id=protocol.experiment_id,
                kind=EvidenceArtifactKind.COHORT_BLOCK_SELECTION,
                artifact_id=f"block-{record.block_ordinal}",
                earliest=record.selected_at,
                latest=protocol.blocks[record.block_ordinal - 1].end,
            )
            or load_persisted_record(archive_dir, receipt, OfflineBlockSelectionV2) != record
        ):
            raise ValueError("archived block selection or receipt disagrees with replay input")

    def source_and_books(record: OfflineBlockSelectionV2) -> tuple[bytes, dict[str, bytes]]:
        raw, stored = read_raw_payload(archive_dir, record.source_provenance.raw_sha256)
        if stored != record.source_provenance:
            raise ValueError("archived Gamma provenance disagrees with block selection")
        books: dict[str, bytes] = {}
        for review, receipt in zip(record.reviews, record.review_receipts, strict=True):
            if load_persisted_record(archive_dir, receipt, HumanSemanticReviewV1) != review:
                raise ValueError("archived semantic review disagrees with block selection")
        for attempt, receipt in zip(
            record.book_attempts, record.book_attempt_receipts, strict=True
        ):
            if load_persisted_record(archive_dir, receipt, BookAttemptV1) != attempt:
                raise ValueError("archived book attempt disagrees with block selection")
            if attempt.source_provenance is not None:
                body, stored_book = read_raw_payload(
                    archive_dir, attempt.source_provenance.raw_sha256
                )
                if stored_book != attempt.source_provenance:
                    raise ValueError("archived CLOB provenance disagrees with book attempt")
                books[attempt.market_id] = body
        return raw, books

    for record, receipt in zip(prior_selections, prior_selection_receipts, strict=True):
        check_selection_receipt(record, receipt)
    check_selection_receipt(selection, selection_receipt)
    prior_pairs = tuple(source_and_books(record) for record in prior_selections)
    source, books = source_and_books(selection)
    return verify_block_selection_v2(
        protocol,
        selection,
        source_payload_bytes=source,
        book_payload_bytes=books,
        prior_selections=prior_selections,
        prior_source_bytes=tuple(pair[0] for pair in prior_pairs),
        prior_book_bytes=tuple(pair[1] for pair in prior_pairs),
    )
