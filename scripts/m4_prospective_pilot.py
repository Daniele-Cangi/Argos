"""Freeze, poll and materialize the bounded public M4 prospective pilot.

The capture itself deliberately stays on the shipped ``argos capture market``
path. This helper owns only evidence that must exist before capture and the
one-shot lifecycle polls that may later establish a cutoff.
"""

from __future__ import annotations

import argparse
import itertools
import subprocess
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import orjson

from argos.clock import LiveClock, ensure_utc
from argos.compiler import CompiledMarketContractV1, compile_market_contract
from argos.config.manifest import RunManifest
from argos.domain.market import MarketDefinitionV1
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.errors import RejectionReason
from argos.evaluation.bundle import record_sha256
from argos.evaluation.claim_artifact import (
    ProspectiveClaimArtifactIndexV1,
    verify_published_claim_artifact,
)
from argos.evaluation.prospective import (
    CutoffBasis,
    EvidenceArtifactKind,
    EvidencePersistenceReceiptV1,
    LifecycleObservationV1,
    ProspectiveExperimentProtocolV1,
    ProspectiveExperimentProtocolV2,
    ProspectiveTargetV1,
    ResolutionCutoffEvidenceV1,
    build_cutoff_evidence_id,
    build_lifecycle_observation_id,
    build_target_id,
    persist_evidence_record,
)
from argos.evaluation.prospective_aggregation_v2 import (
    ProspectiveExperimentBundleV2,
    ProspectiveTargetExclusionV2,
    aggregate_prospective_experiment_v2,
    build_capture_rejection_evidence,
    build_target_exclusion_v2,
)
from argos.evaluation.prospective_aggregation_v3 import (
    aggregate_prospective_experiment_v3,
)
from argos.evaluation.prospective_terminal import (
    CaptureRunSummaryV1,
    LifecyclePollEvidenceV1,
    ProspectiveTargetTerminalEvidenceV1,
    aggregate_prospective_terminal_experiment,
    build_capture_run_summary,
    build_lifecycle_poll_evidence,
    build_target_terminal_evidence,
)
from argos.ingestion.gamma_markets import normalize_markets
from argos.resolution import (
    ResolutionStatus,
    ResolutionV1,
    normalize_gamma_resolution,
)
from argos.store import (
    CompletionStatus,
    open_sqlite_event_store,
    read_raw_payload,
)
from argos.store.raw_archive import archive_relative_location, write_raw_payload

