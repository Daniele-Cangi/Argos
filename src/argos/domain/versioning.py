"""Schema versioning for every persistent ARGOS record.

Engineering rule: *every public schema and persistent record is versioned*.
:class:`VersionedModel` makes that structural rather than optional — a subclass
that does not declare its *own* ``schema_version`` fails at import time, not at
read time three months into a capture.

Container immutability is *not* structural yet: this module supplies :func:`freeze`
and :func:`thaw`, but a subclass must opt in per field with a validator/serializer
pair, as :class:`argos.config.manifest.RunManifest` does. A subclass declaring a
bare ``dict`` field is still mutable through the attribute — see the M2 item in
``docs/BACKLOG.md``.
"""

from __future__ import annotations

import copy
from collections.abc import Collection, Iterator, Mapping
from typing import Any, ClassVar, Self, final

from pydantic import BaseModel, ConfigDict

from argos.errors import SchemaVersionError

SCHEMA_VERSION_KEY = "schema_version"


@final
class FrozenDict(Mapping[str, Any]):
    """A read-only mapping that still behaves like an ordinary Python value.

    ``MappingProxyType`` would be the obvious choice, but it cannot be deep-copied
    or pickled, which would quietly forbid a frozen record from being a pydantic
    default, a nested pre-built value, or a multiprocessing argument.
    """

    __slots__ = ("_data",)

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data: dict[str, Any] = dict(data)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"FrozenDict({self._data!r})"

    def __hash__(self) -> int:
        return hash(frozenset(self._data.items()))

    def __copy__(self) -> FrozenDict:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> FrozenDict:
        return FrozenDict(copy.deepcopy(self._data, memo))

    def __reduce__(self) -> tuple[Any, ...]:
        return (FrozenDict, (self._data,))


def freeze(value: Any) -> Any:
    """Return ``value`` with its mappings and sequences made read-only.

    Scalars and objects of other types are passed through unchanged; this is a
    container freeze, not a general immutability guarantee.

    ``frozen=True`` on a pydantic model only blocks attribute assignment: a
    ``dict`` field stays mutable through the attribute, and the mutation reaches
    :meth:`VersionedModel.to_record`. Core invariant 7 requires that a stored
    payload cannot be edited after the fact, so container fields are frozen on
    validation instead of merely documented as immutable.
    """
    if isinstance(value, Mapping):
        return FrozenDict({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    """Return a plain, JSON-serializable copy of a :func:`freeze`-d value."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


class VersionedModel(BaseModel):
    """Strictly-validated base for persisted contracts.

    Subclasses declare ``schema_version`` as a class variable, for example
    ``"run_manifest.v1"``. The version is not a mutable field: it describes the
    class, and it is re-attached on serialization by :meth:`to_record`.

    ``frozen=True`` blocks attribute assignment. Freezing the *contents* of a
    container field is opt-in per field via :func:`freeze` and :func:`thaw`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", validate_assignment=True)

    schema_version: ClassVar[str]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # cls.__dict__, not getattr: an inherited version would let a subclass with
        # different fields write records labelled with its parent's schema.
        if not cls.__dict__.get(SCHEMA_VERSION_KEY):
            raise SchemaVersionError(
                f"{cls.__qualname__} must declare its own non-empty 'schema_version' "
                "class variable; inheriting one from a parent would mislabel its records",
                model=cls.__qualname__,
                inherited=getattr(cls, SCHEMA_VERSION_KEY, None),
            )

    def to_record(self) -> dict[str, Any]:
        """Serialize to a storable mapping that carries its own schema version."""
        record = self.model_dump(mode="json")
        record[SCHEMA_VERSION_KEY] = type(self).schema_version
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Self:
        """Rebuild from :meth:`to_record` output, rejecting a foreign version."""
        payload = dict(record)
        ensure_supported_version(payload.pop(SCHEMA_VERSION_KEY, None), (cls.schema_version,))
        return cls.model_validate(payload)


def ensure_supported_version(found: object, supported: Collection[str]) -> str:
    """Validate a stored schema version against the versions a reader accepts."""
    if not isinstance(found, str) or not found:
        raise SchemaVersionError(
            "record is missing its schema version", found=found, supported=sorted(supported)
        )
    if found not in supported:
        raise SchemaVersionError(
            "record schema version is not supported by this reader",
            found=found,
            supported=sorted(supported),
        )
    return found
