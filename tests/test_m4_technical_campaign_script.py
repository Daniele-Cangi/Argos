from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def _load_script() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "m4_technical_campaign.py"
    spec = importlib.util.spec_from_file_location("m4_technical_campaign", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_json_command_can_preserve_the_documented_interruption_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script()
    completed = subprocess.CompletedProcess(
        ["argos", "capture"], 130, stdout='{"interrupted":true}', stderr=""
    )
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: completed)
    assert module._run_json(
        completed.args, cwd=tmp_path, allowed_exit_codes=frozenset({0, 130})
    ) == {"interrupted": True}


def test_json_command_still_raises_for_an_unexpected_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script()
    completed = subprocess.CompletedProcess(["argos", "replay"], 2, stdout="", stderr="bad")
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: completed)
    with pytest.raises(subprocess.CalledProcessError):
        module._run_json(completed.args, cwd=tmp_path)


def test_atomic_json_write_syncs_the_containing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script()
    synced: list[Path] = []
    monkeypatch.setattr(module, "_fsync_directory", synced.append)
    destination = tmp_path / "evidence.json"
    module._write_json(destination, {"value": 1})
    assert destination.read_text(encoding="utf-8") == '{\n  "value": 1\n}'
    assert synced == [tmp_path]


def test_artifact_size_accounts_for_nested_files(tmp_path: Path) -> None:
    module = _load_script()
    (tmp_path / "one.bin").write_bytes(b"123")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "two.bin").write_bytes(b"4567")
    assert module._artifact_bytes(tmp_path) == 7


def test_positive_integer_argument_rejects_zero() -> None:
    module = _load_script()
    assert module._positive_int("12") == 12
    with pytest.raises(argparse.ArgumentTypeError, match="positive"):
        module._positive_int("0")


