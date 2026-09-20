from __future__ import annotations

import argparse
import importlib.util
import subprocess
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
                            "accepted": 2,
                            "duplicate": 0,
                            "rejected": 0,
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
