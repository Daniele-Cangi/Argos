"""Operational guards for the frozen prospective pilot helper."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from runpy import run_path

import httpx
import pytest
from test_prospective_bundle import _observation, _valid_result

from argos.evaluation.prospective import ProspectiveExperimentProtocolV2
from argos.resolution import ResolutionStatus

_PILOT = run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "m4_prospective_pilot.py"))
_cutoff = _PILOT["_cutoff"]
_lifecycle_record_complete = _PILOT["_lifecycle_record_complete"]
_public_get = _PILOT["_public_get"]
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


async def test_protocol_v2_rejects_every_incoherent_operational_bound(tmp_path) -> None:
    protocol = (await _valid_result(tmp_path, protocol_v2=True)).bundle.protocol
    lifecycle_seconds = int(
        (protocol.lifecycle_deadline - protocol.observation_window_end).total_seconds()
    )
    observation_seconds = int(
        (protocol.observation_window_end - protocol.observation_window_start).total_seconds()
    )
    cases = [
        ({"lifecycle_deadline": protocol.observation_window_end}, "deadline must follow"),
        (
            {"lifecycle_poll_interval_seconds": lifecycle_seconds + 1},
            "cadence must fit",
        ),
        (
            {"capture_max_seconds_per_target": observation_seconds + 1},
            "duration exceeds",
        ),
        ({"capture_separate_database_per_target": False}, "separate database"),
        ({"capture_subscribe_both_tokens": False}, "subscribe both"),
        ({"capture_raw_archive": False}, "raw archival"),
    ]
    for updates, message in cases:
        with pytest.raises(ValueError, match=message):
            ProspectiveExperimentProtocolV2.model_validate(
                {**protocol.model_dump(mode="python"), **updates}
            )


def test_public_get_retries_one_transient_dns_failure() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("temporary DNS failure", request=request)
        return httpx.Response(200, content=b'{"active":true}', request=request)

    waits: list[float] = []
    raw, endpoint, retrieved_at = _public_get(
        "https://gamma-api.polymarket.com/markets/1",
        {},
        transport=httpx.MockTransport(handler),
        sleep=waits.append,
    )

    assert attempts == 2
    assert waits == [1.0]
    assert raw == b'{"active":true}'
    assert endpoint == "https://gamma-api.polymarket.com/markets/1"
    assert retrieved_at.tzinfo is not None


def test_public_get_exhausts_a_bounded_transport_retry_budget() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("persistent DNS failure", request=request)

    waits: list[float] = []
    with pytest.raises(httpx.ConnectError, match="persistent DNS failure"):
        _public_get(
            "https://gamma-api.polymarket.com/markets/1",
            {},
            transport=httpx.MockTransport(handler),
            sleep=waits.append,
        )

    assert attempts == 3
    assert waits == [1.0, 2.0]


def test_public_get_retries_a_transient_http_status() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        status = 503 if attempts == 1 else 200
        return httpx.Response(status, content=b"{}", request=request)

    waits: list[float] = []
    raw, _, _ = _public_get(
        "https://gamma-api.polymarket.com/markets/1",
        {},
        transport=httpx.MockTransport(handler),
        sleep=waits.append,
    )

    assert attempts == 2
    assert waits == [1.0]
    assert raw == b"{}"


def test_public_get_does_not_retry_a_nontransient_http_failure() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(404, content=b"not found", request=request)

    waits: list[float] = []
    with pytest.raises(httpx.HTTPStatusError):
        _public_get(
            "https://gamma-api.polymarket.com/markets/missing",
            {},
            transport=httpx.MockTransport(handler),
            sleep=waits.append,
        )

    assert attempts == 1
    assert waits == []
