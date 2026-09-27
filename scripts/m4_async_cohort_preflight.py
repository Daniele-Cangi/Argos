"""Read-only preflight for an unlaunched asynchronous M4 cohort protocol.

This command makes no Gamma or CLOB requests and writes no evidence. A passing
preflight is not a protocol freeze or authorization to begin a capture.
"""

from __future__ import annotations

import argparse
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import orjson

from argos.clock import LiveClock, ensure_utc
from argos.domain.provenance import sha256_hex
from argos.evaluation.async_cohort import AsynchronousCohortProtocolV1


def preflight_protocol(
    record: dict[str, Any], *, revision: str, checked_at: datetime
) -> dict[str, Any]:
    """Validate exact bytes' meaning without admitting observations or persisting a claim."""
    protocol = AsynchronousCohortProtocolV1.from_record(record)
    now = ensure_utc(checked_at)
    if protocol.code_revision != revision:
        raise ValueError("cohort protocol revision disagrees with the clean checkout")
    if now >= protocol.blocks[0].start:
        raise ValueError("first cohort block has already begun")
    if protocol.declared_at > now:
        raise ValueError("candidate declaration time is in the future")
    canonical = orjson.dumps(protocol.to_record(), option=orjson.OPT_SORT_KEYS)
    return {
        "status": "PREFLIGHT_ONLY_NOT_FROZEN",
        "experiment_id": protocol.experiment_id,
        "protocol_schema_version": protocol.schema_version,
        "candidate_sha256": sha256_hex(canonical),
        "revision": revision,
        "checked_at": now.isoformat(),
        "first_block_start": protocol.blocks[0].start.isoformat(),
        "final_block_end": protocol.blocks[-1].end.isoformat(),
        "intended_targets": sum(block.intended_targets for block in protocol.blocks),
    }


def _clean_revision() -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain"], check=True, capture_output=True, text=True
    ).stdout
    if status:
        raise ValueError("M4 cohort preflight requires a clean working tree")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    args = parser.parse_args()
    record = orjson.loads(args.spec.read_bytes())
    if not isinstance(record, dict):
        raise ValueError("cohort protocol specification must be a JSON object")
    result = preflight_protocol(record, revision=_clean_revision(), checked_at=LiveClock().now())
    print(orjson.dumps(result, option=orjson.OPT_SORT_KEYS).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
