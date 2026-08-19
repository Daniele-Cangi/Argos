"""Schema versioning for every persistent ARGOS record.

Engineering rule: *every public schema and persistent record is versioned*.
:class:`VersionedModel` makes that structural rather than optional — a subclass
that does not declare its *own* ``schema_version`` fails at import time, not at
read time three months into a capture.

Since 2026-08-17 a subclass also **registers** its version, which does two
things one mechanism (M3 blocker R2, ``docs/BACKLOG.md``):

- it makes ``schema_version`` genuinely unique. The M2 security review
  demonstrated that two classes both declaring ``order_book_snapshot.v1``
  defeat :func:`argos.domain.observation.read_payload`'s version check — it
  built a foreign model out of a book envelope with no error — and filed "a
  registry check in ``__init_subclass__`` closes it". A collision now fails at
  import time;
- it lets a reader turn a stored ``payload_schema_version`` back into the model
  that describes it (:func:`resolve_schema`). Without that, M3's replay
  dispatcher would have to grow an ``if/elif`` chain over version strings —
  inside the one module whose entire purpose is that live and replay run the
  *same* handlers.

The registry is module-global, which deserves a word given that CLAUDE.md
prohibits hidden global state. It is populated at class-definition time and
never written again, and :func:`resolve_schema` returns the same class no
matter what order anything was processed in; the prohibition is about state
that changes *output* with processing order, and this cannot. The one
order-dependent thing about it is which of two colliding classes is named
"first" in the error — and that error is a hard import failure, not an output.

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

_SCHEMA_REGISTRY: dict[str, type[VersionedModel]] = {}


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
        version = cls.__dict__.get(SCHEMA_VERSION_KEY)
        if not version:
            raise SchemaVersionError(
                f"{cls.__qualname__} must declare its own non-empty 'schema_version' "
                "class variable; inheriting one from a parent would mislabel its records",
                model=cls.__qualname__,
                inherited=getattr(cls, SCHEMA_VERSION_KEY, None),
            )
        _register_schema(str(version), cls)

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        """Copy through validation, unlike pydantic's default.

        pydantic documents ``model_copy`` as *not* re-validating, which quietly
        defeats every guarantee this class exists to provide: a caller patching
        one field with the ordinary public API gets a record whose other fields
        were never checked and whose container fields were never frozen. An
        adversarial test reproduced it — ``model_copy(update={"payload": d})``
        left the envelope holding ``d`` by reference, and mutating ``d``
        afterwards reached ``to_record()``, breaking core invariant 7 through a
        call that looks entirely unremarkable in review.

        Re-validating makes the copy as trustworthy as the original. The cost is
        that a copy is no longer free; that is the correct trade for a record
        type whose whole purpose is to be believed later.
        """
        merged: dict[str, Any] = {**dict(self), **(dict(update) if update else {})}
        if deep:
            merged = copy.deepcopy(merged)
        return type(self).model_validate(merged)

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


def _register_schema(version: str, model: type[VersionedModel]) -> None:
    """Claim ``version`` for ``model``, refusing a second claimant.

    The escape hatch is narrow and deliberate: a class whose ``__module__`` and
    ``__qualname__`` both match the incumbent is the *same* class definition
    running again, which is what a module reload does. Two genuinely different
    models cannot collide that way, because they live in different modules or
    carry different names — and letting a reload fail would make the registry a
    trap for a tool this repository does not control.
    """
    incumbent = _SCHEMA_REGISTRY.get(version)
    if incumbent is not None and (
        incumbent.__module__ != model.__module__ or incumbent.__qualname__ != model.__qualname__
    ):
        raise SchemaVersionError(
            "schema_version is already claimed by another model; two classes "
            "sharing one version defeat every version check that reads a stored "
            "record back, including read_payload",
            version=version,
            claimed_by=f"{incumbent.__module__}.{incumbent.__qualname__}",
            attempted_by=f"{model.__module__}.{model.__qualname__}",
        )
    _SCHEMA_REGISTRY[version] = model


def resolve_schema(version: str) -> type[VersionedModel]:
    """Return the model that declares ``version``.

    Raises :class:`SchemaVersionError` naming every version currently known, so
    the far more likely failure — the declaring module was never imported — is
    legible rather than looking like an unknown schema. A reader that resolves a
    version it did not expect should still refuse it deliberately: resolution
    answers "which model describes this", not "should this record be handled
    here".
    """
    model = _SCHEMA_REGISTRY.get(version)
    if model is None:
        raise SchemaVersionError(
            "no registered model declares this schema version; the declaring "
            "module may simply not be imported in this process",
            found=version,
            supported=sorted(_SCHEMA_REGISTRY),
        )
    return model


def registered_schemas() -> Mapping[str, type[VersionedModel]]:
    """Every schema version currently claimed, for tests and diagnostics."""
    return dict(_SCHEMA_REGISTRY)


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
