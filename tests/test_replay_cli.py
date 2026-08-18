"""`argos replay capture` end to end, over a capture written by the real path.

No fake store and no hand-built manifest: the fixture capture is written to a
real SQLite file by `run_capture`, and the command is then driven through
`CliRunner` exactly as an operator would. That is what makes this a test of the
command rather than of the function it calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import orjson
import pytest

# Imported from the sibling module rather than duplicated: the frame
# extraction encodes two fidelity decisions (drop outbound PINGs, drop the
# PONG replies the transport consumes) and a second copy would eventually
# disagree with the first about what the recorded capture contains.
from test_replay import (
    CAPTURE_START,
    GOLDEN_STATE_HASH,
    RUN_ID,
    TOKEN,
    _ListFrameSource,
    _recorded_frames,
)
from typer.testing import CliRunner

from argos import cli
from argos.clock import ReplayClock
from argos.store.event_store import open_sqlite_event_store

runner = CliRunner()


async def _write_capture(db_path: Path) -> None:
    from argos.ingestion.capture import run_capture

    store = open_sqlite_event_store(db_path)
    try:
        await run_capture(
            frame_source=_ListFrameSource(_recorded_frames()),
            store=store,
            clock=ReplayClock(CAPTURE_START),
            capture_run_id=RUN_ID,
            subscribed_token_ids=[TOKEN],
        )
    finally:
        store.close()


async def test_replaying_a_stored_capture_reports_the_golden_hash(tmp_path: Path) -> None:
    db_path = tmp_path / "events.sqlite3"
    await _write_capture(db_path)

    result = runner.invoke(cli.app, ["replay", "capture", RUN_ID, "--db", str(db_path), "--json"])
    assert result.exit_code == 0, result.output
    payload: dict[str, Any] = orjson.loads(result.stdout)

    assert payload["state_hash"] == GOLDEN_STATE_HASH
    assert payload["state_hash_version"] == "state_hash.v1"
    assert payload["counts"]["arrivals"]["arrivals"] == 38
    assert payload["counts"]["dispatch"]["applied_snapshots"] == 4
    assert [entry["token_id"] for entry in payload["projections"]] == [TOKEN]

    manifest_path = Path(payload["manifest_path"])
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "replay_manifest.v1"
    assert manifest["source_capture_run_id"] == RUN_ID
    assert manifest["output_state_hash"] == GOLDEN_STATE_HASH
    assert manifest["late_event_policy"] == {
        "kind": "mark_only",
        "allowed_lateness_microseconds": 0,
    }


async def test_a_replay_leaves_the_capture_exactly_as_it_found_it(tmp_path: Path) -> None:
    """A replay that could modify its own input would not be a replay.

    Compared by file bytes rather than by row counts: an append-only store can
    grow without any row count this test happens to check changing.
    """
    db_path = tmp_path / "events.sqlite3"
    await _write_capture(db_path)
    before = db_path.read_bytes()

    result = runner.invoke(cli.app, ["replay", "capture", RUN_ID, "--db", str(db_path), "--json"])
    assert result.exit_code == 0, result.output
    assert db_path.read_bytes() == before


async def test_an_unknown_capture_run_fails_with_the_reason_not_a_traceback(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "events.sqlite3"
    await _write_capture(db_path)

    result = runner.invoke(cli.app, ["replay", "capture", "never-happened", "--db", str(db_path)])
    assert result.exit_code == 1
    reported = orjson.loads(result.stderr)
    assert reported["error_code"] == "argos.replay"
    assert reported["context"]["capture_run_id"] == "never-happened"


@pytest.mark.parametrize("mode", ["accelerated", "stepwise"])
async def test_the_mode_is_recorded_and_does_not_change_the_hash(tmp_path: Path, mode: str) -> None:
    """`original_arrival` is deliberately excluded from this parametrization: it
    is the one mode that really sleeps, and a test that waited out a 40-second
    capture's real inter-arrival gaps would be the flaky timing test
    `docs/13_TEST_STRATEGY.md` forbids. The mode-independence of the hash is
    proven over all three modes in `tests/test_replay.py` with a virtual pacer,
    which is where that property belongs."""
    db_path = tmp_path / "events.sqlite3"
    await _write_capture(db_path)

    result = runner.invoke(
        cli.app, ["replay", "capture", RUN_ID, "--db", str(db_path), "--mode", mode, "--json"]
    )
    assert result.exit_code == 0, result.output
    payload: dict[str, Any] = orjson.loads(result.stdout)
    assert payload["mode"] == mode
    assert payload["state_hash"] == GOLDEN_STATE_HASH


async def test_a_negative_lateness_tolerance_is_a_usage_error(tmp_path: Path) -> None:
    db_path = tmp_path / "events.sqlite3"
    await _write_capture(db_path)
    result = runner.invoke(
        cli.app,
        ["replay", "capture", RUN_ID, "--db", str(db_path), "--allowed-lateness-ms", "-1"],
    )
    assert result.exit_code == 2
    assert "--allowed-lateness-ms" in result.output


async def test_the_human_summary_names_the_hash_and_the_manifest(tmp_path: Path) -> None:
    db_path = tmp_path / "events.sqlite3"
    await _write_capture(db_path)
    result = runner.invoke(cli.app, ["replay", "capture", RUN_ID, "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert GOLDEN_STATE_HASH in result.output
    assert "replay-manifest.json" in result.output
