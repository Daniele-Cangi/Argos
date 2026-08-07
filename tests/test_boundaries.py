"""Mechanical enforcement of the M0 architecture and no-execution boundaries."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "argos"
DOMAIN = SRC / "domain"

ARCHITECTURE_PACKAGES = (
    "domain",
    "clock",
    "config",
    "sources",
    "ingestion",
    "store",
    "projections",
    "compiler",
    "replay",
    "baselines",
    "resolution",
    "evaluation",
    "cli",
)

# docs/02_ARCHITECTURE.md: domain code must not import HTTP clients, database
# implementations, Typer, environment variables, or wall-clock functions.
FORBIDDEN_DOMAIN_IMPORTS = frozenset(
    {
        "aiohttp",
        "dotenv",
        "duckdb",
        "httpx",
        "psycopg",
        "requests",
        "rich",
        "sqlalchemy",
        "sqlite3",
        "time",
        "typer",
        "urllib",
        "websockets",
    }
)

FORBIDDEN_DOMAIN_CALLS = frozenset({"now", "utcnow", "today", "monotonic", "perf_counter"})

# .claude/rules/no-execution.md: no trading surface may exist anywhere in src.
FORBIDDEN_EXECUTION_TOKENS = (
    "private_key",
    "mnemonic",
    "seed_phrase",
    "place_order",
    "cancel_order",
    "sign_order",
    "submit_order",
    "wallet",
    "allowance",
    "api_secret",
    "api_passphrase",
)


def _modules(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("package", ARCHITECTURE_PACKAGES)
def test_architecture_package_exists(package: str) -> None:
    module = importlib.import_module(f"argos.{package}")
    assert module.__doc__, f"argos.{package} must document its responsibility"


@pytest.mark.parametrize("path", _modules(DOMAIN), ids=lambda p: p.name)
def test_domain_imports_no_infrastructure(path: Path) -> None:
    offending = _imported_roots(_parse(path)) & FORBIDDEN_DOMAIN_IMPORTS
    assert not offending, f"{path.name} imports infrastructure: {sorted(offending)}"


@pytest.mark.parametrize("path", _modules(DOMAIN), ids=lambda p: p.name)
def test_domain_does_not_read_the_wall_clock(path: Path) -> None:
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in FORBIDDEN_DOMAIN_CALLS, (
                f"{path.name} calls {node.func.attr}(); domain code takes an injected Clock"
            )


@pytest.mark.parametrize("path", _modules(SRC), ids=lambda p: p.name)
def test_no_execution_surface_in_source(path: Path) -> None:
    """Scan declared names, not prose, so documentation may discuss the boundary."""
    tree = _parse(path)
    declared: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            declared.add(node.name)
        elif isinstance(node, ast.Name):
            declared.add(node.id)
        elif isinstance(node, ast.Attribute):
            declared.add(node.attr)
        elif isinstance(node, ast.arg):
            declared.add(node.arg)

    lowered = {name.lower() for name in declared}
    offending = {name for name in lowered for token in FORBIDDEN_EXECUTION_TOKENS if token in name}
    assert not offending, f"{path.name} declares execution-related names: {sorted(offending)}"


# --- layering ---------------------------------------------------------------------

# docs/02_ARCHITECTURE.md: dependency direction points inwards. Domain contracts sit
# at the bottom and may not reach back up into adapters, configuration, or the CLI.
FORBIDDEN_DOMAIN_SIBLINGS = frozenset(
    {
        "argos.baselines",
        "argos.cli",
        "argos.compiler",
        "argos.config",
        "argos.evaluation",
        "argos.ingestion",
        "argos.projections",
        "argos.replay",
        "argos.resolution",
        "argos.sources",
        "argos.store",
    }
)


def _imported_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


@pytest.mark.parametrize("path", _modules(DOMAIN), ids=lambda p: p.name)
def test_domain_does_not_depend_on_outer_layers(path: Path) -> None:
    imported = _imported_modules(_parse(path))
    offending = {
        module
        for module in imported
        for forbidden in FORBIDDEN_DOMAIN_SIBLINGS
        if module == forbidden or module.startswith(f"{forbidden}.")
    }
    assert not offending, f"{path.name} imports an outer layer: {sorted(offending)}"


@pytest.mark.parametrize("path", _modules(DOMAIN), ids=lambda p: p.name)
def test_domain_does_not_read_the_environment(path: Path) -> None:
    """Configuration reaches the domain as a validated object, never as os.environ."""
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"environ", "getenv"}, (
                f"{path.name} reads the process environment directly"
            )
        if isinstance(node, ast.Name):
            assert node.id != "getenv", f"{path.name} reads the process environment directly"


# --- security regression ----------------------------------------------------------

# docs/09_SECURITY.md and .claude/rules/no-execution.md: the research core talks only
# to public read endpoints. An authenticated channel or credential parameter appearing
# in a URL literal would not be caught by the declared-name scan above.
FORBIDDEN_ENDPOINT_MARKERS = (
    "ws/user",
    "/auth",
    "api-key",
    "api_secret",
    "passphrase",
    "private-key",
    "mnemonic",
)


@pytest.mark.parametrize("path", _modules(SRC), ids=lambda p: p.name)
def test_no_authenticated_endpoint_literal_in_source(path: Path) -> None:
    literals = [
        node.value.lower()
        for node in ast.walk(_parse(path))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    offending = {
        literal
        for literal in literals
        for marker in FORBIDDEN_ENDPOINT_MARKERS
        if marker in literal
    }
    assert not offending, f"{path.name} contains an authenticated endpoint: {sorted(offending)}"


# --- ADR-0009: pacing is separate from timekeeping --------------------------------


def test_clock_protocol_exposes_only_now() -> None:
    """`Clock` is a pure reader (ADR-0009): adding `sleep` back, even quietly on
    one implementation, would reopen the category error the ADR closed."""
    from argos.clock import Clock

    public_attrs = {name for name in vars(Clock) if not name.startswith("_")}
    assert public_attrs == {"now"}


def test_replay_clock_has_no_pacing_methods() -> None:
    """Moving replay time is impossible, not merely forbidden: `ReplayClock` has
    no `sleep` and no `wait`, so an adapter cannot pace itself on virtual time."""
    from datetime import UTC, datetime

    from argos.clock import ReplayClock

    clock = ReplayClock(datetime(2026, 1, 1, tzinfo=UTC))
    assert not hasattr(clock, "sleep")
    assert not hasattr(clock, "wait")


_NO_PACING_PACKAGES = ("domain", "projections", "baselines", "evaluation")
_PACING_NAMES = frozenset({"Pacer", "RealPacer"})


@pytest.mark.parametrize("package", _NO_PACING_PACKAGES)
def test_no_pacing_import_outside_live_adapters(package: str) -> None:
    """Pacing is a live-adapter concern (ADR-0009): a scheduler-facing package
    reaching for it would be the M3 accelerated/stepwise pacer wearing this
    one's name, and it must never influence a deterministic replay hash."""
    for path in _modules(SRC / package):
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.endswith("clock.pacing"), (
                    f"{path.name} imports argos.clock.pacing directly"
                )
                imported = {alias.name for alias in node.names}
                offending = imported & _PACING_NAMES
                assert not offending, f"{path.name} imports pacing names: {sorted(offending)}"
            if isinstance(node, ast.Import):
                offending_modules = {
                    alias.name for alias in node.names if alias.name.endswith("clock.pacing")
                }
                assert not offending_modules, f"{path.name} imports {sorted(offending_modules)}"


