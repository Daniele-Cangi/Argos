from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


FULL_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("uv", "run", "ruff", "check", "."),
    ("uv", "run", "ruff", "format", "--check", "."),
    ("uv", "run", "mypy", "src"),
    ("uv", "run", "pytest", "-q"),
)

QUICK_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("uv", "run", "ruff", "check", "src", "tests"),
    ("uv", "run", "pytest", "-q", "tests/test_smoke.py"),
)


def run(commands: tuple[tuple[str, ...], ...], root: Path) -> int:
    for command in commands:
        print(f"\n[quality-gate] {' '.join(command)}", flush=True)
        result = subprocess.run(command, cwd=root, check=False)
        if result.returncode != 0:
            print(f"[quality-gate] FAILED: {' '.join(command)}", file=sys.stderr)
            return result.returncode
    print("\n[quality-gate] PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    return run(QUICK_COMMANDS if args.quick else FULL_COMMANDS, root)


if __name__ == "__main__":
    raise SystemExit(main())
