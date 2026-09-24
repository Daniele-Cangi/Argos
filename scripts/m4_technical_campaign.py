"""Run bounded M4 technical scenarios through the shipped operator CLI."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import psutil

from argos.evaluation.technical_campaign import (
    TechnicalScenario,
    TechnicalScenarioResultV1,
    TechnicalScenarioStatus,
)
from argos.evaluation.technical_execution import (
    T2_EXPECTED_SAMPLE_INTERVAL_SECONDS,
    T2_MAXIMUM_ARTIFACT_BYTES,
    T2_MAXIMUM_RESIDENT_MEMORY_BYTES,
    T2_MAXIMUM_SAMPLE_GAP_SECONDS,
    T3_CAPTURE_DURATION_SECONDS,
    T3_EXPECTED_CHECKPOINT_INTERVAL_SECONDS,
    T3_MAXIMUM_ARTIFACT_BYTES,
    T3_MAXIMUM_CHECKPOINT_GAP_SECONDS,
    T3_MAXIMUM_DURATION_SECONDS,
    T3_MAXIMUM_FRAME_COUNT,
    T3_MAXIMUM_RESIDENT_MEMORY_BYTES,
    EnduranceCheckpointV1,
    EnduranceScenarioEvidenceV1,
    FunctionalScenarioEvidenceV1,
    StabilityResourceSampleV1,
    StabilityScenarioEvidenceV1,
    assess_endurance_scenario,
    assess_functional_scenario,
    assess_stability_scenario,
)
from argos.monitoring.faults import (
    TERMINAL_STATE_MATRIX_V1,
    DeterministicFaultAdapter,
    DeterministicFaultScheduleV1,
    InjectedFault,
    TerminalStateFixtureAdapter,
)
from argos.monitoring.resumable import (
    ExclusiveFileLease,
    PollCommit,
    ResumableMonitor,
    ResumableMonitorCheckpointV1,
    write_atomic_checkpoint,
)
from argos.resolution import ResolutionRefusal, ResolutionStatus, normalize_gamma_resolution
from argos.store.event_store import CompletionStatus, open_sqlite_event_store


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity(path: Path, root: Path) -> str:
    return f"sha256:{_sha256(path)}:{path.resolve().relative_to(root.resolve()).as_posix()}"


def _run_json(
    command: list[str], *, cwd: Path, allowed_exit_codes: frozenset[int] = frozenset({0})
) -> dict[str, Any]:
    completed = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True)
    if completed.returncode not in allowed_exit_codes:
        completed.check_returncode()
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
        os.fsync(stream.fileno())
    temporary.replace(path)
    _fsync_directory(path.parent)


def _fsync_directory(directory: Path) -> None:
    """Make the atomic rename durable where Python exposes directory handles."""

    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _clean_revision(root: Path) -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, check=True, capture_output=True, text=True
    ).stdout
    if status:
        raise ValueError("technical evidence requires a clean working tree")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _capture_command(args: argparse.Namespace, run_id: str, db_path: Path) -> list[str]:
    command = ["uv", "run", "argos", "capture", "market"]
    for token_id in args.token_id:
        command.extend(("--token-id", token_id))
    command.extend(
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
    return command


def _resident_bytes(processes: tuple[psutil.Process, ...]) -> int:
    total = 0
    for member in processes:
        try:
            total += member.memory_info().rss
        except psutil.NoSuchProcess:
            continue
    return total


def _tree_resident_bytes(pid: int, tracked: dict[int, psutil.Process]) -> int:
    process = psutil.Process(pid)
    processes = [process, *process.children(recursive=True)]
    for member in processes:
        tracked[member.pid] = member
    return _resident_bytes(tuple(processes))


def _artifact_bytes(directory: Path) -> int:
    return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


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

    capture_command = _capture_command(args, run_id, db_path)
    # Exit 130 is the command's documented, fully reported operator-interrupt
    # outcome. Preserve its JSON and let the deterministic assessor record a
    # FAILED T1 instead of turning it into an unreported runner exception.
    capture = _run_json(capture_command, cwd=root, allowed_exit_codes=frozenset({0, 130}))
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


def run_t2(args: argparse.Namespace) -> int:
    root = Path(args.repository).resolve()
    revision = _clean_revision(root)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    run_id = f"{args.campaign_id}-t2"
    db_path = output / "events.sqlite3"
    started_at = datetime.now(UTC)
    config_path = output / "configuration.json"
    _write_json(
        config_path,
        {
            "schema_version": "technical_t2_configuration.v1",
            "campaign_id": args.campaign_id,
            "code_revision": revision,
            "token_ids": sorted(args.token_id),
            "maximum_duration_seconds": args.max_seconds,
            "maximum_frame_count": args.max_frames,
            "sample_interval_seconds": args.sample_interval,
            "maximum_sample_gap_seconds": args.max_sample_gap,
            "maximum_resident_memory_bytes": args.max_rss_bytes,
            "maximum_artifact_bytes": args.max_artifact_bytes,
            "raw_archive": True,
        },
    )
    stdout_path = output / "capture.stdout.json"
    stderr_path = output / "capture.stderr.log"
    samples: list[StabilityResourceSampleV1] = []
    sampling_errors: list[str] = []
    tracked_processes: dict[int, psutil.Process] = {}
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            _capture_command(args, run_id, db_path), cwd=root, stdout=stdout, stderr=stderr
        )
        while True:
            observed_at = datetime.now(UTC)
            try:
                resident = _tree_resident_bytes(process.pid, tracked_processes)
            except psutil.NoSuchProcess:
                resident = 0
            except psutil.AccessDenied as error:
                resident = 0
                sampling_errors.append(f"AccessDenied: {error}")
            samples.append(
                StabilityResourceSampleV1(
                    ordinal=len(samples),
                    observed_at=observed_at,
                    resident_memory_bytes=resident,
                    artifact_bytes=_artifact_bytes(output),
                )
            )
            try:
                return_code = process.wait(timeout=args.sample_interval)
                break
            except subprocess.TimeoutExpired:
                continue
    try:
        final_resident = _resident_bytes(tuple(tracked_processes.values()))
    except psutil.AccessDenied as error:
        final_resident = 0
        sampling_errors.append(f"AccessDenied: {error}")
    samples.append(
        StabilityResourceSampleV1(
            ordinal=len(samples),
            observed_at=datetime.now(UTC),
            resident_memory_bytes=final_resident,
            artifact_bytes=_artifact_bytes(output),
        )
    )
    capture_ended_at = datetime.now(UTC)
    if return_code not in (0, 130):
        raise subprocess.CalledProcessError(return_code, process.args)
    capture = orjson.loads(stdout_path.read_bytes())
    if not isinstance(capture, dict):
        raise ValueError("capture did not emit a JSON object")
    capture_report_path = output / "capture-report.json"
    _write_json(capture_report_path, capture)
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
    samples_path = output / "resource-samples.json"
    _write_json(
        samples_path,
        {
            "schema_version": "stability_resource_samples.v1",
            "samples": [item.to_record() for item in samples],
        },
    )
    artifact_paths = (
        config_path,
        db_path,
        capture_report_path,
        stderr_path,
        raw_index_path,
        samples_path,
        Path(str(capture["manifest_path"])),
    )
    evidence = StabilityScenarioEvidenceV1(
        campaign_id=args.campaign_id,
        capture_run_id=run_id,
        started_at=started_at,
        ended_at=capture_ended_at,
        capture_completed=(
            capture_run is not None and capture_run.completion_status is CompletionStatus.COMPLETED
        ),
        capture_interrupted=bool(capture.get("interrupted")),
        frames_consumed=int(loop.get("frames_consumed", 0)),
        events_seen=int(loop.get("events_seen", 0)),
        decode_failures=int(loop.get("decode_failures", 0)),
        not_applicable=int(loop.get("not_applicable", 0)),
        unknown_event_type=int(loop.get("unknown_event_type", 0)),
        loop_counts=(
            int(loop.get("accepted", 0)),
            int(loop.get("duplicate", 0)),
            int(loop.get("rejected", 0)),
        ),
        store_counts=(
            int(counts.get("accepted", 0)),
            int(counts.get("duplicate", 0)),
            int(counts.get("rejected", 0)),
        ),
        raw_payload_count=len(raw_payloads),
        expected_sample_interval_seconds=args.sample_interval,
        maximum_sample_gap_seconds=args.max_sample_gap,
        maximum_resident_memory_bytes=args.max_rss_bytes,
        maximum_artifact_bytes=args.max_artifact_bytes,
        sampling_errors=tuple(sampling_errors),
        samples=tuple(samples),
        artifact_identities=tuple(_identity(path, output) for path in artifact_paths),
    )
    _write_json(output / "t2-evidence.json", evidence.to_record())
    result = assess_stability_scenario(evidence)
    _write_json(output / "t2-result.json", result.to_record())
    print(orjson.dumps(result.to_record(), option=orjson.OPT_INDENT_2).decode())
    return 0 if result.status.value == "PASSED" else 1


def _write_endurance_checkpoint(
    directory: Path,
    checkpoints: list[EnduranceCheckpointV1],
    *,
    observed_at: datetime,
    state: str,
    resident_memory_bytes: int,
    artifact_bytes: int,
) -> EnduranceCheckpointV1:
    checkpoint = EnduranceCheckpointV1(
        ordinal=len(checkpoints),
        observed_at=observed_at,
        state=state,
        resident_memory_bytes=resident_memory_bytes,
        artifact_bytes=artifact_bytes,
        previous_checkpoint_sha256=(checkpoints[-1].checkpoint_sha256 if checkpoints else None),
    )
    _write_json(directory / f"{checkpoint.ordinal:06d}.json", checkpoint.to_record())
    checkpoints.append(checkpoint)
    return checkpoint


def _reload_endurance_checkpoints(
    directory: Path, expected_count: int
) -> tuple[EnduranceCheckpointV1, ...]:
    paths = sorted(directory.glob("*.json"))
    expected_names = [f"{ordinal:06d}.json" for ordinal in range(expected_count)]
    if [path.name for path in paths] != expected_names:
        raise ValueError("persisted checkpoint files are incomplete or unexpected")
    return tuple(
        EnduranceCheckpointV1.from_record(orjson.loads(path.read_bytes())) for path in paths
    )


def _probe_terminal_resume(checkpoints: tuple[EnduranceCheckpointV1, ...]) -> bool:
    """Verify a fresh owner derives one unambiguous next position without writing it."""

    if not checkpoints or checkpoints[-1].state != "COMPLETED":
        return False
    identities = tuple(item.checkpoint_sha256 for item in checkpoints)
    if len(identities) != len(set(identities)):
        return False
    next_ordinal = checkpoints[-1].ordinal + 1
    return next_ordinal == len(checkpoints) and bool(identities[-1])


def run_t3(args: argparse.Namespace) -> int:
    """Run the frozen six-hour endurance capture with durable checkpoints."""

    frozen = (
        args.max_seconds,
        args.capture_seconds,
        args.max_frames,
        args.checkpoint_interval,
        args.max_checkpoint_gap,
        args.max_rss_bytes,
        args.max_artifact_bytes,
    )
    if frozen != (
        T3_MAXIMUM_DURATION_SECONDS,
        T3_CAPTURE_DURATION_SECONDS,
        T3_MAXIMUM_FRAME_COUNT,
        T3_EXPECTED_CHECKPOINT_INTERVAL_SECONDS,
        T3_MAXIMUM_CHECKPOINT_GAP_SECONDS,
        T3_MAXIMUM_RESIDENT_MEMORY_BYTES,
        T3_MAXIMUM_ARTIFACT_BYTES,
    ):
        raise ValueError("T3 execution bounds must match the frozen protocol")
    root = Path(args.repository).resolve()
    revision = _clean_revision(root)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    checkpoint_dir = output / "checkpoints"
    checkpoint_dir.mkdir()
    run_id = f"{args.campaign_id}-t3"
    db_path = output / "events.sqlite3"
    started_at = datetime.now(UTC)
    config_path = output / "configuration.json"
    _write_json(
        config_path,
        {
            "schema_version": "technical_t3_configuration.v1",
            "campaign_id": args.campaign_id,
            "code_revision": revision,
            "token_ids": sorted(args.token_id),
            "maximum_duration_seconds": args.max_seconds,
            "capture_duration_seconds": args.capture_seconds,
            "maximum_frame_count": args.max_frames,
            "checkpoint_interval_seconds": args.checkpoint_interval,
            "maximum_checkpoint_gap_seconds": args.max_checkpoint_gap,
            "maximum_resident_memory_bytes": args.max_rss_bytes,
            "maximum_artifact_bytes": args.max_artifact_bytes,
            "raw_archive": True,
        },
    )
    stdout_path = output / "capture.stdout.json"
    stderr_path = output / "capture.stderr.log"
    checkpoints: list[EnduranceCheckpointV1] = []
    checkpoint_errors: list[str] = []
    tracked_processes: dict[int, psutil.Process] = {}
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        capture_args = argparse.Namespace(**{**vars(args), "max_seconds": args.capture_seconds})
        process = subprocess.Popen(
            _capture_command(capture_args, run_id, db_path), cwd=root, stdout=stdout, stderr=stderr
        )
        while True:
            observed_at = datetime.now(UTC)
            try:
                resident = _tree_resident_bytes(process.pid, tracked_processes)
            except psutil.NoSuchProcess:
                resident = 0
            except psutil.AccessDenied as error:
                resident = 0
                checkpoint_errors.append(f"AccessDenied: {error}")
            _write_endurance_checkpoint(
                checkpoint_dir,
                checkpoints,
                observed_at=observed_at,
                state="RUNNING",
                resident_memory_bytes=resident,
                artifact_bytes=_artifact_bytes(output),
            )
            try:
                return_code = process.wait(timeout=args.checkpoint_interval)
                break
            except subprocess.TimeoutExpired:
                continue
    try:
        final_resident = _resident_bytes(tuple(tracked_processes.values()))
    except psutil.AccessDenied as error:
        final_resident = 0
        checkpoint_errors.append(f"AccessDenied: {error}")
    capture_ended_at = datetime.now(UTC)
    _write_endurance_checkpoint(
        checkpoint_dir,
        checkpoints,
        observed_at=capture_ended_at,
        state="COMPLETED",
        resident_memory_bytes=final_resident,
        artifact_bytes=_artifact_bytes(output),
    )
    persisted_checkpoints = _reload_endurance_checkpoints(checkpoint_dir, len(checkpoints))
    persisted_checkpoint_chain_reloaded = persisted_checkpoints == tuple(checkpoints)
    terminal_resume_verified = _probe_terminal_resume(persisted_checkpoints)
    if return_code not in (0, 130):
        raise subprocess.CalledProcessError(return_code, process.args)
    capture = orjson.loads(stdout_path.read_bytes())
    if not isinstance(capture, dict):
        raise ValueError("capture did not emit a JSON object")
    capture_report_path = output / "capture-report.json"
    _write_json(capture_report_path, capture)
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
    checkpoint_index_path = output / "checkpoint-index.json"
    _write_json(
        checkpoint_index_path,
        {
            "schema_version": "endurance_checkpoint_index.v1",
            "terminal_checkpoint_sha256": persisted_checkpoints[-1].checkpoint_sha256,
            "checkpoints": [
                {
                    "ordinal": item.ordinal,
                    "path": f"checkpoints/{item.ordinal:06d}.json",
                    "sha256": item.checkpoint_sha256,
                }
                for item in persisted_checkpoints
            ],
        },
    )
    artifact_paths = (
        config_path,
        db_path,
        capture_report_path,
        stderr_path,
        raw_index_path,
        checkpoint_index_path,
        Path(str(capture["manifest_path"])),
    )
    evidence = EnduranceScenarioEvidenceV1(
        campaign_id=args.campaign_id,
        capture_run_id=run_id,
        started_at=started_at,
        ended_at=capture_ended_at,
        capture_completed=(
            capture_run is not None and capture_run.completion_status is CompletionStatus.COMPLETED
        ),
        capture_interrupted=bool(capture.get("interrupted")),
        frames_consumed=int(loop.get("frames_consumed", 0)),
        decode_failures=int(loop.get("decode_failures", 0)),
        unknown_event_type=int(loop.get("unknown_event_type", 0)),
        loop_counts=(
            int(loop.get("accepted", 0)),
            int(loop.get("duplicate", 0)),
            int(loop.get("rejected", 0)),
        ),
        store_counts=(
            int(counts.get("accepted", 0)),
            int(counts.get("duplicate", 0)),
            int(counts.get("rejected", 0)),
        ),
        raw_payload_count=len(raw_payloads),
        maximum_duration_seconds=args.max_seconds,
        capture_duration_seconds=args.capture_seconds,
        maximum_frame_count=args.max_frames,
        expected_checkpoint_interval_seconds=args.checkpoint_interval,
        maximum_checkpoint_gap_seconds=args.max_checkpoint_gap,
        maximum_resident_memory_bytes=args.max_rss_bytes,
        maximum_artifact_bytes=args.max_artifact_bytes,
        checkpoint_errors=tuple(checkpoint_errors),
        checkpoints=persisted_checkpoints,
        persisted_checkpoint_chain_reloaded=persisted_checkpoint_chain_reloaded,
        terminal_resume_verified=terminal_resume_verified,
        artifact_identities=tuple(_identity(path, output) for path in artifact_paths),
    )
    _write_json(output / "t3-evidence.json", evidence.to_record())
    result = assess_endurance_scenario(evidence)
    _write_json(output / "t3-result.json", result.to_record())
    print(orjson.dumps(result.to_record(), option=orjson.OPT_INDENT_2).decode())
    return 0 if result.status.value == "PASSED" else 1


def _fault_schedule(scenario: TechnicalScenario) -> DeterministicFaultScheduleV1:
    return DeterministicFaultScheduleV1(
        schedule_id=f"{scenario.value.lower()}-schedule-v1",
        scenario=scenario,
        activation_attempts=(0,),
    )


def _write_executor_configuration(
    args: argparse.Namespace,
    scenario: TechnicalScenario,
    output: Path,
    *,
    bounds: dict[str, int],
    fixture_identity: str,
    activation_identity: object,
    code_revision: str,
) -> Path:
    path = output / "configuration.json"
    _write_json(
        path,
        {
            "schema_version": "technical_executor_configuration.v1",
            "campaign_id": args.campaign_id,
            "scenario": scenario.value,
            "code_revision": code_revision,
            "working_tree_clean": True,
            "bounds": bounds,
            "fixture_identity": fixture_identity,
            "activation_identity": activation_identity,
        },
    )
    return path


def _technical_result(
    args: argparse.Namespace,
    scenario: TechnicalScenario,
    started_at: datetime,
    output: Path,
    evidence_path: Path,
    outcome: str,
    artifact_paths: tuple[Path, ...] = (),
    external_artifact_identities: tuple[str, ...] = (),
) -> TechnicalScenarioResultV1:
    ended_at = datetime.now(UTC)
    result = TechnicalScenarioResultV1(
        campaign_id=args.campaign_id,
        scenario=scenario,
        status=TechnicalScenarioStatus.PASSED,
        started_at=started_at,
        last_checkpoint_at=ended_at,
        ended_at=ended_at,
        observed_frame_count=0,
        artifact_identities=(
            *tuple(_identity(path, output) for path in (evidence_path, *artifact_paths)),
            *external_artifact_identities,
        ),
        observed_outcome=outcome,
    )
    _write_json(output / f"{scenario.value.lower()}-result.json", result.to_record())
    print(orjson.dumps(result.to_record(), option=orjson.OPT_INDENT_2).decode())
    return result


def _materialize_executor_failure(args: argparse.Namespace, error: Exception) -> int:
    scenario = (
        TechnicalScenario(args.scenario)
        if args.command == "run-fault"
        else {
            "run-t7": TechnicalScenario.TERMINAL_SIMULATION,
            "run-t8": TechnicalScenario.CROSS_VOLUME,
        }[args.command]
    )
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    result_path = output / f"{scenario.value.lower()}-result.json"
    if result_path.exists():
        print(
            f"refusing to overwrite existing result artifact: {result_path}",
            file=sys.stderr,
        )
        return 1
    now = datetime.now(UTC)
    started_at = getattr(args, "executor_started_at", now)
    last_checkpoint_at = started_at
    checkpoint_path = output / "checkpoint.json"
    if checkpoint_path.exists():
        try:
            checkpoint_record = orjson.loads(checkpoint_path.read_bytes())
            last_checkpoint_at = datetime.fromisoformat(
                str(checkpoint_record["updated_at"]).replace("Z", "+00:00")
            ).astimezone(UTC)
        except (KeyError, TypeError, ValueError, orjson.JSONDecodeError):
            last_checkpoint_at = started_at
    preserved_paths = tuple(
        path
        for path in sorted(output.iterdir())
        if path.is_file() and path != result_path and not path.name.endswith((".tmp", ".partial"))
    )
    result = TechnicalScenarioResultV1(
        campaign_id=args.campaign_id,
        scenario=scenario,
        status=TechnicalScenarioStatus.FAILED,
        started_at=started_at,
        last_checkpoint_at=last_checkpoint_at,
        ended_at=now,
        observed_frame_count=0,
        artifact_identities=tuple(_identity(path, output) for path in preserved_paths),
        reason=f"{type(error).__name__}: {error}",
        follow_up_action="inspect the preserved evidence and repair this executor before rerun",
    )
    _write_json(result_path, result.to_record())
    print(orjson.dumps(result.to_record(), option=orjson.OPT_INDENT_2).decode())
    return 1


def run_fault_scenario(args: argparse.Namespace) -> int:
    scenario = TechnicalScenario(args.scenario)
    if scenario not in {
        TechnicalScenario.NETWORK_INTERRUPTION,
        TechnicalScenario.PROCESS_INTERRUPTION,
        TechnicalScenario.STORAGE_INTERRUPTION,
    }:
        raise ValueError("run-fault requires T4, T5, or T6")
    repository = Path(args.repository).resolve()
    revision = _clean_revision(repository)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    started_at = datetime.now(UTC)
    args.executor_started_at = started_at
    schedule = _fault_schedule(scenario)
    schedule_path = output / "fault-schedule.json"
    _write_json(schedule_path, schedule.to_record())
    configuration_path = _write_executor_configuration(
        args,
        scenario,
        output,
        bounds={"activation_attempt_count": len(schedule.activation_attempts), "resume_count": 1},
        fixture_identity=_sha256(schedule_path),
        activation_identity=list(schedule.activation_attempts),
        code_revision=revision,
    )
    checkpoint_path = output / "checkpoint.json"
    lock_path = output / "owner.lock"
    initial = ResumableMonitorCheckpointV1(
        campaign_id=args.campaign_id,
        configuration_sha256=_sha256(configuration_path),
        next_ordinal=0,
        updated_at=started_at,
    )
    ResumableMonitor(checkpoint_path, lock_path).save(initial)
    adapter = DeterministicFaultAdapter(schedule)
    process_exit_code: int | None = None
    duplicate_owner_rejected: bool | None = None
    attempts = iter((0, 1))
    clock = iter((started_at,) * 8)

    def poll(checkpoint: ResumableMonitorCheckpointV1) -> PollCommit:
        return adapter.invoke(
            next(attempts),
            lambda: PollCommit(
                ordinal=checkpoint.next_ordinal,
                receipt_id="receipt-000000",
                record_sha256="b" * 64,
                persisted_at=started_at,
                previous_receipt_id=checkpoint.last_receipt_id,
            ),
        )

    if scenario is TechnicalScenario.NETWORK_INTERRUPTION:
        try:
            ResumableMonitor(checkpoint_path, lock_path).run_once(
                poll=poll, failure_time=lambda: next(clock)
            )
        except InjectedFault as error:
            first_error = type(error).__name__
        else:
            raise RuntimeError("declared network fault did not activate")
        failed = ResumableMonitor(checkpoint_path, lock_path).load()
        if failed.next_ordinal != 0 or len(failed.gaps) != 1:
            raise RuntimeError("network fault consumed an ordinal or omitted its gap")
        resumed = ResumableMonitor(checkpoint_path, lock_path).run_once(
            poll=poll, failure_time=lambda: next(clock)
        )
    elif scenario is TechnicalScenario.STORAGE_INTERRUPTION:

        def refusing_writer(path: Path, raw: bytes) -> None:
            adapter.invoke(0, lambda: write_atomic_checkpoint(path, raw))

        try:
            ResumableMonitor(
                checkpoint_path, lock_path, checkpoint_writer=refusing_writer
            ).run_once(
                poll=lambda checkpoint: PollCommit(
                    ordinal=checkpoint.next_ordinal,
                    receipt_id="receipt-000000",
                    record_sha256="b" * 64,
                    persisted_at=started_at,
                    previous_receipt_id=checkpoint.last_receipt_id,
                ),
                failure_time=lambda: next(clock),
            )
        except InjectedFault as error:
            first_error = type(error).__name__
        else:
            raise RuntimeError("declared storage refusal did not activate")
        failed = ResumableMonitor(checkpoint_path, lock_path).load()
        if failed != initial:
            raise RuntimeError("storage refusal changed the durable head")
        resumed = ResumableMonitor(checkpoint_path, lock_path).run_once(
            poll=lambda checkpoint: PollCommit(
                ordinal=checkpoint.next_ordinal,
                receipt_id="receipt-000000",
                record_sha256="b" * 64,
                persisted_at=started_at,
                previous_receipt_id=checkpoint.last_receipt_id,
            ),
            failure_time=lambda: next(clock),
        )
    else:
        ready_path = output / "child-ready"
        child_code = (
            "import sys,time; from datetime import UTC,datetime; from pathlib import Path; "
            "from argos.monitoring.resumable import "
            "ExclusiveFileLease,PollCommit,ResumableMonitor; "
            "lock=Path(sys.argv[1]); ready=Path(sys.argv[2]); "
            "checkpoint=Path(sys.argv[3]); monitor=ResumableMonitor(checkpoint,lock); "
            "monitor.run_once(poll=lambda head: PollCommit(ordinal=head.next_ordinal,"
            "receipt_id='receipt-000000',record_sha256='b'*64,persisted_at=head.updated_at,"
            "previous_receipt_id=head.last_receipt_id),failure_time=lambda: datetime.now(UTC)); "
            "lease=ExclusiveFileLease(lock); lease.__enter__(); "
            "ready.write_text('ready'); time.sleep(60)"
        )
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                child_code,
                str(lock_path),
                str(ready_path),
                str(checkpoint_path),
            ],
            cwd=repository,
        )
        for _ in range(100):
            if ready_path.exists():
                break
            if child.poll() is not None:
                raise RuntimeError("T5 lock owner exited before readiness")
            import time

            time.sleep(0.05)
        else:
            child.kill()
            raise RuntimeError("T5 lock owner did not become ready")
        try:
            with ExclusiveFileLease(lock_path):
                pass
        except RuntimeError:
            duplicate_owner_rejected = True
        else:
            duplicate_owner_rejected = False
        try:
            adapter.invoke(0, lambda: None)
        except InjectedFault as error:
            first_error = type(error).__name__
            child.terminate()
        else:
            child.kill()
            raise RuntimeError("declared process interruption did not activate")
        process_exit_code = child.wait(timeout=10)
        failed = ResumableMonitor(checkpoint_path, lock_path).load()
        if failed.next_ordinal != 1 or failed.last_receipt_id != "receipt-000000":
            raise RuntimeError("terminated owner did not leave one durable poll")
        resumed = ResumableMonitor(checkpoint_path, lock_path).run_once(
            poll=lambda checkpoint: PollCommit(
                ordinal=checkpoint.next_ordinal,
                receipt_id="receipt-000001",
                record_sha256="c" * 64,
                persisted_at=started_at,
                previous_receipt_id=checkpoint.last_receipt_id,
            ),
            failure_time=lambda: next(clock),
        )
        if not duplicate_owner_rejected:
            raise RuntimeError("concurrent T5 owner was not rejected")
    expected_next = 2 if scenario is TechnicalScenario.PROCESS_INTERRUPTION else 1
    expected_receipt = (
        "receipt-000001" if scenario is TechnicalScenario.PROCESS_INTERRUPTION else "receipt-000000"
    )
    if resumed.next_ordinal != expected_next or resumed.last_receipt_id != expected_receipt:
        raise RuntimeError("same-ordinal resume did not commit exactly once")
    if checkpoint_path.with_suffix(".json.partial").exists():
        raise RuntimeError("partial checkpoint was exposed")
    evidence_path = output / "evidence.json"
    _write_json(
        evidence_path,
        {
            "schema_version": "deterministic_fault_evidence.v1",
            "scenario": scenario.value,
            "schedule_sha256": _sha256(schedule_path),
            "injected_error": first_error,
            "failed_next_ordinal": failed.next_ordinal,
            "explicit_gap_count": len(failed.gaps),
            "resumed_next_ordinal": resumed.next_ordinal,
            "receipt_id": resumed.last_receipt_id,
            "process_exit_code": process_exit_code,
            "duplicate_owner_rejected": duplicate_owner_rejected,
            "exclusive_owner": True,
            "partial_checkpoint_absent": True,
        },
    )
    _technical_result(
        args,
        scenario,
        started_at,
        output,
        evidence_path,
        "fault activated exactly once; durable head preserved; same ordinal resumed exactly once",
        (configuration_path, schedule_path, checkpoint_path),
    )
    return 0


def run_t7(args: argparse.Namespace) -> int:
    revision = _clean_revision(Path(args.repository).resolve())
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    started_at = datetime.now(UTC)
    args.executor_started_at = started_at
    schedule = DeterministicFaultScheduleV1(
        schedule_id="terminal-state-matrix-v1",
        scenario=TechnicalScenario.TERMINAL_SIMULATION,
        terminal_states=TERMINAL_STATE_MATRIX_V1,
    )
    schedule_path = output / "terminal-state-schedule.json"
    _write_json(schedule_path, schedule.to_record())
    configuration_path = _write_executor_configuration(
        args,
        TechnicalScenario.TERMINAL_SIMULATION,
        output,
        bounds={"fixture_count": len(TERMINAL_STATE_MATRIX_V1)},
        fixture_identity=_sha256(schedule_path),
        activation_identity=[state.value for state in TERMINAL_STATE_MATRIX_V1],
        code_revision=revision,
    )
    adapter = TerminalStateFixtureAdapter(schedule)
    observed = tuple(adapter.read_next() for _ in TERMINAL_STATE_MATRIX_V1)
    if observed != TERMINAL_STATE_MATRIX_V1:
        raise RuntimeError("terminal state matrix changed order or membership")
    try:
        adapter.read_next()
    except StopIteration:
        exhausted = True
    else:
        exhausted = False
    handled = []
    for index, state in enumerate(observed):
        payload = {
            "id": f"market-{index}",
            "conditionId": f"condition-{index}",
            "closed": True,
            "outcomePrices": '["0","0"]' if state.value == "ADMINISTRATIVE_CLOSE" else '["1","0"]',
            "clobTokenIds": '["yes-token","no-token"]',
            "umaResolutionStatuses": orjson.dumps(
                []
                if state.value == "UNKNOWN"
                else [
                    {
                        "PROPOSED": "proposed",
                        "DISPUTED": "disputed",
                        "FINAL": "resolved",
                    }.get(state.value, "")
                ]
            ).decode(),
        }
        digest = hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()
        normalized = normalize_gamma_resolution(
            payload, source_payload_sha256=digest, normalized_at=started_at
        )
        if isinstance(normalized, ResolutionRefusal):
            handled.append({"state": state.value, "refusal": normalized.reason.value})
        else:
            handled.append(
                {"state": state.value, "normalized_status": normalized.resolution_status.value}
            )
    expected_handling = [
        {"state": "UNKNOWN", "normalized_status": ResolutionStatus.UNKNOWN.value},
        {"state": "PROPOSED", "normalized_status": ResolutionStatus.PROPOSED.value},
        {"state": "DISPUTED", "normalized_status": ResolutionStatus.DISPUTED.value},
        {"state": "FINAL", "normalized_status": ResolutionStatus.FINAL.value},
        {"state": "ADMINISTRATIVE_CLOSE", "refusal": "no_determinable_outcome"},
    ]
    if handled != expected_handling:
        raise RuntimeError("terminal-state normalization disagrees with the frozen matrix")
    evidence_path = output / "evidence.json"
    _write_json(
        evidence_path,
        {
            "schema_version": "terminal_matrix_evidence.v1",
            "states": [state.value for state in observed],
            "complete_exact_order": True,
            "fixture_exhausted": exhausted,
            "handled_outcomes": handled,
        },
    )
    _technical_result(
        args,
        TechnicalScenario.TERMINAL_SIMULATION,
        started_at,
        output,
        evidence_path,
        "unknown, proposed, disputed, final, and administrative close handled once in frozen order",
        (configuration_path, schedule_path),
    )
    return 0


def _process_cross_volume_input(
    path: Path, *, normalized_at: datetime
) -> tuple[str, dict[str, object]]:
    raw = path.read_bytes()
    identity = hashlib.sha256(raw).hexdigest()
    normalized = normalize_gamma_resolution(
        orjson.loads(raw),
        source_payload_sha256=identity,
        normalized_at=normalized_at,
    )
    if isinstance(normalized, ResolutionRefusal):
        raise RuntimeError(f"cross-volume normalization refused: {normalized.reason.value}")
    return identity, normalized.to_record()


def run_t8(args: argparse.Namespace) -> int:
    revision = _clean_revision(Path(args.repository).resolve())
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    started_at = datetime.now(UTC)
    args.executor_started_at = started_at
    roots = (Path(args.c_root).resolve(), Path(args.d_root).resolve())
    if {root.drive.upper() for root in roots} != {"C:", "D:"}:
        raise ValueError("T8 requires one C: root and one D: root")
    payload = {
        "id": "portable-market",
        "conditionId": "portable-condition",
        "closed": True,
        "outcomePrices": '["1","0"]',
        "clobTokenIds": '["yes-token","no-token"]',
        "umaResolutionStatuses": '["resolved"]',
    }
    raw = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
    payload_identity = hashlib.sha256(raw).hexdigest()
    configuration_path = _write_executor_configuration(
        args,
        TechnicalScenario.CROSS_VOLUME,
        output,
        bounds={"workspace_count": len(roots)},
        fixture_identity=payload_identity,
        activation_identity={"required_drives": ["C:", "D:"]},
        code_revision=revision,
    )
    identities = []
    semantic_results = []
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
        path = root / "semantic-input.json"
        write_atomic_checkpoint(path, raw)
        identity, semantic_result = _process_cross_volume_input(path, normalized_at=started_at)
        identities.append(identity)
        semantic_results.append(semantic_result)
    if identities[0] != identities[1]:
        raise RuntimeError("cross-volume semantic identities differ")
    if semantic_results[0] != semantic_results[1]:
        raise RuntimeError("cross-volume normalized evidence differs")
    evidence_path = output / "evidence.json"
    _write_json(
        evidence_path,
        {
            "schema_version": "cross_volume_evidence.v1",
            "drives": [root.drive.upper() for root in roots],
            "semantic_sha256": identities[0],
            "path_independent": True,
            "normalized_evidence": semantic_results[0],
        },
    )
    _technical_result(
        args,
        TechnicalScenario.CROSS_VOLUME,
        started_at,
        output,
        evidence_path,
        "equivalent C: and D: workspaces produced identical semantic identities",
        (configuration_path,),
        tuple(
            f"sha256:{identity}:{label}-semantic-input.json"
            for label, identity in zip(("c", "d"), identities, strict=True)
        ),
    )
    return 0


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
    t2 = commands.add_parser("run-t2")
    t2.add_argument("--campaign-id", required=True)
    t2.add_argument("--token-id", action="append", required=True)
    t2.add_argument("--output", required=True)
    t2.add_argument("--repository", default=".")
    t2.set_defaults(
        handler=run_t2,
        max_seconds=7_200,
        max_frames=100_000,
        sample_interval=T2_EXPECTED_SAMPLE_INTERVAL_SECONDS,
        max_sample_gap=T2_MAXIMUM_SAMPLE_GAP_SECONDS,
        max_rss_bytes=T2_MAXIMUM_RESIDENT_MEMORY_BYTES,
        max_artifact_bytes=T2_MAXIMUM_ARTIFACT_BYTES,
    )
    t3 = commands.add_parser("run-t3")
    t3.add_argument("--campaign-id", required=True)
    t3.add_argument("--token-id", action="append", required=True)
    t3.add_argument("--output", required=True)
    t3.add_argument("--repository", default=".")
    t3.set_defaults(
        handler=run_t3,
        max_seconds=T3_MAXIMUM_DURATION_SECONDS,
        capture_seconds=T3_CAPTURE_DURATION_SECONDS,
        max_frames=T3_MAXIMUM_FRAME_COUNT,
        checkpoint_interval=T3_EXPECTED_CHECKPOINT_INTERVAL_SECONDS,
        max_checkpoint_gap=T3_MAXIMUM_CHECKPOINT_GAP_SECONDS,
        max_rss_bytes=T3_MAXIMUM_RESIDENT_MEMORY_BYTES,
        max_artifact_bytes=T3_MAXIMUM_ARTIFACT_BYTES,
    )
    fault = commands.add_parser("run-fault")
    fault.add_argument("--campaign-id", required=True)
    fault.add_argument(
        "--scenario",
        required=True,
        choices=[
            TechnicalScenario.NETWORK_INTERRUPTION.value,
            TechnicalScenario.PROCESS_INTERRUPTION.value,
            TechnicalScenario.STORAGE_INTERRUPTION.value,
        ],
    )
    fault.add_argument("--output", required=True)
    fault.add_argument("--repository", default=".")
    fault.set_defaults(handler=run_fault_scenario)
    t7 = commands.add_parser("run-t7")
    t7.add_argument("--campaign-id", required=True)
    t7.add_argument("--output", required=True)
    t7.add_argument("--repository", default=".")
    t7.set_defaults(handler=run_t7)
    t8 = commands.add_parser("run-t8")
    t8.add_argument("--campaign-id", required=True)
    t8.add_argument("--output", required=True)
    t8.add_argument("--c-root", required=True)
    t8.add_argument("--d-root", required=True)
    t8.add_argument("--repository", default=".")
    t8.set_defaults(handler=run_t8)
    args = parser.parse_args()
    if args.command in {"run-t1", "run-t2", "run-t3"} and (
        len(args.token_id) != 2 or len(set(args.token_id)) != 2
    ):
        parser.error(f"{args.command} requires exactly two distinct --token-id values")
    try:
        return int(args.handler(args))
    except Exception as error:
        if args.command in {"run-fault", "run-t7", "run-t8"}:
            return _materialize_executor_failure(args, error)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
