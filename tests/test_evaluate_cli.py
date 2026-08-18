"""`argos evaluate baseline` end to end, on the real capture and its settlement.

This is the M4 deliverable "CLI to evaluate a captured/resolved dataset", driven
the way an operator would drive it: a SQLite event store written by the real
capture path, and a recorded market payload on disk. Nothing is fetched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import orjson
from test_replay import CAPTURE_START, RUN_ID, TOKEN, _ListFrameSource, _recorded_frames
from typer.testing import CliRunner

from argos import cli
from argos.clock import ReplayClock
from argos.store.event_store import open_sqlite_event_store

runner = CliRunner()

RESOLUTION = Path(__file__).resolve().parent / "fixtures" / "clob" / "market_resolved.raw.json"


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


async def test_the_real_capture_scores_against_its_real_settlement(tmp_path: Path) -> None:
    """The owner gate's completed baseline evaluation, through the CLI.

    The captured token is Diana Shnaider's in a match she lost, so every
    forecast is scored against an outcome of 0 -- read from the CLOB's explicit
    `winner` flag, not inferred from a price.
    """
    db_path = tmp_path / "events.sqlite3"
    await _write_capture(db_path)

    result = runner.invoke(
        cli.app,
        [
            "evaluate",
            "baseline",
            RUN_ID,
            "--token-id",
            TOKEN,
            "--db",
            str(db_path),
            "--resolution",
            str(RESOLUTION),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    report: dict[str, Any] = orjson.loads(result.stdout)

    assert report["schema_version"] == "evaluation_report.v1"
    assert report["scored_count"] > 0
    assert report["source_capture_run_ids"] == [RUN_ID]
    assert report["source_state_hash"]
    assert report["log_loss_epsilon"] == "0.000001"
    assert report["calibration_bin_count"] == 10

    midpoint = report["calibration"]["midpoint"]
    assert midpoint["sample_count"] == report["calibration"]["midpoint"]["sample_count"]
    assert midpoint["calibration_status"] == "uncalibrated"
    assert len(midpoint["bins"]) == 10

    # Required and non-empty, and specific rather than boilerplate.
    assert report["limitations"]
    joined = " ".join(report["limitations"]).lower()
    assert "uncalibrated" in joined
    assert "autocorrelated" in joined
    assert "single market" in joined


async def test_two_runs_of_the_same_evaluation_agree_on_every_metric(tmp_path: Path) -> None:
    """ "Baseline evaluation is reproducible from stored records."

    The run ids and timestamps differ between runs and are expected to; what
    must not differ is the state hash, the counts, or any metric.
    """
    db_path = tmp_path / "events.sqlite3"
    await _write_capture(db_path)

    def run() -> dict[str, Any]:
        outcome = runner.invoke(
            cli.app,
            [
                "evaluate",
                "baseline",
                RUN_ID,
                "--token-id",
                TOKEN,
                "--db",
                str(db_path),
                "--resolution",
                str(RESOLUTION),
                "--json",
            ],
        )
        assert outcome.exit_code == 0, outcome.output
        report: dict[str, Any] = orjson.loads(outcome.stdout)
        return report

    first, second = run(), run()
    for field in (
        "source_state_hash",
        "scored_count",
        "forecast_count",
        "abstention_count",
        "calibration",
        "cohorts",
        "limitations",
    ):
        assert first[field] == second[field], field
    assert first["evaluation_run_id"] != second["evaluation_run_id"]


async def test_the_report_is_written_beside_the_database(tmp_path: Path) -> None:
    db_path = tmp_path / "events.sqlite3"
    await _write_capture(db_path)
    result = runner.invoke(
        cli.app,
        [
            "evaluate",
            "baseline",
            RUN_ID,
            "--token-id",
            TOKEN,
            "--db",
            str(db_path),
            "--resolution",
            str(RESOLUTION),
        ],
    )
    assert result.exit_code == 0, result.output
    written = list(tmp_path.glob("*.evaluation-report.json"))
    assert len(written) == 1
    record = json.loads(written[0].read_text(encoding="utf-8"))
    assert record["schema_version"] == "evaluation_report.v1"
    # The human summary never omits the sample size, and always prints the
    # limitations -- a report whose caveats are one flag away is a report whose
    # caveats get dropped.
    assert "scored of" in result.output
    assert "limitations:" in result.output


async def test_a_payload_that_is_not_a_resolution_is_refused_with_both_reasons(
    tmp_path: Path,
) -> None:
    """Both normalizers' refusals are printed. "This is not a CLOB record" and
    "this is not a resolved Gamma record" are different problems with different
    fixes, and printing one would send the operator down the wrong path."""
    db_path = tmp_path / "events.sqlite3"
    await _write_capture(db_path)
    open_market = Path(__file__).resolve().parent / "fixtures" / "gamma" / "market_by_id.raw.json"
    result = runner.invoke(
        cli.app,
        [
            "evaluate",
            "baseline",
            RUN_ID,
            "--token-id",
            TOKEN,
            "--db",
            str(db_path),
            "--resolution",
            str(open_market),
        ],
    )
    assert result.exit_code == 1
    assert result.stderr.count("\n") >= 2, result.stderr
    assert "not_closed" in result.stderr


async def test_a_malformed_token_id_is_a_usage_error(tmp_path: Path) -> None:
    db_path = tmp_path / "events.sqlite3"
    await _write_capture(db_path)
    result = runner.invoke(
        cli.app,
        [
            "evaluate",
            "baseline",
            RUN_ID,
            "--token-id",
            "../etc/passwd",
            "--db",
            str(db_path),
            "--resolution",
            str(RESOLUTION),
        ],
    )
    assert result.exit_code == 2
    assert "--token-id" in result.output
