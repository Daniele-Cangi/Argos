"""Market definition contracts.

``MarketDefinitionV1`` is ARGOS's normalized view of a market. It is deliberately
*not* an opinion about whether the market is usable: it validates structural
consistency (an outcome list that matches its token map, identifiers that are not
interchangeable) and leaves scope decisions to :mod:`argos.domain.selection`.

Core invariant 3: the question title is not the resolution contract. The original
question, description, and resolution source are carried verbatim and linked to
the raw payload hash they were read from.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import ClassVar, cast

from pydantic import Field, field_serializer, field_validator, model_validator

from argos.clock import ensure_utc
from argos.domain.provenance import SHA256_LENGTH
from argos.domain.versioning import VersionedModel, freeze, thaw
from argos.errors import ContractViolationError, RejectionReason

# A condition id addresses a market; a token id addresses one side of it. The two
# character classes are disjoint, which is what makes confusing them impossible
# rather than merely discouraged — a mix-up would subscribe a capture to the
# wrong stream with no error anywhere.
CONDITION_ID_PATTERN = re.compile(r"0x[0-9a-f]{64}")
TOKEN_ID_PATTERN = re.compile(r"[0-9]{1,120}")


class MarketDefinitionV1(VersionedModel):
    """A normalized market, linked to the raw payload it was derived from."""

    schema_version: ClassVar[str] = "market_definition.v1"

    market_id: str = Field(min_length=1)
    condition_id: str = Field(min_length=1)
    slug: str = Field(min_length=1)
    event_id: str | None = None

    question: str = Field(min_length=1)
    description: str = ""
    resolution_source: str = ""

    start_time: datetime | None = None
    end_time: datetime | None = None

    active: bool
    closed: bool
    archived: bool
    restricted: bool | None = None
    category: str | None = None

    liquidity: Decimal | None = None
    volume: Decimal | None = None
    open_interest: Decimal | None = None

    outcomes: tuple[str, ...]
    outcome_token_map: Mapping[str, str]

    neg_risk: bool | None = None
    tick_size: Decimal | None = None
    source_updated_time: datetime | None = None

    raw_payload_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    normalized_at: datetime
    normalizer_version: str = Field(min_length=1)

    @field_validator("start_time", "end_time", "source_updated_time", "normalized_at")
    @classmethod
    def _anchor_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @field_validator("condition_id")
    @classmethod
    def _validate_condition_id(cls, value: str) -> str:
        lowered = value.lower()
        if not CONDITION_ID_PATTERN.fullmatch(lowered):
            raise ValueError(f"condition_id must be a 0x-prefixed 32-byte hash, got {value!r}")
        return lowered

    @field_validator("outcome_token_map")
    @classmethod
    def _freeze_token_map(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        return cast(Mapping[str, str], freeze(value))

    @field_serializer("outcome_token_map")
    def _serialize_token_map(self, value: Mapping[str, str]) -> dict[str, str]:
        return cast(dict[str, str], thaw(value))

    @model_validator(mode="after")
    def _validate_outcome_token_map(self) -> MarketDefinitionV1:
        """An outcome the system cannot price is worse than no market at all."""
        if not self.outcomes:
            raise ValueError("a market must declare at least one outcome")
        if len(set(self.outcomes)) != len(self.outcomes):
            raise ValueError(f"outcome labels must be unique, got {list(self.outcomes)}")
        if tuple(self.outcome_token_map) != self.outcomes:
            raise ValueError(
                "outcome_token_map must cover exactly the outcomes, in order: "
                f"{list(self.outcomes)} vs {list(self.outcome_token_map)}"
            )

        token_ids = [str(token) for token in self.outcome_token_map.values()]
        for token_id in token_ids:
            # fullmatch, not match: `$` also matches before a trailing newline, so
            # "123\n" would pass as a well-formed id and then fail to key anything.
            if not TOKEN_ID_PATTERN.fullmatch(token_id):
                raise ValueError(f"token id must be a decimal string, got {token_id!r}")
        # Compared by value, not by string: "007" and "7" are the same uint256
        # token, and a string comparison would let both sides of a market map to it.
        if len({int(token_id) for token_id in token_ids}) != len(token_ids):
            raise ValueError(f"two outcomes share one token id: {token_ids}")
        return self

    @property
    def is_binary(self) -> bool:
        """Whether the market has exactly two outcomes."""
        return len(self.outcomes) == 2

    def token_id_for(self, outcome: str) -> str:
        """Return the token id for ``outcome``, refusing an unknown label."""
        try:
            return str(self.outcome_token_map[outcome])
        except KeyError as exc:
            raise ContractViolationError(
                "unknown outcome for this market",
                market_id=self.market_id,
                outcome=outcome,
                known=list(self.outcomes),
            ) from exc


class QuarantinedMarketV1(VersionedModel):
    """A market that could not be normalized, kept with its reason and evidence.

    Core invariant 14: errors are data. A market that fails normalization is
    recorded rather than skipped, so a discovery run can report what it refused
    and why, and the raw payload stays linked by hash.
    """

    schema_version: ClassVar[str] = "quarantined_market.v1"

    market_id: str | None = None
    slug: str | None = None
    reason: RejectionReason
    detail: str = Field(min_length=1)
    raw_payload_sha256: str = Field(min_length=SHA256_LENGTH, max_length=SHA256_LENGTH)
    quarantined_at: datetime
    normalizer_version: str = Field(min_length=1)

    @field_validator("quarantined_at")
    @classmethod
    def _anchor_quarantined_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)
