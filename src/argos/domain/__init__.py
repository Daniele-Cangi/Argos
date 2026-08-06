"""Versioned domain contracts and invariants.

Domain code is pure. It must not import HTTP clients, database implementations,
Typer, environment variables, or wall-clock functions; time enters through the
injected :mod:`argos.clock` protocol. ``tests/test_domain_boundaries.py`` enforces
this mechanically.
"""

from argos.domain.versioning import VersionedModel, ensure_supported_version

__all__ = ["VersionedModel", "ensure_supported_version"]
