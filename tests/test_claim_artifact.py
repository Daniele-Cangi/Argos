"""Repository-level verification for the published M4 claim artifact."""

from __future__ import annotations

from pathlib import Path
from shutil import copy2

import orjson
import pytest

from argos.evaluation.claim_artifact import (
    ProspectiveClaimArtifactIndexV1,
    verify_published_claim_artifact,
)

PROOF_DIR = Path(__file__).resolve().parents[1] / "experiments" / "m4-pilot-20260819" / "proof"


def _index(directory: Path = PROOF_DIR) -> ProspectiveClaimArtifactIndexV1:
    return ProspectiveClaimArtifactIndexV1.from_record(
        orjson.loads((directory / "claim-index-v1.json").read_bytes())
    )


def test_published_claim_artifact_is_complete_and_verifiable() -> None:
    bundle = verify_published_claim_artifact(_index(), PROOF_DIR)

    assert bundle.protocol.experiment_id == "m4-pilot-20260819-v2"
    assert bundle.evidence_digest == (
        "48f535a8b355e41cb879c1af46adef3e1798d402d31ac3d79a7216ba4e1cea03"
    )
    assert len(bundle.target_exclusions) == 2


def test_one_changed_artifact_byte_is_rejected(tmp_path: Path) -> None:
    copy2(PROOF_DIR / "claim-index-v1.json", tmp_path / "claim-index-v1.json")
    copy2(PROOF_DIR / "claim-bundle-v2.json", tmp_path / "claim-bundle-v2.json")
    artifact = tmp_path / "claim-bundle-v2.json"
    raw = bytearray(artifact.read_bytes())
    raw[-1] = ord(" ")
    artifact.write_bytes(raw)

    with pytest.raises(ValueError, match="bytes disagree"):
        verify_published_claim_artifact(_index(tmp_path), tmp_path)
