"""Container freezing for persisted records.

Core invariant 7: raw data is immutable. `frozen=True` only blocks attribute
assignment, so these tests pin the behaviour of the freeze itself.
"""

import copy
import pickle
from typing import Any

import pytest

from argos.domain.versioning import FrozenDict, freeze, thaw

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
