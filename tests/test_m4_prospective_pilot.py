"""Operational guards for the frozen prospective pilot helper."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from runpy import run_path

import pytest
from test_prospective_bundle import _observation, _valid_result

from argos.resolution import ResolutionStatus

_PILOT = run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "m4_prospective_pilot.py"))
_cutoff = _PILOT["_cutoff"]
_lifecycle_record_complete = _PILOT["_lifecycle_record_complete"]
_validate_capture_configuration = _PILOT["_validate_capture_configuration"]


async def test_poller_helper_never_mints_a_post_deadline_cutoff(tmp_path) -> None:
    bundle = (await _valid_result(tmp_path, protocol_v2=True)).bundle
    late_observation = _observation(
        target_id=bundle.target.target_id,
        ordinal=1,
        previous=None,
        retrieved_at=bundle.protocol.lifecycle_deadline + timedelta(seconds=1),
        raw_digest=bundle.resolution.source_payload_sha256,
        finality=ResolutionStatus.FINAL,
        resolution=bundle.resolution,
    )

    with pytest.raises(ValueError, match="after the frozen lifecycle deadline"):
        _cutoff(bundle.protocol, bundle.target, late_observation, bundle.resolution)


async def test_clock_jump_gap_remains_incomplete_lifecycle_evidence(tmp_path) -> None:
    bundle = (await _valid_result(tmp_path, protocol_v2=True)).bundle
    target_state = {
        "cutoff": None,
        "lifecycle": [
            {"observation": observation.to_record()}
            for observation in bundle.lifecycle_observations
        ],
    }

    assert not _lifecycle_record_complete(
        protocol=bundle.protocol,
        targets=[target_state],
        deadline=bundle.protocol.lifecycle_deadline,
        poll_interval_seconds=bundle.protocol.lifecycle_poll_interval_seconds,
    )


async def test_capture_configuration_cannot_drift_from_protocol(tmp_path) -> None:
    protocol = (await _valid_result(tmp_path, protocol_v2=True)).bundle.protocol
    capture = {
        "lifecycle_deadline": protocol.lifecycle_deadline.isoformat(),
        "lifecycle_poll_interval_seconds": protocol.lifecycle_poll_interval_seconds,
        "max_seconds_per_target": protocol.capture_max_seconds_per_target,
        "max_frames_per_target": protocol.capture_max_frames_per_target,
        "separate_database_per_target": protocol.capture_separate_database_per_target,
        "subscribe_both_tokens": protocol.capture_subscribe_both_tokens,
        "raw_archive": protocol.capture_raw_archive,
    }
    _validate_capture_configuration(protocol, capture)
    capture["max_frames_per_target"] += 1

    with pytest.raises(ValueError, match="max_frames_per_target"):
        _validate_capture_configuration(protocol, capture)
