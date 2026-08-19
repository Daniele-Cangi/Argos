"""Freeze and poll the bounded public M4 prospective pilot.

The capture itself deliberately stays on the shipped ``argos capture market``
path. This helper owns only evidence that must exist before capture and the
one-shot lifecycle polls that may later establish a cutoff.
"""

from __future__ import annotations

import argparse
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import orjson

from argos.clock import LiveClock, ensure_utc
from argos.compiler import CompiledMarketContractV1, compile_market_contract
from argos.domain.market import MarketDefinitionV1
from argos.domain.provenance import SourceProvenanceV1, sha256_hex
from argos.evaluation.bundle import record_sha256
from argos.evaluation.prospective import (
    CutoffBasis,
    EvidenceArtifactKind,
    EvidencePersistenceReceiptV1,
    LifecycleObservationV1,
    ProspectiveExperimentProtocolV1,
    ProspectiveTargetV1,
    ResolutionCutoffEvidenceV1,
    build_cutoff_evidence_id,
    build_lifecycle_observation_id,
    build_target_id,
    persist_evidence_record,
)
from argos.ingestion.gamma_markets import normalize_markets
from argos.resolution import (
    ResolutionStatus,
    ResolutionV1,
    normalize_gamma_resolution,
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
    args = parser.parse_args()
    if args.command == "freeze":
        return freeze_experiment(args.spec, args.experiment_dir)
    return poll_experiment(args.experiment_dir)


def freeze_experiment(spec_path: Path, experiment_dir: Path) -> int:
    revision = _clean_revision()
    spec = _object(orjson.loads(spec_path.read_bytes()), "protocol specification")
    protocol_fields = _object(spec.get("protocol"), "protocol")
    if protocol_fields.get("code_revision") != "$PROTOCOL_COMMIT":
        raise ValueError("the committed protocol must resolve code_revision mechanically")
    protocol_fields["code_revision"] = revision
    protocol = ProspectiveExperimentProtocolV1.model_validate(protocol_fields)
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
        "capture": spec.get("capture"),
        "targets": targets,
    }
    _write_state(experiment_dir / "state.json", state)
    print(orjson.dumps(state, option=orjson.OPT_INDENT_2).decode())
    return 0


def poll_experiment(experiment_dir: Path) -> int:
    _clean_revision()
    state_path = experiment_dir / "state.json"
    state = _object(orjson.loads(state_path.read_bytes()), "experiment state")
    protocol = ProspectiveExperimentProtocolV1.from_record(
        _object(state.get("protocol"), "protocol record")
    )
    evidence_dir = experiment_dir / "evidence"
    source_dir = experiment_dir / "source"
    for target_state_raw in _list(state.get("targets"), "targets"):
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
        if finality is ResolutionStatus.FINAL and target_state.get("cutoff") is None:
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
