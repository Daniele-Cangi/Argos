"""The published technical aggregate must remain tied to the archived run."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROOF = ROOT / "experiments/m4-technical-20260926-v1/proof"


def _verify(proof: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/m4_technical_proof.py"),
            "verify",
            "--proof",
            str(proof),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_committed_proof_verifies() -> None:
    completed = _verify(PROOF)
    assert completed.returncode == 0, completed.stderr


def test_digest_valid_but_false_aggregate_is_refused(tmp_path: Path) -> None:
    for name in (
        "campaign-evidence.zip",
        "file-manifest.json",
        "proof-index.json",
        "proof-index-v2.json",
        "technical-campaign.json",
    ):
        shutil.copyfile(PROOF / name, tmp_path / name)
    aggregate = json.loads((tmp_path / "technical-campaign.json").read_bytes())
    aggregate["passed_count"] = 7
    changed = (json.dumps(aggregate, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (tmp_path / "technical-campaign.json").write_bytes(changed)
    index = json.loads((tmp_path / "proof-index-v2.json").read_bytes())
    index["aggregate"]["sha256"] = hashlib.sha256(changed).hexdigest()
    index["aggregate"]["size"] = len(changed)
    (tmp_path / "proof-index-v2.json").write_bytes(
        (json.dumps(index, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    completed = _verify(tmp_path)
    assert completed.returncode != 0
    assert "declared status counts" in completed.stderr


def test_supersession_cannot_discard_limitations(tmp_path: Path) -> None:
    for name in ("proof-index.json", "proof-index-v2.json"):
        shutil.copyfile(PROOF / name, tmp_path / name)
    index = json.loads((tmp_path / "proof-index-v2.json").read_bytes())
    index["limitations"] = []
    (tmp_path / "proof-index-v2.json").write_bytes(
        (json.dumps(index, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    completed = _verify(tmp_path)
    assert completed.returncode != 0
    assert "supersession changed limitations" in completed.stderr
