"""Test hermeticity.

Every ``Settings`` assertion in this suite describes the *declared defaults*, not
whatever the developer or CI runner happens to export. Without this fixture a
single ``ARGOS_LOG_LEVEL`` in the ambient shell silently rewrites the object
under test (``test_fingerprint_is_stable_and_sensitive`` starts comparing DEBUG
to DEBUG) and any stray ``ARGOS_*`` variable makes ``load_settings`` raise before
the assertion it was meant to exercise. Configuration is part of the experiment
(core invariant 13), so the test environment is pinned rather than inherited.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
import structlog
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings

from argos.config.settings import ENV_PREFIX

# docs/13_TEST_STRATEGY.md: no flaky timing in deterministic tests. Hypothesis' default
# per-example deadline turns a slow CI runner into a spurious failure, and the autouse
# environment fixture above is function-scoped by necessity.
hypothesis_settings.register_profile(
    "argos",
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
hypothesis_settings.load_profile("argos")


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ambient ``ARGOS_*`` variables so tests see declared defaults."""
    for key in list(os.environ):
        if key.upper().startswith(ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _clean_log_context() -> Iterator[None]:
    """Structlog state is process-global; never let it leak across tests.

    ``configure_logging`` binds a logger to a concrete stream. A CLI test binds it
    to the ``CliRunner`` capture buffer, which is closed when that test ends — so
    without this reset the next test that logs anything dies with "I/O operation
    on closed file", in a module that never touched the CLI.
    """
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()
    structlog.reset_defaults()
