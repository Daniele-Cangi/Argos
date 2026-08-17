"""Property tests.

docs/13_TEST_STRATEGY.md asks for Hypothesis coverage of serialization round trips
and boundary validation. The example-based suites pin the cases we thought of; these
search for the ones we did not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import orjson
import pytest
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from argos.clock import ReplayClock, ensure_utc
from argos.config import (
    FingerprintScope,
    RunManifest,
    RunMode,
    Settings,
    WorkingTreeStatus,
    fingerprint_scope,
)
from argos.config.settings import ENV_PREFIX, unknown_environment_keys
from argos.domain.market import MarketDefinitionV1
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.domain.selection import MarketSelectionPolicy, select_markets
from argos.errors import ClockRegressionError, NaiveDatetimeError
from argos.ingestion import normalize_market, normalize_markets

# Timestamps outside this window cannot be produced by a Polymarket capture and only
# exercise datetime's own overflow behaviour at the type boundary.
EARLIEST = datetime(1970, 1, 1)
LATEST = datetime(2100, 1, 1)

SAFE_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")), min_size=1, max_size=40
)

AWARE_MOMENTS = st.datetimes(
    min_value=EARLIEST, max_value=LATEST, timezones=st.timezones(), allow_imaginary=False
)
UTC_MOMENTS = st.datetimes(min_value=EARLIEST, max_value=LATEST, timezones=st.just(UTC))
DURATIONS = st.floats(min_value=0.0, max_value=86_400.0, allow_nan=False, allow_infinity=False)


# --- ensure_utc -------------------------------------------------------------------


@given(moment=AWARE_MOMENTS)
def test_ensure_utc_preserves_the_instant_for_any_timezone(moment: datetime) -> None:
    converted = ensure_utc(moment)
    assert converted == moment
    assert converted.tzinfo is UTC
    assert converted.utcoffset() == timedelta(0)


@given(moment=AWARE_MOMENTS)
def test_ensure_utc_is_idempotent(moment: datetime) -> None:
    once = ensure_utc(moment)
    assert ensure_utc(once) == once
    assert ensure_utc(once).isoformat() == once.isoformat()


@given(moment=st.datetimes(min_value=EARLIEST, max_value=LATEST))
def test_ensure_utc_never_guesses_a_timezone(moment: datetime) -> None:
    with pytest.raises(NaiveDatetimeError) as caught:
        ensure_utc(moment)
    assert caught.value.context["value"] == moment.isoformat()


# --- replay clock -----------------------------------------------------------------


@given(start=UTC_MOMENTS, steps=st.lists(DURATIONS, max_size=25))
def test_replay_time_never_goes_backwards_and_is_reproducible(
    start: datetime, steps: list[float]
) -> None:
    def run() -> list[datetime]:
        clock = ReplayClock(start)
        stamps = [clock.now()]
        for step in steps:
            clock.advance_by(step)
            stamps.append(clock.now())
        return stamps

    stamps = run()
    assert stamps == sorted(stamps)
    assert stamps[0] == start
    assert stamps[-1] >= start
    assert run() == stamps


@given(start=UTC_MOMENTS, target=AWARE_MOMENTS)
def test_advance_to_either_moves_forward_or_refuses_without_side_effects(
    start: datetime, target: datetime
) -> None:
    clock = ReplayClock(start)
    if target >= start:
        clock.advance_to(target)
        assert clock.now() == target
        assert clock.now().tzinfo is UTC
    else:
        with pytest.raises(ClockRegressionError):
            clock.advance_to(target)
        assert clock.now() == start


@given(start=UTC_MOMENTS, steps=st.lists(DURATIONS, min_size=1, max_size=10))
def test_the_same_durations_in_any_order_reach_the_same_instant(
    start: datetime, steps: list[float]
) -> None:
    """Replay time must not depend on the order the scheduler happens to emit gaps in."""

    def total(order: list[float]) -> datetime:
        clock = ReplayClock(start)
        for step in order:
            clock.advance_by(step)
        return clock.now()

    assert total(steps) == total(list(reversed(steps)))


# --- record round trips -----------------------------------------------------------

# A snapshot is JSON on the wire, so integers stay inside the range JSON encoders
# accept; `settings_snapshot: dict[str, Any]` does not enforce that itself.
JSON_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**63), max_value=2**63 - 1),
    SAFE_TEXT,
)


@st.composite
def _manifests(draw: st.DrawFn) -> RunManifest:
    """Build manifests that respect the code-revision/working-tree dependency.

    ``working_tree`` may only claim clean or dirty when a revision was actually
    proven, so the two fields are drawn jointly rather than independently; a
    plain ``st.builds`` would spend most of its examples on the combination the
    model rejects by design.
    """
    code_revision = draw(st.one_of(st.none(), SAFE_TEXT))
    working_tree = draw(
        st.just(WorkingTreeStatus.UNKNOWN)
        if code_revision is None
        else st.sampled_from(WorkingTreeStatus)
    )
    return RunManifest(
        run_id=draw(SAFE_TEXT),
        mode=draw(st.sampled_from(RunMode)),
        created_at=draw(UTC_MOMENTS),
        argos_version=draw(SAFE_TEXT),
        code_revision=code_revision,
        working_tree=working_tree,
        config_fingerprint=draw(SAFE_TEXT),
        settings_snapshot=draw(st.dictionaries(SAFE_TEXT, JSON_SCALARS, max_size=6)),
        schema_versions=tuple(draw(st.lists(SAFE_TEXT, max_size=4))),
    )


MANIFESTS = _manifests()


@given(manifest=MANIFESTS)
def test_a_record_survives_a_serialization_round_trip(manifest: RunManifest) -> None:
    record = manifest.to_record()
    assert RunManifest.from_record(record) == manifest
    assert record["schema_version"] == "run_manifest.v5"


@given(manifest=MANIFESTS)
def test_serialization_is_canonical_and_stable(manifest: RunManifest) -> None:
    encode = lambda value: orjson.dumps(value, option=orjson.OPT_SORT_KEYS)  # noqa: E731
    once = encode(manifest.to_record())
    twice = encode(RunManifest.from_record(orjson.loads(once)).to_record())
    assert once == twice


@given(manifest=MANIFESTS, foreign=SAFE_TEXT)
def test_a_foreign_schema_version_is_never_accepted(manifest: RunManifest, foreign: str) -> None:
    from argos.errors import SchemaVersionError

    record = manifest.to_record()
    record["schema_version"] = foreign
    if foreign == RunManifest.schema_version:
        assert RunManifest.from_record(record) == manifest
    else:
        with pytest.raises(SchemaVersionError):
            RunManifest.from_record(record)


# --- configuration ----------------------------------------------------------------


@given(
    log_level=st.sampled_from(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
    timeout=st.floats(min_value=0.001, max_value=120.0, allow_nan=False, allow_infinity=False),
    # one below the ceiling: the property varies attempts + 1, which must stay legal
    attempts=st.integers(min_value=1, max_value=9),
)
def test_the_fingerprint_identifies_the_experiment_exactly(
    log_level: str, timeout: float, attempts: int
) -> None:
    """Every EXPERIMENT-scoped setting moves the fingerprint; no ENVIRONMENT one does.

    Both halves are asserted over the whole field set rather than over a
    hand-picked pair, so a field added later is covered by whichever half its
    declared `FingerprintScope` puts it in — and a field that declares no scope
    fails in `fingerprint_scope` before it reaches either.

    `data_dir` moved from the first half to the second on 2026-08-17 (M3 blocker
    R1): it names where output goes, which `docs/02_ARCHITECTURE.md` allows to
    differ between live and replay, so a replay into a second directory must not
    look like a second experiment.
    """

    def build(**overrides: Any) -> Settings:
        base: dict[str, Any] = {
            "log_level": log_level,
            "http_timeout_seconds": timeout,
            "http_max_attempts": attempts,
        }
        base.update(overrides)
        return Settings(**base)

    # A legal, different value for every field, so the two halves below are
    # driven by the declared scope rather than by which fields were listed.
    alternatives: dict[str, Any] = {
        "gamma_base_url": "https://gamma-api.polymarket.com/alt",
        "data_base_url": "https://data-api.polymarket.com/alt",
        "clob_base_url": "https://clob.polymarket.com/alt",
        "clob_market_ws_url": "wss://ws-subscriptions-clob.polymarket.com/ws/market/alt",
        "data_dir": "/somewhere/else",
        "log_level": "DEBUG" if log_level != "DEBUG" else "ERROR",
        "http_timeout_seconds": timeout / 2,
        "http_max_attempts": attempts + 1,
        "source_jitter_seed": 7,
        # execution_enabled has exactly one legal value (ADR-0007), so there is
        # no alternative to vary; it is excluded here and its scope is still
        # asserted by tests/test_boundaries.py.
    }
    assert set(alternatives) | {"execution_enabled"} == set(Settings.model_fields), (
        "a setting was added without giving this property a legal alternative value"
    )

    reference = build()
    assert reference.fingerprint() == build().fingerprint()
    assert len(reference.fingerprint()) == 64

    for name, alternative in alternatives.items():
        varied = build(**{name: alternative}).fingerprint()
        if fingerprint_scope(name) is FingerprintScope.EXPERIMENT:
            assert varied != reference.fingerprint(), f"{name} must move the fingerprint"
        else:
            assert varied == reference.fingerprint(), f"{name} must not move the fingerprint"


@given(
    attempts=st.one_of(st.integers(max_value=0), st.floats(allow_nan=False), SAFE_TEXT),
)
def test_a_retry_count_below_one_is_never_accepted(attempts: object) -> None:
    from pydantic import ValidationError

    try:
        loaded = Settings(http_max_attempts=attempts)  # type: ignore[arg-type]
    except (ValidationError, ValueError):
        return
    assert loaded.http_max_attempts >= 1


@given(name=SAFE_TEXT.filter(lambda text: "=" not in text and "\x00" not in text))
def test_any_prefixed_variable_is_either_a_known_field_or_reported(name: str) -> None:
    variable = f"{ENV_PREFIX}{name}"
    reported = unknown_environment_keys({variable: "value"})
    is_field = name.upper() in {field.upper() for field in Settings.model_fields}
    assert reported == ([] if is_field else [variable])


@given(field=st.sampled_from(sorted(Settings.model_fields)), upper=st.booleans())
def test_a_known_field_is_recognised_in_any_case(field: str, upper: bool) -> None:
    name = f"{ENV_PREFIX}{field}"
    assert unknown_environment_keys({name.upper() if upper else name.lower(): "value"}) == []


# --- provenance hashing -------------------------------------------------------------


PAYLOADS = st.binary(max_size=512)


@given(payload=PAYLOADS)
def test_a_payload_always_matches_the_provenance_taken_from_it(payload: bytes) -> None:
    provenance = _provenance_for(payload)
    assert provenance.matches(payload)
    assert provenance.raw_sha256 == sha256_hex(payload)
    assert len(provenance.raw_sha256) == 64


@given(payload=PAYLOADS, other=PAYLOADS)
def test_provenance_never_matches_different_bytes(payload: bytes, other: bytes) -> None:
    """A hash link that accepted the wrong bytes would make invariant 7 decorative."""
    assert _provenance_for(payload).matches(other) is (payload == other)


@given(payload=PAYLOADS)
def test_a_provenance_record_round_trips_and_keeps_its_link(payload: bytes) -> None:
    provenance = _provenance_for(payload)
    restored = SourceProvenanceV1.from_record(provenance.to_record())
    assert restored == provenance
    assert restored.matches(payload)


HEX_DIGESTS = st.text(alphabet="0123456789abcdefABCDEF", min_size=64, max_size=64)


@given(payload=PAYLOADS, digest=HEX_DIGESTS)
def test_a_digest_is_stored_lowercased_so_two_records_never_disagree(
    payload: bytes, digest: str
) -> None:
    provenance = _provenance_for(payload, raw_sha256=digest)
    assert provenance.raw_sha256 == digest.lower()


def _provenance_for(payload: bytes, **overrides: Any) -> SourceProvenanceV1:
    fields: dict[str, Any] = {
        "source": "gamma",
        "endpoint": "https://gamma-api.polymarket.com/markets",
        "http_status": 200,
        "retrieved_at": ANCHOR,
        "raw_sha256": sha256_hex(payload),
        "byte_length": len(payload),
    }
    fields.update(overrides)
    return SourceProvenanceV1(**fields)


# --- normalization round trips ------------------------------------------------------

ANCHOR = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
DIGEST = "d" * 64

LABEL_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=12
).filter(lambda text: bool(text.strip()))
CONDITION_IDS = st.binary(min_size=32, max_size=32).map(lambda value: "0x" + value.hex())
TOKEN_IDS = st.integers(min_value=1, max_value=10**60).map(str)
MONEY = st.one_of(
    st.none(),
    st.decimals(min_value=0, max_value=10**12, allow_nan=False, allow_infinity=False, places=6).map(
        str
    ),
)


@st.composite
def gamma_markets(draw: st.DrawFn) -> dict[str, Any]:
    """A payload shaped like Gamma's, with the parts the normalizer must handle."""
    size = draw(st.integers(min_value=1, max_value=4))
    labels = draw(st.lists(LABEL_TEXT, min_size=size, max_size=size, unique=True))
    tokens = draw(st.lists(TOKEN_IDS, min_size=size, max_size=size, unique=True))
    end = draw(st.one_of(st.none(), UTC_MOMENTS))
    return {
        "id": str(draw(st.integers(min_value=1, max_value=10**9))),
        "conditionId": draw(CONDITION_IDS),
        # Required text fields must be non-blank: the normalizer rejects a
        # whitespace-only slug by design, so generating one tests the guard, not
        # the property under test — and does it intermittently, which is worse.
        "slug": draw(SAFE_TEXT.filter(lambda text: text.strip() != "")),
        "question": draw(LABEL_TEXT.filter(lambda text: text.strip() != "")),
        "description": draw(
            st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=60)
        ),
        "resolutionSource": draw(st.text(max_size=20)),
        "outcomes": orjson.dumps(labels).decode(),
        "clobTokenIds": orjson.dumps(tokens).decode(),
        "active": draw(st.booleans()),
        "closed": draw(st.booleans()),
        "archived": draw(st.booleans()),
        "liquidity": draw(MONEY),
        "volume": draw(MONEY),
        "endDate": None if end is None else end.isoformat().replace("+00:00", "Z"),
    }