MAX_RESPONSE_BYTES = 32 * 1024 * 1024
USER_AGENT = "argos-research (public read-only prospective pilot)"


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze = subparsers.add_parser("freeze")
    freeze.add_argument("--spec", type=Path, required=True)
    freeze.add_argument("--experiment-dir", type=Path, required=True)
    poll = subparsers.add_parser("poll")
    poll.add_argument("--experiment-dir", type=Path, required=True)
    materialize = subparsers.add_parser("materialize-exclusions")
    materialize.add_argument("--experiment-dir", type=Path, required=True)
    terminal = subparsers.add_parser("materialize-terminal")
    terminal.add_argument("--experiment-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "freeze":
        return freeze_experiment(args.spec, args.experiment_dir)
    if args.command == "poll":
        return poll_experiment(args.experiment_dir)
    if args.command == "materialize-terminal":
        return materialize_terminal(args.experiment_dir)
    return materialize_exclusions(args.experiment_dir)


def freeze_experiment(spec_path: Path, experiment_dir: Path) -> int:
    revision = _clean_revision()
    spec = _object(orjson.loads(spec_path.read_bytes()), "protocol specification")
    protocol_fields = _object(spec.get("protocol"), "protocol")
    if protocol_fields.get("code_revision") != "$PROTOCOL_COMMIT":
        raise ValueError("the committed protocol must resolve code_revision mechanically")
    capture_config = _object(spec.get("capture"), "capture configuration")
    protocol_fields.update(
        {
            "lifecycle_deadline": capture_config.get("lifecycle_deadline"),
            "lifecycle_poll_interval_seconds": capture_config.get(
                "lifecycle_poll_interval_seconds"
            ),
            "capture_max_seconds_per_target": capture_config.get("max_seconds_per_target"),
            "capture_max_frames_per_target": capture_config.get("max_frames_per_target"),
            "capture_separate_database_per_target": capture_config.get(
                "separate_database_per_target"
            ),
            "capture_subscribe_both_tokens": capture_config.get("subscribe_both_tokens"),
            "capture_raw_archive": capture_config.get("raw_archive"),
        }
    )
    protocol_fields["code_revision"] = revision
    protocol = ProspectiveExperimentProtocolV2.model_validate(protocol_fields)
    _validate_capture_configuration(protocol, capture_config)
    now = LiveClock().now()
    if now >= protocol.observation_window_start:
        raise ValueError("protocol freeze missed its predeclared observation start")

    evidence_dir = experiment_dir / "evidence"
    source_dir = experiment_dir / "source"
    protocol_receipt = persist_evidence_record(
        evidence_dir,
        record=protocol,
        experiment_id=protocol.experiment_id,
        artifact_kind=EvidenceArtifactKind.EXPERIMENT_PROTOCOL,
        artifact_id=protocol.experiment_id,
        persisted_at=now,
    )
    selection = _object(spec.get("selection"), "selection")
    base_url = str(selection["gamma_base_url"])
    response_raw, endpoint, retrieved_at = _public_get(
        f"{base_url.rstrip('/')}/markets",
        _object(selection.get("query"), "selection query"),
    )
    source_provenance = SourceProvenanceV1(
        source="gamma",
        endpoint=endpoint,
        http_status=200,
        retrieved_at=retrieved_at,
        raw_sha256=sha256_hex(response_raw),
        byte_length=len(response_raw),
    )
    write_raw_payload(source_dir, raw=response_raw, provenance=source_provenance)
    payload = orjson.loads(response_raw)
    if not isinstance(payload, list):
        raise ValueError("Gamma selection response is not a market list")
    normalization = normalize_markets(
        payload,
        raw_payload_sha256=source_provenance.raw_sha256,
        normalized_at=retrieved_at,
    )
    raw_by_id = {
        str(item.get("id")): item for item in payload if isinstance(item, dict) and item.get("id")
    }
    candidates = _eligible_markets(normalization.accepted, raw_by_id, selection)
    intended = protocol.minimum_intended_resolved_target_count
    if len(candidates) < intended:
        raise ValueError(f"selection produced {len(candidates)} candidates, need {intended}")

    targets: list[dict[str, Any]] = []
    used_events: set[str] = set()
    for market in candidates:
        independence_key = market.event_id or market.market_id
        if independence_key in used_events:
            continue
        used_events.add(independence_key)
        market_receipt = persist_evidence_record(
            evidence_dir,
            record=market,
            experiment_id=protocol.experiment_id,
            artifact_kind=EvidenceArtifactKind.MARKET_DEFINITION,
            artifact_id=market.market_id,
            persisted_at=LiveClock().now(),
        )
        contract = compile_market_contract(market, compiled_at=LiveClock().now())
        contract_receipt = persist_evidence_record(
            evidence_dir,
            record=contract,
            experiment_id=protocol.experiment_id,
            artifact_kind=EvidenceArtifactKind.COMPILED_CONTRACT,
            artifact_id=contract.contract_id,
            persisted_at=LiveClock().now(),
        )
        target = _target(
            protocol=protocol,
            market=market,
            market_receipt=market_receipt,
            contract=contract,
            contract_receipt=contract_receipt,
            rank=len(targets) + 1,
        )
        target_receipt = persist_evidence_record(
            evidence_dir,
            record=target,
            experiment_id=protocol.experiment_id,
            artifact_kind=EvidenceArtifactKind.TARGET_DECLARATION,
            artifact_id=target.target_id,
            persisted_at=LiveClock().now(),
        )
        targets.append(
            {
                "market": market.to_record(),
                "market_receipt": market_receipt.to_record(),
                "contract": contract.to_record(),
                "contract_receipt": contract_receipt.to_record(),
                "target": target.to_record(),
                "target_receipt": target_receipt.to_record(),
                "lifecycle": [],
                "cutoff": None,
                "resolution": None,
            }
        )
        if len(targets) == intended:
            break
    if len(targets) != intended:
        raise ValueError("not enough candidates from distinct source events")

    state = {
        "format": "m4_prospective_pilot_state.v1",
        "protocol_spec_path": spec_path.as_posix(),
        "protocol_commit": revision,
        "protocol": protocol.to_record(),
        "protocol_receipt": protocol_receipt.to_record(),
        "discovery": {
            "endpoint": endpoint,
            "retrieved_at": retrieved_at.isoformat(),
            "raw_payload_sha256": source_provenance.raw_sha256,
            "byte_length": len(response_raw),
            "raw_payload_location": archive_relative_location(source_provenance),
            "normalized_count": len(normalization.accepted),
            "quarantined_count": len(normalization.quarantined),
            "eligible_count": len(candidates),
        },
        "capture": capture_config,
        "targets": targets,
    }
    _write_state(experiment_dir / "state.json", state)
    print(orjson.dumps(state, option=orjson.OPT_INDENT_2).decode())
    return 0


def poll_experiment(experiment_dir: Path) -> int:
    _clean_revision()
    state_path = experiment_dir / "state.json"
    state = _object(orjson.loads(state_path.read_bytes()), "experiment state")
    protocol = _protocol_from_record(_object(state.get("protocol"), "protocol record"))
    deadline = _protocol_lifecycle_deadline(protocol, state)
    evidence_dir = experiment_dir / "evidence"
    source_dir = experiment_dir / "source"
    for target_state_raw in _list(state.get("targets"), "targets"):
        if LiveClock().now() > deadline:
            break
        target_state = _object(target_state_raw, "target state")
        target = ProspectiveTargetV1.from_record(
            _object(target_state.get("target"), "target record")
        )
        market = MarketDefinitionV1.from_record(
            _object(target_state.get("market"), "market record")
        )
        raw, endpoint, retrieved_at = _public_get(
            f"https://gamma-api.polymarket.com/markets/{market.market_id}", {}
        )
        provenance = SourceProvenanceV1(
            source="gamma",
            endpoint=endpoint,
            http_status=200,
            retrieved_at=retrieved_at,
            raw_sha256=sha256_hex(raw),
            byte_length=len(raw),
        )
        write_raw_payload(source_dir, raw=raw, provenance=provenance)
        payload = _object(orjson.loads(raw), "Gamma lifecycle payload")
        normalized = normalize_gamma_resolution(
            payload,
            source_payload_sha256=provenance.raw_sha256,
            normalized_at=retrieved_at,
        )
        resolution = normalized if isinstance(normalized, ResolutionV1) else None
        finality = resolution.resolution_status if resolution else _nonfinal_status(payload)
        lifecycle = _list(target_state.get("lifecycle"), "lifecycle")
        previous = (
            LifecycleObservationV1.from_record(
                _object(_object(lifecycle[-1], "lifecycle entry").get("observation"), "observation")
            )
            if lifecycle
            else None
        )
        observation = _lifecycle_observation(
            protocol=protocol,
            target=target,
            ordinal=len(lifecycle) + 1,
            previous=previous,
            provenance=provenance,
            payload=payload,
            finality=finality,
            resolution=resolution,
        )
        receipt = persist_evidence_record(
            evidence_dir,
            record=observation,
            experiment_id=protocol.experiment_id,
            artifact_kind=EvidenceArtifactKind.LIFECYCLE_OBSERVATION,
            artifact_id=observation.lifecycle_observation_id,
            persisted_at=LiveClock().now(),
        )
        lifecycle.append({"observation": observation.to_record(), "receipt": receipt.to_record()})
        target_state["lifecycle"] = lifecycle
        if resolution is not None:
            target_state["resolution"] = resolution.to_record()
        if (
            finality is ResolutionStatus.FINAL
            and observation.retrieved_at <= deadline
            and target_state.get("cutoff") is None
        ):
            assert resolution is not None
            cutoff = _cutoff(protocol, target, observation, resolution)
            cutoff_receipt = persist_evidence_record(
                evidence_dir,
                record=cutoff,
                experiment_id=protocol.experiment_id,
                artifact_kind=EvidenceArtifactKind.RESOLUTION_CUTOFF,
                artifact_id=cutoff.cutoff_evidence_id,
                persisted_at=LiveClock().now(),
            )
            target_state["cutoff"] = {
                "evidence": cutoff.to_record(),
                "receipt": cutoff_receipt.to_record(),
            }
        _write_state(state_path, state)
    print(orjson.dumps(state, option=orjson.OPT_INDENT_2).decode())
    return 0


def materialize_exclusions(experiment_dir: Path) -> int:
    """Persist proof-backed exclusions and the current aggregate verdict."""

    materializer_revision = _clean_revision()
    state_path = experiment_dir / "state.json"
    state = _object(orjson.loads(state_path.read_bytes()), "experiment state")
    protocol = _protocol_from_record(_object(state.get("protocol"), "protocol record"))
    protocol_receipt = EvidencePersistenceReceiptV1.from_record(
        _object(state.get("protocol_receipt"), "protocol receipt")
    )
    now = LiveClock().now()
    evidence_dir = experiment_dir / "evidence"
    exclusions: list[ProspectiveTargetExclusionV2] = []
    persisted_exclusions: list[dict[str, Any]] = []

    for target_state_raw in _list(state.get("targets"), "targets"):
        target_state = _object(target_state_raw, "target state")
        target = ProspectiveTargetV1.from_record(
            _object(target_state.get("target"), "target record")
        )
        target_receipt = EvidencePersistenceReceiptV1.from_record(
            _object(target_state.get("target_receipt"), "target receipt")
        )
        capture_dir = experiment_dir / "capture" / f"target-{target.selection_rank}"
        capture_run_id = f"{protocol.experiment_id}-target-{target.selection_rank}"
        manifest_path = capture_dir / f"{capture_run_id}.manifest.json"
        manifest = RunManifest.from_record(
            _object(orjson.loads(manifest_path.read_bytes()), "capture manifest")
        )

        store = open_sqlite_event_store(capture_dir / "events.sqlite3")
        try:
            run = store.get_capture_run(capture_run_id)
            if run is None or run.completion_status is not CompletionStatus.COMPLETED:
                raise ValueError("target capture is not durably completed")
            candidates = [
                item
                for item in store.iter_rejections(capture_run_id)
                if item.rejection.reason is RejectionReason.UNKNOWN_EVENT_TYPE
                and item.rejection.source_event_type == "last_trade_price"
                and item.rejection.condition_id == target.condition_id
                and item.rejection.token_id in {target.yes_token_id, target.no_token_id}
                and protocol.observation_window_start
                <= item.rejection.received_time
                <= protocol.observation_window_end
            ]
        finally:
            store.close()
        if not candidates:
            raise ValueError(
                f"target {target.target_id} has no qualifying standalone last_trade_price rejection"
            )
        first = min(
            candidates,
            key=lambda item: (
                item.rejection.received_time,
                item.ingest_sequence,
            ),
        )
        raw, raw_provenance = read_raw_payload(
            capture_dir / "raw",
            first.rejection.raw_payload_sha256,
        )
        if raw_provenance != first.rejection.provenance:
            raise ValueError("raw archive sidecar disagrees with the rejection ledger")

        raw_location = archive_relative_location(first.rejection.provenance)
        proof = build_capture_rejection_evidence(
            experiment_id=protocol.experiment_id,
            target_id=target.target_id,
            capture_run_manifest=manifest,
            ingest_sequence=first.ingest_sequence,
            rejection=first.rejection,
            raw_payload=raw,
            raw_payload_location=raw_location,
        )
        proof_receipt = persist_evidence_record(
            evidence_dir,
            record=proof,
            experiment_id=protocol.experiment_id,
            artifact_kind=EvidenceArtifactKind.CAPTURE_REJECTION,
            artifact_id=proof.evidence_id,
            persisted_at=now,
        )
        exclusion = build_target_exclusion_v2(
            experiment_id=protocol.experiment_id,
            target=target,
            target_receipt=target_receipt,
            capture_evidence=proof,
            capture_evidence_receipt=proof_receipt,
        )
        exclusion_receipt = persist_evidence_record(
            evidence_dir,
            record=exclusion,
            experiment_id=protocol.experiment_id,
            artifact_kind=EvidenceArtifactKind.TARGET_EXCLUSION,
            artifact_id=exclusion.exclusion_id,
            persisted_at=now,
        )
        exclusions.append(exclusion)
        persisted_exclusions.append(
            {
                "capture_evidence": proof.to_record(),
                "capture_evidence_receipt": proof_receipt.to_record(),
                "exclusion": exclusion.to_record(),
                "exclusion_receipt": exclusion_receipt.to_record(),
            }
        )

    capture_config = _object(state.get("capture"), "capture configuration")
    if isinstance(protocol, ProspectiveExperimentProtocolV2):
        _validate_capture_configuration(protocol, capture_config)
    lifecycle_deadline = _protocol_lifecycle_deadline(protocol, state)
    lifecycle_interval = _protocol_lifecycle_poll_interval(protocol, state)
    targets = _list(state.get("targets"), "targets")
    all_targets_final = _all_targets_have_admissible_cutoff(
        targets,
        lifecycle_deadline,
    )
    experiment_closed = now >= lifecycle_deadline
    lifecycle_record_complete = _lifecycle_record_complete(
        protocol=protocol,
        targets=targets,
        deadline=lifecycle_deadline,
        poll_interval_seconds=lifecycle_interval,
    )
    target_accounting_complete = len(exclusions) == len(targets)
    observation_complete = target_accounting_complete and (all_targets_final or experiment_closed)
    aggregate: ProspectiveExperimentBundleV2
    if isinstance(protocol, ProspectiveExperimentProtocolV2):
        aggregate = aggregate_prospective_experiment_v3(
            protocol=protocol,
            protocol_receipt=protocol_receipt,
            target_bundles=(),
            target_exclusions=exclusions,
            created_at=now,
            observation_complete=observation_complete,
        )
        result_version = 3
    else:
        aggregate = aggregate_prospective_experiment_v2(
            protocol=protocol,
            protocol_receipt=protocol_receipt,
            target_bundles=(),
            target_exclusions=exclusions,
            created_at=now,
            observation_complete=observation_complete,
        )
        result_version = 2
    aggregate_receipt = persist_evidence_record(
        evidence_dir,
        record=aggregate,
        experiment_id=protocol.experiment_id,
        artifact_kind=EvidenceArtifactKind.EXPERIMENT_AGGREGATE,
        artifact_id=aggregate.evidence_digest,
        persisted_at=now,
    )
    result = {
        "format": f"m4_prospective_pilot_result.v{result_version}",
        "experiment_id": protocol.experiment_id,
        "materialized_at": now.isoformat(),
        "materializer_revision": materializer_revision,
        "observation_complete": observation_complete,
        "target_exclusions": persisted_exclusions,
        "aggregate": aggregate.to_record(),
        "aggregate_receipt": aggregate_receipt.to_record(),
        "lifecycle_completion": {
            "frozen_deadline": lifecycle_deadline.isoformat(),
            "poll_interval_seconds": lifecycle_interval,
            "target_accounting_complete": target_accounting_complete,
            "all_targets_final_by_frozen_deadline": all_targets_final,
            "experiment_closed_by_frozen_deadline": experiment_closed,
            "lifecycle_record_complete": lifecycle_record_complete,
        },
    }
    result_key = f"result_v{result_version}"
    state[result_key] = result
    _write_state(state_path, state)
    _write_state(experiment_dir / f"result-v{result_version}.json", result)
    print(orjson.dumps(result, option=orjson.OPT_INDENT_2).decode())
    return 0


def materialize_terminal(experiment_dir: Path) -> int:
    """Publish the frozen V3 terminal claim without issuing a lifecycle request."""

    materializer_revision = _clean_revision()
    state_path = experiment_dir / "state.json"
    state = _object(orjson.loads(state_path.read_bytes()), "experiment state")
    if state.get("result_v4") is not None:
        raise ValueError("terminal evidence has already been materialized")

    protocol_raw = _object(state.get("protocol"), "protocol record")
    protocol = ProspectiveExperimentProtocolV2.from_record(protocol_raw)
    protocol_receipt = EvidencePersistenceReceiptV1.from_record(
        _object(state.get("protocol_receipt"), "protocol receipt")
    )
    now = LiveClock().now()
    if now < protocol.lifecycle_deadline:
        raise ValueError("terminal evidence cannot close before the frozen deadline")

    evidence_dir = experiment_dir / "evidence"
    source_dir = experiment_dir / "source"
    terminal_targets: list[ProspectiveTargetTerminalEvidenceV1] = []
    terminal_receipts: list[EvidencePersistenceReceiptV1] = []
    terminal_summaries: list[dict[str, Any]] = []

    for target_state_raw in _list(state.get("targets"), "targets"):
        target_state = _object(target_state_raw, "target state")
        if target_state.get("cutoff") is not None:
            raise ValueError("terminal target unexpectedly carries cutoff evidence")
        target = ProspectiveTargetV1.from_record(
            _object(target_state.get("target"), "target record")
        )
        target_receipt = EvidencePersistenceReceiptV1.from_record(
            _object(target_state.get("target_receipt"), "target receipt")
        )
        lifecycle_polls = _terminal_lifecycle_polls(
            target_state=target_state,
            source_dir=source_dir,
        )
        capture_summary = _terminal_capture_summary(
            experiment_dir=experiment_dir,
            protocol=protocol,
            target=target,
        )
        terminal = build_target_terminal_evidence(
            protocol=protocol,
            protocol_receipt=protocol_receipt,
            target=target,
            target_receipt=target_receipt,
            capture_summary=capture_summary,
            lifecycle_polls=lifecycle_polls,
            closed_at=now,
        )
        receipt = persist_evidence_record(
            evidence_dir,
            record=terminal,
            experiment_id=protocol.experiment_id,
            artifact_kind=EvidenceArtifactKind.TARGET_TERMINAL,
            artifact_id=terminal.terminal_evidence_id,
            persisted_at=now,
        )
        terminal_targets.append(terminal)
        terminal_receipts.append(receipt)
        last = terminal.lifecycle_polls[-1].observation
        terminal_summaries.append(
            {
                "target_id": target.target_id,
                "market_id": target.market_id,
                "lifecycle_observation_count": len(terminal.lifecycle_polls),
                "last_lifecycle_ordinal": last.ordinal,
                "last_observation_id": last.lifecycle_observation_id,
                "last_retrieved_at": last.retrieved_at.isoformat(),
                "last_observed_finality": last.finality.value,
                "admissible_cutoff_count": terminal.admissible_cutoff_count,
                "final_polling_gap_microseconds": (terminal.final_polling_gap_microseconds),
                "maximum_polling_gap_microseconds": (terminal.maximum_polling_gap_microseconds),
                "continuity_status": terminal.continuity_status.value,
                "disposition": terminal.disposition.value,
                "capture_summary": capture_summary.to_record(),
            }
        )

    aggregate = aggregate_prospective_terminal_experiment(
        protocol=protocol,
        protocol_receipt=protocol_receipt,
        terminal_targets=terminal_targets,
        terminal_target_receipts=terminal_receipts,
        created_at=now,
    )
    aggregate_receipt = persist_evidence_record(
        evidence_dir,
        record=aggregate,
        experiment_id=protocol.experiment_id,
        artifact_kind=EvidenceArtifactKind.EXPERIMENT_AGGREGATE,
        artifact_id=aggregate.evidence_digest,
        persisted_at=now,
    )
    aggregate_raw = orjson.dumps(aggregate.to_record(), option=orjson.OPT_SORT_KEYS)
    if (
        sha256_hex(aggregate_raw) != aggregate_receipt.artifact_sha256
        or len(aggregate_raw) != aggregate_receipt.artifact_byte_length
    ):
        raise ValueError("terminal aggregate bytes disagree with their persistence receipt")

    proof_dir = experiment_dir / "proof"
    bundle_path = proof_dir / "claim-bundle-v4.json"
    index_path = proof_dir / "claim-index-v1.json"
    _write_exact_claim_bytes(bundle_path, aggregate_raw)
    index = ProspectiveClaimArtifactIndexV1(
        experiment_id=protocol.experiment_id,
        bundle_relative_path=bundle_path.name,
        bundle_schema_version=aggregate.schema_version,
        bundle_evidence_digest=aggregate.evidence_digest,
        bundle_artifact_sha256=aggregate_receipt.artifact_sha256,
        bundle_byte_length=aggregate_receipt.artifact_byte_length,
        aggregate_receipt=aggregate_receipt,
        published_from_storage_identity=aggregate_receipt.storage_identity,
    )
    _write_state(index_path, index.to_record())
    verified = verify_published_claim_artifact(index, proof_dir)
    if verified != aggregate:
        raise ValueError("published terminal claim did not round-trip exactly")

    result = {
        "format": "m4_prospective_pilot_terminal_result.v4",
        "experiment_id": protocol.experiment_id,
        "materialized_at": now.isoformat(),
        "materializer_revision": materializer_revision,
        "frozen_deadline": protocol.lifecycle_deadline.isoformat(),
        "poll_interval_seconds": protocol.lifecycle_poll_interval_seconds,
        "terminal_targets": terminal_summaries,
        "terminal_report": aggregate.report.to_record(),
        "aggregate_receipt": aggregate_receipt.to_record(),
        "published_claim_artifact": {
            "index_path": index_path.relative_to(Path.cwd()).as_posix(),
            "bundle_path": bundle_path.relative_to(Path.cwd()).as_posix(),
            "bundle_schema_version": aggregate.schema_version,
            "bundle_evidence_digest": aggregate.evidence_digest,
            "bundle_artifact_sha256": aggregate_receipt.artifact_sha256,
            "bundle_byte_length": aggregate_receipt.artifact_byte_length,
            "receipt_id": aggregate_receipt.receipt_id,
        },
    }
    state["result_v4"] = result
    _write_state(state_path, state)
    _write_state(experiment_dir / "result-v4.json", result)
    print(orjson.dumps(result, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS).decode())
    return 0


def _terminal_lifecycle_polls(
    *,
    target_state: dict[str, Any],
    source_dir: Path,
) -> tuple[LifecyclePollEvidenceV1, ...]:
    polls: list[LifecyclePollEvidenceV1] = []
    for lifecycle_raw in _list(target_state.get("lifecycle"), "lifecycle"):
        lifecycle = _object(lifecycle_raw, "lifecycle entry")
        observation = LifecycleObservationV1.from_record(
            _object(lifecycle.get("observation"), "lifecycle observation")
        )
        receipt = EvidencePersistenceReceiptV1.from_record(
            _object(lifecycle.get("receipt"), "lifecycle receipt")
        )
        raw, provenance = read_raw_payload(source_dir, observation.raw_payload_sha256)
        # Identical Gamma bytes share one content-addressed sidecar, whose
        # retrieved_at belongs to the first archive write rather than every
        # later lifecycle observation that references those same bytes.
        if (
            not provenance.matches(raw)
            or provenance.source != observation.source
            or provenance.endpoint != observation.endpoint
            or archive_relative_location(provenance) != observation.raw_payload_location
        ):
            raise ValueError("lifecycle raw archive disagrees with its observation")
        polls.append(
            build_lifecycle_poll_evidence(
                observation=observation,
                receipt=receipt,
                raw_payload=raw,
            )
        )
    return tuple(polls)


def _terminal_capture_summary(
    *,
    experiment_dir: Path,
    protocol: ProspectiveExperimentProtocolV2,
    target: ProspectiveTargetV1,
) -> CaptureRunSummaryV1:
    capture_dir = experiment_dir / "capture" / f"target-{target.selection_rank}"
    capture_run_id = f"{protocol.experiment_id}-target-{target.selection_rank}"
    manifest = RunManifest.from_record(
        _object(
            orjson.loads((capture_dir / f"{capture_run_id}.manifest.json").read_bytes()),
            "capture manifest",
        )
    )
    ledger_path = capture_dir / "events.sqlite3"
    ledger_raw = ledger_path.read_bytes()
    store = open_sqlite_event_store(ledger_path)
    try:
        run = store.get_capture_run(capture_run_id)
        if (
            run is None
            or run.ended_at is None
            or run.completion_status is not CompletionStatus.COMPLETED
        ):
            raise ValueError("target capture is not durably completed")
        counts = store.counts_for_capture_run(capture_run_id)
        deliveries = tuple(store.iter_deliveries(capture_run_id))
        rejections = tuple(store.iter_rejections(capture_run_id))
        schema_counts: Counter[str] = Counter()
        delivery_material: list[dict[str, Any]] = []
        frame_hashes: set[str] = set()
        for delivery in deliveries:
            observation = store.get_observation(delivery.observation_id)
            if observation is None:
                raise ValueError("capture delivery names a missing observation")
            schema_counts[observation.payload_schema_version] += 1
            frame_hashes.add(delivery.source_frame_sha256)
            delivery_material.append(
                {
                    "capture_run_id": delivery.capture_run_id,
                    "ingest_sequence": delivery.ingest_sequence,
                    "observation_id": delivery.observation_id,
                    "observation_record_sha256": record_sha256(observation.to_record()),
                    "payload_schema_version": observation.payload_schema_version,
                    "received_time": delivery.received_time.isoformat(),
                    "disposition": delivery.disposition.value,
                    "source_frame_sha256": delivery.source_frame_sha256,
                    "source_frame_offset": delivery.source_frame_offset,
                }
            )
        reason_counts: Counter[str] = Counter()
        rejection_material: list[dict[str, Any]] = []
        for rejection_record in rejections:
            rejection = rejection_record.rejection
            reason_counts[rejection.reason.value] += 1
            frame_hashes.add(rejection.raw_payload_sha256)
            rejection_material.append(
                {
                    "capture_run_id": rejection_record.capture_run_id,
                    "ingest_sequence": rejection_record.ingest_sequence,
                    "duplicate_of_observation_id": (rejection_record.duplicate_of_observation_id),
                    "rejection_id": rejection.rejection_id,
                    "rejection_record_sha256": record_sha256(rejection.to_record()),
                    "reason": rejection.reason.value,
                    "raw_payload_sha256": rejection.raw_payload_sha256,
                }
            )
    finally:
        store.close()
    if ledger_path.read_bytes() != ledger_raw:
        raise ValueError("read-only terminal audit changed the frozen capture ledger")

    decode_reasons = {
        RejectionReason.MALFORMED_PAYLOAD.value,
        RejectionReason.SCHEMA_VERSION_MISMATCH.value,
        RejectionReason.INVALID_TIMESTAMP.value,
    }
    return build_capture_run_summary(
        experiment_id=protocol.experiment_id,
        target_id=target.target_id,
        manifest=manifest,
        started_at=run.started_at,
        ended_at=run.ended_at,
        completion_status=run.completion_status,
        frame_count=len(frame_hashes),
        accepted_count=counts.accepted,
        duplicate_count=counts.duplicate,
        rejected_count=counts.rejected,
        decode_failure_count=sum(reason_counts[reason] for reason in decode_reasons),
        unknown_event_count=reason_counts[RejectionReason.UNKNOWN_EVENT_TYPE.value],
        observation_schema_counts=dict(schema_counts),
        rejection_reason_counts=dict(reason_counts),
        ledger_sha256=sha256_hex(ledger_raw),
        ledger_byte_length=len(ledger_raw),
        delivery_collection_digest=record_sha256(
            {
                "version": "capture_delivery_collection.v1",
                "records": delivery_material,
            }
        ),
        rejection_collection_digest=record_sha256(
            {
                "version": "capture_rejection_collection.v1",
                "records": rejection_material,
            }
        ),
    )


def _write_exact_claim_bytes(path: Path, raw: bytes) -> None:
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("refusing to overwrite different published claim bytes")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        import os

        os.fsync(stream.fileno())
    temporary.replace(path)


def _eligible_markets(
    markets: tuple[MarketDefinitionV1, ...],
    raw_by_id: dict[str, dict[str, Any]],
    selection: dict[str, Any],
) -> list[MarketDefinitionV1]:
    minimum_liquidity = Decimal(str(selection["minimum_liquidity"]))
    minimum_price = Decimal(str(selection["minimum_outcome_price"]))
    maximum_price = Decimal(str(selection["maximum_outcome_price"]))
    end_min = _time(str(selection["target_end_min"]))
    end_max = _time(str(selection["target_end_max"]))
    eligible: list[MarketDefinitionV1] = []
    for market in markets:
        raw = raw_by_id.get(market.market_id, {})
        prices = _decimal_list(raw.get("outcomePrices"))
        if (
            market.active
            and not market.closed
            and not market.archived
            and market.outcomes == ("Yes", "No")
            and market.end_time is not None
            and end_min <= market.end_time <= end_max
            and (market.liquidity or Decimal(0)) >= minimum_liquidity
            and raw.get("enableOrderBook") is True
            and raw.get("acceptingOrders") is True
            and prices is not None
            and all(minimum_price <= price <= maximum_price for price in prices)
        ):
            eligible.append(market)
    return sorted(
        eligible,
        key=lambda market: (-(market.liquidity or Decimal(0)), int(market.market_id)),
    )


def _target(
    *,
    protocol: ProspectiveExperimentProtocolV1,
    market: MarketDefinitionV1,
    market_receipt: EvidencePersistenceReceiptV1,
    contract: CompiledMarketContractV1,
    contract_receipt: EvidencePersistenceReceiptV1,
    rank: int,
) -> ProspectiveTargetV1:
    yes_token = market.token_id_for("Yes")
    no_token = market.token_id_for("No")
    return ProspectiveTargetV1(
        target_id=build_target_id(
            experiment_id=protocol.experiment_id,
            market_id=market.market_id,
            condition_id=market.condition_id,
            yes_token_id=yes_token,
            no_token_id=no_token,
        ),
        experiment_id=protocol.experiment_id,
        selection_rank=rank,
        selected_at=LiveClock().now(),
        market_id=market.market_id,
        condition_id=market.condition_id,
        yes_token_id=yes_token,
        no_token_id=no_token,
        category=market.category,
        cutoff_basis=protocol.cutoff_basis,
        market_record_sha256=record_sha256(market.to_record()),
        market_receipt_id=market_receipt.receipt_id,
        contract_id=contract.contract_id,
        contract_record_sha256=record_sha256(contract.to_record()),
        contract_receipt_id=contract_receipt.receipt_id,
    )


def _lifecycle_observation(
    *,
    protocol: ProspectiveExperimentProtocolV1,
    target: ProspectiveTargetV1,
    ordinal: int,
    previous: LifecycleObservationV1 | None,
    provenance: SourceProvenanceV1,
    payload: dict[str, Any],
    finality: ResolutionStatus,
    resolution: ResolutionV1 | None,
) -> LifecycleObservationV1:
    source_time = _optional_time(payload.get("updatedAt"))
    resolution_digest = record_sha256(resolution.to_record()) if resolution else None
    location = archive_relative_location(provenance)
    fields = {
        "experiment_id": protocol.experiment_id,
        "target_id": target.target_id,
        "ordinal": ordinal,
        "previous_observation_id": previous.lifecycle_observation_id if previous else None,
        "source": provenance.source,
        "endpoint": provenance.endpoint,
        "source_time": source_time,
        "retrieved_at": provenance.retrieved_at,
        "raw_payload_sha256": provenance.raw_sha256,
        "byte_length": provenance.byte_length,
        "raw_payload_location": location,
        "finality": finality,
        "resolution_id": resolution.resolution_id if resolution else None,
        "resolution_record_sha256": resolution_digest,
    }
    identity = build_lifecycle_observation_id(
        experiment_id=protocol.experiment_id,
        target_id=target.target_id,
        ordinal=ordinal,
        previous_observation_id=previous.lifecycle_observation_id if previous else None,
        source=provenance.source,
        endpoint=provenance.endpoint,
        source_time=source_time,
        retrieved_at=provenance.retrieved_at,
        raw_payload_sha256=provenance.raw_sha256,
        byte_length=provenance.byte_length,
        raw_payload_location=location,
        finality=finality,
        resolution_id=resolution.resolution_id if resolution else None,
        resolution_record_sha256=resolution_digest,
    )
    return LifecycleObservationV1.model_validate({"lifecycle_observation_id": identity, **fields})


def _cutoff(
    protocol: ProspectiveExperimentProtocolV1,
    target: ProspectiveTargetV1,
    observation: LifecycleObservationV1,
    resolution: ResolutionV1,
) -> ResolutionCutoffEvidenceV1:
    if protocol.cutoff_basis is not CutoffBasis.FIRST_OBSERVED_FINAL_SETTLEMENT:
        raise ValueError("pilot helper currently supports only first-observed-final cutoff")
    if isinstance(protocol, ProspectiveExperimentProtocolV2) and (
        observation.retrieved_at > protocol.lifecycle_deadline
    ):
        raise ValueError("resolution cutoff is after the frozen lifecycle deadline")
    fields = {
        "experiment_id": protocol.experiment_id,
        "target_id": target.target_id,
        "cutoff_basis": protocol.cutoff_basis,
        "lifecycle_observation_id": observation.lifecycle_observation_id,
        "source": observation.source,
        "endpoint": observation.endpoint,
        "source_time": observation.source_time,
        "retrieved_at": observation.retrieved_at,
        "selected_cutoff": observation.retrieved_at,
        "raw_payload_sha256": observation.raw_payload_sha256,
        "byte_length": observation.byte_length,
        "raw_payload_location": observation.raw_payload_location,
        "resolution_id": resolution.resolution_id,
        "resolution_record_sha256": record_sha256(resolution.to_record()),
    }
    identity = build_cutoff_evidence_id(
        experiment_id=protocol.experiment_id,
        target_id=target.target_id,
        cutoff_basis=protocol.cutoff_basis,
        lifecycle_observation_id=observation.lifecycle_observation_id,
        source=observation.source,
        endpoint=observation.endpoint,
        source_time=observation.source_time,
        retrieved_at=observation.retrieved_at,
        selected_cutoff=observation.retrieved_at,
        raw_payload_sha256=observation.raw_payload_sha256,
        byte_length=observation.byte_length,
        raw_payload_location=observation.raw_payload_location,
        resolution_id=resolution.resolution_id,
        resolution_record_sha256=record_sha256(resolution.to_record()),
    )
    return ResolutionCutoffEvidenceV1.model_validate(
        {
            "cutoff_evidence_id": identity,
            "finality": ResolutionStatus.FINAL,
            **fields,
        }
    )


def _protocol_from_record(
    record: dict[str, Any],
) -> ProspectiveExperimentProtocolV1 | ProspectiveExperimentProtocolV2:
    schema_version = record.get("schema_version")
    if schema_version == ProspectiveExperimentProtocolV2.schema_version:
        return ProspectiveExperimentProtocolV2.from_record(record)
    if schema_version == ProspectiveExperimentProtocolV1.schema_version:
        return ProspectiveExperimentProtocolV1.from_record(record)
    raise ValueError(f"unsupported prospective protocol schema: {schema_version!r}")


def _protocol_lifecycle_deadline(
    protocol: ProspectiveExperimentProtocolV1 | ProspectiveExperimentProtocolV2,
    state: dict[str, Any],
) -> datetime:
    if isinstance(protocol, ProspectiveExperimentProtocolV2):
        return protocol.lifecycle_deadline
    capture = _object(state.get("capture"), "legacy capture configuration")
    return _time(str(capture["lifecycle_deadline"]))


def _protocol_lifecycle_poll_interval(
    protocol: ProspectiveExperimentProtocolV1 | ProspectiveExperimentProtocolV2,
    state: dict[str, Any],
) -> int:
    if isinstance(protocol, ProspectiveExperimentProtocolV2):
        return protocol.lifecycle_poll_interval_seconds
    capture = _object(state.get("capture"), "legacy capture configuration")
    interval = capture.get("lifecycle_poll_interval_seconds")
    if not isinstance(interval, int) or isinstance(interval, bool) or interval <= 0:
        raise ValueError("legacy lifecycle cadence must be a positive integer")
    return interval


def _validate_capture_configuration(
    protocol: ProspectiveExperimentProtocolV2,
    capture: dict[str, Any],
) -> None:
    expected: dict[str, Any] = {
        "lifecycle_deadline": protocol.lifecycle_deadline,
        "lifecycle_poll_interval_seconds": protocol.lifecycle_poll_interval_seconds,
        "max_seconds_per_target": protocol.capture_max_seconds_per_target,
        "max_frames_per_target": protocol.capture_max_frames_per_target,
        "separate_database_per_target": protocol.capture_separate_database_per_target,
        "subscribe_both_tokens": protocol.capture_subscribe_both_tokens,
        "raw_archive": protocol.capture_raw_archive,
    }
    for key, expected_value in expected.items():
        actual = capture.get(key)
        if key == "lifecycle_deadline":
            try:
                actual = _time(str(actual))
            except (TypeError, ValueError) as error:
                raise ValueError("capture lifecycle deadline is invalid") from error
        if actual != expected_value or type(actual) is not type(expected_value):
            raise ValueError(f"capture configuration disagrees with protocol field {key!r}")


def _all_targets_have_admissible_cutoff(
    targets: list[Any],
    deadline: datetime,
) -> bool:
    complete = True
    for raw_target in targets:
        target = _object(raw_target, "target state")
        raw_cutoff = target.get("cutoff")
        if raw_cutoff is None:
            complete = False
            continue
        cutoff_container = _object(raw_cutoff, "cutoff container")
        cutoff = ResolutionCutoffEvidenceV1.from_record(
            _object(cutoff_container.get("evidence"), "cutoff evidence")
        )
        if cutoff.retrieved_at > deadline or cutoff.selected_cutoff > deadline:
            raise ValueError("persisted resolution cutoff is after the frozen deadline")
    return complete


def _lifecycle_record_complete(
    *,
    protocol: ProspectiveExperimentProtocolV1 | ProspectiveExperimentProtocolV2,
    targets: list[Any],
    deadline: datetime,
    poll_interval_seconds: int,
) -> bool:
    """Return whether no complete polling window is absent before closure."""

    maximum_gap = timedelta(seconds=poll_interval_seconds * 2)
    for raw_target in targets:
        target = _object(raw_target, "target state")
        coverage_end = deadline
        raw_cutoff = target.get("cutoff")
        if raw_cutoff is not None:
            cutoff = ResolutionCutoffEvidenceV1.from_record(
                _object(_object(raw_cutoff, "cutoff container").get("evidence"), "cutoff evidence")
            )
            if cutoff.retrieved_at > deadline or cutoff.selected_cutoff > deadline:
                raise ValueError("persisted resolution cutoff is after the frozen deadline")
            coverage_end = cutoff.retrieved_at
        observations = []
        for raw_entry in _list(target.get("lifecycle"), "lifecycle"):
            entry = _object(raw_entry, "lifecycle entry")
            observation = LifecycleObservationV1.from_record(
                _object(entry.get("observation"), "lifecycle observation")
            )
            if observation.retrieved_at <= coverage_end:
                observations.append(observation.retrieved_at)
        points = [protocol.observation_window_end]
        points.extend(
            sorted(
                observed_at
                for observed_at in observations
                if observed_at > protocol.observation_window_end
            )
        )
        if any(later - earlier > maximum_gap for earlier, later in itertools.pairwise(points)):
            return False
        if coverage_end - points[-1] > maximum_gap:
            return False
    return True


def _public_get(url: str, params: dict[str, Any]) -> tuple[bytes, str, datetime]:
    with (
        httpx.Client(
            timeout=20,
            follow_redirects=False,
            headers={"accept": "application/json", "user-agent": USER_AGENT},
        ) as client,
        client.stream("GET", url, params=params) as response,
    ):
        response.raise_for_status()
        chunks: list[bytes] = []
        length = 0
        for chunk in response.iter_bytes():
            length += len(chunk)
            if length > MAX_RESPONSE_BYTES:
                raise ValueError("public source response exceeded 32 MiB")
            chunks.append(chunk)
        return b"".join(chunks), str(response.request.url), datetime.now(UTC)


def _clean_revision() -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain"], check=True, capture_output=True, text=True
    ).stdout
    if status:
        raise ValueError("prospective evidence requires a clean working tree")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as stream:
        stream.write(orjson.dumps(state, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))
        stream.flush()
        import os

        os.fsync(stream.fileno())
    temporary.replace(path)


def _nonfinal_status(payload: dict[str, Any]) -> ResolutionStatus:
    raw = payload.get("umaResolutionStatuses")
    statuses = orjson.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(statuses, list) or not statuses:
        return ResolutionStatus.UNKNOWN
    latest = str(statuses[-1]).lower()
    return {
        "proposed": ResolutionStatus.PROPOSED,
        "disputed": ResolutionStatus.DISPUTED,
    }.get(latest, ResolutionStatus.UNKNOWN)


def _decimal_list(raw: Any) -> tuple[Decimal, ...] | None:
    values = orjson.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(values, list):
        return None
    try:
        return tuple(Decimal(str(value)) for value in values)
    except ValueError:
        return None


def _time(value: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _optional_time(value: Any) -> datetime | None:
    return _time(value) if isinstance(value, str) and value else None


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} is not an object")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} is not a list")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