@pytest.mark.parametrize("package", ("sources", "ingestion"))
def test_no_direct_anyio_pacing_calls(package: str) -> None:
    """Without this, the next adapter re-creates ADR-0009's defect 4: `GammaClient`
    once bounded its request with a bare `anyio.move_on_after`, which reads the
    event-loop's real clock regardless of which `Clock` it was handed. Pacing must
    go through the injected `Pacer`, never straight through `anyio`."""
    pacing_calls = {"sleep", "move_on_after"}
    for path in _modules(SRC / package):
        tree = _parse(path)
        anyio_aliases = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name == "anyio"
        }
        imported_functions = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "anyio"
            for alias in node.names
            if alias.name in pacing_calls
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in pacing_calls
                and isinstance(func.value, ast.Name)
                and func.value.id in anyio_aliases
            ):
                pytest.fail(f"{path.name} calls anyio.{func.attr}() directly; use a Pacer")
            if isinstance(func, ast.Name) and func.id in imported_functions:
                pytest.fail(f"{path.name} calls anyio.{func.id}() directly; use a Pacer")


def test_settings_declare_no_credential_shaped_field() -> None:
    """Settings feed run manifests and log lines; a secret field would leak into both."""
    from argos.config import Settings

    credential_tokens = ("key", "secret", "token", "password", "passphrase", "credential")
    offending = {
        name
        for name in Settings.model_fields
        for token in credential_tokens
        if token in name.lower()
    }
    assert not offending, f"Settings exposes credential-shaped fields: {sorted(offending)}"


def test_every_default_endpoint_is_public_and_encrypted() -> None:
    from argos.config import Settings

    settings = Settings()
    urls = [
        value
        for name, value in settings.snapshot().items()
        if name.endswith("_url") and isinstance(value, str)
    ]
    assert len(urls) == 4
    for url in urls:
        assert url.startswith(("https://", "wss://")), f"{url} is not encrypted"
        assert url.endswith("polymarket.com") or ".polymarket.com/" in url, (
            f"{url} is not a Polymarket public endpoint"
        )
        assert "@" not in url, f"{url} embeds credentials"
