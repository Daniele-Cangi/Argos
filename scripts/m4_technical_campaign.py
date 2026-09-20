"""Run bounded M4 technical scenarios through the shipped operator CLI."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson

from argos.evaluation.technical_execution import (
    FunctionalScenarioEvidenceV1,
    assess_functional_scenario,
)
from argos.store.event_store import CompletionStatus, open_sqlite_event_store


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity(path: Path, root: Path) -> str:
    return f"sha256:{_sha256(path)}:{path.resolve().relative_to(root.resolve()).as_posix()}"


def _run_json(command: list[str], *, cwd: Path) -> dict[str, Any]:
    completed = subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)
    value = orjson.loads(completed.stdout)
    if not isinstance(value, dict):
        raise ValueError(f"command did not emit a JSON object: {command!r}")
    return value


def _write_json(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(orjson.dumps(record, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))
        stream.flush()
        import os

        os.fsync(stream.fileno())
    temporary.replace(path)


def _clean_revision(root: Path) -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, check=True, capture_output=True, text=True
    ).stdout
    if status:
        raise ValueError("technical evidence requires a clean working tree")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def run_t1(args: argparse.Namespace) -> int:
    root = Path(args.repository).resolve()
    revision = _clean_revision(root)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    run_id = f"{args.campaign_id}-t1"
    db_path = output / "events.sqlite3"
    started_at = datetime.now(UTC)
    config = {
        "schema_version": "technical_t1_configuration.v1",
        "campaign_id": args.campaign_id,
        "code_revision": revision,
        "token_ids": sorted(args.token_id),
        "maximum_duration_seconds": args.max_seconds,
        "maximum_frame_count": args.max_frames,
        "raw_archive": True,
    }
    config_path = output / "configuration.json"
    _write_json(config_path, config)

    capture_command = ["uv", "run", "argos", "capture", "market"]
    for token_id in args.token_id:
        capture_command.extend(("--token-id", token_id))
    capture_command.extend(
        (
            "--max-seconds",
            str(args.max_seconds),
            "--max-frames",
            str(args.max_frames),
            "--capture-run-id",
            run_id,
            "--db",
            str(db_path),
            "--json",
        )
    )
    capture = _run_json(capture_command, cwd=root)
    capture_report_path = output / "capture-report.json"
    _write_json(capture_report_path, capture)

    before = _sha256(db_path)
    replay_reports: list[dict[str, Any]] = []
    database_hashes: list[str] = []
    for ordinal in (1, 2):
        manifest_path = output / f"replay-{ordinal}.manifest.json"
        replay = _run_json(
            [
                "uv",
                "run",
                "argos",
                "replay",
                "capture",
                run_id,
                "--db",
                str(db_path),
                "--manifest-out",
                str(manifest_path),
                "--json",
            ],
            cwd=root,
        )
        replay_path = output / f"replay-{ordinal}.report.json"
        _write_json(replay_path, replay)
        replay_reports.append(replay)
        database_hashes.append(_sha256(db_path))

    store = open_sqlite_event_store(db_path)
    try:
        capture_run = store.get_capture_run(run_id)
    finally:
        store.close()
    loop = capture.get("loop_health") or {}
    counts = capture.get("store_counts") or {}
    raw_payloads = tuple((output / "raw").glob("*/*.raw.json"))
    raw_index_path = output / "raw-index.json"
    _write_json(
        raw_index_path,
        {
            "schema_version": "technical_raw_index.v1",
            "payloads": [
                {
                    "path": path.resolve().relative_to(output).as_posix(),
                    "sha256": _sha256(path),
                }
                for path in sorted(raw_payloads)
            ],
        },
    )
    ended_at = datetime.now(UTC)
    artifact_paths = [
        config_path,
        db_path,
        raw_index_path,
        capture_report_path,
        Path(str(capture["manifest_path"])),
        output / "replay-1.report.json",
        output / "replay-1.manifest.json",
        output / "replay-2.report.json",
        output / "replay-2.manifest.json",
    ]
    evidence = FunctionalScenarioEvidenceV1(
        campaign_id=args.campaign_id,
        capture_run_id=run_id,
        started_at=started_at,
        ended_at=ended_at,
        capture_completed=(
            capture_run is not None and capture_run.completion_status is CompletionStatus.COMPLETED
        ),
        capture_interrupted=bool(capture.get("interrupted")),
        loop_accepted=int(loop.get("accepted", 0)),
        loop_duplicate=int(loop.get("duplicate", 0)),
        loop_rejected=int(loop.get("rejected", 0)),
        store_accepted=int(counts.get("accepted", 0)),
        store_duplicate=int(counts.get("duplicate", 0)),
        store_rejected=int(counts.get("rejected", 0)),
        frames_consumed=int(loop.get("frames_consumed", 0)),
        raw_payload_count=len(raw_payloads),
        database_sha256_before_replay=before,
        database_sha256_after_first_replay=database_hashes[0],
        database_sha256_after_second_replay=database_hashes[1],
        first_replay_state_hash=str(replay_reports[0]["state_hash"]),
        second_replay_state_hash=str(replay_reports[1]["state_hash"]),
        artifact_identities=tuple(_identity(path, output) for path in artifact_paths),
    )
    evidence_path = output / "t1-evidence.json"
    _write_json(evidence_path, evidence.to_record())
    result = assess_functional_scenario(evidence)
    result_path = output / "t1-result.json"
    _write_json(result_path, result.to_record())
    print(orjson.dumps(result.to_record(), option=orjson.OPT_INDENT_2).decode())
    return 0 if result.status.value == "PASSED" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    t1 = commands.add_parser("run-t1")
    t1.add_argument("--campaign-id", required=True)
    t1.add_argument("--token-id", action="append", required=True)
    t1.add_argument("--output", required=True)
    t1.add_argument("--repository", default=".")
    t1.add_argument("--max-seconds", type=int, default=600)
    t1.add_argument("--max-frames", type=int, default=500)
    t1.set_defaults(handler=run_t1)
    args = parser.parse_args()
    if len(args.token_id) != 2 or len(set(args.token_id)) != 2:
        parser.error("run-t1 requires exactly two distinct --token-id values")
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