@given(payload=gamma_markets())
def test_normalizing_the_same_payload_twice_gives_the_same_record(payload: dict[str, Any]) -> None:
    """M1 exit criterion: repeated normalization of the same raw payload is deterministic."""
    once = normalize_market(payload, raw_payload_sha256=DIGEST, normalized_at=ANCHOR)
    twice = normalize_market(payload, raw_payload_sha256=DIGEST, normalized_at=ANCHOR)
    assert once == twice
    assert once.to_record() == twice.to_record()


@given(payload=gamma_markets())
def test_a_normalized_market_survives_a_storage_round_trip(payload: dict[str, Any]) -> None:
    market = normalize_market(payload, raw_payload_sha256=DIGEST, normalized_at=ANCHOR)
    record = market.to_record()
    assert MarketDefinitionV1.from_record(record) == market
    encode = lambda value: orjson.dumps(value, option=orjson.OPT_SORT_KEYS)  # noqa: E731
    assert encode(MarketDefinitionV1.from_record(record).to_record()) == encode(record)


@given(payload=gamma_markets())
def test_the_token_map_always_covers_exactly_the_outcomes(payload: dict[str, Any]) -> None:
    """A market that could price an outcome it does not declare is a mapping defect."""
    market = normalize_market(payload, raw_payload_sha256=DIGEST, normalized_at=ANCHOR)
    assert tuple(market.outcome_token_map) == market.outcomes
    assert len(set(market.outcome_token_map.values())) == len(market.outcomes)
    for outcome in market.outcomes:
        assert market.token_id_for(outcome) == market.outcome_token_map[outcome]


