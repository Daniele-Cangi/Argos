"""Publish and independently verify the immutable 2026-09-26 M4 technical run.

The archive contains the original campaign tree byte-for-byte plus the separate
C: fixture used by T8.  The index is a *post-run proof inventory*, not a
retroactive protocol or a new experiment.  Verification needs only this repo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from argos.evaluation.technical_campaign import (
    TechnicalCampaignV1,
    TechnicalScenarioResultV1,
    TechnicalScenarioSpecV1,
)

CAMPAIGN = "m4-technical-20260926-v1"
REVISION = "7b75006c6adb1f01d84ab675e46960d47c12cc64"
RESULT_PATHS = (
    "t1/t1-result.json",
    "t2/t2-result.json",
    "t3/t3-result.json",
    "t4/t4_network_interruption-result.json",
    "t5/t5_process_interruption-result.json",
    "t6/t6_storage_interruption-result.json",
    "t7/t7_terminal_simulation-result.json",
    "t8/t8_cross_volume-result.json",
)
SCENARIOS = (
    "T1_FUNCTIONAL",
    "T2_STABILITY",
    "T3_ENDURANCE",
    "T4_NETWORK_INTERRUPTION",
    "T5_PROCESS_INTERRUPTION",
    "T6_STORAGE_INTERRUPTION",
    "T7_TERMINAL_SIMULATION",
    "T8_CROSS_VOLUME",
)
MAX_SECONDS = (600, 7200, 21600, 3600, 3600, 3600, 3600, 3600)
MAX_FRAMES = (500, 100000, 300000, 500, 500, 500, 500, 500)
EXTERNAL_T8 = "external/t8-c-workspace/semantic-input.json"
EXPECTED = (
    "bounded capture completes with durable accounting, raw evidence, "
    "immutable database and identical replay digests",
    "capture accounting, cadence, memory and artifact growth remain inside frozen bounds",
    "six-hour capture finishes with an intact checkpoint chain and reloadable terminal state",
    "bounded retry records the gap and resumes the same ordinal exactly once",
    "replacement ownership is exclusive and resumes at the correct ordinal",
    "failed persistence publishes no partial claim and preserves the prior durable head",
    "all frozen lifecycle states are normalized once in declared order",
    "equivalent C: and D: inputs produce identical semantic identities",
)
FAULTS = (
    None,
    None,
    None,
    "deterministic network adapter failure",
    "forced child-process termination after durable persistence",
    "deterministic storage refusal",
    "recorded terminal-state fixtures",
    None,
)
FIXTURE_LABELS = (
    "fault-schedule-t4",
    "fault-schedule-t5",
    "fault-schedule-t6",
    "terminal-state-schedule-t7",
    "cross-volume-semantic-input-t8",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _safe_name(name: str) -> None:
    path = PurePosixPath(name)
    if not name or name.startswith("/") or "\\" in name or ":" in name:
        raise ValueError(f"unsafe archive path: {name!r}")
    if any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"unsafe archive path: {name!r}")


def publish(source: Path, c_fixture: Path, proof: Path) -> None:
    if proof.exists() and any(proof.iterdir()):
        raise ValueError("proof output is not empty; refusing to replace evidence")
    if not source.is_dir() or not c_fixture.is_file():
        raise ValueError("campaign tree and T8 C: fixture must exist")
    proof.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, object]] = []
    archive = proof / "campaign-evidence.zip"
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"symlink is not proof material: {path}")
            if not path.is_file():
                continue
            name = path.relative_to(source).as_posix()
            _safe_name(name)
            data = path.read_bytes()
            output.writestr(name, data)
            entries.append({"path": name, "size": len(data), "sha256": digest(data)})
        data = c_fixture.read_bytes()
        output.writestr(EXTERNAL_T8, data)
        entries.append({"path": EXTERNAL_T8, "size": len(data), "sha256": digest(data)})
    entries.sort(key=lambda entry: str(entry["path"]))
    manifest = canonical(
        {"schema_version": "technical_campaign_file_manifest.v1", "files": entries}
    )
    (proof / "file-manifest.json").write_bytes(manifest)
    archive_bytes = archive.read_bytes()
    index = {
        "schema_version": "technical_campaign_proof_index.v1",
        "campaign_id": CAMPAIGN,
        "code_revision": REVISION,
        "archive": {
            "path": archive.name,
            "size": len(archive_bytes),
            "sha256": digest(archive_bytes),
        },
        "manifest": {
            "path": "file-manifest.json",
            "size": len(manifest),
            "sha256": digest(manifest),
        },
        "result_paths": list(RESULT_PATHS),
        "limitations": [
            "This index inventories a completed technical run; it was not frozen before the run.",
            "T1-T8 qualification does not establish predictive performance or calibration.",
        ],
    }
    (proof / "proof-index.json").write_bytes(canonical(index))
    attach_aggregate(proof)


def attach_aggregate(proof: Path) -> None:
    """Supersede the inventory with an aggregate without changing original bytes."""

    predecessor = proof / "proof-index.json"
    successor = proof / "proof-index-v2.json"
    aggregate_path = proof / "technical-campaign.json"
    if successor.exists() or aggregate_path.exists():
        raise ValueError("aggregate proof already exists; refusing to rewrite it")
    v1_bytes = predecessor.read_bytes()
    v1 = json.loads(v1_bytes)
    archive_path = proof / v1["archive"]["path"]
    with zipfile.ZipFile(archive_path) as archive:
        entries = json.loads((proof / v1["manifest"]["path"]).read_bytes())["files"]
        results, configs = _verify_campaign(archive, entries)
        campaign = _aggregate(results, configs, archive)
    aggregate_bytes = canonical(campaign.to_record())
    aggregate_path.write_bytes(aggregate_bytes)
    v2 = {
        **v1,
        "schema_version": "technical_campaign_proof_index.v2",
        "predecessor_index_sha256": digest(v1_bytes),
        "aggregate": {
            "path": aggregate_path.name,
            "size": len(aggregate_bytes),
            "sha256": digest(aggregate_bytes),
        },
        "configuration_sha256_derivation": (
            "sha256(canonical sorted JSON list of scenario and archived configuration.json SHA-256)"
        ),
    }
    successor.write_bytes(canonical(v2))
    verify(proof)


def verify(proof: Path) -> None:
    index = json.loads((proof / "proof-index-v2.json").read_bytes())
    if index.get("schema_version") != "technical_campaign_proof_index.v2":
        raise ValueError("unsupported proof index")
    previous = (proof / "proof-index.json").read_bytes()
    if digest(previous) != index.get("predecessor_index_sha256"):
        raise ValueError("proof index supersession chain is broken")
    v1 = json.loads(previous)
    for key in (
        "campaign_id",
        "code_revision",
        "archive",
        "manifest",
        "result_paths",
        "limitations",
    ):
        if index.get(key) != v1.get(key):
            raise ValueError(f"proof index supersession changed {key}")
    if index.get("campaign_id") != CAMPAIGN or index.get("code_revision") != REVISION:
        raise ValueError("wrong campaign or code revision")
    if tuple(index.get("result_paths", ())) != RESULT_PATHS:
        raise ValueError("result partition changed")
    for key in ("archive", "manifest", "aggregate"):
        item = index[key]
        name = item["path"]
        _safe_name(name)
        if len(PurePosixPath(name).parts) != 1:
            raise ValueError("proof item must be adjacent to index")
        data = (proof / name).read_bytes()
        if len(data) != item["size"] or digest(data) != item["sha256"]:
            raise ValueError(f"{key} bytes disagree with index")
    manifest = json.loads((proof / index["manifest"]["path"]).read_bytes())
    if manifest.get("schema_version") != "technical_campaign_file_manifest.v1":
        raise ValueError("unsupported file manifest")
    entries = manifest["files"]
    names = [entry["path"] for entry in entries]
    if names != sorted(set(names)):
        raise ValueError("manifest paths are not unique and sorted")
    with zipfile.ZipFile(proof / index["archive"]["path"]) as archive:
        archived = archive.namelist()
        if sorted(archived) != names or len(archived) != len(names):
            raise ValueError("archive and file manifest have different entries")
        for entry in entries:
            name = entry["path"]
            _safe_name(name)
            data = archive.read(name)
            if len(data) != entry["size"] or digest(data) != entry["sha256"]:
                raise ValueError(f"file bytes disagree with manifest: {name}")
        results, configs = _verify_campaign(archive, entries)
        aggregate_bytes = (proof / index["aggregate"]["path"]).read_bytes()
        aggregate = TechnicalCampaignV1.from_record(json.loads(aggregate_bytes))
        expected = _aggregate(results, configs, archive, created_at=aggregate.created_at)
        if aggregate != expected or not aggregate.technically_qualified:
            raise ValueError("versioned aggregate disagrees with original run evidence")
    print(f"VERIFIED {CAMPAIGN}: 8 PASSED, {len(entries)} original proof files")


def _verify_campaign(
    archive: zipfile.ZipFile, entries: list[dict[str, object]]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    by_name = {str(entry["path"]): entry for entry in entries}
    prior_end: datetime | None = None
    results: list[dict[str, object]] = []
    configs: list[dict[str, object]] = []
    for number, (result_path, scenario, seconds, frames) in enumerate(
        zip(RESULT_PATHS, SCENARIOS, MAX_SECONDS, MAX_FRAMES, strict=True), start=1
    ):
        result = json.loads(archive.read(result_path))
        config_path = f"t{number}/configuration.json"
        config = json.loads(archive.read(config_path))
        results.append(result)
        configs.append(config)
        if result.get("schema_version") != "technical_scenario_result.v1":
            raise ValueError(f"T{number} result schema changed")
        if result.get("campaign_id") != CAMPAIGN or result.get("scenario") != scenario:
            raise ValueError(f"T{number} belongs to another campaign or scenario")
        if result.get("status") != "PASSED" or result.get("reason") is not None:
            raise ValueError(f"T{number} did not pass")
        if config.get("campaign_id") != CAMPAIGN or config.get("code_revision") != REVISION:
            raise ValueError(f"T{number} configuration is from a different run")
        started = datetime.fromisoformat(result["started_at"].replace("Z", "+00:00"))
        checkpoint = datetime.fromisoformat(result["last_checkpoint_at"].replace("Z", "+00:00"))
        ended = datetime.fromisoformat(result["ended_at"].replace("Z", "+00:00"))
        if not started <= checkpoint <= ended or (ended - started).total_seconds() > seconds:
            raise ValueError(f"T{number} exceeds time bound")
        if result["observed_frame_count"] > frames or (prior_end and started < prior_end):
            raise ValueError(f"T{number} exceeds frame bound or overlaps prior scenario")
        prior_end = ended
        if not result.get("artifact_identities") or not result.get("observed_outcome"):
            raise ValueError(f"T{number} lacks outcome evidence")
        for identity in result["artifact_identities"]:
            prefix, expected, basename = identity.split(":", 2)
            if prefix != "sha256":
                raise ValueError("unsupported artifact identity")
            if number == 8 and basename in ("c-semantic-input.json", "d-semantic-input.json"):
                fixture_path = (
                    EXTERNAL_T8
                    if basename.startswith("c-")
                    else "t8-d-workspace/semantic-input.json"
                )
                if by_name[fixture_path]["sha256"] != expected:
                    raise ValueError(f"T8 fixture digest disagrees: {basename}")
                continue
            matches = [
                entry
                for name, entry in by_name.items()
                if name.startswith(f"t{number}/")
                and PurePosixPath(name).name == basename
                and entry["sha256"] == expected
            ]
            if not matches:
                raise ValueError(f"T{number} artifact not in archive: {basename}")
    status = json.loads(archive.read("runner.status.json"))
    if status.get("state") != "PASSED":
        raise ValueError("runner did not finish PASSED")
    if archive.read("runner.err.log"):
        raise ValueError("runner recorded stderr")
    c_bytes = archive.read(EXTERNAL_T8)
    d_bytes = archive.read("t8-d-workspace/semantic-input.json")
    if c_bytes != d_bytes:
        raise ValueError("T8 cross-volume fixture bytes disagree")
    return results, configs


def _aggregate(
    results: list[dict[str, object]],
    configs: list[dict[str, object]],
    archive: zipfile.ZipFile,
    *,
    created_at: datetime | None = None,
) -> TechnicalCampaignV1:
    specs = tuple(
        TechnicalScenarioSpecV1(
            scenario=scenario,
            maximum_duration_seconds=seconds,
            maximum_frame_count=frames,
            expected_outcome=expected,
            fault_injection=fault,
        )
        for scenario, seconds, frames, expected, fault in zip(
            SCENARIOS, MAX_SECONDS, MAX_FRAMES, EXPECTED, FAULTS, strict=True
        )
    )
    for number, config in enumerate(configs[:3], start=1):
        if config.get("maximum_duration_seconds") != MAX_SECONDS[number - 1]:
            raise ValueError(f"T{number} config duration differs from frozen bound")
        if config.get("maximum_frame_count") != MAX_FRAMES[number - 1]:
            raise ValueError(f"T{number} config frame count differs from frozen bound")
    config_items = [
        {
            "scenario": scenario,
            "sha256": digest(archive.read(f"t{number}/configuration.json")),
        }
        for number, scenario in enumerate(SCENARIOS, start=1)
    ]
    tokens = set(configs[0]["token_ids"])
    if any(set(config["token_ids"]) != tokens for config in configs[1:3]) or len(tokens) != 2:
        raise ValueError("T1-T3 public token identities disagree")
    inputs = [f"polymarket-token:{token}" for token in sorted(tokens)]
    for config, label in zip(configs[3:], FIXTURE_LABELS, strict=True):
        inputs.append(f"sha256:{config['fixture_identity']}:{label}")
    return TechnicalCampaignV1(
        campaign_id=CAMPAIGN,
        created_at=created_at or datetime.now(UTC),
        code_revision=REVISION,
        configuration_sha256=digest(canonical(config_items)),
        input_identities=tuple(inputs),
        specifications=specs,
        results=tuple(TechnicalScenarioResultV1.from_record(item) for item in results),
        passed_count=8,
        failed_count=0,
        incomplete_count=0,
        not_run_count=0,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("publish")
    create.add_argument("--source", type=Path, required=True)
    create.add_argument("--c-fixture", type=Path, required=True)
    create.add_argument("--proof", type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("--proof", type=Path, required=True)
    aggregate = commands.add_parser("attach-aggregate")
    aggregate.add_argument("--proof", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "publish":
        publish(args.source, args.c_fixture, args.proof)
    elif args.command == "attach-aggregate":
        attach_aggregate(args.proof)
    else:
        verify(args.proof)


if __name__ == "__main__":
    main()
