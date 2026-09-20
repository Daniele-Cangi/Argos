from __future__ import annotations

import argparse
import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

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