@given(payload=gamma_markets())
def test_the_source_rule_material_is_never_altered(payload: dict[str, Any]) -> None:
    """Core invariant 3: rule material is evidence, carried verbatim or not at all."""
    market = normalize_market(payload, raw_payload_sha256=DIGEST, normalized_at=ANCHOR)
    assert market.question == payload["question"]
    assert market.description == (payload["description"] or "")
    assert market.resolution_source == (payload["resolutionSource"] or "")
    assert market.raw_payload_sha256 == DIGEST


@hypothesis_settings(max_examples=30)
@given(payloads=st.lists(gamma_markets(), max_size=6))
def test_a_page_is_fully_accounted_for(payloads: list[dict[str, Any]]) -> None:
    """Core invariant 14: nothing is dropped, so the two buckets must sum to the input."""
    report = normalize_markets(payloads, raw_payload_sha256=DIGEST, normalized_at=ANCHOR)
    assert report.total == len(payloads)
    assert sum(report.counts_by_reason().values()) == len(report.quarantined)
    assert all(record.detail for record in report.quarantined)


# --- selection accounting -----------------------------------------------------------


POLICIES = st.builds(
    MarketSelectionPolicy,
    require_binary=st.booleans(),
    require_standard_outcome_labels=st.booleans(),
    require_active=st.booleans(),
    exclude_closed=st.booleans(),
    exclude_archived=st.booleans(),
    require_end_time=st.booleans(),
    min_liquidity=st.one_of(st.none(), st.integers(min_value=0, max_value=10**6).map(Decimal)),
    min_hours_to_end=st.one_of(st.none(), st.integers(min_value=0, max_value=10_000)),
)


