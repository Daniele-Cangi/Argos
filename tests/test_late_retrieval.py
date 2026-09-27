"""Adversarial persistence checks for per-poll Gamma retrieval evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
import pytest
from pydantic import ValidationError

from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.evaluation.late_retrieval import (
    LifecyclePollRetrievalV1,
    build_lifecycle_poll_retrieval_id,
    load_lifecycle_poll_retrievals,
)
from argos.evaluation.prospective import EvidenceArtifactKind, persist_evidence_record
from argos.store.raw_archive import write_raw_payload

RETRIEVED_AT = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
RAW = b'{"id":"market-1"}'


def _retrieval(
    *,
    target_id: str = "target-1",
    attempted_at: datetime = RETRIEVED_AT - timedelta(seconds=1),
) -> LifecyclePollRetrievalV1:
    provenance = SourceProvenanceV1(
        source="gamma",
        endpoint="https://gamma-api.polymarket.com/markets/market-1",
        http_status=200,
        retrieved_at=RETRIEVED_AT,
        raw_sha256=sha256_hex(RAW),
        byte_length=len(RAW),
    )
    fields = {
        "schedule_id": "schedule-1",
        "experiment_id": "experiment-1",
        "target_id": target_id,
        "ordinal": 1,
        "observation_id": "observation-1",
        "attempted_at": attempted_at,
        "provenance": provenance,
    }
    return LifecyclePollRetrievalV1(
        retrieval_id=build_lifecycle_poll_retrieval_id(**fields), **fields
    )


def _persist(directory: Path, retrieval: LifecyclePollRetrievalV1) -> None:
    persist_evidence_record(
        directory,
        record=retrieval,
        experiment_id=retrieval.experiment_id,
        artifact_kind=EvidenceArtifactKind.LIFECYCLE_POLL_RETRIEVAL,
        artifact_id=retrieval.retrieval_id,
        persisted_at=RETRIEVED_AT + timedelta(seconds=1),
    )


@pytest.mark.parametrize("mutation", ["foreign_source", "reconstructed", "backdated", "wrong_id"])
def test_retrieval_contract_rejects_inconsistent_provenance(mutation: str) -> None:
    retrieval = _retrieval()
    if mutation == "foreign_source":
        update = {"provenance": retrieval.provenance.model_copy(update={"source": "other"})}
        message = "first-hand Gamma provenance"
    elif mutation == "reconstructed":
        update = {"provenance": retrieval.provenance.model_copy(update={"reconstructed": True})}
        message = "first-hand Gamma provenance"
    elif mutation == "backdated":
        update = {"attempted_at": RETRIEVED_AT + timedelta(seconds=1)}
        message = "precedes its poll attempt"
    else:
        update = {"retrieval_id": "wrong-id"}
        message = "identity disagrees"
    with pytest.raises(ValidationError, match=message):
        retrieval.model_copy(update=update)


@pytest.mark.parametrize("raw,message", [(b"{", "malformed JSON"), (b"[]", "not a JSON object")])
def test_retrieval_loader_rejects_invalid_evidence_objects(
    tmp_path: Path, raw: bytes, message: str
) -> None:
    provenance = SourceProvenanceV1(
        source="argos_evidence",
        endpoint="argos-evidence://invalid/test",
        retrieved_at=RETRIEVED_AT,
        raw_sha256=sha256_hex(raw),
        byte_length=len(raw),
    )
    write_raw_payload(tmp_path, raw=raw, provenance=provenance)
    with pytest.raises(ValueError, match=message):
        load_lifecycle_poll_retrievals(tmp_path, experiment_id="experiment-1", target_id="target-1")


def test_retrieval_loader_ignores_other_targets(tmp_path: Path) -> None:
    _persist(tmp_path, _retrieval(target_id="target-2"))
    assert not load_lifecycle_poll_retrievals(
        tmp_path, experiment_id="experiment-1", target_id="target-1"
    )


def test_retrieval_loader_rejects_wrong_archive_provenance(tmp_path: Path) -> None:
    retrieval = _retrieval()
    raw = orjson.dumps(retrieval.to_record(), option=orjson.OPT_SORT_KEYS)
    wrong_provenance = SourceProvenanceV1(
        source="argos_evidence",
        endpoint="argos-evidence://wrong/retrieval",
        retrieved_at=RETRIEVED_AT,
        raw_sha256=sha256_hex(raw),
        byte_length=len(raw),
    )
    write_raw_payload(tmp_path, raw=raw, provenance=wrong_provenance)
    with pytest.raises(ValueError, match="not first-hand canonical"):
        load_lifecycle_poll_retrievals(tmp_path, experiment_id="experiment-1", target_id="target-1")


def test_retrieval_loader_rejects_two_proofs_for_one_observation(tmp_path: Path) -> None:
    _persist(tmp_path, _retrieval())
    _persist(tmp_path, _retrieval(attempted_at=RETRIEVED_AT - timedelta(seconds=2)))
    with pytest.raises(ValueError, match="duplicate late retrieval evidence"):
        load_lifecycle_poll_retrievals(tmp_path, experiment_id="experiment-1", target_id="target-1")
