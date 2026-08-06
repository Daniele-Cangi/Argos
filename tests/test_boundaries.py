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