@hypothesis_settings(max_examples=30)
@given(payloads=st.lists(gamma_markets(), max_size=6), policy=POLICIES)
def test_selection_accounts_for_every_market_it_was_offered(
    payloads: list[dict[str, Any]], policy: MarketSelectionPolicy
) -> None:
    markets = _normalized(payloads)
    result = select_markets(markets, policy, as_of=ANCHOR)
    assert result.total == len(markets)
    assert sum(result.counts_by_reason().values()) == len(result.excluded)
    assert all(exclusion.detail for exclusion in result.excluded)


@hypothesis_settings(max_examples=30)
@given(payloads=st.lists(gamma_markets(), max_size=6), policy=POLICIES)
def test_selection_preserves_order_and_never_invents_a_market(
    payloads: list[dict[str, Any]], policy: MarketSelectionPolicy
) -> None:
    markets = _normalized(payloads)
    result = select_markets(markets, policy, as_of=ANCHOR)
    offered = [market.market_id for market in markets]
    assert sorted(
        [market.market_id for market in result.selected]
        + [exclusion.market_id for exclusion in result.excluded]
    ) == sorted(offered)
    assert _is_subsequence([m.market_id for m in result.selected], offered)
    assert _is_subsequence([e.market_id for e in result.excluded], offered)


def _is_subsequence(part: list[str], whole: list[str]) -> bool:
    remaining = iter(whole)
    return all(item in remaining for item in part)


