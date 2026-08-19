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
import socket
from collections.abc import Iterator
from typing import Any

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


@pytest.fixture(autouse=True, scope="session")
def _no_outbound_network() -> Iterator[None]:
    """Make "unit tests do not require internet" structural rather than a convention.

    The M1 exit criterion is currently satisfied because every adapter test happens to
    mount respx; nothing stops the next one from reaching the real Gamma API and turning
    a contract test into an availability test. Only outbound IP connects are refused —
    ``AF_UNIX`` socket pairs are how asyncio builds its own self-pipe.
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection
    blocked = (socket.AF_INET, socket.AF_INET6)

    def guard(name: str, original: Any) -> Any:
        def wrapper(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
            # Windows implements ``socket.socketpair`` with a temporary TCP
            # connection to loopback; asyncio uses that pair for its internal
            # wake-up pipe. Blocking it prevents an event loop from existing,
            # not an adapter from reaching the internet. External IP connects
            # remain impossible.
            host = address[0] if isinstance(address, tuple) and address else None
            is_loopback = host in {"127.0.0.1", "::1"}
            if self.family in blocked and not is_loopback:
                raise RuntimeError(
                    f"tests must not open a network connection ({name} to {address!r}); "
                    "record a fixture and mount respx instead"
                )
            return original(self, address, *args, **kwargs)

        return wrapper

    def refuse_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(
            f"tests must not open a network connection (create_connection to {address!r}); "
            "record a fixture and mount respx instead"
        )

    socket.socket.connect = guard("connect", real_connect)  # type: ignore[method-assign]
    socket.socket.connect_ex = guard("connect_ex", real_connect_ex)  # type: ignore[method-assign]
    socket.create_connection = refuse_create_connection  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]
        socket.create_connection = real_create_connection  # type: ignore[assignment]


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
