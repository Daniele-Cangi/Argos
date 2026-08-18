"""Container freezing for persisted records.

Core invariant 7: raw data is immutable. `frozen=True` only blocks attribute
assignment, so these tests pin the behaviour of the freeze itself.
"""

import copy
import importlib
import pickle
from typing import Any, ClassVar

import pytest

from argos.domain.versioning import (
    FrozenDict,
    VersionedModel,
    freeze,
    registered_schemas,
    resolve_schema,
    thaw,
)
from argos.errors import SchemaVersionError

NESTED: dict[str, Any] = {
    "level": "INFO",
    "endpoints": ["https://a", "https://b"],
    "limits": {"attempts": 5, "nested": {"deep": [1, {"deeper": True}]}},
}


def test_a_frozen_mapping_refuses_every_write() -> None:
    frozen = freeze(NESTED)
    with pytest.raises(TypeError):
        frozen["level"] = "DEBUG"
    with pytest.raises(TypeError):
        del frozen["level"]
    with pytest.raises(AttributeError):
        frozen.update({"level": "DEBUG"})


def test_freezing_reaches_every_depth() -> None:
    frozen = freeze(NESTED)
    assert isinstance(frozen["limits"]["nested"], FrozenDict)
    with pytest.raises(TypeError):
        frozen["limits"]["nested"]["deep"][1]["deeper"] = False
    with pytest.raises(AttributeError):
        frozen["endpoints"].append("https://c")


def test_freezing_copies_rather_than_wrapping_the_caller_s_dict() -> None:
    """A live handle into the original would defeat the freeze entirely."""
    source = {"level": "INFO"}
    frozen = freeze(source)
    source["level"] = "DEBUG"
    assert frozen["level"] == "INFO"


def test_a_frozen_mapping_survives_deep_copy_and_pickling() -> None:
    """MappingProxyType supports neither, which would forbid a frozen record from
    being a pydantic default, a nested value, or a multiprocessing argument."""
    frozen = freeze(NESTED)
    assert copy.deepcopy(frozen) == frozen
    assert pickle.loads(pickle.dumps(frozen)) == frozen
    assert copy.copy(frozen) == frozen


def test_a_frozen_mapping_is_hashable_as_frozen_really_ought_to_imply() -> None:
    assert hash(freeze(NESTED)) == hash(freeze(NESTED))
    assert hash(freeze({"a": 1})) != hash(freeze({"a": 2}))


def test_a_frozen_mapping_compares_equal_to_the_plain_dict() -> None:
    assert freeze({"a": 1}) == {"a": 1}
    assert dict(freeze({"a": 1})) == {"a": 1}


def test_thaw_restores_plain_json_containers() -> None:
    restored = thaw(freeze(NESTED))
    assert restored == NESTED
    assert isinstance(restored, dict)
    assert isinstance(restored["endpoints"], list)
    assert isinstance(restored["limits"]["nested"]["deep"][1], dict)


def test_freeze_and_thaw_round_trip_is_stable() -> None:
    once = freeze(NESTED)
    assert freeze(thaw(once)) == once


@pytest.mark.parametrize("scalar", [1, "text", 3.5, True, None, b"bytes"])
def test_scalars_pass_through_untouched(scalar: object) -> None:
    assert freeze(scalar) is scalar


# --- the schema registry (M3 blocker R2) ------------------------------------------


def test_every_shipped_contract_is_resolvable_from_its_version() -> None:
    """The property M3 replay dispatch rests on.

    Asserted as an exact set rather than a subset: a payload model whose module
    is never imported is silently missing from the registry, and "the reader
    could not resolve it" would then look identical to "the record named a
    schema that does not exist".
    """
    # Imported for the side effect that matters here: defining the classes is
    # what registers them. `importlib` rather than a bare `import ... # noqa`
    # so the intent is stated in code rather than in a lint suppression.
    for package in (
        "argos.baselines",
        "argos.compiler",
        "argos.config",
        "argos.domain",
        "argos.evaluation",
        "argos.replay",
        "argos.resolution",
    ):
        importlib.import_module(package)

    shipped = {
        version: model.__qualname__
        for version, model in registered_schemas().items()
        if model.__module__.startswith("argos.")
    }
    assert shipped == {
        "compiled_market_contract.v1": "CompiledMarketContractV1",
        "evaluation_report.v1": "EvaluationReportV1",
        "forecast_evaluation.v1": "ForecastEvaluationV1",
        "market_baseline_forecast.v1": "MarketBaselineForecastV1",
        "market_audit.v1": "MarketAuditV1",
        "market_definition.v1": "MarketDefinitionV1",
        "market_quote.v1": "MarketQuoteV1",
        "observation_envelope.v1": "ObservationEnvelopeV1",
        "order_book_snapshot.v1": "OrderBookSnapshotV1",
        "price_change.v1": "PriceChangeV1",
        "quarantined_market.v1": "QuarantinedMarketV1",
        "rejected_observation.v1": "RejectedObservationV1",
        "replay_manifest.v1": "ReplayManifestV1",
        "resolution.v1": "ResolutionV1",
        "run_manifest.v5": "RunManifest",
        "source_provenance.v1": "SourceProvenanceV1",
        "ws_book_snapshot.v1": "WsBookSnapshotV1",
    }


def test_two_models_cannot_claim_one_schema_version() -> None:
    """The M2 security review's finding, closed structurally.

    Review built a foreign model out of a book envelope with no error, because
    two classes both declaring `order_book_snapshot.v1` defeat `read_payload`'s
    version check -- the check compares strings, and both strings matched. The
    collision was not hypothetical: `tests/test_observation_envelope.py` had a
    stub declaring exactly that version, inside the test module for
    `read_payload` itself.
    """
    with pytest.raises(SchemaVersionError) as caught:

        class _Impostor(VersionedModel):
            schema_version: ClassVar[str] = "order_book_snapshot.v1"

    assert caught.value.context["claimed_by"].endswith("OrderBookSnapshotV1")


def test_resolving_an_unknown_version_names_what_is_known() -> None:
    """A version that resolves to nothing is far more often an unimported module
    than an unknown schema, and the two need different fixes."""
    with pytest.raises(SchemaVersionError) as caught:
        resolve_schema("not_a_real_schema.v9")
    supported = caught.value.context["supported"]
    assert isinstance(supported, list)
    assert "order_book_snapshot.v1" in supported


def test_the_registry_returns_the_declaring_class_itself() -> None:
    from argos.domain.pricechange import PriceChangeV1

    assert resolve_schema("price_change.v1") is PriceChangeV1


def test_registered_schemas_hands_back_a_copy() -> None:
    """A caller mutating the registry through a diagnostic accessor would be
    exactly the hidden global state the module docstring argues this is not."""
    snapshot = registered_schemas()
    snapshot["injected.v1"] = FrozenDict  # type: ignore[assignment]
    with pytest.raises(SchemaVersionError):
        resolve_schema("injected.v1")
