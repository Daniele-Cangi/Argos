from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
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

# docs/13_TEST_STRATEGY.md, "Quality thresholds", transcribed rather than
# reinterpreted: "90%+ branch coverage for domain contracts, replay ordering,
# and scoring rules; 80%+ branch coverage for source adapters using fixtures".
#
# Two judgements worth stating, because a threshold table invites the reader to
# assume it was mechanical. `argos.replay` is where "replay ordering" lives and
# `argos.evaluation` is where "scoring rules" will; both are named at 90%.
# `argos.projections`, `argos.store`, `argos.ingestion`, `argos.compiler`,
# `argos.config` and `argos.cli` are *not* named by that document at any
# threshold, so nothing is enforced against them here and they are reported
# instead of quietly assigned a number nobody chose. The document's own first
# sentence is "Do not optimize for a vanity global coverage number", which is
# also why there is no aggregate threshold.
COVERAGE_THRESHOLDS: tuple[tuple[str, float], ...] = (
    ("src/argos/domain/", 90.0),
    ("src/argos/replay/", 90.0),
    ("src/argos/evaluation/", 90.0),
    ("src/argos/sources/", 80.0),
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


def run_coverage(root: Path) -> int:
    """Measure branch coverage and enforce the per-area thresholds.

    Kept out of the default gate deliberately: measured on the development
    machine, the suite runs in ~80 s and the same suite under coverage takes
    ~610 s, and a gate that slow stops being run between slices, which is worse
    than one that is fast and occasionally supplemented. CI runs this as its own
    step, where nobody is waiting.
    """
    with tempfile.TemporaryDirectory() as scratch:
        report = Path(scratch) / "coverage.json"
        command = (
            "uv",
            "run",
            "pytest",
            "-q",
            "--cov=argos",
            "--cov-branch",
            f"--cov-report=json:{report}",
            "--cov-report=term-missing:skip-covered",
        )
        print(f"\n[quality-gate] {' '.join(command)}", flush=True)
        result = subprocess.run(command, cwd=root, check=False)
        if result.returncode != 0:
            print("[quality-gate] FAILED: tests did not pass under coverage", file=sys.stderr)
            return result.returncode
        measured = json.loads(report.read_text(encoding="utf-8"))

    failures: list[str] = []
    print("\n[quality-gate] branch coverage against docs/13_TEST_STRATEGY.md")
    for prefix, threshold in COVERAGE_THRESHOLDS:
        files = {
            name: data
            for name, data in measured["files"].items()
            if name.replace("\\", "/").startswith(prefix)
        }
        if not files:
            # Not an error: `argos.replay` and `argos.evaluation` are empty
            # packages until M3 and M4 build them. Reported so an empty area is
            # visibly empty rather than silently passing.
            print(f"  {prefix:<28} no measured module yet (threshold {threshold:.0f}%)")
            continue
        for name, data in sorted(files.items()):
            percent = float(data["summary"]["percent_covered"])
            verdict = "ok" if percent >= threshold else "BELOW"
            print(f"  {name:<44} {percent:6.2f}%  (>= {threshold:.0f}%)  {verdict}")
            if percent < threshold:
                failures.append(f"{name}: {percent:.2f}% < {threshold:.0f}%")

    if failures:
        print("\n[quality-gate] FAILED: coverage below the declared threshold", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("\n[quality-gate] COVERAGE PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Measure branch coverage and enforce docs/13_TEST_STRATEGY.md thresholds.",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if args.coverage:
        return run_coverage(root)
    return run(QUICK_COMMANDS if args.quick else FULL_COMMANDS, root)


if __name__ == "__main__":
    raise SystemExit(main())
