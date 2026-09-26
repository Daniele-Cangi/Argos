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
from datetime import datetime
from pathlib import Path, PurePosixPath

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
    verify(proof)


def verify(proof: Path) -> None:
    index = json.loads((proof / "proof-index.json").read_bytes())
    if index.get("schema_version") != "technical_campaign_proof_index.v1":
        raise ValueError("unsupported proof index")
    if index.get("campaign_id") != CAMPAIGN or index.get("code_revision") != REVISION:
        raise ValueError("wrong campaign or code revision")
    if tuple(index.get("result_paths", ())) != RESULT_PATHS:
        raise ValueError("result partition changed")
    for key in ("archive", "manifest"):
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
        _verify_campaign(archive, entries)
    print(f"VERIFIED {CAMPAIGN}: 8 PASSED, {len(entries)} original proof files")


def _verify_campaign(archive: zipfile.ZipFile, entries: list[dict[str, object]]) -> None:
    by_name = {str(entry["path"]): entry for entry in entries}
    prior_end: datetime | None = None
    for number, (result_path, scenario, seconds, frames) in enumerate(
        zip(RESULT_PATHS, SCENARIOS, MAX_SECONDS, MAX_FRAMES, strict=True), start=1
    ):
        result = json.loads(archive.read(result_path))
        config_path = f"t{number}/configuration.json"
        config = json.loads(archive.read(config_path))
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


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("publish")
    create.add_argument("--source", type=Path, required=True)
    create.add_argument("--c-fixture", type=Path, required=True)
    create.add_argument("--proof", type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("--proof", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "publish":
        publish(args.source, args.c_fixture, args.proof)
    else:
        verify(args.proof)


if __name__ == "__main__":
    main()
