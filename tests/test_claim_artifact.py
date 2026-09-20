"""Repository-level verification for the published M4 claim artifact."""

from __future__ import annotations

from pathlib import Path
from shutil import copy2
from typing import Any

import orjson
import pytest

from argos.domain.provenance import sha256_hex
from argos.evaluation.claim_artifact import (
    ProspectiveClaimArtifactIndexV1,
    verify_published_claim_artifact,
)
from argos.evaluation.prospective import EvidencePersistenceReceiptV1

PROOF_DIR = Path(__file__).resolve().parents[1] / "experiments" / "m4-pilot-20260819" / "proof"
V8_PROOF_DIR = (
    Path(__file__).resolve().parents[1] / "experiments" / "m4-prospective-20260917-v8" / "proof"
)


def _index(directory: Path = PROOF_DIR) -> ProspectiveClaimArtifactIndexV1:
    return ProspectiveClaimArtifactIndexV1.from_record(
        orjson.loads((directory / "claim-index-v1.json").read_bytes())
    )


def _unchecked_index(
    index: ProspectiveClaimArtifactIndexV1,
    **updates: Any,
) -> ProspectiveClaimArtifactIndexV1:
    values = {name: getattr(index, name) for name in type(index).model_fields}
    values.update(updates)
    return ProspectiveClaimArtifactIndexV1.model_construct(**values)


def _unchecked_receipt(
    receipt: EvidencePersistenceReceiptV1,
    **updates: Any,
) -> EvidencePersistenceReceiptV1:
    values = {name: getattr(receipt, name) for name in type(receipt).model_fields}
    values.update(updates)
    return EvidencePersistenceReceiptV1.model_construct(**values)


def test_published_claim_artifact_is_complete_and_verifiable() -> None:
    index = _index()
    bundle = verify_published_claim_artifact(index, PROOF_DIR)

    assert ProspectiveClaimArtifactIndexV1.from_record(index.to_record()) == index
    assert bundle.protocol.experiment_id == "m4-pilot-20260819-v2"
    assert bundle.evidence_digest == (
        "48f535a8b355e41cb879c1af46adef3e1798d402d31ac3d79a7216ba4e1cea03"
    )
    assert len(bundle.target_exclusions) == 2


def test_v8_terminal_claim_is_complete_and_verifiable() -> None:
    index = _index(V8_PROOF_DIR)
    bundle = verify_published_claim_artifact(index, V8_PROOF_DIR)

    assert bundle.protocol.experiment_id == "m4-prospective-20260917-v8"
    assert bundle.evidence_digest == (
        "3a705b4a14735962b5d1035ad64d3aeaf8d9ac6a1583c179adb27b2fa8df2624"
    )
    assert bundle.report.target_accounting_status.value == "TARGET_ACCOUNTING_COMPLETE"
    assert bundle.report.lifecycle_continuity_status.value == "LIFECYCLE_CONTINUITY_INCOMPLETE"
    assert bundle.report.resolution_admissibility_status.value == "NO_ADMISSIBLE_CUTOFF_OBSERVED"
    assert bundle.report.measurement_layer_verdict.value == "M4_BLOCKED"
    assert bundle.report.calibration_verdict.value == "CALIBRATION_NOT_EVALUABLE"
    assert tuple(len(target.lifecycle_polls) for target in bundle.terminal_targets) == (
        554,
        554,
    )


def test_one_changed_artifact_byte_is_rejected(tmp_path: Path) -> None:
    copy2(PROOF_DIR / "claim-index-v1.json", tmp_path / "claim-index-v1.json")
    copy2(PROOF_DIR / "claim-bundle-v2.json", tmp_path / "claim-bundle-v2.json")
    artifact = tmp_path / "claim-bundle-v2.json"
    raw = bytearray(artifact.read_bytes())
    raw[-1] = ord(" ")
    artifact.write_bytes(raw)

    with pytest.raises(ValueError, match="bytes disagree"):
        verify_published_claim_artifact(_index(tmp_path), tmp_path)


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("bundle_relative_path", "/outside.json", "paths must stay relative"),
        ("published_from_storage_identity", "../outside.json", "paths must stay relative"),
        ("bundle_evidence_digest", "g" * 64, "digests must be hexadecimal"),
        ("experiment_id", "other-experiment", "index disagrees"),
    ],
)
def test_claim_index_rejects_false_identity_fields(
    field_name: str,
    value: str,
    message: str,
) -> None:
    record = _index().to_record()
    record[field_name] = value

    with pytest.raises(ValueError, match=message):
        ProspectiveClaimArtifactIndexV1.from_record(record)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"[]", "not a JSON object"),
        (b'{"schema_version":"unsupported.v1"}', "unsupported bundle schema"),
    ],
)
def test_published_claim_rejects_non_bundle_json(
    tmp_path: Path,
    raw: bytes,
    message: str,
) -> None:
    artifact = tmp_path / "attacked.json"
    artifact.write_bytes(raw)
    index = _unchecked_index(
        _index(),
        bundle_relative_path=artifact.name,
        bundle_artifact_sha256=sha256_hex(raw),
        bundle_byte_length=len(raw),
    )

    with pytest.raises(ValueError, match=message):
        verify_published_claim_artifact(index, tmp_path)


def test_published_claim_rejects_false_index_and_receipt_identity(tmp_path: Path) -> None:
    copy2(PROOF_DIR / "claim-bundle-v2.json", tmp_path / "claim-bundle-v2.json")
    index = _unchecked_index(_index(), experiment_id="other-experiment")
    with pytest.raises(ValueError, match="identity disagrees"):
        verify_published_claim_artifact(index, tmp_path)

    bad_receipt = _unchecked_receipt(index.aggregate_receipt, artifact_sha256="0" * 64)
    index = _unchecked_index(_index(), aggregate_receipt=bad_receipt)
    with pytest.raises(ValueError, match="persistence receipt disagrees"):
        verify_published_claim_artifact(index, tmp_path)


def test_published_claim_rejects_symlink_and_parent_escape(tmp_path: Path) -> None:
    link = tmp_path / "claim-link.json"
    try:
        link.symlink_to(PROOF_DIR / "claim-bundle-v2.json")
    except OSError:
        pytest.skip("symlink creation is unavailable")
    index = _unchecked_index(_index(), bundle_relative_path=link.name)
    with pytest.raises(ValueError, match="symbolic link"):
        verify_published_claim_artifact(index, tmp_path)

    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    copy2(PROOF_DIR / "claim-bundle-v2.json", outside / "claim-bundle-v2.json")
    parent_link = tmp_path / "outside-link"
    parent_link.symlink_to(outside, target_is_directory=True)
    index = _unchecked_index(
        _index(),
        bundle_relative_path="outside-link/claim-bundle-v2.json",
    )
    with pytest.raises(ValueError, match="outside its index directory"):
        verify_published_claim_artifact(index, tmp_path)