@pytest.mark.parametrize(
    ("return_code", "interrupted", "expected_status"), [(0, False, "PASSED"), (130, True, "FAILED")]
)
def test_run_t2_materializes_evidence_from_a_controlled_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    return_code: int,
    interrupted: bool,
    expected_status: str,
) -> None:
    module = _load_script()
    output = tmp_path / "t2"

    class FakeProcess:
        pid = 42
        args = ("fake-capture",)

        def __init__(self, command, *, cwd, stdout, stderr):
            del command, cwd, stderr
            database = output / "events.sqlite3"
            database.write_bytes(b"sqlite-evidence")
            manifest = output / "campaign-t2.manifest.json"
            manifest.write_bytes(b"{}")
            raw = output / "raw" / "clob_market_ws" / "a.raw.json"
            raw.parent.mkdir(parents=True)
            raw.write_bytes(b"raw")
            stdout.write(
                module.orjson.dumps(
                    {
                        "capture_run_id": "campaign-t2",
                        "interrupted": interrupted,
                        "manifest_path": str(manifest),
                        "loop_health": {
                            "frames_consumed": 1,
                            "events_seen": 1,
                            "decode_failures": 0,
                            "accepted": 2,
                            "duplicate": 0,
                            "rejected": 0,
                            "not_applicable": 0,
                            "unknown_event_type": 0,
                        },
                        "store_counts": {"accepted": 2, "duplicate": 0, "rejected": 0},
                    }
                )
            )
            stdout.flush()

        def wait(self, timeout):
            del timeout
            return return_code

    class FakeStore:
        def get_capture_run(self, run_id):
            assert run_id == "campaign-t2"
            return SimpleNamespace(completion_status=module.CompletionStatus.COMPLETED)

        def close(self):
            return None

    monkeypatch.setattr(module, "_clean_revision", lambda root: "a" * 40)
    monkeypatch.setattr(module.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(module, "_tree_resident_bytes", lambda pid, tracked: 123)
    monkeypatch.setattr(module, "_resident_bytes", lambda processes: 0)
    monkeypatch.setattr(module, "open_sqlite_event_store", lambda path: FakeStore())
    args = Namespace(
        repository=tmp_path,
        output=output,
        campaign_id="campaign",
        token_id=["1", "2"],
        max_seconds=7_200,
        max_frames=100_000,
        sample_interval=60,
        max_sample_gap=120,
        max_rss_bytes=536_870_912,
        max_artifact_bytes=2_147_483_648,
    )
    assert module.run_t2(args) == (0 if expected_status == "PASSED" else 1)
    result = module.orjson.loads((output / "t2-result.json").read_bytes())
    evidence = module.orjson.loads((output / "t2-evidence.json").read_bytes())
    assert result["status"] == expected_status
    assert len(evidence["samples"]) == 2
    assert evidence["raw_payload_count"] == 1


def test_run_t3_materializes_and_reloads_a_terminal_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script()
    output = tmp_path / "t3"
    capture_commands: list[list[str]] = []

    class FakeProcess:
        pid = 42
        args = ("fake-capture",)

        def __init__(self, command, *, cwd, stdout, stderr):
            capture_commands.append(command)
            del cwd, stderr
            (output / "events.sqlite3").write_bytes(b"sqlite-evidence")
            manifest = output / "campaign-t3.manifest.json"
            manifest.write_bytes(b"{}")
            raw = output / "raw" / "clob_market_ws" / "a.raw.json"
            raw.parent.mkdir(parents=True)
            raw.write_bytes(b"raw")
            stdout.write(
                module.orjson.dumps(
                    {
                        "capture_run_id": "campaign-t3",
                        "interrupted": False,
                        "manifest_path": str(manifest),
                        "loop_health": {
                            "frames_consumed": 1,
                            "decode_failures": 0,
                            "accepted": 2,
                            "duplicate": 0,
                            "rejected": 0,
                            "unknown_event_type": 0,
                        },
                        "store_counts": {"accepted": 2, "duplicate": 0, "rejected": 0},
                    }
                )
            )
            stdout.flush()

        def wait(self, timeout):
            del timeout
            return 0

    class FakeStore:
        def get_capture_run(self, run_id):
            assert run_id == "campaign-t3"
            return SimpleNamespace(completion_status=module.CompletionStatus.COMPLETED)

        def close(self):
            return None

    monkeypatch.setattr(module, "_clean_revision", lambda root: "a" * 40)
    monkeypatch.setattr(module.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(module, "_tree_resident_bytes", lambda pid, tracked: 123)
    monkeypatch.setattr(module, "_resident_bytes", lambda processes: 0)
    monkeypatch.setattr(module, "open_sqlite_event_store", lambda path: FakeStore())
    args = Namespace(
        repository=tmp_path,
        output=output,
        campaign_id="campaign",
        token_id=["1", "2"],
        max_seconds=module.T3_MAXIMUM_DURATION_SECONDS,
        capture_seconds=module.T3_CAPTURE_DURATION_SECONDS,
        max_frames=module.T3_MAXIMUM_FRAME_COUNT,
        checkpoint_interval=module.T3_EXPECTED_CHECKPOINT_INTERVAL_SECONDS,
        max_checkpoint_gap=module.T3_MAXIMUM_CHECKPOINT_GAP_SECONDS,
        max_rss_bytes=module.T3_MAXIMUM_RESIDENT_MEMORY_BYTES,
        max_artifact_bytes=module.T3_MAXIMUM_ARTIFACT_BYTES,
    )
    assert module.run_t3(args) == 0
    result = module.orjson.loads((output / "t3-result.json").read_bytes())
    evidence = module.orjson.loads((output / "t3-evidence.json").read_bytes())
    index = module.orjson.loads((output / "checkpoint-index.json").read_bytes())
    assert result["status"] == "PASSED"
    assert evidence["persisted_checkpoint_chain_reloaded"] is True
    assert evidence["terminal_resume_verified"] is True
    assert [item["state"] for item in evidence["checkpoints"]] == ["RUNNING", "COMPLETED"]
    assert len(index["checkpoints"]) == 2
    configuration = module.orjson.loads((output / "configuration.json").read_bytes())
    assert configuration["maximum_duration_seconds"] == 21_600
    assert configuration["capture_duration_seconds"] == 21_540
    max_seconds_index = capture_commands[0].index("--max-seconds")
    assert capture_commands[0][max_seconds_index + 1] == "21540"


def test_run_t3_refuses_modified_frozen_bounds(tmp_path: Path) -> None:
    module = _load_script()
    args = Namespace(
        repository=tmp_path,
        output=tmp_path / "t3",
        campaign_id="campaign",
        token_id=["1", "2"],
        max_seconds=60,
        capture_seconds=module.T3_CAPTURE_DURATION_SECONDS,
        max_frames=module.T3_MAXIMUM_FRAME_COUNT,
        checkpoint_interval=module.T3_EXPECTED_CHECKPOINT_INTERVAL_SECONDS,
        max_checkpoint_gap=module.T3_MAXIMUM_CHECKPOINT_GAP_SECONDS,
        max_rss_bytes=module.T3_MAXIMUM_RESIDENT_MEMORY_BYTES,
        max_artifact_bytes=module.T3_MAXIMUM_ARTIFACT_BYTES,
    )
    with pytest.raises(ValueError, match="frozen protocol"):
        module.run_t3(args)
    args.max_seconds = module.T3_MAXIMUM_DURATION_SECONDS
    args.capture_seconds -= 1
    with pytest.raises(ValueError, match="frozen protocol"):
        module.run_t3(args)


def test_t3_reload_refuses_missing_or_unexpected_checkpoint_files(tmp_path: Path) -> None:
    module = _load_script()
    checkpoint = module.EnduranceCheckpointV1(
        ordinal=0,
        observed_at=module.datetime.now(module.UTC),
        state="COMPLETED",
        resident_memory_bytes=0,
        artifact_bytes=0,
    )
    module._write_json(tmp_path / "000000.json", checkpoint.to_record())
    assert module._reload_endurance_checkpoints(tmp_path, 1) == (checkpoint,)
    (tmp_path / "unexpected.json").write_bytes(b"{}")
    with pytest.raises(ValueError, match="incomplete or unexpected"):
        module._reload_endurance_checkpoints(tmp_path, 1)


def test_t3_terminal_resume_probe_rejects_nonterminal_or_duplicate_state() -> None:
    module = _load_script()
    running = module.EnduranceCheckpointV1(
        ordinal=0,
        observed_at=module.datetime.now(module.UTC),
        state="RUNNING",
        resident_memory_bytes=0,
        artifact_bytes=0,
    )
    assert module._probe_terminal_resume(()) is False
    assert module._probe_terminal_resume((running,)) is False
    terminal = running.model_copy(update={"state": "COMPLETED"})
    assert module._probe_terminal_resume((terminal,)) is True
    assert module._probe_terminal_resume((terminal, terminal)) is False


def _run_campaign_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    script = Path(__file__).parents[1] / "scripts" / "m4_technical_campaign.py"
    return subprocess.run(
        [sys.executable, str(script), *arguments],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        (
            "T4_NETWORK_INTERRUPTION",
            {
                "injected_error": "InjectedNetworkLoss",
                "failed_next_ordinal": 0,
                "explicit_gap_count": 1,
                "resumed_next_ordinal": 1,
            },
        ),
        (
            "T5_PROCESS_INTERRUPTION",
            {
                "injected_error": "InjectedProcessTermination",
                "failed_next_ordinal": 1,
                "resumed_next_ordinal": 2,
                "duplicate_owner_rejected": True,
            },
        ),
        (
            "T6_STORAGE_INTERRUPTION",
            {
                "injected_error": "InjectedStorageRefusal",
                "failed_next_ordinal": 0,
                "explicit_gap_count": 0,
                "resumed_next_ordinal": 1,
            },
        ),
    ],
)
def test_fault_cli_materializes_a_passing_result(
    tmp_path: Path, scenario: str, expected: dict[str, object]
) -> None:
    output = tmp_path / scenario.lower()
    completed = _run_campaign_cli(
        "run-fault",
        "--campaign-id",
        "test-campaign",
        "--scenario",
        scenario,
        "--output",
        str(output),
        "--repository",
        str(Path(__file__).parents[1]),
    )
    assert completed.returncode == 0, completed.stderr
    result = next(output.glob("*-result.json"))
    result_record = module_orjson(result)
    assert result_record["status"] == "PASSED"
    assert len(result_record["artifact_identities"]) == 3
    evidence = module_orjson(output / "evidence.json")
    assert evidence["partial_checkpoint_absent"] is True
    assert evidence["exclusive_owner"] is True
    for key, value in expected.items():
        assert evidence[key] == value
    if scenario == "T5_PROCESS_INTERRUPTION":
        assert evidence["process_exit_code"] is not None


def module_orjson(path: Path) -> dict[str, object]:
    import orjson

    value = orjson.loads(path.read_bytes())
    assert isinstance(value, dict)
    return value


def test_t7_cli_drives_real_resolution_normalization(tmp_path: Path) -> None:
    output = tmp_path / "t7"
    completed = _run_campaign_cli(
        "run-t7", "--campaign-id", "test-campaign", "--output", str(output)
    )
    assert completed.returncode == 0, completed.stderr
    evidence = module_orjson(output / "evidence.json")
    handled = evidence["handled_outcomes"]
    assert isinstance(handled, list)
    assert handled[-1] == {
        "refusal": "no_determinable_outcome",
        "state": "ADMINISTRATIVE_CLOSE",
    }


def test_t8_cli_materializes_failure_for_non_cross_volume_paths(tmp_path: Path) -> None:
    output = tmp_path / "t8"
    completed = _run_campaign_cli(
        "run-t8",
        "--campaign-id",
        "test-campaign",
        "--output",
        str(output),
        "--c-root",
        str(tmp_path / "one"),
        "--d-root",
        str(tmp_path / "two"),
    )
    assert completed.returncode == 1
    result = module_orjson(output / "t8_cross_volume-result.json")
    assert result["status"] == "FAILED"
    assert "requires one C: root and one D: root" in str(result["reason"])


def test_cross_volume_processing_is_path_independent(tmp_path: Path) -> None:
    module = _load_script()
    raw = module.orjson.dumps(
        {
            "id": "portable-market",
            "conditionId": "portable-condition",
            "closed": True,
            "outcomePrices": '["1","0"]',
            "clobTokenIds": '["yes-token","no-token"]',
            "umaResolutionStatuses": '["resolved"]',
        },
        option=module.orjson.OPT_SORT_KEYS,
    )
    paths = (tmp_path / "one" / "input.json", tmp_path / "two" / "input.json")
    for path in paths:
        path.parent.mkdir()
        path.write_bytes(raw)
    now = module.datetime.now(module.UTC)
    first = module._process_cross_volume_input(paths[0], normalized_at=now)
    second = module._process_cross_volume_input(paths[1], normalized_at=now)
    assert first == second


def test_output_reuse_preserves_existing_result(tmp_path: Path) -> None:
    output = tmp_path / "t7"
    first = _run_campaign_cli(
        "run-t7", "--campaign-id", "test-campaign", "--output", str(output)
    )
    assert first.returncode == 0, first.stderr
    result_path = output / "t7_terminal_simulation-result.json"
    original = result_path.read_bytes()
    second = _run_campaign_cli(
        "run-t7", "--campaign-id", "test-campaign", "--output", str(output)
    )
    assert second.returncode == 1
    assert result_path.read_bytes() == original
    assert "refusing to overwrite existing result artifact" in second.stderr