@hypothesis_settings(max_examples=30)
@given(payloads=st.lists(gamma_markets(), max_size=6), policy=POLICIES)
def test_selection_is_reproducible(
    payloads: list[dict[str, Any]], policy: MarketSelectionPolicy
) -> None:
    """No hidden global state: the same inputs must always give the same result."""
    markets = _normalized(payloads)
    assert select_markets(markets, policy, as_of=ANCHOR) == select_markets(
        markets, policy, as_of=ANCHOR
    )


@hypothesis_settings(max_examples=30)
@given(payloads=st.lists(gamma_markets(), min_size=1, max_size=6))
def test_an_empty_policy_selects_everything_it_is_given(payloads: list[dict[str, Any]]) -> None:
    """Every exclusion must come from a configured clause, never from a default opinion."""
    permissive = MarketSelectionPolicy(
        require_binary=False,
        require_standard_outcome_labels=False,
        require_active=False,
        exclude_closed=False,
        exclude_archived=False,
        require_end_time=False,
    )
    markets = _normalized(payloads)
    result = select_markets(markets, permissive)
    assert len(result.selected) == len(markets)
    assert result.excluded == ()


def _normalized(payloads: list[dict[str, Any]]) -> list[MarketDefinitionV1]:
    report = normalize_markets(payloads, raw_payload_sha256=DIGEST, normalized_at=ANCHOR)
    return list(report.accepted)
