"""Schema versioning for every persistent ARGOS record.

Engineering rule: *every public schema and persistent record is versioned*.
:class:`VersionedModel` makes that structural rather than optional — a subclass
that forgets ``schema_version`` fails at import time, not at read time three
months into a capture.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from argos.errors import SchemaVersionError

SCHEMA_VERSION_KEY = "schema_version"


class VersionedModel(BaseModel):
    """Immutable, strictly-validated base for persisted contracts.

    Subclasses declare ``schema_version`` as a class variable, for example
    ``"run_manifest.v1"``. The version is not a mutable field: it describes the
    class, and it is re-attached on serialization by :meth:`to_record`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", validate_assignment=True)

    schema_version: ClassVar[str]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not getattr(cls, SCHEMA_VERSION_KEY, ""):
            raise SchemaVersionError(
                f"{cls.__qualname__} must declare a non-empty 'schema_version' class variable",
                model=cls.__qualname__,
            )

    def to_record(self) -> dict[str, Any]:
        """Serialize to a storable mapping that carries its own schema version."""
        record = self.model_dump(mode="json")
        record[SCHEMA_VERSION_KEY] = type(self).schema_version
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Any:
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
