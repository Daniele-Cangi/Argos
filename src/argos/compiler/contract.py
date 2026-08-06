"""The market-rule contract skeleton.

Core invariant 3: resolution rules are the ground-truth contract, and the
question title is not sufficient. Core invariant 15: a compiler can surface
ambiguity; it cannot silently redefine resolution.

So this compiler is deliberately unambitious. It copies the source rule material
verbatim, records what is *missing* from it, and refuses to fill the gaps. Turning
prose into `yes_condition` / `no_condition` requires semantic extraction, which is
an M5 concern behind the owner gate — until then those fields stay empty and their
absence is an ambiguity flag rather than a guess.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from pydantic import Field, field_validator

from argos.clock import ensure_utc
from argos.domain.market import MarketDefinitionV1
from argos.domain.provenance import SHA256_LENGTH
from argos.domain.selection import BINARY_OUTCOME_LABELS
from argos.domain.versioning import VersionedModel

COMPILER_VERSION = "market-contract-compiler/1"


class ReviewStatus(StrEnum):
    """How much scrutiny a contract has actually received."""

    UNREVIEWED = "unreviewed"
    MACHINE_CHECKED = "machine_checked"
    HUMAN_REVIEWED = "human_reviewed"
    REJECTED = "rejected"


class AmbiguityFlag(StrEnum):
    """Something the source material does not settle.

    Each flag names a gap in the *evidence*, never a judgement about the market.
    """

    RESOLUTION_SOURCE_MISSING = "resolution_source_missing"
    DESCRIPTION_MISSING = "description_missing"
    END_BOUNDARY_MISSING = "end_boundary_missing"
    END_BEFORE_START = "end_before_start"
    NON_BINARY_OUTCOMES = "non_binary_outcomes"
    NONSTANDARD_OUTCOME_LABELS = "nonstandard_outcome_labels"
    ALREADY_CLOSED = "already_closed"
    CONDITIONS_NOT_EXTRACTED = "conditions_not_extracted"


class CompiledMarketContractV1(VersionedModel):
    """A versioned interpretation of a market's rules, superseding nothing.

    The source material stays immutable in ``MarketDefinitionV1``; this record is
    a separate, superseding representation linked back by
    ``source_market_hash`` (core invariant 7).
    """

    schema_version: ClassVar[str] = "compiled_market_contract.v1"

    contract_id: str = Field(min_length=1)
    market_id: str = Field(min_length=1)
    condition_id: str = Field(min_length=1)
    source_market_hash: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    compiler_version: str = Field(min_length=1)
    compiled_at: datetime

    proposition: str = Field(min_length=1)
    """The market question, verbatim. Never paraphrased."""

    resolution_source: str = ""
    """The declared resolution source, verbatim. Frequently empty on Gamma."""

    source_rule_material: str = ""
    """The market description, verbatim — the closest thing to the rule text."""

    yes_condition: str | None = None
    no_condition: str | None = None
    start_boundary: datetime | None = None
    end_boundary: datetime | None = None

    edge_cases: tuple[str, ...] = ()
    clarifications: tuple[str, ...] = ()
    """Both stay empty until semantic extraction is approved at the owner gate."""

    ambiguity_flags: tuple[AmbiguityFlag, ...] = ()
    ambiguity_score: int = Field(default=0, ge=0)
    """A count of unresolved flags. A score, not a probability (invariant 8)."""

    review_status: ReviewStatus = ReviewStatus.UNREVIEWED
    review_notes: tuple[str, ...] = ()
    supersedes_contract_id: str | None = None

    @field_validator("compiled_at", "start_boundary", "end_boundary")
    @classmethod
    def _anchor_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @field_validator("review_status")
    @classmethod
    def _refuse_automatic_human_review(cls, value: ReviewStatus) -> ReviewStatus:
        """Only a person may record that a person reviewed something."""
        if value is ReviewStatus.HUMAN_REVIEWED:
            raise ValueError(
                "human_reviewed cannot be set by the compiler; it is recorded by the "
                "human review flow after an actual review"
            )
        return value

    @property
    def is_unambiguous(self) -> bool:
        return not self.ambiguity_flags


def compile_market_contract(
    market: MarketDefinitionV1,
    *,
    compiled_at: datetime,
    supersedes_contract_id: str | None = None,
) -> CompiledMarketContractV1:
    """Compile the rule skeleton for ``market``.

    Deterministic: the same market and timestamp always produce the same record,
    and ``contract_id`` is derived from the source hash rather than generated, so
    recompiling the same evidence does not invent a new identity.
    """
    flags = tuple(_detect_ambiguity(market))
    return CompiledMarketContractV1(
        contract_id=_contract_id(market),
        market_id=market.market_id,
        condition_id=market.condition_id,
        source_market_hash=market.raw_payload_sha256,
        compiler_version=COMPILER_VERSION,
        compiled_at=compiled_at,
        proposition=market.question,
        resolution_source=market.resolution_source,
        source_rule_material=market.description,
        yes_condition=None,
        no_condition=None,
        start_boundary=market.start_time,
        end_boundary=market.end_time,
        ambiguity_flags=flags,
        ambiguity_score=len(flags),
        review_status=(ReviewStatus.MACHINE_CHECKED if not flags else ReviewStatus.UNREVIEWED),
        supersedes_contract_id=supersedes_contract_id,
    )


def _detect_ambiguity(market: MarketDefinitionV1) -> list[AmbiguityFlag]:
    """Report gaps in the evidence, in a fixed order so the result is stable."""
    flags: list[AmbiguityFlag] = []
    if not market.resolution_source.strip():
        flags.append(AmbiguityFlag.RESOLUTION_SOURCE_MISSING)
    if not market.description.strip():
        flags.append(AmbiguityFlag.DESCRIPTION_MISSING)
    if market.end_time is None:
        flags.append(AmbiguityFlag.END_BOUNDARY_MISSING)
    if (
        market.start_time is not None
        and market.end_time is not None
        and market.end_time < market.start_time
    ):
        flags.append(AmbiguityFlag.END_BEFORE_START)
    if not market.is_binary:
        flags.append(AmbiguityFlag.NON_BINARY_OUTCOMES)
    elif set(market.outcomes) != BINARY_OUTCOME_LABELS:
        flags.append(AmbiguityFlag.NONSTANDARD_OUTCOME_LABELS)
    if market.closed:
        flags.append(AmbiguityFlag.ALREADY_CLOSED)

    # Always present at this compiler version: the YES/NO conditions are not
    # extracted from prose before the owner gate, and pretending otherwise would
    # be exactly the silent redefinition invariant 15 forbids.
    flags.append(AmbiguityFlag.CONDITIONS_NOT_EXTRACTED)
    return flags


def _contract_id(market: MarketDefinitionV1) -> str:
    digest = hashlib.sha256(
        "|".join(
            (COMPILER_VERSION, market.market_id, market.condition_id, market.raw_payload_sha256)
        ).encode()
    ).hexdigest()
    return f"contract-{digest[:32]}"
